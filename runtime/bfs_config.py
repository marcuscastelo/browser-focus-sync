"""Machine-local configuration. Never load settings from browser profiles."""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

CONFIG_FILE = Path(os.environ.get(
    "BROWSER_FOCUS_SYNC_CONFIG",
    str(Path.home() / ".config/browser-focus-sync/config.toml"),
)).expanduser()
if CONFIG_FILE.exists():
    with CONFIG_FILE.open("rb") as stream:
        SETTINGS = tomllib.load(stream)
else:
    SETTINGS = {}


def get(section: str, key: str, default=None):
    return SETTINGS.get(section, {}).get(key, default)


def path(section: str, key: str, default=None) -> Path:
    value = get(section, key, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Set [{section}] {key} in {CONFIG_FILE}")
    result = Path(value).expanduser()
    if not result.is_absolute():
        raise ValueError(f"[{section}] {key} must be absolute (or start with ~)")
    return result


def profile(platform: str) -> Path:
    result = path(platform, "profile")
    if not (result / "zen-sessions.jsonlz4").is_file():
        raise ValueError(f"[{platform}] profile has no Zen session file: {result}")
    return result


CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = path("paths", "data_dir", "~/.local/share/browser-focus-sync")
STATE_DIR = path("paths", "state_dir", "~/.local/state/browser-focus-sync")
SOCKET = path("paths", "socket_path", str(STATE_DIR / "coordinator.sock"))
PYTHON = Path(sys.executable)
# One newline-delimited coordinator request or response; handoffs carry tab records.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def prepare_directories() -> None:
    os.umask(0o077)
    for directory in (DATA_DIR, STATE_DIR):
        if directory in (Path.home(), Path('/')):
            raise ValueError("Use a dedicated private state directory, not a home or filesystem root")
        if directory.is_symlink():
            raise ValueError(f"Refusing symlinked state directory: {directory}")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
