import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import mac_agent as m


def config(allow_restart):
    def get(section, key, default=None):
        if (section, key) == ("mac", "allow_restart"):
            return allow_restart
        return default
    return get


class Agent(ExitStack):
    """mac_agent with its data files in a temporary directory and no real browser."""

    def __init__(self, **values):
        super().__init__()
        self.values = {"twilight_identity": "100:Thu Oct  8 19:24:38 2026", "twilight_running": True,
                       "marionette_ready": False, "bridge_ready": False, "install_bridge": True,
                       "release_control": True, "restart_twilight_with_control": True,
                       "request_control": None, "take_lease": True, **values}

    def __enter__(self):
        super().__enter__()
        tmp = Path(self.enter_context(tempfile.TemporaryDirectory()))
        for name in ("RESTART_BLOCKED", "REOPEN_REQUEST", "REOPEN_RESULT", "BRIDGE_NOTICE"):
            self.enter_context(patch.object(m, name, tmp / name.lower()))
        self.enter_context(patch.object(m, "ADOPTION_FAILED", set()))
        self.enter_context(patch("builtins.print"))
        self.mocks = {n: self.enter_context(patch.object(m, n, return_value=v)) for n, v in self.values.items()}
        self.run = self.enter_context(patch.object(m, "run"))
        return self

    def notices(self):
        return [c for c in self.run.call_args_list if c.args[0][0] == "/usr/bin/osascript"]


class NoAutomaticRestartTests(unittest.TestCase):
    def test_missing_bridge_never_restarts_and_notifies_once_per_browser_process(self):
        with Agent() as agent, patch.object(m.cfg, "get", side_effect=config(False)):
            self.assertFalse(m.ensure_control())
            self.assertFalse(m.ensure_control())
            agent.mocks["restart_twilight_with_control"].assert_not_called()
            self.assertEqual(len(agent.notices()), 1)
            agent.mocks["twilight_identity"].return_value = "200:Fri Oct  9 09:00:00 2026"
            self.assertFalse(m.ensure_control())
            self.assertEqual(len(agent.notices()), 2)

    def test_allow_restart_still_opts_in(self):
        with Agent() as agent, patch.object(m.cfg, "get", side_effect=config(True)):
            self.assertTrue(m.ensure_control())
            agent.mocks["restart_twilight_with_control"].assert_called_once()
            self.assertEqual(agent.notices(), [])


class AdoptStartupListenerTests(unittest.TestCase):
    def test_installs_the_bridge_and_turns_marionette_off_in_place(self):
        # Twilight restarted itself (an update) with the inherited MOZ_MARIONETTE.
        with Agent(marionette_ready=True) as agent:
            m.adopt_startup_listener()
            agent.mocks["request_control"].assert_called_once_with(True)
            agent.mocks["install_bridge"].assert_called_once()
            agent.mocks["release_control"].assert_called_once()
            agent.mocks["restart_twilight_with_control"].assert_not_called()

    def test_leaves_alone_a_bridge_in_use_or_a_browser_without_marionette(self):
        for values in ({"marionette_ready": True, "bridge_ready": True}, {"marionette_ready": False}):
            with Agent(**values) as agent:
                m.adopt_startup_listener()
                agent.mocks["install_bridge"].assert_not_called()
        with Agent(marionette_ready=True) as agent, patch.object(m.LEASE, "_lock_fd", 3):
            m.adopt_startup_listener()
            agent.mocks["install_bridge"].assert_not_called()

    def test_failure_is_reported_once_and_not_retried_every_loop(self):
        with Agent(marionette_ready=True, install_bridge=False) as agent:
            m.adopt_startup_listener()
            m.adopt_startup_listener()
            agent.mocks["install_bridge"].assert_called_once()
            self.assertEqual(len(agent.notices()), 1)
            agent.mocks["restart_twilight_with_control"].assert_not_called()


class ReopenOnRequestTests(unittest.TestCase):
    def test_restarts_only_when_asked_and_answers_the_request(self):
        with Agent() as agent:
            m.handle_reopen_request()
            agent.mocks["restart_twilight_with_control"].assert_not_called()
            m.REOPEN_REQUEST.write_text("42")
            m.handle_reopen_request()
            agent.mocks["restart_twilight_with_control"].assert_called_once()
            self.assertFalse(m.REOPEN_REQUEST.exists())
            result = json.loads(m.REOPEN_RESULT.read_text())
            self.assertEqual((result["request"], result["ok"], result["restarted"]), ("42", True, True))

    def test_does_not_restart_a_browser_that_already_has_the_bridge(self):
        with Agent(bridge_ready=True) as agent:
            m.REOPEN_REQUEST.write_text("7")
            m.handle_reopen_request()
            agent.mocks["restart_twilight_with_control"].assert_not_called()
            self.assertFalse(json.loads(m.REOPEN_RESULT.read_text())["restarted"])

    def test_command_waits_for_the_agent_answer(self):
        with Agent() as agent:
            def agent_loop():
                for _ in range(50):
                    if m.REOPEN_REQUEST.exists():
                        m.handle_reopen_request()
                        return
                    threading.Event().wait(0.1)
            loop = threading.Thread(target=agent_loop)
            loop.start()
            self.assertEqual(m.request_reopen(), 0)
            loop.join()
            agent.mocks["restart_twilight_with_control"].assert_called_once()

    def test_command_gives_up_when_no_agent_answers(self):
        with Agent(), patch.object(m, "REOPEN_TIMEOUT_SECONDS", 0):
            self.assertEqual(m.request_reopen(), 1)
            self.assertFalse(m.REOPEN_REQUEST.exists())


if __name__ == "__main__":
    unittest.main()
