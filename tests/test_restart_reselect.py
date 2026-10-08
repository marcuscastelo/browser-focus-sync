import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import lz4.block
import mac_agent as m

def session(selected, tabs):
    data = json.dumps({"windows": [{"selected": selected, "tabs": tabs}]}).encode()
    return b"mozLz40\0" + lz4.block.compress(data)

class ReselectTests(unittest.TestCase):
    def test_reads_selected_tab_from_the_session_written_on_quit(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / "sessionstore.jsonlz4").write_bytes(session(2, [{"zenSyncId": "a"}, {"zenSyncId": "b"}]))
            self.assertEqual(m.selected_tab_id(profile), "b")
    def test_falls_back_to_recovery_and_tolerates_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            self.assertIsNone(m.selected_tab_id(profile))
            (profile / "sessionstore-backups").mkdir()
            (profile / "sessionstore-backups/recovery.jsonlz4").write_bytes(session(1, [{"zenSyncId": "r"}]))
            self.assertEqual(m.selected_tab_id(profile), "r")
    def test_restart_puts_the_user_back_on_their_tab(self):
        # Seen on 08/10: after the restart at 19:24 Zen sat on its empty tab, a blank page.
        names = dict(twilight_profile=Path('/profile'), snapshot_session=Path('/backup'), quit_twilight=True,
                     tab_ids={'a'}, selected_tab_id='a', open_twilight=True, live_tab_ids={'a'},
                     install_bridge=True, request_control=None)
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'RESTART_BLOCKED', Path(tmp) / 'blocked'), \
                patch.object(m, 'reselect_tab', return_value=True) as reselect, patch('builtins.print'):
            mocks = [patch.object(m, n, return_value=v) for n, v in names.items()]
            for p in mocks: p.start()
            try:
                self.assertTrue(m.restart_twilight_with_control())
            finally:
                for p in mocks: p.stop()
        reselect.assert_called_once_with('a')

if __name__ == '__main__': unittest.main()
