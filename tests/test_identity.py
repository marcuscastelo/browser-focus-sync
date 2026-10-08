import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import bfs_config as cfg
import mac_agent as m

PROFILE = Path("/home/u/.zen/abc.Default (twilight)")


class ProfileTests(unittest.TestCase):
    def test_other_profile_is_not_ours(self):
        # Seen on 08/10: a disposable test Twilight hid the real bridge.
        self.assertTrue(cfg.runs_other_profile(["zen-bin", "--no-remote", "--profile", "/tmp/lab/profA"], PROFILE))

    def test_our_profile_and_no_profile_are_ours(self):
        self.assertFalse(cfg.runs_other_profile(["zen-bin", "--profile", str(PROFILE), "--marionette"], PROFILE))
        self.assertFalse(cfg.runs_other_profile(["zen-bin"], PROFILE))
        self.assertFalse(cfg.runs_other_profile(["zen-bin", f"--profile={PROFILE}"], PROFILE))

    def test_mac_skips_a_second_twilight_with_another_profile(self):
        executable = m.TWILIGHT_EXECUTABLE
        listing = (f"  900 {executable} --headless --no-remote --profile /tmp/bfs-lab/profA\n"
                   f"  950 {executable} --profile /Users/u/Profiles/x.Default (twilight) --marionette\n")
        with patch.object(m, "run", return_value=CompletedProcess([], 0, listing, "")), \
                patch.object(m.cfg, "configured_profile", return_value=Path("/Users/u/Profiles/x.Default (twilight)")):
            self.assertEqual(m.twilight_pid(), "950")

    def test_mac_dock_launch_without_profile_is_ours(self):
        listing = f"  77 {m.TWILIGHT_EXECUTABLE}\n"
        with patch.object(m, "run", return_value=CompletedProcess([], 0, listing, "")), \
                patch.object(m.cfg, "configured_profile", return_value=Path("/Users/u/Profiles/x")):
            self.assertEqual(m.twilight_pid(), "77")


if __name__ == "__main__":
    unittest.main()
