import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
import coordinator as c
import mac_agent as m

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'baseline.json'
        self.path.write_text(json.dumps({'identity':'old','tabIds':['keep','closed'],'structureHash':'hash','handoffId':'handoff'}))
    def tearDown(self): self.tmp.cleanup()
    def linux_changes(self, identity):
        with patch.object(c,'LINUX_ACTIVE_BASELINE',self.path), patch.object(c.Coordinator,'load_state'), patch.object(c.linux_control,'twilight_identity',return_value=identity), patch.object(c.linux_control,'stored_tab_ids',return_value={'keep','new'}), patch.object(c.linux_control,'stored_structure_hash',return_value='hash'):
            return asyncio.run(c.Coordinator().linux_changes())
    def test_restart_keeps_additions_but_does_not_infer_deletions(self):
        self.assertEqual(self.linux_changes('new'),(set(),{'new'},False,{'keep','new'}))
    def test_normal_handoff_propagates_user_closures(self):
        self.assertEqual(self.linux_changes('old'),({'closed'},{'new'},False,{'keep','new'}))
    def test_closed_browser_is_not_empty_session(self):
        self.assertIsNone(self.linux_changes(None)[3])
    def test_mac_baseline_survives_browser_restart(self):
        with patch.object(m,'ACTIVE_BASELINE',self.path),patch.object(m,'twilight_identity',return_value='new'):
            self.assertEqual(m.active_baseline_ids(),{'keep','closed'})
    def test_mac_refocus_does_not_acknowledge_untransferred_edits(self):
        with patch.object(m,'remote',return_value={'ok':True,'owner':'mac'}),patch.object(m,'active_baseline_ids',return_value={'keep'}),patch.object(m,'save_active_baseline') as save:
            self.assertTrue(m.enter_mac());save.assert_not_called()
    def test_mac_enter_retains_pending_edits_from_before_handoff(self):
        response={'ok':True,'owner':'linux','handoffId':'h','openedTabRecords':[],'closedTabIds':[]}
        with patch.object(m,'remote',side_effect=[response,{'ok':True,'owner':'mac'}]),patch.object(m,'active_baseline_ids',return_value={'keep'}),patch.object(m,'twilight_profile'),patch.object(m,'tab_ids',return_value={'keep','unsent'}),patch.object(m,'save_active_baseline',return_value=True) as save:
            self.assertTrue(m.enter_mac());self.assertEqual(save.call_args.kwargs['tab_ids_override'],{'keep'})
    def test_mac_restart_does_not_send_deletions(self):
        calls=[]
        def remote(event,**kwargs):
            calls.append((event,kwargs))
            return {'ok':True,'linux_idle':False,'owner':'linux' if event=='linux-synced' else 'mac'}
        with patch.object(m,'ACTIVE_BASELINE',self.path),patch.object(m,'remote',side_effect=remote),patch.object(m,'twilight_identity',return_value='new'),patch.object(m,'twilight_profile'),patch.object(m,'tab_ids',return_value={'keep','new'}),patch.object(m,'ensure_control',return_value=True),patch.object(m,'release_control',return_value=True),patch.object(m,'live_tab_ids',return_value={'keep','new'}),patch.object(m,'export_tab_records',return_value=[]),patch.object(m,'save_active_baseline',return_value=True),patch.object(m,'sync_mac') as native:
            self.assertTrue(m.leave_mac_for_linux())
            sent=next(args for event,args in calls if event=='mac-idle')
            self.assertEqual(sent['closed_tab_ids'],set());native.assert_not_called()

if __name__=='__main__': unittest.main()
