import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_client as bc


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeBridge(threading.Thread):
    """Plays the in-process bridge: listens on the port while the request says on."""

    def __init__(self, paths, port, identity="123:456", updated_at=None):
        super().__init__(daemon=True)
        self.paths, self.port, self.identity = paths, port, identity
        self.updated_at = updated_at
        self.server = None
        self.stop = threading.Event()
        self.write_status()

    def write_status(self):
        running = self.server is not None
        self.paths.status.write_text(json.dumps({
            "identity": self.identity, "running": running, "webdriverActive": running,
            "updatedAt": self.updated_at if self.updated_at is not None else time.time() * 1000,
        }))

    def run(self):
        while not self.stop.is_set():
            wanted = bc.read_request(self.paths) == "on"
            if wanted and self.server is None:
                self.server = socket.socket()
                self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server.bind(("127.0.0.1", self.port))
                self.server.listen(8)
            elif not wanted and self.server is not None:
                self.server.close()
                self.server = None
            self.write_status()
            time.sleep(0.05)
        if self.server is not None:
            self.server.close()


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "data"
        self.directory.mkdir(mode=0o700)
        self.port = free_port()
        self.paths = bc.bridge_paths(self.directory, "linux")
        self.paths.request.write_text("off")
        self.current = patch.object(bc, "identity_is_current", return_value=True)
        self.current.start()
        self.identity = patch.object(bc, "browser_identity", return_value="123:456")
        self.identity.start()
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            bridge.stop.set()
            bridge.join(2)
        self.current.stop()
        self.identity.stop()
        self.tmp.cleanup()

    def bridge(self, **options):
        bridge = FakeBridge(self.paths, self.port, **options)
        bridge.start()
        self.bridges.append(bridge)
        return bridge

    def lease(self, **options):
        values = dict(port=self.port, directory=self.directory, executable_path="/x/zen",
                      prefix="linux", system="Linux", lock_timeout=0.3,
                      start_timeout=3, stop_timeout=3)
        values.update(options)
        return bc.Lease(**values)

    def test_lease_turns_on_and_back_off(self):
        self.bridge()
        with self.lease() as lease:
            self.assertTrue(bc.listening(self.port))
            self.assertEqual(lease.identity, "123:456")
            self.assertFalse(lease.inherited)
        self.assertFalse(bc.listening(self.port))
        self.assertEqual(bc.read_request(self.paths), "off")

    def test_inherited_listener_stays_on_unless_forced(self):
        self.paths.request.write_text("on")
        self.bridge()
        self.assertTrue(bc.wait_listening(self.port, True, 3))
        with self.lease() as lease:
            self.assertTrue(lease.inherited)
        self.assertTrue(bc.listening(self.port))
        lease = self.lease().acquire()
        self.assertTrue(lease.release(force_off=True))
        self.assertFalse(bc.listening(self.port))

    def test_lock_is_exclusive_across_leases(self):
        self.bridge()
        first = self.lease().acquire()
        try:
            with self.assertRaises(bc.BridgeError) as caught:
                self.lease().acquire()
            self.assertEqual(caught.exception.code, "bridge_busy")
        finally:
            first.release()
        with self.lease():
            pass

    def test_holder_file_names_who_holds_the_lock(self):
        self.bridge()
        first = self.lease(label="coordinator").acquire()
        try:
            state = bc.holder(self.paths)
            self.assertEqual((state["label"], state["pid"], state["alive"]), ("coordinator", os.getpid(), True))
            self.assertLessEqual(state["since"], time.time() * 1000)
            with self.assertRaises(bc.BridgeError) as caught:
                self.lease(label="mcp").acquire()
            self.assertIn("coordinator", str(caught.exception))
        finally:
            first.release()
        self.assertIsNone(bc.holder(self.paths))

    def test_no_bridge_never_restarts_anything(self):
        with patch.object(bc.subprocess, "run") as run, patch.object(bc.subprocess, "Popen") as popen:
            with self.assertRaises(bc.BridgeError) as caught:
                self.lease(system="Linux").acquire()
        self.assertEqual(caught.exception.code, "bridge_unavailable")
        popen.assert_not_called()
        run.assert_not_called()

    def test_stale_bridge_is_refused_without_touching_request(self):
        bridge = self.bridge(updated_at=(time.time() - 600) * 1000)
        with self.assertRaises(bc.BridgeError) as caught:
            self.lease().acquire()
        self.assertEqual(caught.exception.code, "bridge_unavailable")
        self.assertEqual(bc.read_request(self.paths), "off")
        bridge.updated_at = None
        time.sleep(0.2)
        with self.lease():  # the lock was released after the failure
            pass

    def test_bridge_of_another_process_is_refused(self):
        self.bridge()
        with patch.object(bc, "identity_is_current", return_value=False):
            with self.assertRaises(bc.BridgeError) as caught:
                self.lease().acquire()
        self.assertEqual(caught.exception.code, "bridge_unavailable")

    def test_request_rewritten_by_someone_else_is_not_released(self):
        self.bridge()
        lease = self.lease().acquire()
        time.sleep(0.01)
        bc.write_request(self.paths, "on")
        self.assertTrue(lease.release())
        self.assertEqual(bc.read_request(self.paths), "on")

    def test_public_directory_is_refused(self):
        self.directory.chmod(0o755)
        with self.assertRaises(bc.BridgeError) as caught:
            self.lease().acquire()
        self.assertEqual(caught.exception.code, "bridge_unavailable")


class ConfigTests(unittest.TestCase):
    def test_subset_parser_matches_tomllib_on_example(self):
        text = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
        if bc.tomllib is None:
            self.skipTest("tomllib unavailable")
        expected = bc.tomllib.loads(text)
        parsed = bc._parse_toml_subset(text)
        for section, values in expected.items():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if isinstance(value, (str, bool, int)):
                    self.assertEqual(parsed[section][key], value, f"{section}.{key}")

    def test_data_dir_and_executable(self):
        settings = {"paths": {"data_dir": "~/d"}, "linux": {"executable": "/opt/zen"}}
        self.assertEqual(bc.data_dir(settings), Path("~/d").expanduser())
        self.assertEqual(bc.executable(settings, "linux"), "/opt/zen")
        self.assertEqual(bc.executable({}, "mac"), bc.DEFAULT_EXECUTABLES["mac"])


class FakeMarionette(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.commands = []

    def send(self, conn, payload):
        body = json.dumps(payload).encode()
        conn.sendall(str(len(body)).encode() + b":" + body)

    def run(self):
        conn, _ = self.server.accept()
        with conn:
            self.send(conn, {"applicationType": "gecko", "marionetteProtocol": 3})
            stream = conn.makefile("rb")
            while True:
                header = b""
                while not header.endswith(b":"):
                    byte = stream.read(1)
                    if not byte:
                        return
                    header += byte
                _, message_id, name, params = json.loads(stream.read(int(header[:-1])))
                self.commands.append(name)
                if name == "WebDriver:ExecuteScript" and params["script"] == "boom":
                    self.send(conn, [1, message_id, {"error": "javascript error", "message": "boom"}, None])
                elif name == "WebDriver:ExecuteScript":
                    self.send(conn, [1, message_id, None, {"value": params["args"][0] * 2}])
                else:
                    self.send(conn, [1, message_id, None, {}])


class MarionetteClientTests(unittest.TestCase):
    def test_chrome_session_runs_script_and_deletes_session(self):
        fake = FakeMarionette()
        fake.start()
        with bc.chrome_session(fake.port) as client:
            self.assertEqual(client.execute("return arguments[0] * 2", [21]), 42)
            with self.assertRaises(bc.MarionetteError) as caught:
                client.execute("boom")
            self.assertEqual(caught.exception.error, "javascript error")
        fake.join(2)
        self.assertEqual(fake.commands[:2], ["WebDriver:NewSession", "Marionette:SetContext"])
        self.assertEqual(fake.commands[-1], "WebDriver:DeleteSession")


if __name__ == "__main__":
    unittest.main()
