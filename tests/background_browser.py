"""Mac regression against a disposable headless browser, never the user's screen.

Run with the focus-sync venv: background_browser.py RUNTIME EXECUTABLE.
Instrument the browser's native focus entry points: ordinary sessions must hit
them, both background clients must not. Only a fresh profile and loopback port.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import bridge_client as bc
from marionette_driver.marionette import Marionette

with tempfile.TemporaryDirectory(prefix="bfs-background-") as directory:
    root = Path(directory)
    os.environ["HOME"] = directory
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    profile = root / "profile"
    profile.mkdir()
    (profile / "user.js").write_text(
        f'user_pref("marionette.port", {port});\n'
        'user_pref("remote.prefs.recommended", false);\n'
        'user_pref("zen.welcome-screen.seen", true);\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n')
    with (root / "browser.log").open("w") as log:
        proc = subprocess.Popen([sys.argv[2], "--headless", "--no-remote", "--profile",
                                 str(profile), "--marionette", "--remote-allow-system-access"],
                                stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while not bc.listening(port):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Disposable browser did not start")
                time.sleep(.1)
            # Install spies with an ordinary session. This baseline also verifies
            # that the browser permits replacing these methods, rather than
            # silently passing a test with inert spies.
            c = bc.Marionette(port)
            c.command("WebDriver:NewSession")
            c.command("Marionette:SetContext", {"value": "chrome"})
            c.execute('''
                const w = Services.wm.getMostRecentBrowserWindow();
                w.__focusCalls = 0;
                for (const object of [w, w.gBrowser.selectedBrowser]) {
                  const original = object.focus.bind(object);
                  object.focus = (...args) => {w.__focusCalls++; return original(...args);};
                }
            ''')
            c.command("WebDriver:DeleteSession")
            c.close()
            read = "const w=Services.wm.getMostRecentBrowserWindow(); const n=w.__focusCalls; w.__focusCalls=0; return n;"
            c = bc.Marionette(port)
            c.command("WebDriver:NewSession")
            c.command("Marionette:SetContext", {"value": "chrome"})
            before = c.execute(read)
            c.command("WebDriver:DeleteSession")
            c.close()
            assert before >= 2, before
            after = []
            for _ in range(3):
                with bc.chrome_session(port) as c:
                    after.append(c.execute(read))
                    assert c.execute("return window === Services.wm.getMostRecentBrowserWindow()")
            for _ in range(3):
                c = Marionette(port=port)
                try:
                    bc.start_chrome_session(c)
                    after.append(c.execute_script(read))
                    assert c.execute_script("return window === Services.wm.getMostRecentBrowserWindow()")
                finally:
                    c.delete_session()
            assert after == [0] * 6, after
            # Exceptions still delete the session; the next call must succeed.
            try:
                with bc.chrome_session(port) as c:
                    raise ValueError("fixture")
            except ValueError:
                pass
            with bc.chrome_session(port) as c:
                assert c.execute(read) == 0
                c.execute("window.focus();")
                assert c.execute(read) >= 1
            print(json.dumps({"ordinary_focus_calls": before, "background_focus_calls": after,
                              "chrome_context": True, "cleanup_after_exception": True,
                              "explicit_focus_callable": True}))
        finally:
            proc.terminate()
            proc.wait(timeout=15)
