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


BASE = cfg.DATA_DIR
STATE_DIR = cfg.STATE_DIR
STATE_FILE = STATE_DIR / "state.json"
SOCKET = cfg.SOCKET
SYNC = cfg.PYTHON
SYNC_SCRIPT = cfg.CODE_DIR / "sync_now.py"
APPLY_TAB_DELETIONS = cfg.CODE_DIR / "apply_tab_deletions.py"
APPLY_TAB_RECORDS = cfg.CODE_DIR / "apply_tab_records.py"
EXPORT_TAB_RECORDS = cfg.CODE_DIR / "export_tab_records.py"
LINUX_ACTIVE_BASELINE = BASE / "linux-active-baseline.json"
AUTHORIZED_TOMBSTONES = BASE / "authorized-linux-tombstones.json"
IDLE_EVENTS = BASE / "idle-events"
IDLE_THRESHOLD_MS = 30000


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
        self.linux_ack_tab_ids: set[str] | None = None
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
            acknowledged = state.get("linux_ack_tab_ids")
            self.linux_ack_tab_ids = set(acknowledged) if isinstance(acknowledged, list) else None
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
                    "linux_ack_tab_ids": (
                        sorted(self.linux_ack_tab_ids)
                        if self.linux_ack_tab_ids is not None
                        else None
                    ),
                    "updated_at": time.time(),
                },
                separators=(",", ":"),
            )
        )
        temporary.replace(STATE_FILE)

    def linux_twilight_identity(self) -> str | None:
        return linux_control.twilight_identity()

    async def live_tab_ids(self) -> set[str] | None:
        process = await asyncio.create_subprocess_exec(
            str(SYNC),
            str(SYNC_SCRIPT),
            "--inspect",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        try:
            payload = json.loads(output)
            if process.returncode != 0 or not payload.get("ok"):
                return None
            return set(payload["tabIds"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    async def save_linux_active_baseline(self, tab_ids: set[str] | None = None) -> bool:
        identity = self.linux_twilight_identity()
        try:
            ids = linux_control.stored_tab_ids() if tab_ids is None else tab_ids
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if identity is None:
            return False
        temporary = LINUX_ACTIVE_BASELINE.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "identity": identity,
            "tabIds": sorted(ids),
            "structureHash": linux_control.stored_structure_hash(),
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
        return await asyncio.to_thread(linux_control.acquire)

    async def release_linux_control(self) -> bool:
        clean = await asyncio.to_thread(linux_control.release)
        return clean

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

    async def export_linux_tab_records(self, ids: set[str]) -> list[dict[str, object]] | None:
        if not ids:
            return []
        if not await self.acquire_linux_control():
            return None
        process = None
        output = b""
        try:
            process = await asyncio.create_subprocess_exec(
                str(SYNC),
                str(EXPORT_TAB_RECORDS),
                "--ids-json",
                json.dumps(sorted(ids), separators=(",", ":")),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
        finally:
            clean = await self.release_linux_control()
        if process is None or not clean:
            return None
        try:
            payload = json.loads(output)
            if process.returncode != 0 or not payload.get("ok"):
                print(f"export-linux-tab-records exit={process.returncode} {output.decode(errors='replace').strip()}", flush=True)
                return None
            return payload["records"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

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

    async def apply_mac_tab_deletions(self, ids: list[str]) -> bool:
        if not ids:
            return True
        if not await self.acquire_linux_control():
            return False
        process = None
        output = b""
        try:
            process = await asyncio.create_subprocess_exec(
                str(SYNC),
                str(APPLY_TAB_DELETIONS),
                "--ids-json",
                json.dumps(ids, separators=(",", ":")),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
        finally:
            clean = await self.release_linux_control()
        if process is None or not clean:
            return False
        message = output.decode(errors="replace").strip()
        print(f"apply-mac-tab-deletions exit={process.returncode} {message}", flush=True)
        return process.returncode == 0

    async def apply_mac_tab_records(self, records: list[dict[str, object]]) -> bool:
        if not records:
            return True
        if not await self.acquire_linux_control():
            return False
        incoming = BASE / "incoming-mac-tab-records.json"
        temporary = incoming.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, separators=(",", ":")))
        temporary.replace(incoming)
        process = None
        output = b""
        try:
            process = await asyncio.create_subprocess_exec(
                str(SYNC),
                str(APPLY_TAB_RECORDS),
                "--records-file",
                str(incoming),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
        finally:
            incoming.unlink(missing_ok=True)
            clean = await self.release_linux_control()
        if process is None or not clean:
            return False
        message = output.decode(errors="replace").strip()
        print(f"apply-mac-tab-records exit={process.returncode} {message}", flush=True)
        return process.returncode == 0

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
    ) -> dict[str, object]:
        async with self.lock:
            print(f"remote-event {event}", flush=True)
            if event == "mac-active":
                closed_tabs: set[str] = set()
                opened_records: list[dict[str, object]] = []
                if self.owner == "linux":
                    self.mac_handoff_id = secrets.token_hex(16)
                    closed_tabs, opened_tabs, structure_changed, linux_snapshot = await self.linux_changes()
                    if linux_snapshot is None:
                        return {"ok": False, "error": "Linux handoff baseline is unavailable"}
                    self.mac_handoff_linux_tab_ids = linux_snapshot
                    self.linux_ack_tab_ids = None
                    exported = await self.export_linux_tab_records(opened_tabs)
                    if exported is None:
                        return {"ok": False, "error": "failed to export Linux tabs"}
                    opened_records = exported
                    self.authorized_linux_tombstones.update(closed_tabs)
                    self.save_state()
                    if structure_changed:
                        if not await self.sync_linux(
                            "before-mac",
                            allowed_tombstone_ids=self.authorized_linux_tombstones,
                        ):
                            return {"ok": False, "error": "linux sync failed"}
                return {
                    "ok": True,
                    "owner": self.owner,
                    "closedTabIds": sorted(closed_tabs),
                    "openedTabRecords": opened_records,
                    "nativeSyncRequired": structure_changed if self.owner == "linux" else False,
                    "handoffId": self.mac_handoff_id,
                }

            if event == "mac-synced":
                if (
                    not handoff_id
                    or handoff_id != self.mac_handoff_id
                    or self.mac_handoff_linux_tab_ids is None
                ):
                    return {"ok": True, "owner": self.owner, "ignored": "stale-handoff"}
                if not await self.save_linux_active_baseline(self.mac_handoff_linux_tab_ids):
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
                if not await self.save_linux_active_baseline(self.linux_ack_tab_ids):
                    self.owner = "mac"
                    return {"ok": False, "owner": self.owner, "error": "failed to commit Linux return baseline"}
                self.mac_handoff_id = None
                self.mac_handoff_linux_tab_ids = None
                self.linux_ack_tab_ids = None
                self.save_state()
                return {"ok": True, "owner": self.owner}

            if event == "mac-idle":
                if self.owner != "mac" or not handoff_id or handoff_id != self.mac_handoff_id:
                    return {"ok": True, "owner": self.owner, "ignored": "stale-handoff"}
                self.last_mac_idle_at = time.time()
                self.save_state()
                if self.linux_idle is False:
                    linux_closed, linux_opened, linux_structure_changed, linux_snapshot = await self.linux_changes()
                    if linux_snapshot is None:
                        return {"ok": False, "error": "Linux return baseline is unavailable"}
                    linux_records = await self.export_linux_tab_records(linux_opened)
                    if linux_records is None:
                        return {"ok": False, "error": "failed to export concurrent Linux tabs"}
                    if not await self.apply_mac_tab_records(opened_tab_records or []):
                        return {"ok": False, "error": "failed to apply Mac tab records"}
                    if not await self.apply_mac_tab_deletions(sorted(closed_tab_ids or [])):
                        return {"ok": False, "error": "failed to apply Mac tab deletions"}
                    if native_sync_required and not await self.sync_linux("after-mac"):
                        return {"ok": False, "error": "linux sync failed"}
                    if linux_structure_changed:
                        if not await self.sync_linux(
                            "before-linux-return",
                            allowed_tombstone_ids=self.authorized_linux_tombstones | linux_closed,
                        ):
                            return {"ok": False, "error": "concurrent Linux sync failed"}
                    opened_ids = {
                        record.get("id") or record.get("cleartext", {}).get("id")
                        for record in (opened_tab_records or [])
                    }
                    opened_ids.discard(None)
                    self.linux_ack_tab_ids = (
                        linux_snapshot - set(closed_tab_ids or [])
                    ) | opened_ids
                    self.save_state()
                    return {
                        "ok": True,
                        "owner": self.owner,
                        "closedTabIds": sorted(linux_closed),
                        "openedTabRecords": linux_records,
                        "nativeSyncRequired": linux_structure_changed,
                        "handoffId": self.mac_handoff_id,
                    }
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
            closed_tab_ids = message.get("closedTabIds", [])
            opened_tab_records = message.get("openedTabRecords", [])
            native_sync_required = message.get("nativeSyncRequired", False)
            handoff_id = message.get("handoffId")
            if not isinstance(closed_tab_ids, list) or not all(isinstance(item, str) for item in closed_tab_ids):
                raise ValueError("closedTabIds must be a list of strings")
            if not isinstance(opened_tab_records, list) or not all(isinstance(item, dict) for item in opened_tab_records):
                raise ValueError("openedTabRecords must be a list of objects")
            if not isinstance(native_sync_required, bool):
                raise ValueError("nativeSyncRequired must be a boolean")
            if handoff_id is not None and not isinstance(handoff_id, str):
                raise ValueError("handoffId must be a string")
        else:
            event = payload
            closed_tab_ids = []
            opened_tab_records = []
            native_sync_required = False
            handoff_id = None
        response = await coordinator.handle_event(
            event,
            closed_tab_ids,
            opened_tab_records,
            native_sync_required,
            handoff_id,
        )
    except Exception as error:
        response = {"ok": False, "error": str(error)}
    writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def main() -> None:
    cfg.prepare_directories()
    coordinator = Coordinator()
    if SOCKET.exists():
        SOCKET.unlink()
    server = await asyncio.start_unix_server(
        lambda reader, writer: client_handler(coordinator, reader, writer),
        path=SOCKET,
    )
    os.chmod(SOCKET, 0o600)
    # Never acknowledge untransferred changes just because the service or
    # browser restarted. Existing baselines survive process identity changes.
    coordinator.save_state()
    async with server:
        await asyncio.gather(server.serve_forever(), idle_loop(coordinator))


if __name__ == "__main__":
    asyncio.run(main())
