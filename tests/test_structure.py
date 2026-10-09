import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bridge_client
import coordinator as c
import linux_control as lc
import mac_agent as m
import structure as s
import structure_records as sr


def session():
    return {
        "lastCollected": 1,
        "spaces": [{"uuid": "S1", "name": "Space"}, {"uuid": "S2", "name": "Work"}],
        "folders": [{"id": "F1", "name": "F1", "workspaceId": "S1"}],
        "splitViewData": [],
        "groups": [],
        "tabs": [
            {"zenSyncId": "a", "zenWorkspace": "S1", "pinned": True, "entries": [{"url": "https://a.invalid/"}], "index": 1},
            {"zenSyncId": "b", "zenWorkspace": "S1", "groupId": "F1", "pinned": True, "entries": [{"url": "https://b.invalid/"}], "index": 1},
            {"zenSyncId": "c", "zenWorkspace": "S2", "entries": [{"url": "https://c.invalid/"}], "index": 1, "selected": True},
        ],
    }


class FingerprintTests(unittest.TestCase):
    def changed(self, edit):
        before = session()
        after = copy.deepcopy(before)
        edit(after)
        return s.fingerprint(before) != s.fingerprint(after)

    def test_structure_placement_order_and_navigation_change_it(self):
        self.assertTrue(self.changed(lambda d: d["tabs"][2].update(zenWorkspace="S1")))
        self.assertTrue(self.changed(lambda d: d["tabs"][0].update(pinned=False)))
        self.assertTrue(self.changed(lambda d: d["tabs"].reverse()))
        self.assertTrue(self.changed(lambda d: d["folders"][0].update(name="Renamed")))
        self.assertTrue(self.changed(lambda d: d["tabs"][2].update(
            entries=[{"url": "https://c.invalid/"}, {"url": "https://c2.invalid/"}], index=2)))

    def test_selection_scroll_and_collection_time_do_not(self):
        self.assertFalse(self.changed(lambda d: d.update(lastCollected=2)))
        self.assertFalse(self.changed(lambda d: d["tabs"][2].update(selected=False, scroll={"y": 9}, image="x")))


class PlanTests(unittest.TestCase):
    base = {"S1": ["space", "1"], "F1": ["folder", "1"], "a": ["tab", "1"], "SPL": ["split", "1"],
            "K": ["container", "1"], "gone-tab": ["tab", "1"]}

    def test_old_baseline_without_digests_only_establishes(self):
        self.assertEqual(s.plan(None, {"S1": ["space", "2"]}, set(), same_browser=True), ([], []))

    def test_modified_and_created_records_travel(self):
        current = {**self.base, "S1": ["space", "2"], "NEW": ["folder", "1"]}
        self.assertEqual(s.plan(self.base, current, set(current), same_browser=True)[0], ["NEW", "S1"])

    def test_after_restart_only_creations_travel_and_nothing_is_deleted(self):
        current = {"S1": ["space", "2"], "NEW": ["folder", "1"]}
        self.assertEqual(s.plan(self.base, current, set(current), same_browser=False), (["NEW"], []))

    def test_only_spaces_folders_and_splits_are_deleted_as_structure(self):
        current = {"S1": ["space", "1"], "a": ["tab", "1"]}
        changed, deleted = s.plan(self.base, current, set(current), same_browser=True)
        self.assertEqual(deleted, ["F1", "SPL"])

    def test_something_that_still_exists_is_not_deleted(self):
        current = {"S1": ["space", "1"], "a": ["tab", "1"]}
        # A live folder whose provider is not loaded is not projected but exists.
        self.assertEqual(s.plan(self.base, current, {"F1"}, same_browser=True)[1], ["SPL"])

    def test_skipped_ids_neither_travel_nor_delete(self):
        current = {"S1": ["space", "2"], "a": ["tab", "2"]}
        self.assertEqual(s.plan(self.base, current, set(), same_browser=True, skip_ids={"S1", "F1", "a"}), ([], ["SPL"]))


class BaselineTests(unittest.TestCase):
    def test_receiver_stores_its_own_digests_of_what_it_applied(self):
        base = {"S1": ["space", "1"], "F1": ["folder", "1"], "x": ["tab", "1"], "P": ["folder", "p"], "Z": ["space", "z"]}
        report = {
            "before": {"S1": ["space", "1"], "F1": ["folder", "1"], "x": ["tab", "1"],
                       "P": ["folder", "edited here"], "Q": ["folder", "created here"], "Z": ["space", "z"]},
            "records": {"S1": ["space", "as applied"], "NEW": ["tab", "n"], "P": ["folder", "edited here"],
                        "Q": ["folder", "created here"], "Z": ["space", "reordered by the apply"]},
            "deleted": ["F1"],
        }
        sent = [{"id": "S1"}, {"cleartext": {"id": "NEW"}}]
        self.assertEqual(s.received(base, report, sent, ["x"]), {
            "S1": ["space", "as applied"],
            "NEW": ["tab", "n"],
            "P": ["folder", "p"],  # local edit not sent yet: stays pending
            "Z": ["space", "reordered by the apply"],  # consequence of the apply: absorbed
        })

    def test_receiver_without_baseline_takes_the_whole_state(self):
        report = {"records": {"S1": ["space", "1"]}}
        self.assertEqual(s.received(None, report, [], []), {"S1": ["space", "1"]})

    def test_nothing_applied_keeps_the_baseline(self):
        self.assertEqual(s.received({"S1": ["space", "1"]}, {"records": None}, [], []), {"S1": ["space", "1"]})

    def test_records_are_marked_create_or_update(self):
        marked = s.outgoing([{"id": "S1"}, {"id": "NEW"}], {"S1": ["space", "1"]})
        self.assertEqual([r["op"] for r in marked], ["update", "create"])

    def test_invalid_digests_are_rejected(self):
        self.assertIsNone(s.valid_digests({"a": ["tab"]}))
        self.assertIsNone(s.valid_digests([]))

    def test_summary_names_ids_only(self):
        line = s.summary({"ok": True, "requested": 3, "kept": ["F1"], "records": {"x": ["tab", "d"]}})
        self.assertEqual(line, "ok=True requested=3 kept=F1")


class PayloadTests(unittest.TestCase):
    def write(self, payload):
        path = Path(self.tmp.name) / "p.json"
        path.write_text(json.dumps(payload))
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_structure_records_must_say_create_or_update(self):
        record = {"id": "S1", "cleartext": {"id": "S1", "kind": "space", "data": {}}}
        with self.assertRaisesRegex(ValueError, "structure record"):
            sr.load_payload(self.write({"records": [record]}))
        sr.load_payload(self.write({"records": [dict(record, op="update")]}))

    def test_opened_tabs_must_be_tab_records(self):
        record = {"id": "S1", "cleartext": {"id": "S1", "kind": "space", "data": {}}}
        with self.assertRaisesRegex(ValueError, "tab record"):
            sr.load_payload(self.write({"tabRecords": [record]}))

    def test_deleted_ids_are_strings(self):
        with self.assertRaisesRegex(ValueError, "deletedIds"):
            sr.load_payload(self.write({"deletedIds": [1]}))


class CoordinatorTests(unittest.TestCase):
    def coordinator(self):
        with patch.object(c.Coordinator, "load_state"):
            coordinator = c.Coordinator()
        coordinator.save_state = lambda: None
        return coordinator

    def test_mac_wins_conflicts_and_linux_returns_the_rest(self):
        coordinator = self.coordinator()
        linux = c.Outgoing()
        linux.tab_ids = {"a", "b"}
        linux.records = {"S1": ["space", "linux"], "a": ["tab", "1"]}
        linux.structure_records = [{"id": "F9", "op": "create"}]
        report = {"ok": True, "records": {"S1": ["space", "mac"], "a": ["tab", "1"], "n": ["tab", "n"]}, "deleted": []}
        mac_structure = [{"id": "S1", "op": "update", "cleartext": {"id": "S1", "kind": "space", "data": {}}}]
        with patch.object(coordinator, "linux_outgoing", AsyncMock(return_value=linux)) as outgoing, \
                patch.object(coordinator, "apply_mac_changes", AsyncMock(return_value=report)), \
                patch.object(c.linux_control, "stored_fingerprint", return_value="fp"):
            response = asyncio.run(coordinator.on_mac_idle(["b"], [{"id": "n"}], False, mac_structure, ["F2"]))
        self.assertEqual(outgoing.call_args.kwargs["skip_ids"], {"S1", "F2"})
        self.assertEqual(response["structureRecords"], [{"id": "F9", "op": "create"}])
        self.assertEqual(coordinator.linux_ack_tab_ids, {"a", "n"})
        self.assertEqual(coordinator.linux_ack_records, {"S1": ["space", "mac"], "a": ["tab", "1"], "n": ["tab", "n"]})
        self.assertEqual(coordinator.linux_ack_fingerprint, "fp")

    def test_one_lease_covers_the_whole_handoff(self):
        coordinator = self.coordinator()
        coordinator.mac_handoff_id = "h"
        coordinator.owner = "mac"
        coordinator.linux_idle = False
        calls = []

        async def run_tool(*args):
            calls.append(args[1] if len(args) > 1 else args[0])
            if args[1:2] == ("state",):
                return 0, {"ok": True, "records": {"S1": ["space", "2"]}, "presentIds": ["S1"]}, ""
            if args[1:2] == ("export",):
                return 0, {"ok": True, "records": [{"id": "S1", "cleartext": {"id": "S1", "kind": "space", "data": {}}}]}, ""
            return 0, {"ok": True, "records": {"S1": ["space", "2"]}, "deleted": []}, ""

        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.json"
            baseline.write_text(json.dumps({"identity": "id", "tabIds": ["a"], "records": {"S1": ["space", "1"]}, "fingerprint": "old"}))
            with patch.object(c, "LINUX_ACTIVE_BASELINE", baseline), \
                    patch.object(c, "BASE", Path(directory)), \
                    patch.object(c.linux_control, "twilight_identity", return_value="id"), \
                    patch.object(c.linux_control, "stored_tab_ids", return_value={"a"}), \
                    patch.object(c.linux_control, "stored_fingerprint", return_value="new"), \
                    patch.object(c.linux_control, "acquire", return_value=True) as acquire, \
                    patch.object(c.linux_control, "release", return_value=True) as release, \
                    patch.object(coordinator, "run_tool", side_effect=run_tool):
                response = asyncio.run(coordinator.handle_event(
                    "mac-idle", ["x"], [], False, "h",
                    [{"id": "F1", "op": "update", "cleartext": {"id": "F1", "kind": "folder", "data": {}}}], [],
                ))
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["structureRecords"][0]["op"], "update")
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(release.call_count, 1)

    def test_unclean_release_fails_the_handoff(self):
        coordinator = self.coordinator()
        coordinator.control_held = True
        with patch.object(c.linux_control, "release", return_value=False):
            response = asyncio.run(coordinator.handoff_scope(AsyncMock(return_value={"ok": True})))
        self.assertFalse(response["ok"])


class ControlTests(unittest.TestCase):
    def test_linux_without_bridge_refuses_instead_of_restarting(self):
        error = bridge_client.BridgeError("bridge_unavailable", "missing")
        with patch.object(lc, "marionette_ready", return_value=False), \
                patch.object(lc.LEASE, "acquire", side_effect=error), \
                patch.object(lc, "start_twilight") as start, patch("builtins.print"):
            self.assertFalse(lc.acquire())
        start.assert_not_called()

    def test_linux_release_turns_off_even_an_inherited_listener(self):
        with patch.object(lc, "marionette_ready", return_value=True), \
                patch.object(type(lc.LEASE), "held", new=property(lambda self: False)), \
                patch.object(lc.LEASE, "acquire") as acquire, \
                patch.object(lc.LEASE, "release", return_value=True) as release, patch("builtins.print"):
            self.assertTrue(lc.release())
        acquire.assert_called_once()
        self.assertTrue(release.call_args.kwargs["force_off"])


class MacFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "baseline.json"
        self.path.write_text(json.dumps({"identity": "same", "tabIds": ["a"], "handoffId": "h",
                                         "records": {"S1": ["space", "1"]}, "fingerprint": "old"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_moving_a_tab_without_opening_or_closing_still_hands_off(self):
        events = []

        def remote(event, **kwargs):
            events.append((event, kwargs))
            return {"ok": True, "linux_idle": False, "owner": "linux" if event == "linux-synced" else "mac"}

        moved = [{"id": "a", "op": "update", "cleartext": {"id": "a", "kind": "tab", "data": {}}}]
        with patch.object(m, "ACTIVE_BASELINE", self.path), patch.object(m, "remote", side_effect=remote), \
                patch.object(m, "twilight_identity", return_value="same"), patch.object(m, "twilight_profile"), \
                patch.object(m, "tab_ids", return_value={"a"}), patch.object(m, "fingerprint", return_value="new"), \
                patch.object(m, "ensure_control", return_value=True), patch.object(m, "release_control", return_value=True), \
                patch.object(m, "live_tab_ids", return_value={"a"}), patch.object(m, "export_tab_records", return_value=[]), \
                patch.object(m, "structure_changes", return_value=(moved, ["F1"], {"S1": ["space", "1"], "a": ["tab", "2"]})), \
                patch.object(m, "save_active_baseline", return_value=True) as save:
            self.assertTrue(m.leave_mac_for_linux())
        sent = next(kwargs for event, kwargs in events if event == "mac-idle")
        self.assertEqual(sent["structure_records"], moved)
        self.assertEqual(sent["deleted_structure_ids"], ["F1"])
        self.assertEqual(save.call_args.kwargs["records"], {"S1": ["space", "1"], "a": ["tab", "2"]})

    def test_unsent_mac_edits_stay_dirty_after_entering(self):
        response = {"ok": True, "owner": "linux", "handoffId": "h2", "openedTabRecords": [], "closedTabIds": [],
                    "structureRecords": [], "deletedStructureIds": []}
        with patch.object(m, "ACTIVE_BASELINE", self.path), \
                patch.object(m, "remote", side_effect=[response, {"ok": True, "owner": "mac"}]), \
                patch.object(m, "twilight_identity", return_value="same"), patch.object(m, "twilight_profile"), \
                patch.object(m, "tab_ids", return_value={"a"}), patch.object(m, "fingerprint", return_value="edited"), \
                patch.object(m, "save_active_baseline", return_value=True) as save:
            self.assertTrue(m.enter_mac())
        self.assertIsNone(save.call_args.kwargs["stored_fingerprint"])
        self.assertEqual(save.call_args.kwargs["records"], {"S1": ["space", "1"]})


if __name__ == "__main__":
    unittest.main()
