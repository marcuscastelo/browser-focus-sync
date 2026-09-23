#!/usr/bin/env python3
from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import signal
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
EXECUTABLE = str(cfg.get("linux", "executable", ""))
RESTART_BLOCKED = BASE / "linux-restart-blocked"
CONTROL_REQUEST = BASE / "linux-control-request"
BRIDGE_STATUS = BASE / "linux-control-bridge.json"


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def profile_path() -> Path:
    return cfg.profile("linux")


def twilight_pid() -> int | None:
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = (proc / "cmdline").read_bytes().split(b"\0")
            if command and command[0].decode() == EXECUTABLE and b"-contentproc" not in command:
                return int(proc.name)
        except (FileNotFoundError, PermissionError, UnicodeDecodeError):
            continue
    return None


def twilight_identity() -> str | None:
    pid = twilight_pid()
    if pid is None:
        return None
    try:
        started = Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (FileNotFoundError, PermissionError, IndexError):
        return None
    return f"{pid}:{started}"


def marionette_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 2828), timeout=1):
            return True
    except OSError:
        return False


def stored_tab_ids(profile: Path | None = None) -> set[str]:
    profile = profile or profile_path()
    payload = (profile / "zen-sessions.jsonlz4").read_bytes()
    data = json.loads(lz4.block.decompress(payload[8:]))
    return {tab["zenSyncId"] for tab in data.get("tabs", []) if tab.get("zenSyncId")}


def stored_structure_hash(profile: Path | None = None) -> str:
    profile = profile or profile_path()
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


def snapshot_session(profile: Path) -> Path:
    backup = BASE / "linux-restart-backups" / str(time.time_ns())
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
            destination = backup / source.relative_to(profile)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return backup


def restore_session(profile: Path, backup: Path) -> None:
    for source in backup.rglob("*"):
        if source.is_file():
            destination = profile / source.relative_to(backup)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def stop_twilight() -> bool:
    pid = twilight_pid()
    if pid is None:
        return True
    os.kill(pid, signal.SIGTERM)
    for _ in range(120):
        if twilight_pid() is None:
            return True
        time.sleep(0.25)
    return False


def start_twilight(*, controlled: bool) -> bool:
    unit = f"twilight-focus-browser-{time.time_ns()}"
    command = [
        "/usr/bin/systemd-run",
        "--user",
        f"--unit={unit}",
        "--collect",
        "--property=Type=exec",
        "--quiet",
        "--setenv=LANG=C.UTF-8",
        "--setenv=LC_ALL=C.UTF-8",
        "--setenv=MOZ_DISABLE_AUTO_SAFE_MODE=1",
        EXECUTABLE,
        "--profile",
        str(profile_path()),
        "--restore-last-session",
    ]
    if controlled:
        command.extend(["--marionette", "--remote-allow-system-access"])
    if run(command, timeout=15).returncode != 0:
        return False
    for _ in range(120):
        ready = twilight_pid() is not None and marionette_ready() == controlled
        if ready:
            return True
        time.sleep(0.25)
    return False


def acquire() -> bool:
    if marionette_ready():
        request_control(True)
        if not bridge_ready() and not install_bridge():
            return False
        return wait_for_control(True)
    if bridge_ready():
        request_control(True)
        if wait_for_control(True):
            print("Temporary Linux Twilight control enabled in place", flush=True)
            return True
        return False
    print("Linux Twilight has no in-process control bridge; refusing to restart it", flush=True)
    return False


def bootstrap() -> bool:
    if bridge_ready():
        return release()
    for _ in range(120):
        if marionette_ready():
            break
        if twilight_pid() is None:
            time.sleep(0.25)
            continue
        time.sleep(0.25)
    else:
        return False
    request_control(True)
    if not install_bridge():
        return False
    return release()


def release() -> bool:
    if not marionette_ready():
        return True
    print("Stopping temporary Linux Twilight control", flush=True)
    if not bridge_ready() and not install_bridge():
        print("Cannot install in-process Twilight control; leaving listener unchanged", flush=True)
        return False
    request_control(False)
    if not wait_for_control(False):
        print("Linux Marionette did not stop in place", flush=True)
        return False
    clean = wait_for_bridge_clean()
    if clean:
        print("Linux Twilight control disabled in place", flush=True)
    return clean


def status() -> dict[str, object]:
    ids = stored_tab_ids()
    pid = twilight_pid()
    command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode() if pid else ""
    try:
        bridge = json.loads(BRIDGE_STATUS.read_text()) if bridge_ready() else {}
    except (json.JSONDecodeError, OSError):
        bridge = {}
    return {
        "running": pid is not None,
        "pid": pid,
        "marionette": marionette_ready(),
        "marionetteFlag": "--marionette" in command,
        "bridge": bridge_ready(),
        "webdriverActive": bridge.get("webdriverActive"),
        "tabs": len(ids),
        "hash": hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest(),
    }


def main() -> int:
    action = sys.argv[1] if len(sys.argv) == 2 else "status"
    if action == "acquire":
        ok = acquire()
        print(json.dumps({"ok": ok, **status()}, separators=(",", ":")))
        return 0 if ok else 1
    if action == "release":
        ok = release()
        print(json.dumps({"ok": ok, **status()}, separators=(",", ":")))
        return 0 if ok else 1
    if action == "bootstrap":
        ok = bootstrap()
        print(json.dumps({"ok": ok, **status()}, separators=(",", ":")))
        return 0 if ok else 1
    if action == "status":
        print(json.dumps({"ok": True, **status()}, separators=(",", ":")))
        return 0
    print(f"unknown action: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
