#!/usr/bin/env python3
"""Send one authenticated-by-local-UID request to the coordinator."""
from __future__ import annotations
import base64
import json
import socket
import sys
import bfs_config as cfg


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: focusctl.py status|mac-active|base64:PAYLOAD|- (stdin)", file=sys.stderr)
        return 2
    payload = sys.argv[1]
    if payload == "-":
        payload = sys.stdin.read(cfg.MAX_MESSAGE_BYTES + 1)
        if len(payload) > cfg.MAX_MESSAGE_BYTES:
            print(json.dumps({"ok": False, "error": "request exceeds 16 MiB"}))
            return 1
    try:
        if payload.startswith("base64:"):
            payload = base64.urlsafe_b64decode(payload[7:]).decode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(120)
            client.connect(str(cfg.SOCKET))
            try:
                client.sendall((payload + "\n").encode())
            except BrokenPipeError:
                # The coordinator may reject a request before reading all of it;
                # report its error instead of the local write failure.
                pass
            response = b""
            while not response.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
                if len(response) > cfg.MAX_MESSAGE_BYTES:
                    raise ValueError("response exceeds 16 MiB")
        if not response:
            raise OSError("coordinator closed the connection without a response")
        print(response.decode().strip())
        return 0 if json.loads(response).get("ok") is True else 1
    except (OSError, ValueError, UnicodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
