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
        print("usage: focusctl.py status|mac-active|base64:PAYLOAD", file=sys.stderr)
        return 2
    payload = sys.argv[1]
    try:
        if payload.startswith("base64:"):
            payload = base64.urlsafe_b64decode(payload[7:]).decode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(120)
            client.connect(str(cfg.SOCKET))
            client.sendall((payload + "\n").encode())
            response = b""
            while not response.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
                if len(response) > 16 * 1024 * 1024:
                    raise ValueError("response exceeds 16 MiB")
        print(response.decode().strip())
        return 0 if json.loads(response).get("ok") is True else 1
    except (OSError, ValueError, UnicodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
