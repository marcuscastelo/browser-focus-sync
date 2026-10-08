"""The one client of the in-process Marionette control bridge.

Every user of the bridge goes through this module: the focus-sync daemons, the
fleet's route applier and the Twilight MCP. Standard library only and Python
3.9 compatible, because the Mac side may run it with /usr/bin/python3 over SSH.

A ``Lease`` holds an exclusive ``flock`` on ``<data_dir>/<platform>-control.lock``
from acquire to release, so two users never switch Marionette under each other.
The bridge itself (installed by ``linux_control``/``mac_agent``) only turns the
listener on while ``<platform>-control-request`` says ``on``. A lease turns it
on only when it was off, and turns it back off on release only if the request
is still the one it wrote. Nothing here restarts the browser or installs a
bridge.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import platform
import re
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional

try:  # Python 3.11+
    import tomllib  # type: ignore
except ImportError:  # pragma: no cover - exercised on Python 3.9
    tomllib = None  # type: ignore

DEFAULT_PORT = 2828
STATUS_MAX_AGE_MS = 180_000  # the bridge rewrites its status at least every 60 s
DEFAULT_EXECUTABLES = {
    "mac": "/Applications/Twilight.app/Contents/MacOS/zen",
    "linux": "",
}


class BridgeError(RuntimeError):
    """A bridge failure with a stable ``code`` for callers and tools."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MarionetteError(RuntimeError):
    def __init__(self, error: str, message: str):
        super().__init__(f"{error}: {message}")
        self.error = error
        self.message = message


class Paths(NamedTuple):
    directory: Path
    request: Path
    status: Path
    lock: Path
    holder: Path


# --- configuration ---------------------------------------------------------

def config_file() -> Path:
    return Path(os.environ.get(
        "BROWSER_FOCUS_SYNC_CONFIG", "~/.config/browser-focus-sync/config.toml"
    )).expanduser()


def _parse_toml_subset(text: str) -> Dict[str, Dict[str, Any]]:
    """Enough TOML for config.toml on Python 3.9: tables, strings, booleans, ints."""
    data: Dict[str, Dict[str, Any]] = {}
    section = data.setdefault("", {})
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        table = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if table:
            section = data.setdefault(table.group(1), {})
            continue
        pair = re.fullmatch(r'([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*(?:#.*)?', line)
        if not pair:
            continue
        key, value = pair.groups()
        if value.startswith('"') and value.endswith('"'):
            section[key] = json.loads(value)
        elif value in ("true", "false"):
            section[key] = value == "true"
        elif re.fullmatch(r"-?\d+", value):
            section[key] = int(value)
    return data


def load_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or config_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}
    return _parse_toml_subset(text)


def platform_prefix(system: Optional[str] = None) -> str:
    return "mac" if (system or platform.system()) == "Darwin" else "linux"


def data_dir(settings: Optional[Dict[str, Any]] = None) -> Path:
    settings = load_settings() if settings is None else settings
    value = settings.get("paths", {}).get("data_dir", "~/.local/share/browser-focus-sync")
    return Path(value).expanduser()


def executable(settings: Optional[Dict[str, Any]] = None, prefix: Optional[str] = None) -> str:
    settings = load_settings() if settings is None else settings
    prefix = prefix or platform_prefix()
    return str(settings.get(prefix, {}).get("executable", DEFAULT_EXECUTABLES[prefix]))


def bridge_paths(directory: Path, prefix: Optional[str] = None) -> Paths:
    prefix = prefix or platform_prefix()
    return Paths(
        directory,
        directory / f"{prefix}-control-request",
        directory / f"{prefix}-control-bridge.json",
        directory / f"{prefix}-control.lock",
        directory / f"{prefix}-control.holder.json",
    )


# --- browser identity ------------------------------------------------------

def _linux_identity(executable_path: str) -> Optional[str]:
    # A quick restart briefly leaves the exiting browser alive; prefer the newest.
    newest = None
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        return None
    for proc in processes:
        if not proc.name.isdigit():
            continue
        try:
            command = (proc / "cmdline").read_bytes().split(b"\0")
            if not command or command[0].decode() != executable_path or b"-contentproc" in command:
                continue
            started = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, UnicodeDecodeError, IndexError, ValueError):
            continue
        if newest is None or int(started) > int(newest[0]):
            newest = (started, proc.name)
    return f"{newest[1]}:{newest[0]}" if newest else None


def _mac_identity(executable_path: str) -> Optional[str]:
    try:
        listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True,
                                 text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in listing.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[1].split(maxsplit=1)[0] == executable_path:
            try:
                started = subprocess.run(["/bin/ps", "-p", fields[0], "-o", "lstart="],
                                         capture_output=True, text=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if started.returncode or not started.stdout.strip():
                return None
            return f"{fields[0]}:{started.stdout.strip()}"
    return None


def browser_identity(executable_path: str, system: Optional[str] = None) -> Optional[str]:
    """``pid:start`` of the running browser, in the format the bridge records."""
    if not executable_path:
        return None
    if (system or platform.system()) == "Darwin":
        return _mac_identity(executable_path)
    return _linux_identity(executable_path)


def identity_is_current(identity: str, executable_path: str, system: Optional[str] = None) -> bool:
    """Whether ``identity`` still names a live process of ``executable_path``."""
    pid, separator, started = identity.partition(":")
    if not separator or not pid.isdecimal() or not started or not executable_path:
        return False
    if (system or platform.system()) == "Darwin":
        try:
            result = subprocess.run(["/bin/ps", "-p", pid, "-o", "lstart=", "-o", "command="],
                                    capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        line = result.stdout.strip()
        if result.returncode or not line.startswith(started + " "):
            return False
        return line[len(started):].strip().split()[0] == executable_path
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return command == executable_path and stat[19] == started
    except (OSError, UnicodeDecodeError, IndexError, ValueError):
        return False


# --- bridge state ----------------------------------------------------------

def listening(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def read_status(paths: Paths) -> Optional[Dict[str, Any]]:
    try:
        if paths.status.stat().st_size > 4096:
            return None
        state = json.loads(paths.status.read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def read_request(paths: Paths) -> Optional[str]:
    try:
        value = paths.request.read_text().strip()
    except OSError:
        return None
    return value if value in ("on", "off") else None


def private_directory(directory: Path) -> bool:
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        return False
    metadata = directory.stat()
    return metadata.st_uid == os.getuid() and not metadata.st_mode & 0o077


def current_bridge(paths: Paths, executable_path: str, system: Optional[str] = None) -> Optional[str]:
    """Identity of a fresh bridge that belongs to the running browser, else None."""
    if not private_directory(paths.directory) or read_request(paths) is None:
        return None
    state = read_status(paths)
    if not state:
        return None
    identity, updated_at = state.get("identity"), state.get("updatedAt")
    if not isinstance(identity, str) or not isinstance(updated_at, (int, float)):
        return None
    if abs(time.time() * 1000 - updated_at) > STATUS_MAX_AGE_MS:
        return None
    return identity if identity_is_current(identity, executable_path, system) else None


def write_request(paths: Paths, value: str) -> int:
    """Atomically write ``on``/``off``; return the new request mtime."""
    descriptor, temporary = tempfile.mkstemp(prefix=".control-request-", dir=str(paths.directory))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.chmod(temporary, 0o600)
        os.replace(temporary, paths.request)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return paths.request.stat().st_mtime_ns


def wait_listening(port: int, expected: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if listening(port) is expected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def wait_clean(paths: Paths, identity: str, timeout: float) -> bool:
    """Wait until the bridge reports Marionette and webdriver both off."""
    deadline = time.monotonic() + timeout
    while True:
        state = read_status(paths) or {}
        if (state.get("identity") == identity and state.get("running") is False
                and state.get("webdriverActive") is False):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


# --- lease -----------------------------------------------------------------

def holder(paths: Paths) -> Optional[Dict[str, Any]]:
    """Who holds the lock and since when (ms), for diagnostics; None when free.

    A holder killed mid-lease leaves its file behind; ``alive`` says whether its pid
    still runs (the kernel already dropped its lock)."""
    try:
        state = json.loads(paths.holder.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    try:
        os.kill(int(state.get("pid", 0)), 0)
        state["alive"] = True
    except (OSError, ValueError):
        state["alive"] = False
    return state


class Lease:
    """Exclusive use of Marionette through the bridge, from acquire to release.

    Long-lived users (the coordinator, the Mac agent) keep one Lease across a
    whole handoff; short users wrap a single call in ``controlled()``. The
    subprocesses a holder starts connect to the port without taking the lock.
    """

    def __init__(self, port: int = DEFAULT_PORT, directory: Optional[Path] = None,
                 executable_path: Optional[str] = None, prefix: Optional[str] = None,
                 lock_timeout: float = 30.0, start_timeout: float = 15.0,
                 stop_timeout: float = 15.0, system: Optional[str] = None, label: str = ""):
        settings = load_settings() if directory is None or executable_path is None else {}
        self.system = system or platform.system()
        self.prefix = prefix or platform_prefix(self.system)
        self.paths = bridge_paths(directory or data_dir(settings), self.prefix)
        self.executable = executable_path if executable_path is not None else executable(settings, self.prefix)
        self.port = port
        self.lock_timeout = lock_timeout
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.label = label or Path(os.environ.get("_", "") or "python").name
        self.identity: Optional[str] = None
        self.inherited = False
        self._lock_fd: Optional[int] = None
        self._owned_mtime: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._lock_fd is not None

    def _lock(self) -> None:
        if not private_directory(self.paths.directory):
            raise BridgeError("bridge_unavailable", "Twilight control directory is missing or not private")
        descriptor = os.open(str(self.paths.lock), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fd = descriptor
                self._write_holder()
                return
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    os.close(descriptor)
                    raise
            if time.monotonic() >= deadline:
                os.close(descriptor)
                current = holder(self.paths) or {}
                who = f" ({current.get('label')}, pid {current.get('pid')})" if current else ""
                raise BridgeError("bridge_busy", f"Another user holds Twilight control{who}")
            time.sleep(0.1)

    def _write_holder(self) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".control-holder-", dir=str(self.paths.directory))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "label": self.label, "since": int(time.time() * 1000)}, stream)
            os.replace(temporary, self.paths.holder)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _unlock(self) -> None:
        if self._lock_fd is not None:
            current = holder(self.paths)
            if current and current.get("pid") == os.getpid():
                try:
                    self.paths.holder.unlink()
                except OSError:
                    pass
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def acquire(self) -> "Lease":
        if self.held:
            raise BridgeError("bridge_busy", "This lease is already held")
        self._lock()
        try:
            if listening(self.port):
                # Someone outside the lock (startup listener, an older client) left
                # it on. Use it, and leave the decision to turn it off to that owner.
                self.inherited = True
                self.identity = browser_identity(self.executable, self.system)
                return self
            # The bridge rewrites its status file in place; retry a torn read.
            identity = None
            for _ in range(8):
                identity = current_bridge(self.paths, self.executable, self.system)
                if identity is not None:
                    break
                time.sleep(0.125)
            if identity is None:
                raise BridgeError("bridge_unavailable",
                                  "Twilight control bridge is unavailable or stale; refusing to restart the browser")
            self.identity = identity
            if read_request(self.paths) == "off":
                self._owned_mtime = write_request(self.paths, "on")
            if not wait_listening(self.port, True, self.start_timeout):
                self._turn_off()
                raise BridgeError("start_failed", "Twilight control bridge did not start Marionette")
            return self
        except BaseException:
            self._unlock()
            raise

    def _turn_off(self) -> bool:
        if self._owned_mtime is None:
            return True
        try:
            if self.paths.request.stat().st_mtime_ns != self._owned_mtime:
                return True  # someone else rewrote the request; theirs to release
        except OSError:
            return False
        write_request(self.paths, "off")
        self._owned_mtime = None
        return (wait_listening(self.port, False, self.stop_timeout)
                and (self.identity is None or wait_clean(self.paths, self.identity, self.stop_timeout)))

    def release(self, force_off: bool = False) -> bool:
        """Turn Marionette off if this lease turned it on; ``force_off`` also
        stops a listener it inherited (the launcher's startup listener)."""
        if not self.held:
            return True
        try:
            if force_off and self._owned_mtime is None and listening(self.port):
                identity = current_bridge(self.paths, self.executable, self.system)
                if identity is None:
                    return False
                self.identity = identity
                write_request(self.paths, "off")
                return (wait_listening(self.port, False, self.stop_timeout)
                        and wait_clean(self.paths, identity, self.stop_timeout))
            return self._turn_off()
        finally:
            self._unlock()
            self.inherited = False

    def __enter__(self) -> "Lease":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        clean = self.release()
        if not clean and exc[0] is None:
            raise BridgeError("release_failed", "Twilight control bridge did not release Marionette")


@contextmanager
def controlled(**options: Any) -> Iterator[Lease]:
    with Lease(**options) as lease:
        yield lease


# --- minimal Marionette client ---------------------------------------------

class Marionette:
    """Marionette wire protocol: ``<length>:<json>``, commands ``[0, id, name, params]``."""

    def __init__(self, port: int = DEFAULT_PORT, timeout: float = 30.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.last_id = 0
        self._read()  # greeting

    def _read(self) -> Any:
        header = b""
        while not header.endswith(b":"):
            byte = self.sock.recv(1)
            if not byte or len(header) > 20:
                raise MarionetteError("connection", "Marionette closed the connection")
            header += byte
        missing, body = int(header[:-1]), b""
        while len(body) < missing:
            part = self.sock.recv(missing - len(body))
            if not part:
                raise MarionetteError("connection", "Marionette closed the connection")
            body += part
        return json.loads(body)

    def command(self, name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self.last_id += 1
        body = json.dumps([0, self.last_id, name, params or {}]).encode()
        self.sock.sendall(str(len(body)).encode() + b":" + body)
        _, _, error, result = self._read()
        if error:
            raise MarionetteError(str(error.get("error")), str(error.get("message")))
        return result

    def execute(self, script: str, args: Optional[List[Any]] = None, asynchronous: bool = False,
                timeout_ms: int = 20_000) -> Any:
        self.command("WebDriver:SetTimeouts", {"script": timeout_ms})
        name = "WebDriver:ExecuteAsyncScript" if asynchronous else "WebDriver:ExecuteScript"
        return self.command(name, {"script": script, "args": args or []})["value"]

    def close(self) -> None:
        self.sock.close()


@contextmanager
def chrome_session(port: int = DEFAULT_PORT, timeout: float = 30.0) -> Iterator[Marionette]:
    """A Marionette session in the browser's chrome context, always deleted."""
    client = Marionette(port, timeout)
    started = False
    try:
        client.command("WebDriver:NewSession", {"capabilities": {}})
        started = True
        client.command("Marionette:SetContext", {"value": "chrome"})
        yield client
    finally:
        try:
            if started:
                client.command("WebDriver:DeleteSession")
        except (OSError, MarionetteError):
            pass
        client.close()


def run_chrome_script(script: str, args: Optional[List[Any]] = None, *, asynchronous: bool = False,
                      timeout_ms: int = 20_000, **lease_options: Any) -> Any:
    """Acquire, run one chrome-context script, release."""
    port = lease_options.get("port", DEFAULT_PORT)
    with controlled(**lease_options):
        with chrome_session(port) as client:
            return client.execute(script, args, asynchronous, timeout_ms)
