"""Three participants, synthetic records, no browser processes or network."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import coordinator as c


class Participants(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.patches = [patch.object(c, 'STATE_DIR', root),
                        patch.object(c, 'STATE_FILE', root / 'state.json')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.hub = c.ParticipantCoordinator()
        self.hub.read_linux_baseline = lambda: {'tabIds': ['t'], 'records': {'f': ['folder', 'd']}}
        self.hub.linux_twilight_identity = lambda: 'hub-process'
        self.hub.linux_outgoing = AsyncMock(side_effect=self.local)
        self.hub.apply_mac_changes = AsyncMock(return_value={'ok': True})
        self.hub.save_linux_active_baseline = AsyncMock(return_value=True)
        self.local_delta = c.Outgoing()
        self.local_delta.tab_ids = {'t'}
        self.local_delta.records = {'f': ['folder', 'd']}
        for name in ('polaris', 'workstation'):
            result = await self.call(name, 'participant-enroll', tabIds=['t'], records={'f': ['folder', 'd']})
            self.assertTrue(result['ok'])

    async def local(self):
        result = self.local_delta
        self.local_delta = copy.deepcopy(result)
        self.local_delta.closed = set()
        self.local_delta.tab_records = []
        self.local_delta.structure_records = []
        return result

    async def call(self, name, event='participant-exchange', **fields):
        return await self.hub.participant_event(dict(protocol=2, participantId=name, event=event, **fields))

    async def exchange(self, name, request, **fields):
        return await self.call(name, requestId=request, sameBrowser=True, **fields)

    async def ack(self, name, request):
        peer = self.hub.replicas[name]
        return await self.call(name, 'participant-ack', requestId=request,
                               tabIds=peer['tabIds'], baselineRecords=peer['records'])

    async def test_both_remotes_fanout_and_third_update_not_consumed(self):
        for source, target in [('polaris', 'workstation'), ('workstation', 'polaris')]:
            result = await self.exchange(source, source, openedTabRecords=[{'id': source}])
            self.assertTrue(result['ok'])
            self.assertTrue((await self.ack(source, source))['ok'])
            queue = self.hub.replicas[target]['queue']
            self.assertTrue(any({'id': source} in batch['openedTabRecords'] for batch in queue))
        self.assertEqual(self.hub.apply_mac_changes.await_count, 2)

    async def test_hub_to_both_structure_and_closures(self):
        self.local_delta.closed = {'t'}
        self.local_delta.structure_records = [{'id': 'f', 'op': 'update'}]
        response = await self.exchange('polaris', 'one')
        self.assertEqual(response['deliveries'][0]['closedTabIds'], ['t'])
        other = await self.exchange('workstation', 'two')
        self.assertEqual(other['deliveries'][0]['structureRecords'][0]['id'], 'f')

    async def test_offline_retry_restart_and_ack(self):
        first = await self.exchange('polaris', 'one', openedTabRecords=[{'id': 'new'}])
        replay = await self.exchange('polaris', 'one', openedTabRecords=[{'id': 'new'}])
        self.assertEqual(first, replay)
        self.assertEqual(self.hub.apply_mac_changes.await_count, 1)
        restarted = c.ParticipantCoordinator()
        self.assertEqual(restarted.replicas, self.hub.replicas)
        self.assertFalse((await self.exchange('polaris', 'two'))['ok'])
        self.assertFalse((await self.call('polaris', 'participant-ack', requestId='stale'))['ok'])
        self.assertTrue((await self.ack('polaris', 'one'))['ok'])
        self.assertTrue((await self.ack('polaris', 'one'))['ok'])

    async def test_identity_change_and_unobserved_closures_rejected(self):
        self.assertFalse((await self.exchange('polaris', 'one', closedTabIds=['unknown']))['ok'])
        self.assertFalse((await self.call('polaris', requestId='two', sameBrowser=False, closedTabIds=['t']))['ok'])
        self.assertFalse((await self.exchange('polaris', 'three', deletedStructureIds=['unknown']))['ok'])
        self.hub.apply_mac_changes.assert_not_awaited()

    async def test_failure_does_not_ack_and_blocks_restart(self):
        self.hub.apply_mac_changes.return_value = None
        self.assertFalse((await self.exchange('polaris', 'one', closedTabIds=['t']))['ok'])
        self.assertIsNone(self.hub.replicas['polaris']['last'])
        self.hub.save_linux_active_baseline.assert_not_awaited()
        restarted = c.ParticipantCoordinator()
        self.assertTrue(restarted.intent_file.exists())
        self.assertFalse((await restarted.participant_event(dict(protocol=2, participantId='polaris', event='participant-exchange', requestId='one')))['ok'])

    async def test_conflict_later_serialized_record_wins(self):
        self.local_delta.structure_records = [{'id': 'f', 'value': 'hub'}]
        result = await self.exchange('polaris', 'one', structureRecords=[{'id': 'f', 'value': 'peer'}])
        self.assertFalse(any(batch['structureRecords'] for batch in result['deliveries']))
        queued = self.hub.replicas['workstation']['queue']
        self.assertEqual(queued[-1]['structureRecords'], [{'id': 'f', 'value': 'peer'}])

    async def test_baseline_commit_failure_retains_intent(self):
        self.hub.save_linux_active_baseline.return_value = False
        self.assertFalse((await self.exchange('polaris', 'one'))['ok'])
        self.assertTrue(self.hub.intent_file.exists())
        self.assertIsNone(self.hub.replicas['polaris']['last'])


    async def test_reenroll_requires_exact_alignment_and_archives_prior_queue(self):
        prior = copy.deepcopy(self.hub.replicas['workstation'])
        self.hub.replicas['workstation']['queue'].append({'closedTabIds': ['old']})
        wrong = await self.call('workstation', 'participant-reenroll', tabIds=['different'], records={'f': ['folder', 'd']})
        self.assertFalse(wrong['ok'])
        self.assertTrue(self.hub.replicas['workstation']['queue'])
        right = await self.call('workstation', 'participant-reenroll', tabIds=['t'], records={'f': ['folder', 'd']})
        self.assertTrue(right['ok'])
        archives = list(self.hub.replica_file.parent.glob('workstation-before-reenroll-*.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads(archives[0].read_text())['queue'], [{'closedTabIds': ['old']}])
        self.assertEqual(self.hub.replicas['workstation']['queue'], [])



    async def test_reenroll_persistence_failure_keeps_old_peer_in_memory(self):
        self.hub.replicas['workstation']['queue'].append({'closedTabIds': ['old']})
        before = copy.deepcopy(self.hub.replicas['workstation'])
        with patch.object(self.hub, 'persist_replicas', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.call('workstation', 'participant-reenroll', tabIds=['t'], records={'f': ['folder', 'd']})
        self.assertEqual(self.hub.replicas['workstation'], before)



class ParticipantAgent(unittest.TestCase):
    def setUp(self):
        import mac_agent as m
        self.m = m
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        baseline = self.root / 'active-baseline.json'
        baseline.write_text(json.dumps({'identity': 'old', 'tabIds': ['t'], 'records': {'f': ['folder', 'd']}}))
        def snapshot_changes(baseline, *, same_browser, skip_ids):
            return [], [], {'f': ['folder', 'd']}
        replacements = dict(BASE=self.root, ACTIVE_BASELINE=baseline,
                            ensure_control=lambda: True, release_control=lambda: True,
                            twilight_identity=lambda: 'new', twilight_profile=lambda: self.root,
                            live_tab_ids=lambda: {'added'}, export_tab_records=lambda ids: [{'id': x} for x in ids],
                            structure_changes=snapshot_changes)
        for name, value in replacements.items():
            item = patch.object(m, name, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(m.cfg, 'PARTICIPANT_ID', 'workstation')
        item.start()
        self.addCleanup(item.stop)

    def test_offline_preserves_exact_request_and_identity_suppresses_close(self):
        with patch.object(self.m, 'participant_rpc', return_value=None):
            self.assertFalse(self.m.participant_cycle())
        pending = json.loads((self.root / 'participant-pending.json').read_text())
        self.assertEqual(pending['message']['closedTabIds'], [])
        self.assertFalse(pending['message']['sameBrowser'])
        with patch.object(self.m, 'participant_rpc', return_value=None) as rpc:
            self.assertFalse(self.m.participant_cycle())
            self.assertEqual(rpc.call_args.args[0], pending['message'])

    def test_failed_apply_never_saves_baseline_or_sends_ack(self):
        def reply(message):
            return {'ok': True, 'requestId': message['requestId'], 'deliveries': [{'closedTabIds': ['added']}]}
        with patch.object(self.m, 'participant_rpc', side_effect=reply) as rpc, \
             patch.object(self.m, 'apply_changes', return_value=None), \
             patch.object(self.m, 'save_active_baseline') as save:
            self.assertFalse(self.m.participant_cycle())
            save.assert_not_called()
            self.assertEqual(rpc.call_count, 1)
        self.assertTrue((self.root / 'participant-receive-intent.json').exists())
        with self.assertRaises(RuntimeError):
            self.m.participant_cycle()

    def test_lost_ack_retries_ack_without_reapplying(self):
        def reply(message):
            if message['event'] == 'participant-ack':
                return None
            return {'ok': True, 'requestId': message['requestId'], 'deliveries': []}
        with patch.object(self.m, 'participant_rpc', side_effect=reply), \
             patch.object(self.m, 'save_active_baseline', return_value=True):
            self.assertFalse(self.m.participant_cycle())
        with patch.object(self.m, 'participant_rpc', return_value={'ok': True}) as rpc, \
             patch.object(self.m, 'apply_changes') as apply:
            self.assertTrue(self.m.participant_cycle())
            self.assertEqual(rpc.call_args.args[0]['event'], 'participant-ack')
            apply.assert_not_called()
        self.assertFalse((self.root / 'participant-pending.json').exists())

    def test_initialize_refuses_existing_baseline_without_browser_access(self):
        with patch.object(self.m.sys, 'argv', ['mac_agent.py', 'initialize', '/unused']), \
             patch.object(self.m, 'ensure_control') as control:
            with self.assertRaisesRegex(ValueError, 'existing baseline'):
                self.m.participant_initialize()
            control.assert_not_called()

    def test_pending_restart_blocks_before_rpc_or_apply(self):
        with patch.object(self.m, 'participant_rpc', return_value=None):
            self.assertFalse(self.m.participant_cycle())
        with patch.object(self.m, 'twilight_identity', return_value='restarted'), \
             patch.object(self.m, 'participant_rpc') as rpc, \
             patch.object(self.m, 'apply_changes') as apply:
            self.assertFalse(self.m.participant_cycle())
            rpc.assert_not_called()
            apply.assert_not_called()

    def test_offline_later_edit_is_retained_for_next_export(self):
        with patch.object(self.m, 'participant_rpc', return_value=None):
            self.assertFalse(self.m.participant_cycle())
        current = {'f': ['folder', 'later-local']}
        def reply(message):
            if message['event'] == 'participant-ack':
                return {'ok': True}
            return {'ok': True, 'requestId': message['requestId'], 'deliveries': [
                {'structureRecords': [{'id': 'f', 'value': 'older-remote'}]}]}
        report = {'ok': True, 'before': current, 'records': current}
        with patch.object(self.m, 'participant_rpc', side_effect=reply), \
             patch.object(self.m, 'structure_changes', return_value=([], [], current)), \
             patch.object(self.m, 'apply_changes', return_value=report) as apply, \
             patch.object(self.m, 'save_active_baseline', return_value=True) as save:
            self.assertTrue(self.m.participant_cycle())
            self.assertEqual(apply.call_args.args[2], [])
            self.assertEqual(save.call_args.kwargs['records']['f'], ['folder', 'd'])

    def test_later_edit_survives_remote_deletion_and_is_recreated_next_cycle(self):
        with patch.object(self.m, 'participant_rpc', return_value=None):
            self.assertFalse(self.m.participant_cycle())
        current = {'f': ['folder', 'later-local']}
        def reply(message):
            if message['event'] == 'participant-ack':
                return {'ok': True}
            return {'ok': True, 'requestId': message['requestId'], 'deliveries': [
                {'deletedStructureIds': ['f']}]}
        report = {'ok': True, 'before': current, 'records': current}
        with patch.object(self.m, 'participant_rpc', side_effect=reply), \
             patch.object(self.m, 'structure_changes', return_value=([], [], current)), \
             patch.object(self.m, 'apply_changes', return_value=report) as apply, \
             patch.object(self.m, 'save_active_baseline', return_value=True) as save:
            self.assertTrue(self.m.participant_cycle())
            self.assertEqual(apply.call_args.args[3], [])
            self.assertNotIn('f', save.call_args.kwargs['records'])

    def test_rpc_timeout_is_transient_and_preserves_pending(self):
        import subprocess
        with patch.object(self.m, 'run', side_effect=subprocess.TimeoutExpired(['ssh'], 120)):
            self.assertFalse(self.m.participant_cycle())
        self.assertTrue((self.root / 'participant-pending.json').is_file())
        self.assertFalse((self.root / 'participant-receive-intent.json').exists())

    def test_reenroll_refuses_stale_identity_ids_or_digests_without_rpc(self):
        baseline_file = self.root / 'active-baseline.json'
        for baseline in (
            {'identity': 'old', 'tabIds': ['added'], 'records': {'f': ['folder', 'd']}},
            {'identity': 'new', 'tabIds': ['t'], 'records': {'f': ['folder', 'd']}},
            {'identity': 'new', 'tabIds': ['added'], 'records': {'f': ['folder', 'old']}},
        ):
            baseline_file.write_text(json.dumps(baseline))
            with patch.object(self.m, 'participant_rpc') as rpc:
                self.assertFalse(self.m.participant_cycle(enroll=True, replacing=True))
                rpc.assert_not_called()
        baseline_file.write_text(json.dumps({'identity': 'new', 'tabIds': ['added'], 'records': {'f': ['folder', 'd']}}))
        with patch.object(self.m, 'participant_rpc', return_value={'ok': True}) as rpc:
            self.assertTrue(self.m.participant_cycle(enroll=True, replacing=True))
            self.assertEqual(rpc.call_args.args[0]['event'], 'participant-reenroll')

    def test_reenroll_refuses_pending_transfer_without_exchange(self):
        (self.root / 'participant-pending.json').write_text('{}')
        with patch.object(self.m, 'participant_rpc') as rpc:
            self.assertFalse(self.m.participant_cycle(enroll=True, replacing=True))
            rpc.assert_not_called()
