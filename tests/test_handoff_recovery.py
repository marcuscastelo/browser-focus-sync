import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from subprocess import CompletedProcess
import coordinator as c
import linux_control as lc
import mac_agent as m

class RecoveryTests(unittest.TestCase):
    def test_remote_sends_large_handoff_over_stdin(self):
        records=[{'url':'https://example.com/' + str(i)} for i in range(6000)]
        with patch.object(m,'run',return_value=CompletedProcess([],0,'{"ok":true}')) as run:
            self.assertEqual(m.remote('mac-idle',opened_tab_records=records),{'ok':True})
            self.assertEqual(run.call_args.args[0][-1],'-')
            self.assertGreater(len(run.call_args.kwargs['input_data']),100000)
    def test_coordinator_accepts_handoff_larger_than_asyncio_default(self):
        records=[{'id':str(i),'cleartext':{'kind':'tab','data':{'url':'https://example.com/'+'x'*200}}} for i in range(600)]
        line=json.dumps({'event':'mac-idle','openedTabRecords':records,'handoffId':'h'})+'\n'
        self.assertGreater(len(line),2**16)
        async def exchange(path):
            coordinator=c.Coordinator.__new__(c.Coordinator)
            coordinator.handle_event=AsyncMock(return_value={'ok':True})
            server=await c.start_server(coordinator,path)
            async with server:
                reader,writer=await asyncio.open_unix_connection(str(path))
                writer.write(line.encode());await writer.drain()
                response=json.loads(await reader.readline())
                writer.close();await writer.wait_closed()
            return response,coordinator.handle_event.call_args.args
        response,args=asyncio.run(exchange(Path(self.tmp.name)/'s.sock'))
        self.assertEqual(response,{'ok':True});self.assertEqual(len(args[2]),600)
    def test_mac_reports_unusable_marionette_listener(self):
        with patch.object(m,'marionette_ready',return_value=True),patch.object(m,'request_control'),patch.object(m,'bridge_ready',return_value=False),patch.object(m,'install_bridge',return_value=False),patch('builtins.print') as log:
            self.assertFalse(m.ensure_control());self.assertIn('could not be installed',log.call_args.args[0])
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
    def test_linux_bootstrap_ignores_bridge_of_exiting_browser(self):
        with patch.object(lc,'marionette_ready',side_effect=[False,True]),patch.object(lc,'time'),patch.object(lc,'bridge_ready',return_value=True),patch.object(lc,'request_control'),patch.object(lc,'install_bridge',return_value=True) as install,patch.object(lc,'release',return_value=True):
            self.assertTrue(lc.bootstrap());install.assert_called_once()
    def test_linux_bootstrap_without_listener_reports_existing_bridge(self):
        with patch.object(lc,'marionette_ready',return_value=False),patch.object(lc,'time'),patch.object(lc,'bridge_ready',return_value=False),patch.object(lc,'install_bridge') as install:
            self.assertFalse(lc.bootstrap());install.assert_not_called()

if __name__=='__main__': unittest.main()
