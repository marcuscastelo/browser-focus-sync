"""Manual Linux proof: bridge timers survive Marionette session destruction."""
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
import linux_control as control

root = Path(tempfile.mkdtemp(prefix='bridge-lifetime-', dir=os.environ.get('BFS_LAB_ROOT')))
root.chmod(0o700)
profile = root / 'profile'
profile.mkdir()
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
(profile / 'user.js').write_text(
    f'user_pref("marionette.port", {port});\n'
    'user_pref("remote.prefs.recommended", false);\n'
    'user_pref("zen.welcome-screen.seen", true);\n'
    'user_pref("browser.shell.checkDefaultBrowser", false);\n')
control.CONTROL_REQUEST = root / 'linux-control-request'
control.BRIDGE_STATUS = root / 'linux-control-bridge.json'
control.CONTROL_REQUEST.write_text('on')
executable = '/opt/zen-twilight-bin/zen-bin'
with (root / 'browser.log').open('w') as log:
    process = subprocess.Popen(
        [executable, '--headless', '--no-remote', '--profile', str(profile),
         '--marionette', '--remote-allow-system-access'], stdout=log, stderr=log,
        env={**os.environ, 'MOZ_REMOTE_ALLOW_SYSTEM_ACCESS': '1'})
    try:
        deadline = time.monotonic() + 30
        while not bc.listening(port):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('disposable browser did not start: ' + str(root))
            time.sleep(.1)
        started = (Path('/proc') / str(process.pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        identity = f'{process.pid}:{started}'
        # Bind this isolated transport to its exact child, never another test or real browser.
        bc.browser_identity = lambda executable_path, system: identity
        control.twilight_identity = lambda: identity
        control.marionette_ready = lambda: bc.listening(port)
        assert control.install_bridge(port=port), 'bridge injection failed'
        lease = bc.Lease(port=port, directory=root, executable_path=executable,
                         prefix='linux', system='linux', label='disposable bridge proof')
        lease.acquire()
        assert lease.release(force_off=True), json.loads(control.BRIDGE_STATUS.read_text())
        for index in range(3):
            lease.acquire()
            with bc.chrome_session(port) as client:
                assert client.execute('return Services.prefs.getBoolPref("remote.prefs.recommended",false)') is False
            assert lease.release(), json.loads(control.BRIDGE_STATUS.read_text())
            status = json.loads(control.BRIDGE_STATUS.read_text())
            assert status['running'] is False and status['webdriverActive'] is False, status
            print('PASS control cycle', index + 1, 'listener off; webdriver false', flush=True)
        print('Artifact', root)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
