#!/usr/bin/env python3
from __future__ import annotations

import json
import re
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

import bridge_client
import lz4.block
import structure
from marionette_driver.errors import MarionetteException
from marionette_driver.marionette import Marionette


BASE = cfg.DATA_DIR
PYTHON = cfg.PYTHON
SYNC = cfg.CODE_DIR / "sync_now.py"
STRUCTURE_RECORDS = cfg.CODE_DIR / "structure_records.py"
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
# `mac_agent.py reopen` asks the running agent to restart Twilight with control now;
# the agent answers in REOPEN_RESULT. Without the request it never restarts it unless
# `allow_restart` opts in.
REOPEN_REQUEST = BASE / "reopen-request"
REOPEN_RESULT = BASE / "reopen-result.json"
REOPEN_TIMEOUT_SECONDS = 300
# The Twilight identity last told about a missing bridge: one notice per browser process.
BRIDGE_NOTICE = BASE / "bridge-missing-notice"
ACTIVE_BASELINE = BASE / "active-baseline.json"
CONTROL_REQUEST = BASE / "mac-control-request"
BRIDGE_STATUS = BASE / "mac-control-bridge.json"
TWILIGHT_EXECUTABLE = str(cfg.get("mac", "executable", "/Applications/Twilight.app/Contents/MacOS/zen"))
# One lease per process, held from the first need of control to the end of the
# handoff, so the Twilight MCP and route applier wait instead of interleaving.
LEASE = bridge_client.Lease(directory=BASE, executable_path=TWILIGHT_EXECUTABLE, prefix="mac", label="focus-sync mac_agent")


def run(command: list[str], timeout: int = 120, input_data: str | None = None) -> subprocess.CompletedProcess[str]:
    # stderr stays separate: diagnostics there (e.g. macOS MallocStackLogging) must not
    # corrupt JSON read from stdout.
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        input=input_data,
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return f"stdout={(result.stdout or '').strip()!r} stderr={(result.stderr or '').strip()!r}"


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
    hid_idle = float(function(0, 0xFFFFFFFF))
    away = seconds_on_other_screen()
    return hid_idle if away is None else max(hid_idle, away)


DESKFLOW_LOG = cfg.get("mac", "deskflow_server_log", "")
DESKFLOW_SCREEN = str(cfg.get("mac", "deskflow_screen", ""))
DESKFLOW_SWITCH = re.compile(r'^\[(\S+)\] INFO: switch from "[^"]*" to "([^"]*)"')


def seconds_on_other_screen() -> float | None:
    """With this Mac as the Deskflow server, its keyboard and mouse stay physically
    busy while they drive another screen, so HID idle never reaches the threshold.
    The server log's last screen switch tells where the user actually is."""
    if not DESKFLOW_LOG or not DESKFLOW_SCREEN:
        return None
    try:
        with open(Path(DESKFLOW_LOG).expanduser(), "rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 65536))
            lines = log.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        match = DESKFLOW_SWITCH.match(line)
        if not match:
            continue
        if match.group(2) == DESKFLOW_SCREEN:
            return None
        try:
            switched = time.mktime(time.strptime(match.group(1).split(".")[0], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None
        return max(0.0, time.time() - switched)
    return None


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


def twilight_pid() -> str | None:
    result = run(["/bin/ps", "-axo", "pid=,command="], timeout=5)
    profile = cfg.configured_profile("mac")
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[1].split(maxsplit=1)[0] == TWILIGHT_EXECUTABLE:
            if profile is not None and not mac_command_runs_profile(fields[1], profile):
                continue
            return fields[0]
    return None


def mac_command_runs_profile(command: str, profile: Path) -> bool:
    """ps joins arguments with spaces and profile paths contain spaces: compare text."""
    if " --profile " not in command and " -profile " not in command:
        return True  # opened from the Dock: the default profile, ours
    expected = str(profile.expanduser())
    return f"--profile {expected}" in command or f"-profile {expected}" in command


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


def fingerprint(profile: Path) -> str | None:
    try:
        return structure.stored_fingerprint(profile)
    except (OSError, ValueError):
        return None


def structure_hash(profile: Path) -> str:
    payload = (profile / "zen-sessions.jsonlz4").read_bytes()
    data = json.loads(lz4.block.decompress(payload[8:]))
    structure = {key: data.get(key, []) for key in ("spaces", "folders", "groups", "splitViewData")}
    encoded = json.dumps(structure, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def live_tab_ids() -> set[str] | None:
    result = run([str(PYTHON), str(SYNC), "--inspect"], timeout=20)
    try:
        payload = json.loads(result.stdout)
        if result.returncode == 0 and payload.get("ok"):
            return set(payload["tabIds"])
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    print(f"Twilight tab inspection failed: exit={result.returncode} {output(result)}", flush=True)
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
    result = run(command, timeout=15)
    if result.returncode != 0:
        print(f"Twilight open failed: exit={result.returncode} {output(result)}", flush=True)
        return False
    for _ in range(80):
        ready = twilight_running() and marionette_ready() == controlled
        if ready:
            return True
        time.sleep(0.25)
    return False


def selected_tab_id(profile: Path) -> str | None:
    """The tab selected when Twilight quit, read from the session it wrote on exit."""
    for name in ("sessionstore.jsonlz4", "sessionstore-backups/recovery.jsonlz4"):
        try:
            payload = (profile / name).read_bytes()
            window = json.loads(lz4.block.decompress(payload[8:]))["windows"][0]
            tab = window["tabs"][window["selected"] - 1]
        except (OSError, ValueError, KeyError, IndexError, TypeError, lz4.block.LZ4BlockError):
            continue
        return tab.get("zenSyncId") or None
    return None


def reselect_tab(tab_id: str) -> bool:
    """On startup Zen selects its empty tab when the restored selection is not in
    the active space, leaving a blank page; put the user back on their tab."""
    client = Marionette(host="127.0.0.1", port=2828, socket_timeout=30)
    try:
        client.start_session()
        client.set_context(client.CONTEXT_CHROME)
        return bool(client.execute_async_script(
            """
            const [id] = arguments; const done = arguments[arguments.length - 1];
            const win = Services.wm.getMostRecentWindow("navigator:browser");
            const tab = win && win.document.getElementById(id);
            if (!tab || !win.gBrowser.isTab(tab)) { done(false); return; }
            const space = tab.getAttribute("zen-workspace-id");
            const change = space && space !== win.gZenWorkspaces.activeWorkspace
              ? win.gZenWorkspaces.changeWorkspaceWithID(space) : Promise.resolve();
            change.then(() => { win.gBrowser.selectedTab = tab; done(win.gBrowser.selectedTab === tab); },
                        () => done(false));
            """,
            script_args=[tab_id],
        ))
    except (OSError, MarionetteException):
        return False
    finally:
        try:
            client.delete_session()
        except Exception:
            pass


def restart_twilight_with_control() -> bool:
    print("Starting temporary Twilight control", flush=True)
    profile = twilight_profile()
    backup = snapshot_session(profile)
    if not quit_twilight():
        print("Twilight did not quit; refusing temporary control", flush=True)
        return False
    before = tab_ids(profile)
    selected = selected_tab_id(profile)
    backup = snapshot_session(profile)
    if not open_twilight(profile, controlled=True):
        failure = "Twilight did not reopen with control"
    else:
        # "Lost tabs" is only a verdict over ids actually read from the live browser.
        failure = "Twilight tabs could not be read after restart"
        for _ in range(60):
            live_tabs = live_tab_ids()
            if live_tabs is not None:
                missing = before - live_tabs
                if not missing:
                    request_control(True)
                    if install_bridge():
                        RESTART_BLOCKED.unlink(missing_ok=True)
                        if selected and not reselect_tab(selected):
                            print("Could not reselect the tab selected before restart", flush=True)
                        print(f"Temporary Twilight control ready: {len(live_tabs)} tabs", flush=True)
                        return True
                    failure = "Twilight control bridge installation failed"
                    break
                failure = f"Twilight restart lost tabs: {len(missing)} of {len(before)} missing"
            time.sleep(1)
    print(f"{failure}; restoring snapshot and refusing Sync", flush=True)
    if not quit_twilight():
        RESTART_BLOCKED.touch()
        print("Browser still running; refusing to restore profile files over a live session", flush=True)
        return False
    restore_session(profile, backup)
    RESTART_BLOCKED.touch()
    open_twilight(profile, controlled=False)
    return False


def take_lease() -> bool:
    try:
        LEASE.acquire()
        return True
    except bridge_client.BridgeError as error:
        print(f"Twilight control unavailable ({error.code}): {error}", flush=True)
        return False


def notify_bridge_missing(reason: str) -> None:
    """Tell the user once per Twilight process that handoffs wait for them to reopen
    it with control; the agent itself never chooses when to restart the browser."""
    identity = twilight_identity() or "none"
    try:
        if BRIDGE_NOTICE.read_text() == identity:
            return
    except OSError:
        pass
    BRIDGE_NOTICE.write_text(identity)
    hint = str(cfg.get("mac", "reopen_command", f"{PYTHON} {Path(__file__).resolve()} reopen"))
    print(f"{reason}; handoffs wait until Twilight is reopened with control ({hint})", flush=True)
    message = f"Tab handoff is paused: {reason}. Reopen Twilight with control when convenient: {hint}"
    script = f"display notification {json.dumps(message)} with title \"Browser focus sync\""
    try:
        run(["/usr/bin/osascript", "-e", script], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def ensure_control() -> bool:
    if LEASE.held:
        return True
    if marionette_ready() and not bridge_ready():
        request_control(True)
        if not install_bridge():
            notify_bridge_missing("Marionette is listening but the control bridge could not be installed")
            return False
    if not twilight_running():
        return False
    if bridge_ready() or marionette_ready():
        return take_lease()
    if not cfg.get("mac", "allow_restart", False):
        notify_bridge_missing("Twilight was opened without the control bridge")
        return False
    if RESTART_BLOCKED.exists():
        return False
    return restart_twilight_with_control() and take_lease()


ADOPTION_FAILED: set[str] = set()


def adopt_startup_listener() -> None:
    """Twilight restarting itself (an update, about:restart) inherits MOZ_MARIONETTE and
    MOZ_REMOTE_ALLOW_SYSTEM_ACCESS from the process the agent opened with control, so it
    comes back with Marionette on and no bridge. Install the bridge and turn Marionette
    off right away, as the Linux launcher's bootstrap does, instead of leaving
    navigator.webdriver on until the next handoff needs control."""
    if LEASE.held or not marionette_ready() or bridge_ready():
        return
    identity = twilight_identity()
    if identity is None or identity in ADOPTION_FAILED:
        return
    request_control(True)
    if install_bridge() and release_control():
        print(f"Control bridge installed in place for Twilight {identity}", flush=True)
        return
    ADOPTION_FAILED.add(identity)
    notify_bridge_missing("Marionette is listening but the control bridge could not be installed")


def reopen_with_control() -> dict[str, object]:
    before = twilight_identity()
    if before is None:
        return {"ok": False, "error": "Twilight is not running"}
    if bridge_ready():
        return {"ok": True, "restarted": False, "identity": before, "message": "control bridge already installed"}
    ok = restart_twilight_with_control() and release_control()
    return {"ok": ok, "restarted": True, "before": before, "identity": twilight_identity()}


def handle_reopen_request() -> None:
    try:
        request = REOPEN_REQUEST.read_text().strip()
    except OSError:
        return
    REOPEN_REQUEST.unlink(missing_ok=True)
    print("Reopening Twilight with control, as requested", flush=True)
    result = {"request": request, **reopen_with_control()}
    temporary = REOPEN_RESULT.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(REOPEN_RESULT)


def request_reopen() -> int:
    """`mac_agent.py reopen`: the running agent restarts Twilight between handoffs, so
    nothing else writes to the browser while it quits and comes back."""
    request = str(time.time_ns())
    REOPEN_RESULT.unlink(missing_ok=True)
    REOPEN_REQUEST.write_text(request)
    deadline = time.monotonic() + REOPEN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            result = json.loads(REOPEN_RESULT.read_text())
        except (OSError, ValueError):
            result = None
        if isinstance(result, dict) and result.get("request") == request:
            print(json.dumps(result))
            return 0 if result.get("ok") else 1
        time.sleep(1)
    REOPEN_REQUEST.unlink(missing_ok=True)
    print(json.dumps({"ok": False, "error": "the agent did not answer; is it running?"}))
    return 1


def release_control() -> bool:
    """End this process's lease and leave Marionette off, whoever turned it on."""
    if not LEASE.held:
        if not marionette_ready():
            return True
        if not take_lease():
            return False
    clean = LEASE.release(force_off=True)
    print("Mac Twilight control disabled in place" if clean else "Mac Marionette did not stop in place", flush=True)
    return clean


def save_active_baseline(
    *,
    live: bool,
    handoff_id: str | None = None,
    tab_ids_override: set[str] | None = None,
    records: dict[str, list[str]] | None = None,
    stored_fingerprint: str | None = None,
) -> bool:
    identity = twilight_identity()
    ids = tab_ids_override
    if ids is None:
        ids = live_tab_ids() if live else tab_ids(twilight_profile())
    if identity is None or ids is None:
        return False
    if records is None:
        records = active_baseline_records()
    temporary = ACTIVE_BASELINE.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "identity": identity,
        "tabIds": sorted(ids),
        "structureHash": structure_hash(twilight_profile()),
        "records": records,
        "fingerprint": stored_fingerprint,
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


def active_baseline_records() -> dict[str, list[str]] | None:
    try:
        return structure.valid_digests(json.loads(ACTIVE_BASELINE.read_text()).get("records"))
    except (FileNotFoundError, AttributeError, ValueError, json.JSONDecodeError):
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
    print(f"mac-sync reason={reason} exit={result.returncode} {output(result)}", flush=True)
    return result.returncode == 0


def apply_changes(
    closed_tab_ids: list[str],
    tab_records: list[dict[str, object]],
    structure_records: list[dict[str, object]],
    deleted_structure_ids: list[str],
) -> dict[str, object] | None:
    """Apply Linux's handoff in one batch; return the browser's report."""
    if not (closed_tab_ids or tab_records or structure_records or deleted_structure_ids):
        return {"ok": True, "records": None, "deleted": []}
    incoming = BASE / "incoming-linux-changes.json"
    temporary = incoming.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "tabRecords": tab_records,
        "records": structure_records,
        "deletedIds": deleted_structure_ids,
        "closingTabIds": closed_tab_ids,
    }, separators=(",", ":")))
    temporary.replace(incoming)
    try:
        result = run([str(PYTHON), str(STRUCTURE_RECORDS), "apply", "--payload-file", str(incoming)])
    finally:
        incoming.unlink(missing_ok=True)
    report = parse(result)
    print(f"apply-linux-changes exit={result.returncode} {structure.summary(report) if report else output(result)}", flush=True)
    if result.returncode != 0 or not report or not report.get("ok"):
        return None
    return report


def parse(result: subprocess.CompletedProcess[str]) -> dict[str, object] | None:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def structure_changes(
    baseline: dict[str, list[str]] | None,
    *,
    same_browser: bool,
    skip_ids: set[str],
) -> tuple[list[dict[str, object]], list[str], dict[str, list[str]]] | None:
    """Changed structure records, structure deletions and the digests they came from."""
    result = run([str(PYTHON), str(STRUCTURE_RECORDS), "state"])
    state = parse(result) or {}
    records = structure.valid_digests(state.get("records"))
    if result.returncode != 0 or records is None:
        print(f"mac structure state failed: exit={result.returncode} {output(result)}", flush=True)
        return None
    changed, deleted = structure.plan(
        baseline, records, set(state.get("presentIds") or []), same_browser=same_browser, skip_ids=skip_ids,
    )
    exported: list[dict[str, object]] = []
    if changed:
        result = run([str(PYTHON), str(STRUCTURE_RECORDS), "export", "--ids-json", json.dumps(changed)])
        payload = parse(result) or {}
        if result.returncode != 0 or not isinstance(payload.get("records"), list):
            print(f"export-mac-structure exit={result.returncode} {output(result)}", flush=True)
            return None
        exported = payload["records"]
    return structure.outgoing(exported, baseline), deleted, records


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
            print(f"export-mac-tab-records exit={result.returncode} {output(result)}", flush=True)
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
    structure_records: list[dict[str, object]] | None = None,
    deleted_structure_ids: list[str] | None = None,
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
                "structureRecords": structure_records or [],
                "deletedStructureIds": deleted_structure_ids or [],
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
        print(f"remote event={event} exit={result.returncode} {output(result)}", flush=True)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def incoming(response: dict[str, object]) -> tuple[list[str], list, list, list[str], bool] | None:
    closed = response.get("closedTabIds", [])
    opened = response.get("openedTabRecords", [])
    records = response.get("structureRecords", [])
    deleted = response.get("deletedStructureIds", [])
    native = response.get("nativeSyncRequired", False)
    if (
        not isinstance(closed, list) or not all(isinstance(item, str) for item in closed)
        or not isinstance(opened, list) or not isinstance(records, list)
        or not all(isinstance(item, dict) for item in records)
        or not isinstance(deleted, list) or not all(isinstance(item, str) for item in deleted)
        or not isinstance(native, bool)
    ):
        return None
    return closed, opened, records, deleted, native


def enter_mac() -> bool:
    response = remote("mac-active")
    if not response or not response.get("ok"):
        return False
    if response.get("owner") == "mac":
        # A brief idle period is not a fresh handoff. Resetting the baseline
        # here silently acknowledged Mac edits before Linux received them.
        return active_baseline_ids() is not None
    prior_baseline = active_baseline_ids()
    prior_records = active_baseline_records()
    handoff_id = response.get("handoffId")
    changes = incoming(response)
    if changes is None or not isinstance(handoff_id, str):
        return False
    closed_tabs, opened_records, structure_records, deleted_structure, native_sync_required = changes
    needs_control = bool(closed_tabs or opened_records or structure_records or deleted_structure or native_sync_required)
    opened_ids = record_ids(opened_records)
    if opened_ids is None:
        return False
    profile = twilight_profile()
    mac_snapshot = tab_ids(profile)
    # Mac edits not yet sent keep the stored fingerprint dirty, so the return
    # trip still reads and sends them.
    try:
        prior_fingerprint = json.loads(ACTIVE_BASELINE.read_text()).get("fingerprint")
    except (FileNotFoundError, AttributeError, ValueError, json.JSONDecodeError):
        prior_fingerprint = None
    pending = prior_fingerprint is None or prior_fingerprint != fingerprint(profile)
    success = True
    clean = True
    records = prior_records
    if needs_control:
        if not ensure_control():
            return False
        try:
            live_snapshot = live_tab_ids()
            if live_snapshot is None:
                return False
            mac_snapshot = live_snapshot
            report = None
            success = (not native_sync_required or sync_mac("after-linux"))
            if success:
                report = apply_changes(closed_tabs, opened_records, structure_records, deleted_structure)
                success = report is not None
            if report is not None:
                records = structure.received(prior_records, report, opened_records + structure_records, closed_tabs)
        finally:
            clean = release_control()
    # Keep local edits that predate the handoff pending for the return trip.
    expected_ids = ((prior_baseline if prior_baseline is not None else mac_snapshot) - set(closed_tabs)) | opened_ids
    if not success or not clean or not save_active_baseline(
        live=False,
        handoff_id=handoff_id,
        tab_ids_override=expected_ids,
        records=records,
        stored_fingerprint=None if pending else fingerprint(profile),
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
    base_records = structure.valid_digests(baseline_data.get("records"))
    profile = twilight_profile()
    stored = tab_ids(profile)
    structure_changed = (
        cfg.get("sync", "allow_native_structure_sync", False) and same_browser and baseline_data.get("structureHash") is not None
        and baseline_data["structureHash"] != structure_hash(profile)
    )
    structure_dirty = base_records is None or baseline_data.get("fingerprint") != fingerprint(profile)
    local_changed = stored != baseline or structure_changed or structure_dirty
    controlled = False
    success = False
    acknowledged_ids = stored
    acknowledged_records = base_records
    try:
        closed_tabs: set[str] = set()
        opened_records: list[dict[str, object]] = []
        structure_records: list[dict[str, object]] = []
        deleted_structure: list[str] = []
        if local_changed:
            if not ensure_control():
                return False
            controlled = True
            current = live_tab_ids()
            if current is None:
                return False
            acknowledged_ids = current
            closed_tabs = baseline - current if same_browser else set()
            opened_ids = current - baseline
            opened = export_tab_records(opened_ids)
            if opened is None or (structure_changed and not sync_mac("before-linux")):
                return False
            opened_records = opened
            changes = structure_changes(base_records, same_browser=same_browser, skip_ids=opened_ids)
            if changes is None:
                return False
            structure_records, deleted_structure, acknowledged_records = changes
        response = remote(
            "mac-idle",
            closed_tab_ids=closed_tabs,
            opened_tab_records=opened_records,
            native_sync_required=structure_changed,
            handoff_id=handoff_id,
            structure_records=structure_records,
            deleted_structure_ids=deleted_structure,
        )
        if (
            not response
            or not response.get("ok")
            or response.get("deferred")
            or response.get("ignored")
            or response.get("owner") != "mac"
        ):
            return False
        changes_back = incoming(response)
        if changes_back is None:
            return False
        returned_closed, returned_opened, returned_structure, returned_deleted, returned_native = changes_back
        returned_ids = record_ids(returned_opened)
        if returned_ids is None:
            return False
        acknowledged_ids = (acknowledged_ids - set(returned_closed)) | returned_ids
        if returned_closed or returned_opened or returned_structure or returned_deleted or returned_native:
            if not controlled:
                if not ensure_control():
                    return False
                controlled = True
            if returned_native and not sync_mac("after-linux"):
                return False
            report = apply_changes(returned_closed, returned_opened, returned_structure, returned_deleted)
            if report is None:
                return False
            acknowledged_records = structure.received(
                acknowledged_records, report, returned_opened + returned_structure, returned_closed,
            )
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
        live=False,
        handoff_id=handoff_id,
        tab_ids_override=acknowledged_ids,
        records=acknowledged_records,
        stored_fingerprint=fingerprint(profile),
    )


def main() -> int:
    cfg.prepare_directories()
    mac_was_active: bool | None = None
    leave_reported = False
    next_idle_probe_at = 0.0
    enter_confirmed = False
    next_enter_probe_at = 0.0
    while True:
        handle_reopen_request()
        adopt_startup_listener()
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
    if sys.argv[1:] == ["reopen"]:
        sys.exit(request_reopen())
    sys.exit(main())
