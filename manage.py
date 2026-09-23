#!/usr/bin/env python3
"""Explicit setup utilities. Installing never starts a service or browser."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME_FILES = (
    "bfs_config.py", "coordinator.py", "linux_control.py", "mac_agent.py",
    "sync_now.py", "apply_tab_records.py", "apply_tab_deletions.py",
    "export_tab_records.py", "focusctl.py",
)
LABEL = "io.github.browser-focus-sync"


def load_config(filename=None):
    if filename:
        os.environ["BROWSER_FOCUS_SYNC_CONFIG"] = str(Path(filename).expanduser().resolve())
    sys.path.insert(0, str(ROOT / "runtime"))
    import bfs_config
    return bfs_config


def systemd_quote(value):
    # systemd has its own escaping (not shell quoting).
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def service_files(kind, data, config, home):
    python = data / "venv/bin/python"
    if kind == "linux":
        content = "\n".join([
            "[Unit]", "Description=Browser focus handoff coordinator",
            "After=graphical-session.target", "PartOf=graphical-session.target", "",
            "[Service]", "Type=simple", "UMask=0077",
            "Environment=" + systemd_quote("BROWSER_FOCUS_SYNC_CONFIG=" + str(config)),
            "ExecStart=" + systemd_quote(python) + " " + systemd_quote(data / "runtime/coordinator.py"),
            "Restart=on-failure", "RestartSec=5", "",
            "[Install]", "WantedBy=graphical-session.target", "",
        ])
        return home / ".config/systemd/user/browser-focus-sync.service", content.encode()
    content = plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(python), str(data / "runtime/mac_agent.py")],
        "EnvironmentVariables": {"BROWSER_FOCUS_SYNC_CONFIG": str(config)},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(data / "agent.log"),
        "StandardErrorPath": str(data / "agent.log"),
    })
    return home / "Library/LaunchAgents" / (LABEL + ".plist"), content


def inventory(cfg, kind):
    import lz4.block
    profile = cfg.profile(kind)
    payload = (profile / "zen-sessions.jsonlz4").read_bytes()
    if payload[:8] != b"mozLz40\0":
        raise ValueError("Unrecognized Zen session file")
    session = json.loads(lz4.block.decompress(payload[8:]))
    ids = sorted({t["zenSyncId"] for t in session.get("tabs", []) if t.get("zenSyncId")})
    spaces = sorted(s["uuid"] for s in session.get("spaces", []) if s.get("uuid"))
    if not ids or not spaces:
        raise ValueError("Refusing empty or incomplete session inventory")
    return {"version": 1, "tabIds": ids, "spaceIds": spaces,
            "folderIds": sorted(f["id"] for f in session.get("folders", []) if f.get("id")),
            "sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest()}


def require_alignment(own, peer):
    for key in ("tabIds", "spaceIds", "folderIds"):
        if set(own[key]) != set(peer[key]):
            raise ValueError(f"Sessions differ ({key}). Back up and reconcile first; no baseline written.")


def install(cfg, kind, dry_run):
    cfg.profile(kind)  # Fail before installing if the chosen profile is wrong.
    data = cfg.DATA_DIR
    target, content = service_files(kind, data, cfg.CONFIG_FILE, Path.home())
    if dry_run:
        print(json.dumps({"data": str(data), "service": str(target), "startsAnything": False}))
        return
    if data.exists() or target.exists():
        raise ValueError("Destination already exists. Refusing to overwrite an existing installation.")
    cfg.prepare_directories()
    runtime = data / "runtime"
    runtime.mkdir(mode=0o700)
    for filename in RUNTIME_FILES:
        shutil.copy2(ROOT / "runtime" / filename, runtime / filename)
    subprocess.run([sys.executable, "-m", "venv", str(data / "venv")], check=True)
    subprocess.run([str(data / "venv/bin/python"), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)
    if kind == "linux":
        subprocess.run(["cmake", "-S", str(ROOT / "native"), "-B", str(data / "build")], check=True)
        subprocess.run(["cmake", "--build", str(data / "build")], check=True)
        shutil.copy2(data / "build/idle-events", data / "idle-events")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    target.chmod(0o600)
    print(f"Installed, NOT started. Service definition: {target}")
    print("Follow README setup: back up, preferences, control bridge, matching inventories, baselines, then start.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="machine-local TOML path")
    parser.add_argument("--platform", choices=["linux", "mac"], default="mac" if platform.system() == "Darwin" else "linux")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-config")
    sub.add_parser("doctor")
    setup = sub.add_parser("install")
    setup.add_argument("--dry-run", action="store_true")
    inv = sub.add_parser("inventory")
    inv.add_argument("--out", required=True, type=Path)
    seed = sub.add_parser("baseline")
    seed.add_argument("--peer-inventory", required=True, type=Path)
    seed.add_argument("--confirm-backed-up-and-aligned", action="store_true", required=True)
    sub.add_parser("bootstrap-control")
    args = parser.parse_args()
    os.umask(0o077)
    cfg = load_config(args.config)
    if args.command == "init-config":
        cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with cfg.CONFIG_FILE.open("x") as stream:
            stream.write((ROOT / "config.example.toml").read_text())
        print(f"Edit {cfg.CONFIG_FILE}; no browser changes made.")
    elif args.command == "doctor":
        profile = cfg.profile(args.platform)
        items = inventory(cfg, args.platform)
        print(json.dumps({"config": str(cfg.CONFIG_FILE), "profile": str(profile),
            "tabs": len(items['tabIds']), "spaces": len(items['spaceIds']),
            "baselineExists": (cfg.DATA_DIR / ("active-baseline.json" if args.platform == "mac" else "linux-active-baseline.json")).exists(),
            "nativeStructureSync": bool(cfg.get("sync", "allow_native_structure_sync", False)),
            "readOnly": True}))
    elif args.command == "install":
        install(cfg, args.platform, args.dry_run)
    elif args.command == "inventory":
        items = inventory(cfg, args.platform)
        with args.out.expanduser().open("x") as stream:
            json.dump(items, stream)
        print(f"Wrote ID-only inventory ({len(items['tabIds'])} tabs); treat as private.")
    elif args.command == "baseline":
        own = inventory(cfg, args.platform)
        peer = json.loads(args.peer_inventory.expanduser().read_text())
        require_alignment(own, peer)
        name = "active-baseline.json" if args.platform == "mac" else "linux-active-baseline.json"
        if (cfg.DATA_DIR / name).exists() or (cfg.STATE_DIR / "state.json").exists():
            raise ValueError("Existing baseline/state found. Refusing to acknowledge pending changes.")
        cfg.prepare_directories()
        if args.platform == "mac":
            import mac_agent
            if not mac_agent.save_active_baseline(live=False, tab_ids_override=set(own['tabIds'])):
                raise ValueError("Browser must be running to establish baseline")
        else:
            import asyncio
            import coordinator
            if not asyncio.run(coordinator.Coordinator().save_linux_active_baseline(set(own['tabIds']))):
                raise ValueError("Browser must be running to establish baseline")
        print("Initial baseline saved. No tabs applied or deleted.")
    elif args.command == "bootstrap-control":
        cfg.prepare_directories()
        if args.platform == "mac":
            import mac_agent
            acquire, release = mac_agent.ensure_control, mac_agent.release_control
        else:
            import linux_control
            acquire, release = linux_control.bootstrap, linux_control.release
        try:
            if not acquire():
                raise ValueError("Start the selected browser with temporary control flags; see README")
        finally:
            if not release():
                raise ValueError("CONTROL DID NOT STOP: stop the agent and restart browser normally")
        print("Control bridge ready; Marionette released.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
