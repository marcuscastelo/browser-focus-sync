#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

import bfs_config as cfg

import linux_control
import structure


BASE = cfg.DATA_DIR
STATE_DIR = cfg.STATE_DIR
STATE_FILE = STATE_DIR / "state.json"
SOCKET = cfg.SOCKET
SYNC = cfg.PYTHON
SYNC_SCRIPT = cfg.CODE_DIR / "sync_now.py"
STRUCTURE_RECORDS = cfg.CODE_DIR / "structure_records.py"
EXPORT_TAB_RECORDS = cfg.CODE_DIR / "export_tab_records.py"
LINUX_ACTIVE_BASELINE = BASE / "linux-active-baseline.json"
AUTHORIZED_TOMBSTONES = BASE / "authorized-linux-tombstones.json"
IDLE_EVENTS = BASE / "idle-events"
IDLE_THRESHOLD_MS = 30000


class Outgoing:
    """What one side changed since its baseline, ready to send."""

    def __init__(self) -> None:
        self.closed: set[str] = set()
        self.tab_records: list[dict[str, object]] = []
        self.structure_records: list[dict[str, object]] = []
        self.deleted_structure: list[str] = []
        self.native_changed = False
        self.tab_ids: set[str] = set()
        self.records: dict[str, list[str]] | None = None
        self.fingerprint: str | None = None

    def message(self) -> dict[str, object]:
        return {
            "closedTabIds": sorted(self.closed),
            "openedTabRecords": self.tab_records,
            "structureRecords": self.structure_records,
            "deletedStructureIds": self.deleted_structure,
            "nativeSyncRequired": self.native_changed,
        }


class Coordinator:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.linux_idle: bool | None = None
        self.owner = "linux"
        self.linux_flushed_at = 0.0
        self.last_mac_idle_at = 0.0
        self.pending_linux_task: asyncio.Task[None] | None = None
        self.authorized_linux_tombstones: set[str] = set()
        self.mac_handoff_id: str | None = None
        self.mac_handoff_linux_tab_ids: set[str] | None = None
        self.mac_handoff_linux_records: dict[str, list[str]] | None = None
        self.mac_handoff_linux_fingerprint: str | None = None
        self.linux_ack_tab_ids: set[str] | None = None
        self.linux_ack_records: dict[str, list[str]] | None = None
        self.linux_ack_fingerprint: str | None = None
        self.control_held = False
        self.control_scope = 0
        self.load_state()

    def load_state(self) -> None:
        try:
            state = json.loads(STATE_FILE.read_text())
            if state.get("owner") in {"linux", "mac"}:
                self.owner = state["owner"]
            self.linux_flushed_at = float(state.get("linux_flushed_at", 0))
            self.last_mac_idle_at = float(state.get("last_mac_idle_at", 0))
            self.authorized_linux_tombstones = set(state.get("authorized_linux_tombstones", []))
            handoff_id = state.get("mac_handoff_id")
            self.mac_handoff_id = handoff_id if isinstance(handoff_id, str) else None
            handed_off = state.get("mac_handoff_linux_tab_ids")
            self.mac_handoff_linux_tab_ids = set(handed_off) if isinstance(handed_off, list) else None
            self.mac_handoff_linux_records = structure.valid_digests(state.get("mac_handoff_linux_records"))
            self.mac_handoff_linux_fingerprint = state.get("mac_handoff_linux_fingerprint")
            acknowledged = state.get("linux_ack_tab_ids")
            self.linux_ack_tab_ids = set(acknowledged) if isinstance(acknowledged, list) else None
            self.linux_ack_records = structure.valid_digests(state.get("linux_ack_records"))
            self.linux_ack_fingerprint = state.get("linux_ack_fingerprint")
        except (FileNotFoundError, ValueError, TypeError):
            pass

    def save_state(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "owner": self.owner,
                    "linux_idle": self.linux_idle,
                    "linux_flushed_at": self.linux_flushed_at,
                    "last_mac_idle_at": self.last_mac_idle_at,
                    "authorized_linux_tombstones": sorted(self.authorized_linux_tombstones),
                    "mac_handoff_id": self.mac_handoff_id,
                    "mac_handoff_linux_tab_ids": (
                        sorted(self.mac_handoff_linux_tab_ids)
                        if self.mac_handoff_linux_tab_ids is not None
                        else None
                    ),
                    "mac_handoff_linux_records": self.mac_handoff_linux_records,
                    "mac_handoff_linux_fingerprint": self.mac_handoff_linux_fingerprint,
                    "linux_ack_tab_ids": (
                        sorted(self.linux_ack_tab_ids)
                        if self.linux_ack_tab_ids is not None
                        else None
                    ),
                    "linux_ack_records": self.linux_ack_records,
                    "linux_ack_fingerprint": self.linux_ack_fingerprint,
                    "updated_at": time.time(),
                },
                separators=(",", ":"),
            )
        )
        temporary.replace(STATE_FILE)

    def linux_twilight_identity(self) -> str | None:
        return linux_control.twilight_identity()

    async def run_tool(self, *args: str) -> tuple[int | None, dict[str, object] | None, str]:
        process = await asyncio.create_subprocess_exec(
            str(SYNC), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, errors = await process.communicate()
        detail = (
            f"stdout={output.decode(errors='replace').strip()[:2000]!r} "
            f"stderr={errors.decode(errors='replace').strip()[:2000]!r}"
        )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = None
        return process.returncode, payload if isinstance(payload, dict) else None, detail

    async def live_tab_ids(self) -> set[str] | None:
        code, payload, detail = await self.run_tool(str(SYNC_SCRIPT), "--inspect")
        if code == 0 and payload and payload.get("ok") and isinstance(payload.get("tabIds"), list):
            return set(payload["tabIds"])
        print(f"Twilight tab inspection failed: exit={code} {detail}", flush=True)
        return None

    def read_linux_baseline(self) -> dict[str, object] | None:
        try:
            baseline = json.loads(LINUX_ACTIVE_BASELINE.read_text())
            return baseline if isinstance(baseline, dict) else None
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return None

    async def save_linux_active_baseline(
        self,
        tab_ids: set[str] | None = None,
        records: dict[str, list[str]] | None = None,
        fingerprint: str | None = None,
    ) -> bool:
        identity = self.linux_twilight_identity()
        try:
            ids = linux_control.stored_tab_ids() if tab_ids is None else tab_ids
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if identity is None:
            return False
        if records is None:
            records = structure.valid_digests((self.read_linux_baseline() or {}).get("records"))
        temporary = LINUX_ACTIVE_BASELINE.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "identity": identity,
            "tabIds": sorted(ids),
            "structureHash": linux_control.stored_structure_hash(),
            "records": records,
            "fingerprint": fingerprint,
        }))
        temporary.replace(LINUX_ACTIVE_BASELINE)
        return True

    def rebind_linux_active_baseline(self) -> None:
        try:
            baseline = json.loads(LINUX_ACTIVE_BASELINE.read_text())
            identity = self.linux_twilight_identity()
            if identity is None:
                return
            baseline["identity"] = identity
            temporary = LINUX_ACTIVE_BASELINE.with_suffix(".tmp")
            temporary.write_text(json.dumps(baseline))
            temporary.replace(LINUX_ACTIVE_BASELINE)
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            pass

    async def acquire_linux_control(self) -> bool:
        if self.control_held:
            return True
        self.control_held = await asyncio.to_thread(linux_control.acquire)
        return self.control_held

    async def release_linux_control(self) -> bool:
        # Inside a handoff the lease lasts until the whole event is answered.
        if self.control_scope or not self.control_held:
            return True
        self.control_held = False
        return await asyncio.to_thread(linux_control.release)

    async def handoff_scope(self, handler) -> dict[str, object]:
        """Hold Linux control, once taken, until the handoff event is answered."""
        self.control_scope += 1
        try:
            response = await handler()
        finally:
            self.control_scope -= 1
        if not await self.release_linux_control():
            print("Linux control was not released cleanly after the handoff", flush=True)
            return {"ok": False, "error": "failed to release Linux browser control"}
        return response

    async def linux_changes(self) -> tuple[set[str], set[str], bool, set[str] | None]:
        try:
            baseline = json.loads(LINUX_ACTIVE_BASELINE.read_text())
            identity = self.linux_twilight_identity()
            if identity is None:
                return set(), set(), False, None
            current = linux_control.stored_tab_ids()
            previous = set(baseline["tabIds"])
            if baseline.get("identity") != identity:
                # A restored browser is not evidence of deletions. Keep the
                # acknowledged IDs until a successful handoff commits anew.
                return set(), current - previous, False, current
            old_structure = baseline.get("structureHash")
            structure_changed = bool(cfg.get("sync", "allow_native_structure_sync", False)) and old_structure is not None and old_structure != linux_control.stored_structure_hash()
            return previous - current, current - previous, structure_changed, current
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return set(), set(), False, None

    async def linux_outgoing(self, skip_ids: set[str] = frozenset()) -> Outgoing | None:
        """Linux changes since its baseline. ``skip_ids`` were decided by the Mac."""
        closed, opened, native_changed, snapshot = await self.linux_changes()
        if snapshot is None:
            return None
        baseline = self.read_linux_baseline() or {}
        base_records = structure.valid_digests(baseline.get("records"))
        same_browser = baseline.get("identity") == self.linux_twilight_identity()
        out = Outgoing()
        out.closed, out.native_changed, out.tab_ids = closed, native_changed, snapshot
        out.records = base_records
        try:
            out.fingerprint = linux_control.stored_fingerprint()
        except (OSError, ValueError):
            return None
        structure_dirty = base_records is None or baseline.get("fingerprint") != out.fingerprint
        if not opened and not structure_dirty:
            return out
        if not await self.acquire_linux_control():
            return None
        if opened:
            records = await self.export_linux_tab_records(opened)
            if records is None:
                return None
            out.tab_records = records
        if structure_dirty:
            code, state, detail = await self.run_tool(str(STRUCTURE_RECORDS), "state")
            records = structure.valid_digests((state or {}).get("records"))
            if code != 0 or records is None:
                print(f"linux structure state failed: exit={code} {detail}", flush=True)
                return None
            present = set((state or {}).get("presentIds") or [])
            changed, deleted = structure.plan(
                base_records, records, present, same_browser=same_browser, skip_ids=opened | skip_ids,
            )
            exported = await self.export_structure(changed)
            if exported is None:
                return None
            out.structure_records = structure.outgoing(exported, base_records)
            out.deleted_structure = deleted
            out.records = records
        return out

    async def export_structure(self, ids: list[str]) -> list[dict[str, object]] | None:
        if not ids:
            return []
        code, payload, detail = await self.run_tool(
            str(STRUCTURE_RECORDS), "export", "--ids-json", json.dumps(ids, separators=(",", ":")),
        )
        if code != 0 or not payload or not isinstance(payload.get("records"), list):
            print(f"export-linux-structure exit={code} {detail}", flush=True)
            return None
        return payload["records"]

    async def export_linux_tab_records(self, ids: set[str]) -> list[dict[str, object]] | None:
        if not ids:
            return []
        if not await self.acquire_linux_control():
            return None
        try:
            code, payload, detail = await self.run_tool(
                str(EXPORT_TAB_RECORDS), "--ids-json", json.dumps(sorted(ids), separators=(",", ":")),
            )
        finally:
            clean = await self.release_linux_control()
        if not clean:
            return None
        if code != 0 or not payload or not payload.get("ok"):
            print(f"export-linux-tab-records exit={code} {detail}", flush=True)
            return None
        return payload.get("records")

    async def sync_linux(
        self,
        reason: str,
        *,
        only_if_pending: bool = False,
        allowed_tombstone_ids: set[str] | None = None,
    ) -> bool:
        if not await self.acquire_linux_control():
            print(f"linux-sync reason={reason} skipped: temporary control unavailable", flush=True)
            return False
        command = [str(SYNC), str(SYNC_SCRIPT), "--reason", reason]
        if only_if_pending:
            command.append("--if-spaces-pending")
        authorization = (
            self.authorized_linux_tombstones
            if allowed_tombstone_ids is None
            else allowed_tombstone_ids
        )
        if authorization:
            AUTHORIZED_TOMBSTONES.write_text(json.dumps(sorted(authorization)))
            command.extend(["--allow-tombstones-file", str(AUTHORIZED_TOMBSTONES)])
        process = None
        output = b""
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
        finally:
            AUTHORIZED_TOMBSTONES.unlink(missing_ok=True)
            clean = await self.release_linux_control()
        if process is None or not clean:
            return False
        message = output.decode(errors="replace").strip()
        print(f"linux-sync reason={reason} exit={process.returncode} {message}", flush=True)
        if process.returncode == 0:
            self.linux_flushed_at = time.time()
            self.save_state()
            return True
        return False

    async def apply_mac_changes(
        self,
        closed_tab_ids: list[str],
        tab_records: list[dict[str, object]],
        structure_records: list[dict[str, object]],
        deleted_structure_ids: list[str],
    ) -> dict[str, object] | None:
        """Apply the Mac's handoff in one batch; return the browser's report."""
        if not (closed_tab_ids or tab_records or structure_records or deleted_structure_ids):
            return {"ok": True, "records": None, "deleted": []}
        if not await self.acquire_linux_control():
            return None
        incoming = BASE / "incoming-mac-changes.json"
        temporary = incoming.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "tabRecords": tab_records,
            "records": structure_records,
            "deletedIds": deleted_structure_ids,
            "closingTabIds": closed_tab_ids,
        }, separators=(",", ":")))
        temporary.replace(incoming)
        try:
            code, payload, detail = await self.run_tool(
                str(STRUCTURE_RECORDS), "apply", "--payload-file", str(incoming),
            )
        finally:
            incoming.unlink(missing_ok=True)
            clean = await self.release_linux_control()
        print(f"apply-mac-changes exit={code} {structure.summary(payload) if payload else detail}", flush=True)
        if not clean or code != 0 or not payload or not payload.get("ok"):
            return None
        return payload

    async def on_linux_idle(self) -> None:
        if self.linux_idle is True:
            return
        self.linux_idle = True
        print("linux-state idle", flush=True)
        self.save_state()

    async def on_linux_active(self) -> None:
        if self.linux_idle is False:
            return
        self.linux_idle = False
        print("linux-state active", flush=True)
        self.save_state()
        if self.owner == "mac":
            if self.pending_linux_task and not self.pending_linux_task.done():
                self.pending_linux_task.cancel()
            self.pending_linux_task = asyncio.create_task(self.wait_for_mac_flush())

    async def wait_for_mac_flush(self) -> None:
        observed = self.last_mac_idle_at
        for _ in range(15):
            await asyncio.sleep(1)
            if self.last_mac_idle_at > observed or self.owner != "mac":
                return
        async with self.lock:
            if self.owner == "mac" and self.linux_idle is False:
                print("mac-idle timeout; waiting without enabling browser control", flush=True)

    async def handle_event(
        self,
        event: str,
        closed_tab_ids: list[str] | None = None,
        opened_tab_records: list[dict[str, object]] | None = None,
        native_sync_required: bool = False,
        handoff_id: str | None = None,
        structure_records: list[dict[str, object]] | None = None,
        deleted_structure_ids: list[str] | None = None,
    ) -> dict[str, object]:
        async with self.lock:
            print(f"remote-event {event}", flush=True)
            if event == "mac-active":
                return await self.handoff_scope(self.on_mac_active)

            if event == "mac-synced":
                if (
                    not handoff_id
                    or handoff_id != self.mac_handoff_id
                    or self.mac_handoff_linux_tab_ids is None
                ):
                    return {"ok": True, "owner": self.owner, "ignored": "stale-handoff"}
                if not await self.save_linux_active_baseline(
                    self.mac_handoff_linux_tab_ids,
                    self.mac_handoff_linux_records,
                    self.mac_handoff_linux_fingerprint,
                ):
                    return {"ok": False, "owner": self.owner, "error": "failed to commit Linux handoff baseline"}
                self.owner = "mac"
                self.save_state()
                return {"ok": True, "owner": self.owner}

            if event == "linux-synced":
                if (
                    self.owner != "mac"
                    or not handoff_id
                    or handoff_id != self.mac_handoff_id
                    or self.linux_ack_tab_ids is None
                ):
                    return {"ok": True, "owner": self.owner, "ignored": "stale-handoff"}
                self.owner = "linux"
                if not await self.save_linux_active_baseline(
                    self.linux_ack_tab_ids, self.linux_ack_records, self.linux_ack_fingerprint,
                ):
                    self.owner = "mac"
                    return {"ok": False, "owner": self.owner, "error": "failed to commit Linux return baseline"}
                self.mac_handoff_id = None
                self.mac_handoff_linux_tab_ids = None
                self.mac_handoff_linux_records = None
                self.mac_handoff_linux_fingerprint = None
                self.linux_ack_tab_ids = None
                self.linux_ack_records = None
                self.linux_ack_fingerprint = None
                self.save_state()
                return {"ok": True, "owner": self.owner}

            if event == "mac-idle":
                if self.owner != "mac" or not handoff_id or handoff_id != self.mac_handoff_id:
                    return {"ok": True, "owner": self.owner, "ignored": "stale-handoff"}
                self.last_mac_idle_at = time.time()
                self.save_state()
                if self.linux_idle is False:
                    return await self.handoff_scope(lambda: self.on_mac_idle(
                        closed_tab_ids or [],
                        opened_tab_records or [],
                        native_sync_required,
                        structure_records or [],
                        deleted_structure_ids or [],
                    ))
                return {"ok": True, "owner": self.owner, "deferred": True}

            if event == "status":
                return {
                    "ok": True,
                    "owner": self.owner,
                    "linux_idle": self.linux_idle,
                    "linux_flushed_at": self.linux_flushed_at,
                    "last_mac_idle_at": self.last_mac_idle_at,
                }

            return {"ok": False, "error": f"unknown event: {event}"}

    async def on_mac_active(self) -> dict[str, object]:
        out = Outgoing()
        if self.owner == "linux":
            self.mac_handoff_id = secrets.token_hex(16)
            changes = await self.linux_outgoing()
            if changes is None:
                return {"ok": False, "error": "failed to read Linux changes"}
            out = changes
            self.mac_handoff_linux_tab_ids = out.tab_ids
            self.mac_handoff_linux_records = out.records
            self.mac_handoff_linux_fingerprint = out.fingerprint
            self.linux_ack_tab_ids = None
            self.linux_ack_records = None
            self.linux_ack_fingerprint = None
            self.authorized_linux_tombstones.update(out.closed)
            self.save_state()
            if out.native_changed:
                if not await self.sync_linux(
                    "before-mac",
                    allowed_tombstone_ids=self.authorized_linux_tombstones,
                ):
                    return {"ok": False, "error": "linux sync failed"}
        return {
            "ok": True,
            "owner": self.owner,
            **out.message(),
            "nativeSyncRequired": out.native_changed if self.owner == "linux" else False,
            "handoffId": self.mac_handoff_id,
        }

    async def on_mac_idle(
        self,
        closed_tab_ids: list[str],
        opened_tab_records: list[dict[str, object]],
        native_sync_required: bool,
        structure_records: list[dict[str, object]],
        deleted_structure_ids: list[str],
    ) -> dict[str, object]:
        # On a conflict the Mac's version wins: it is applied here and not echoed.
        decided = structure.record_ids(structure_records) | set(deleted_structure_ids)
        linux = await self.linux_outgoing(skip_ids=decided)
        if linux is None:
            return {"ok": False, "error": "failed to read concurrent Linux changes"}
        applied = await self.apply_mac_changes(
            closed_tab_ids, opened_tab_records, structure_records, deleted_structure_ids,
        )
        if applied is None:
            return {"ok": False, "error": "failed to apply Mac changes"}
        if native_sync_required and not await self.sync_linux("after-mac"):
            return {"ok": False, "error": "linux sync failed"}
        if linux.native_changed:
            if not await self.sync_linux(
                "before-linux-return",
                allowed_tombstone_ids=self.authorized_linux_tombstones | linux.closed,
            ):
                return {"ok": False, "error": "concurrent Linux sync failed"}
        opened_ids = structure.record_ids(opened_tab_records)
        self.linux_ack_tab_ids = (linux.tab_ids - set(closed_tab_ids)) | opened_ids
        self.linux_ack_records = structure.received(
            linux.records, applied, opened_tab_records + structure_records, closed_tab_ids,
        )
        try:
            self.linux_ack_fingerprint = linux_control.stored_fingerprint()
        except (OSError, ValueError):
            self.linux_ack_fingerprint = None
        self.save_state()
        return {
            "ok": True,
            "owner": self.owner,
            **linux.message(),
            "handoffId": self.mac_handoff_id,
        }


async def idle_loop(coordinator: Coordinator) -> None:
    while True:
        process = await asyncio.create_subprocess_exec(
            str(IDLE_EVENTS),
            str(IDLE_THRESHOLD_MS),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout
        async for raw_line in process.stdout:
            event = raw_line.decode(errors="replace").strip()
            if event == "idle":
                await coordinator.on_linux_idle()
            elif event == "active":
                await coordinator.on_linux_active()
        print(f"idle detector exited with {await process.wait()}; restarting", flush=True)
        await asyncio.sleep(2)


def string_list(message: dict[str, object], key: str) -> list[str]:
    value = message.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


def object_list(message: dict[str, object], key: str) -> list[dict[str, object]]:
    value = message.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be a list of objects")
    return value


async def client_handler(
    coordinator: Coordinator,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        payload = (await asyncio.wait_for(reader.readline(), timeout=5)).decode().strip()
        if payload.startswith("{"):
            message = json.loads(payload)
            event = message.get("event", "")
            closed_tab_ids = string_list(message, "closedTabIds")
            opened_tab_records = object_list(message, "openedTabRecords")
            structure_records = object_list(message, "structureRecords")
            deleted_structure_ids = string_list(message, "deletedStructureIds")
            native_sync_required = message.get("nativeSyncRequired", False)
            handoff_id = message.get("handoffId")
            if not isinstance(native_sync_required, bool):
                raise ValueError("nativeSyncRequired must be a boolean")
            if handoff_id is not None and not isinstance(handoff_id, str):
                raise ValueError("handoffId must be a string")
        else:
            event = payload
            closed_tab_ids = []
            opened_tab_records = []
            structure_records = []
            deleted_structure_ids = []
            native_sync_required = False
            handoff_id = None
        response = await coordinator.handle_event(
            event,
            closed_tab_ids,
            opened_tab_records,
            native_sync_required,
            handoff_id,
            structure_records,
            deleted_structure_ids,
        )
    except Exception as error:
        response = {"ok": False, "error": str(error)}
    writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def start_server(coordinator: Coordinator, path: Path) -> asyncio.AbstractServer:
    # The asyncio default (64 KiB) rejected ordinary multi-tab handoffs.
    server = await asyncio.start_unix_server(
        lambda reader, writer: client_handler(coordinator, reader, writer),
        path=path,
        limit=cfg.MAX_MESSAGE_BYTES + 1,
    )
    os.chmod(path, 0o600)
    return server


async def main() -> None:
    cfg.prepare_directories()
    coordinator = Coordinator()
    if SOCKET.exists():
        SOCKET.unlink()
    server = await start_server(coordinator, SOCKET)
    # Never acknowledge untransferred changes just because the service or
    # browser restarted. Existing baselines survive process identity changes.
    coordinator.save_state()
    async with server:
        await asyncio.gather(server.serve_forever(), idle_loop(coordinator))


if __name__ == "__main__":
    asyncio.run(main())
