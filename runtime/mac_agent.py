#!/usr/bin/env python3
from __future__ import annotations

import json
import base64
import configparser
import ctypes
import hashlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import bfs_config as cfg

import lz4.block
from marionette_driver.errors import MarionetteException
from marionette_driver.marionette import Marionette


BASE = cfg.DATA_DIR
PYTHON = cfg.PYTHON
SYNC = cfg.CODE_DIR / "sync_now.py"
APPLY_TAB_DELETIONS = cfg.CODE_DIR / "apply_tab_deletions.py"
APPLY_TAB_RECORDS = cfg.CODE_DIR / "apply_tab_records.py"
EXPORT_TAB_RECORDS = cfg.CODE_DIR / "export_tab_records.py"
REMOTE_CTL = [
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    str(cfg.get("mac", "linux_host", "linux-sync")),
    str(cfg.get("mac", "remote_ctl", "~/.local/share/browser-focus-sync/venv/bin/python ~/.local/share/browser-focus-sync/runtime/focusctl.py")),
]
IDLE_SECONDS = 8
IDLE_PROBE_SECONDS = 10
RESTART_BLOCKED = BASE / "restart-blocked"
ACTIVE_BASELINE = BASE / "active-baseline.json"
CONTROL_REQUEST = BASE / "mac-control-request"
BRIDGE_STATUS = BASE / "mac-control-bridge.json"
TWILIGHT_EXECUTABLE = str(cfg.get("mac", "executable", "/Applications/Twilight.app/Contents/MacOS/zen"))


def run(command: list[str], timeout: int = 120, input_data: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        input=input_data,
    )


def mac_idle_seconds() -> float:
    function = getattr(mac_idle_seconds, "_function", None)
    if function is None:
        library = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        function = library.CGEventSourceSecondsSinceLastEventType
        function.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        function.restype = ctypes.c_double
        mac_idle_seconds._function = function
        mac_idle_seconds._library = library
    return float(function(0, 0xFFFFFFFF))


def marionette_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 2828), timeout=1):
            return True
    except OSError:
        return False


def bridge_ready() -> bool:
    try:
        status = json.loads(BRIDGE_STATUS.read_text())
        return status.get("identity") == twilight_identity()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def request_control(enabled: bool) -> None:
    temporary = CONTROL_REQUEST.with_suffix(".tmp")
    temporary.write_text("on" if enabled else "off")
    temporary.replace(CONTROL_REQUEST)


def install_bridge() -> bool:
    identity = twilight_identity()
    if identity is None or not marionette_ready():
        return False
    client = Marionette(host="127.0.0.1", port=2828, socket_timeout=30)
    try:
        client.start_session()
        client.set_context(client.CONTEXT_CHROME)
        installed = client.execute_async_script(
            """
              const done = arguments[arguments.length - 1];
              const [requestPath, statusPath, identity] = arguments;
              const { Marionette } = ChromeUtils.importESModule(
                "chrome://remote/content/components/Marionette.sys.mjs"
              );
              const { RecommendedPreferences } = ChromeUtils.importESModule(
                "chrome://remote/content/shared/RecommendedPreferences.sys.mjs"
              );
              const win = Services.wm.getMostRecentBrowserWindow();
              if (!win) {
                done(false);
                return;
              }
              if (win.__twilightControlInterval) win.clearInterval(win.__twilightControlInterval);
              if (win.__twilightControlTimer) win.clearTimeout(win.__twilightControlTimer);
              const generation = (win.__twilightControlGeneration || 0) + 1;
              win.__twilightControlGeneration = generation;
              let busy = false;
              let lastStatusKey = "";
              let lastWriteAt = 0;
              const update = async () => {
                if (busy) return;
                busy = true;
                let desired = "off";
                try {
                  desired = (await IOUtils.readUTF8(requestPath).catch(() => "off")).trim();
                  if (desired === "on" && !Marionette.running) {
                    await Marionette.init();
                  } else if (desired !== "on" && Marionette.running) {
                    await Marionette.uninit();
                  }
                  if (desired !== "on") {
                    RecommendedPreferences.restoreAllPreferences();
                  }
                  const now = Date.now();
                  const statusKey = `${Marionette.running}:${Marionette.isBrowserAutomationRunning}`;
                  if (statusKey !== lastStatusKey || now - lastWriteAt >= 60000) {
                    await IOUtils.writeUTF8(statusPath, JSON.stringify({
                      identity,
                      running: Marionette.running,
                      webdriverActive: Marionette.isBrowserAutomationRunning,
                      updatedAt: now,
                    }));
                    lastStatusKey = statusKey;
                    lastWriteAt = now;
                  }
                } finally {
                  busy = false;
                  if (win.__twilightControlGeneration === generation) {
                    win.__twilightControlTimer = win.setTimeout(
                      update,
                      desired === "on" ? 250 : 5000
                    );
                  }
                }
              };
              update().then(() => done(true), () => done(false));
            """,
            script_args=[str(CONTROL_REQUEST), str(BRIDGE_STATUS), identity],
        )
        return bool(installed)
    except (OSError, MarionetteException):
        return False
    finally:
        try:
            client.delete_session()
        except Exception:
            pass


def wait_for_control(enabled: bool, attempts: int = 80) -> bool:
    for _ in range(attempts):
        if marionette_ready() == enabled:
            return True
        time.sleep(0.25)
    return False


def wait_for_bridge_clean(attempts: int = 40) -> bool:
    identity = twilight_identity()
    for _ in range(attempts):
        try:
            status = json.loads(BRIDGE_STATUS.read_text())
            if (
                status.get("identity") == identity
                and status.get("running") is False
                and status.get("webdriverActive") is False
            ):
                return True
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        time.sleep(0.1)
    return False


def twilight_pid() -> str | None:
    result = run(["/bin/ps", "-axo", "pid=,command="], timeout=5)
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[1].split(maxsplit=1)[0] == TWILIGHT_EXECUTABLE:
            return fields[0]
    return None


def twilight_running() -> bool:
    return twilight_pid() is not None


def twilight_identity() -> str | None:
    pid = twilight_pid()
    if pid is None:
        return None
    started = run(["/bin/ps", "-p", pid, "-o", "lstart="], timeout=5)
    if started.returncode != 0:
        return None
    return f"{pid}:{started.stdout.strip()}"


def twilight_profile() -> Path:
    return cfg.profile("mac")


def tab_ids(profile: Path) -> set[str]:
    payload = (profile / "zen-sessions.jsonlz4").read_bytes()
    data = json.loads(lz4.block.decompress(payload[8:]))
    return {tab["zenSyncId"] for tab in data.get("tabs", []) if tab.get("zenSyncId")}


def structure_hash(profile: Path) -> str:
    payload = (profile / "zen-sessions.jsonlz4").read_bytes()
    data = json.loads(lz4.block.decompress(payload[8:]))
    structure = {key: data.get(key, []) for key in ("spaces", "folders", "groups", "splitViewData")}
    encoded = json.dumps(structure, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def live_tab_ids() -> set[str] | None:
    result = run([str(PYTHON), str(SYNC), "--inspect"], timeout=20)
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        return set(payload["tabIds"]) if payload.get("ok") else None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def snapshot_session(profile: Path) -> Path:
    backup = BASE / "restart-backups" / str(time.time_ns())
    backup.mkdir(parents=True)
    files = [
        profile / "zen-sessions.jsonlz4",
        profile / "sessionstore.jsonlz4",
        profile / "sessionstore-backups/recovery.jsonlz4",
        profile / "sessionstore-backups/recovery.baklz4",
        profile / "sessionstore-backups/previous.jsonlz4",
    ]
    for source in files:
        if source.exists():
            relative = source.relative_to(profile)
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return backup


def restore_session(profile: Path, backup: Path) -> None:
    for source in backup.rglob("*"):
        if source.is_file():
            destination = profile / source.relative_to(backup)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def quit_twilight() -> bool:
    try:
        run(["/usr/bin/osascript", "-e", 'tell application ' + json.dumps(cfg.get("mac", "application", "Twilight")) + ' to quit'], timeout=15)
    except subprocess.TimeoutExpired:
        # The Apple event can outlive osascript; keep waiting for Twilight itself.
        pass
    for _ in range(80):
        if not twilight_running():
            return True
        time.sleep(0.25)
    return False


def open_twilight(profile: Path, *, controlled: bool) -> bool:
    command = [
        "/usr/bin/open",
        "-a",
        str(cfg.get("mac", "application", "Twilight")),
        "--args",
        "--profile",
        str(profile),
        "--restore-last-session",
    ]
    if controlled:
        command.extend(["--marionette", "--remote-allow-system-access"])
    if run(command, timeout=15).returncode != 0:
        return False
    for _ in range(80):
        ready = twilight_running() and marionette_ready() == controlled
        if ready:
            return True
        time.sleep(0.25)
    return False


def restart_twilight_with_control() -> bool:
    print("Starting temporary Twilight control", flush=True)
    profile = twilight_profile()
    backup = snapshot_session(profile)
    if not quit_twilight():
        print("Twilight did not quit; refusing temporary control", flush=True)
        return False
    before = tab_ids(profile)
    backup = snapshot_session(profile)
    if open_twilight(profile, controlled=True):
        for _ in range(60):
            live_tabs = live_tab_ids()
            if live_tabs is not None and before <= live_tabs:
                request_control(True)
                if install_bridge():
                    RESTART_BLOCKED.unlink(missing_ok=True)
                    print(f"Temporary Twilight control ready: {len(live_tabs)} tabs", flush=True)
                    return True
                print("Twilight control bridge installation failed", flush=True)
                break
            time.sleep(1)
    print("Twilight restart lost tabs; restoring snapshot and refusing Sync", flush=True)
    if not quit_twilight():
        RESTART_BLOCKED.touch()
        print("Browser still running; refusing to restore profile files over a live session", flush=True)
        return False
    restore_session(profile, backup)
    RESTART_BLOCKED.touch()
    open_twilight(profile, controlled=False)
    return False


def ensure_control() -> bool:
    if marionette_ready():
        request_control(True)
        if not bridge_ready() and not install_bridge():
            # Seen after Twilight restarted itself with an inherited
            # MOZ_MARIONETTE: the port listens but never completes a session.
            print("Marionette is listening but the control bridge could not be installed", flush=True)
            return False
        return wait_for_control(True)
    if not twilight_running():
        return False
    if RESTART_BLOCKED.exists():
        return False
    if bridge_ready():
        request_control(True)
        if wait_for_control(True):
            print("Temporary Twilight control enabled in place", flush=True)
            return True
        return False
    if not cfg.get("mac", "allow_restart", False):
        print("Control bridge missing; restart is disabled by configuration", flush=True)
        return False
    return restart_twilight_with_control()


def release_control() -> bool:
    if not marionette_ready():
        return True
    print("Stopping temporary Twilight control", flush=True)
    if not bridge_ready() and not install_bridge():
        print("Cannot install in-process Twilight control; leaving listener unchanged", flush=True)
        return False
    request_control(False)
    if not wait_for_control(False) or not wait_for_bridge_clean():
        print("Mac Marionette did not stop in place", flush=True)
        return False
    print("Mac Twilight control disabled in place", flush=True)
    return True


def save_active_baseline(
    *,
    live: bool,
    handoff_id: str | None = None,
    tab_ids_override: set[str] | None = None,
) -> bool:
    identity = twilight_identity()
    ids = tab_ids_override
    if ids is None:
        ids = live_tab_ids() if live else tab_ids(twilight_profile())
    if identity is None or ids is None:
        return False
    temporary = ACTIVE_BASELINE.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "identity": identity,
        "tabIds": sorted(ids),
        "structureHash": structure_hash(twilight_profile()),
        "handoffId": handoff_id,
    }))
    temporary.replace(ACTIVE_BASELINE)
    return True


def active_baseline_ids() -> set[str] | None:
    try:
        baseline = json.loads(ACTIVE_BASELINE.read_text())
        if twilight_identity() is None:
            return None
        return set(baseline["tabIds"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def record_ids(records: list[dict[str, object]]) -> set[str] | None:
    ids: set[str] = set()
    for record in records:
        identifier = record.get("id")
        if not isinstance(identifier, str):
            cleartext = record.get("cleartext")
            identifier = cleartext.get("id") if isinstance(cleartext, dict) else None
        if not isinstance(identifier, str):
            return None
        ids.add(identifier)
    return ids


def rebind_active_baseline() -> None:
    try:
        baseline = json.loads(ACTIVE_BASELINE.read_text())
        identity = twilight_identity()
        if identity is None:
            return
        baseline["identity"] = identity
        temporary = ACTIVE_BASELINE.with_suffix(".tmp")
        temporary.write_text(json.dumps(baseline))
        temporary.replace(ACTIVE_BASELINE)
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        pass


def sync_mac(reason: str) -> bool:
    if not marionette_ready():
        print("Mac Sync skipped: temporary control is unavailable", flush=True)
        return False
    command = [str(PYTHON), str(SYNC), "--reason", reason]
    authorization = BASE / "authorized-tombstones.json"
    try:
        result = run(command)
    finally:
        authorization.unlink(missing_ok=True)
    print(f"mac-sync reason={reason} exit={result.returncode} {result.stdout.strip()}", flush=True)
    return result.returncode == 0


def apply_linux_tab_deletions(ids: list[str]) -> bool:
    if not ids:
        return True
    if len(ids) > 5000 or not all(isinstance(item, str) for item in ids):
        return False
    result = run(
        [
            str(PYTHON),
            str(APPLY_TAB_DELETIONS),
            "--ids-json",
            json.dumps(list(dict.fromkeys(ids)), separators=(",", ":")),
        ]
    )
    print(f"apply-linux-tab-deletions exit={result.returncode} {result.stdout.strip()}", flush=True)
    return result.returncode == 0


def apply_linux_tab_records(records: list[dict[str, object]]) -> bool:
    if not records:
        return True
    incoming = BASE / "incoming-linux-tab-records.json"
    temporary = incoming.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, separators=(",", ":")))
    temporary.replace(incoming)
    try:
        result = run([str(PYTHON), str(APPLY_TAB_RECORDS), "--records-file", str(incoming)])
    finally:
        incoming.unlink(missing_ok=True)
    print(f"apply-linux-tab-records exit={result.returncode} {result.stdout.strip()}", flush=True)
    return result.returncode == 0


def export_tab_records(ids: set[str]) -> list[dict[str, object]] | None:
    if not ids:
        return []
    result = run(
        [
            str(PYTHON),
            str(EXPORT_TAB_RECORDS),
            "--ids-json",
            json.dumps(sorted(ids), separators=(",", ":")),
        ]
    )
    try:
        payload = json.loads(result.stdout)
        if result.returncode != 0 or not payload.get("ok"):
            print(f"export-mac-tab-records exit={result.returncode} {result.stdout.strip()}", flush=True)
            return None
        return payload["records"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def remote(
    event: str,
    *,
    closed_tab_ids: set[str] | None = None,
    opened_tab_records: list[dict[str, object]] | None = None,
    native_sync_required: bool | None = None,
    handoff_id: str | None = None,
) -> dict[str, object] | None:
    payload = event
    if (
        closed_tab_ids is not None
        or opened_tab_records is not None
        or native_sync_required is not None
        or handoff_id is not None
    ):
        message = json.dumps(
            {
                "event": event,
                "closedTabIds": sorted(closed_tab_ids or set()),
                "openedTabRecords": opened_tab_records or [],
                "nativeSyncRequired": bool(native_sync_required),
                "handoffId": handoff_id,
            },
            separators=(",", ":"),
        ).encode()
        payload = "base64:" + base64.urlsafe_b64encode(message).decode()
    # A handoff can include hundreds of tab records. Passing it as an SSH
    # argument can exceed the remote shell's argument limit.
    result = run([*REMOTE_CTL, "-"], input_data=payload)
    if result.returncode != 0:
        print(f"remote event={event} exit={result.returncode} {result.stdout.strip()}", flush=True)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def enter_mac() -> bool:
    response = remote("mac-active")
    if not response or not response.get("ok"):
        return False
    if response.get("owner") == "mac":
        # A brief idle period is not a fresh handoff. Resetting the baseline
        # here silently acknowledged Mac edits before Linux received them.
        return active_baseline_ids() is not None
    prior_baseline = active_baseline_ids()
    closed_tabs = response.get("closedTabIds", [])
    opened_records = response.get("openedTabRecords", [])
    native_sync_required = response.get("nativeSyncRequired", False)
    handoff_id = response.get("handoffId")
    if (
        not isinstance(opened_records, list)
        or not isinstance(closed_tabs, list)
        or not isinstance(native_sync_required, bool)
        or not isinstance(handoff_id, str)
    ):
        return False
    needs_control = bool(closed_tabs) or bool(opened_records) or native_sync_required
    opened_ids = record_ids(opened_records)
    if opened_ids is None:
        return False
    mac_snapshot = tab_ids(twilight_profile())
    success = True
    clean = True
    if needs_control:
        if not ensure_control():
            return False
        try:
            live_snapshot = live_tab_ids()
            if live_snapshot is None:
                return False
            mac_snapshot = live_snapshot
            success = (
                (not native_sync_required or sync_mac("after-linux"))
                and apply_linux_tab_records(opened_records)
                and apply_linux_tab_deletions(closed_tabs)
            )
        finally:
            clean = release_control()
    # Keep local edits that predate the handoff pending for the return trip.
    expected_ids = ((prior_baseline if prior_baseline is not None else mac_snapshot) - set(closed_tabs)) | opened_ids
    if not success or not clean or not save_active_baseline(
        live=False,
        handoff_id=handoff_id,
        tab_ids_override=expected_ids,
    ):
        return False
    confirmed = remote("mac-synced", handoff_id=handoff_id)
    return bool(
        confirmed
        and confirmed.get("ok")
        and not confirmed.get("ignored")
        and confirmed.get("owner") == "mac"
    )


def leave_mac_for_linux() -> bool | None:
    status = remote("status")
    if not status or status.get("linux_idle") is not False or status.get("owner") != "mac":
        return None
    baseline = active_baseline_ids()
    if baseline is None:
        return False
    baseline_data = json.loads(ACTIVE_BASELINE.read_text())
    same_browser = baseline_data.get("identity") == twilight_identity()
    handoff_id = baseline_data.get("handoffId")
    if not isinstance(handoff_id, str):
        return False
    profile = twilight_profile()
    stored = tab_ids(profile)
    structure_changed = (
        cfg.get("sync", "allow_native_structure_sync", False) and same_browser and baseline_data.get("structureHash") is not None
        and baseline_data["structureHash"] != structure_hash(profile)
    )
    local_changed = stored != baseline or structure_changed
    controlled = False
    success = False
    acknowledged_ids = stored
    try:
        closed_tabs: set[str] = set()
        opened_records: list[dict[str, object]] = []
        if local_changed:
            if not ensure_control():
                return False
            controlled = True
            current = live_tab_ids()
            if current is None:
                return False
            acknowledged_ids = current
            closed_tabs = baseline - current if same_browser else set()
            opened = export_tab_records(current - baseline)
            if opened is None or (structure_changed and not sync_mac("before-linux")):
                return False
            opened_records = opened
        response = remote(
            "mac-idle",
            closed_tab_ids=closed_tabs,
            opened_tab_records=opened_records,
            native_sync_required=structure_changed,
            handoff_id=handoff_id,
        )
        if (
            not response
            or not response.get("ok")
            or response.get("deferred")
            or response.get("ignored")
            or response.get("owner") != "mac"
        ):
            return False
        returned_closed = response.get("closedTabIds", [])
        returned_opened = response.get("openedTabRecords", [])
        returned_native = response.get("nativeSyncRequired", False)
        if (
            not isinstance(returned_closed, list)
            or not isinstance(returned_opened, list)
            or not isinstance(returned_native, bool)
        ):
            return False
        returned_ids = record_ids(returned_opened)
        if returned_ids is None:
            return False
        acknowledged_ids = (acknowledged_ids - set(returned_closed)) | returned_ids
        if returned_closed or returned_opened or returned_native:
            if not controlled:
                if not ensure_control():
                    return False
                controlled = True
            if returned_native and not sync_mac("after-linux"):
                return False
            if not apply_linux_tab_records(returned_opened):
                return False
            if not apply_linux_tab_deletions(returned_closed):
                return False
        confirmed = remote("linux-synced", handoff_id=handoff_id)
        success = bool(
            confirmed
            and confirmed.get("ok")
            and not confirmed.get("ignored")
            and confirmed.get("owner") == "linux"
        )
    finally:
        clean = not controlled or release_control()
    return success and clean and save_active_baseline(
        live=False, handoff_id=handoff_id, tab_ids_override=acknowledged_ids
    )


def main() -> int:
    cfg.prepare_directories()
    mac_was_active: bool | None = None
    leave_reported = False
    next_idle_probe_at = 0.0
    enter_confirmed = False
    next_enter_probe_at = 0.0
    while True:
        mac_is_active = mac_idle_seconds() < IDLE_SECONDS
        if mac_was_active is None:
            mac_was_active = mac_is_active
            if mac_is_active:
                enter_confirmed = enter_mac()
                next_enter_probe_at = time.monotonic() + IDLE_PROBE_SECONDS
        elif mac_is_active and not mac_was_active:
            mac_was_active = True
            leave_reported = False
            enter_confirmed = enter_mac()
            next_enter_probe_at = time.monotonic() + IDLE_PROBE_SECONDS
        elif mac_is_active and not enter_confirmed and time.monotonic() >= next_enter_probe_at:
            enter_confirmed = enter_mac()
            next_enter_probe_at = time.monotonic() + IDLE_PROBE_SECONDS
        elif not mac_is_active and mac_was_active:
            mac_was_active = False
            leave_reported = False
            next_idle_probe_at = 0.0
        elif (
            not mac_is_active
            and not leave_reported
            and time.monotonic() >= next_idle_probe_at
        ):
            attempted = leave_mac_for_linux()
            if attempted is True:
                leave_reported = True
            else:
                next_idle_probe_at = time.monotonic() + IDLE_PROBE_SECONDS
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
