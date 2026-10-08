import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import coordinator as c
import linux_control as lc
import mac_agent as m

# The inspector prints valid JSON on stdout and a diagnostic on stderr, as Python does on
# macOS with MallocStackLogging. Mixing the channels used to turn this into "no tabs read".
INSPECT = """import sys
print('{"ok": true, "tabIds": ["a", "b"]}')
print('Python(1) MallocStackLogging: can not turn off malloc stack logging', file=sys.stderr)
"""

class InspectChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.script=Path(self.tmp.name)/'inspect.py'
        self.script.write_text(INSPECT)
    def tearDown(self): self.tmp.cleanup()
    def test_mac_reads_ids_despite_stderr_noise(self):
        with patch.object(m,'PYTHON',sys.executable),patch.object(m,'SYNC',self.script):
            self.assertEqual(m.live_tab_ids(),{'a','b'})
    def test_linux_reads_ids_despite_stderr_noise(self):
        with patch.object(lc,'PYTHON',sys.executable),patch.object(lc,'SYNC',self.script):
            self.assertEqual(lc.live_tab_ids(),{'a','b'})
    def test_coordinator_reads_ids_despite_stderr_noise(self):
        with patch.object(c,'SYNC',sys.executable),patch.object(c,'SYNC_SCRIPT',self.script):
            coordinator=c.Coordinator.__new__(c.Coordinator)
            self.assertEqual(asyncio.run(coordinator.live_tab_ids()),{'a','b'})
    def test_failed_inspection_logs_both_channels(self):
        self.script.write_text("import sys\nprint('partial')\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
        with patch.object(m,'PYTHON',sys.executable),patch.object(m,'SYNC',self.script),patch('builtins.print') as log:
            self.assertIsNone(m.live_tab_ids())
        line=log.call_args.args[0]
        self.assertIn('exit=3',line);self.assertIn("stdout='partial'",line);self.assertIn("stderr='boom'",line)

class RestartVerdictTests(unittest.TestCase):
    def restart(self,**patches):
        defaults=dict(twilight_profile=Path('/profile'),snapshot_session=Path('/backup'),quit_twilight=True,tab_ids={'a','b'},open_twilight=True,live_tab_ids=None,install_bridge=True,request_control=None,restore_session=None)
        defaults.update(patches)
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'RESTART_BLOCKED',Path(tmp)/'restart-blocked'),patch.object(m,'time'),patch('builtins.print') as log:
            mocks=[patch.object(m,name,return_value=value) for name,value in defaults.items()]
            for p in mocks: p.start()
            try:
                result=m.restart_twilight_with_control()
            finally:
                for p in mocks: p.stop()
            blocked=(Path(tmp)/'restart-blocked').exists()
        return result,blocked,[call.args[0] for call in log.call_args_list]
    def test_unreadable_tabs_are_not_reported_as_lost(self):
        result,blocked,lines=self.restart(live_tab_ids=None)
        self.assertFalse(result);self.assertTrue(blocked)
        self.assertIn('Twilight tabs could not be read after restart; restoring snapshot and refusing Sync',lines)
        self.assertFalse([l for l in lines if 'lost tabs' in l])
    def test_failed_open_is_not_reported_as_lost(self):
        result,_,lines=self.restart(open_twilight=False)
        self.assertIn('Twilight did not reopen with control; restoring snapshot and refusing Sync',lines)
        self.assertFalse([l for l in lines if 'lost tabs' in l])
    def test_missing_ids_are_reported_as_lost(self):
        result,_,lines=self.restart(live_tab_ids={'a'})
        self.assertIn('Twilight restart lost tabs: 1 of 2 missing; restoring snapshot and refusing Sync',lines)
    def test_all_ids_present_installs_bridge_and_unblocks(self):
        result,blocked,lines=self.restart(live_tab_ids={'a','b','c'})
        self.assertTrue(result);self.assertFalse(blocked)
        self.assertIn('Temporary Twilight control ready: 3 tabs',lines)

if __name__=='__main__': unittest.main()
