import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import mac_agent as m

def stamp(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(time.time() - seconds_ago))

class DeskflowFocusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.log=Path(self.tmp.name)/'server.log'
    def tearDown(self): self.tmp.cleanup()
    def away(self,lines):
        self.log.write_text("\n".join(lines)+"\n")
        with patch.object(m,'DESKFLOW_LOG',str(self.log)),patch.object(m,'DESKFLOW_SCREEN','polaris'):
            return m.seconds_on_other_screen()
    def test_cursor_on_other_screen_counts_as_idle_since_switch(self):
        # Seen on 08/10: the Mac keyboard typed into sirius, so HID idle stayed near zero.
        away=self.away([f'[{stamp(40)}] INFO: switch from "sirius" to "polaris" at 1,1',
                        f'[{stamp(20)}] INFO: switch from "polaris" to "sirius" at 2,1079'])
        self.assertGreaterEqual(away,19);self.assertLess(away,30)
    def test_cursor_back_on_this_mac_uses_hid_idle(self):
        self.assertIsNone(self.away([f'[{stamp(20)}] INFO: switch from "polaris" to "sirius" at 2,1079',
                                     f'[{stamp(5)}] INFO: switch from "sirius" to "polaris" at 1,1']))
    def test_suspend_resume_returns_to_mac_without_switch(self):
        # server.log on 11/10/2026: return during suspend, reconnect after resume.
        lines = [
            '[2026-10-11T17:20:47.943] INFO: switch from "polaris" to "sirius" at 1919,1079',
            '[2026-10-11T17:20:47.951] INFO: leaving computer',
            '[2026-10-11T17:24:51.811] INFO: suspend',
            '[2026-10-11T17:24:51.852] INFO: disconnecting client "sirius"',
            '[2026-10-11T17:24:51.872] INFO: jump from "sirius" to "polaris" at 756,491',
            '[2026-10-11T17:24:51.872] INFO: entering computer',
            '[2026-10-11T17:54:46.335] INFO: resume',
            '[2026-10-11T17:54:47.942] INFO: accepted client connection',
        ]
        self.assertIsNone(self.away(lines))
        self.assertIsNone(self.away(lines[:5]))  # jump alone restores HID idle
        self.assertIsNone(self.away(lines[:4] + lines[5:]))  # entering alone too

    def test_jump_to_other_screen_starts_new_idle_interval(self):
        with patch.object(m.time, 'time', return_value=1800000000):
            away = self.away([
                f'[{stamp(100)}] INFO: switch from "polaris" to "sirius" at 1,1',
                f'[{stamp(60)}] INFO: entering computer',
                f'[{stamp(20)}] INFO: jump from "polaris" to "sirius" at 2,2',
            ])
        self.assertEqual(away, 20)

    def test_last_event_wins_after_entering(self):
        away = self.away([
            f'[{stamp(40)}] INFO: entering computer',
            f'[{stamp(20)}] INFO: switch from "polaris" to "sirius" at 2,2',
        ])
        self.assertGreaterEqual(away, 19)
        self.assertLess(away, 30)

    def test_without_configuration_or_log_falls_back_to_hid(self):
        with patch.object(m,'DESKFLOW_LOG',''):
            self.assertIsNone(m.seconds_on_other_screen())
        with patch.object(m,'DESKFLOW_LOG',str(self.log)),patch.object(m,'DESKFLOW_SCREEN','polaris'):
            self.assertIsNone(m.seconds_on_other_screen())

if __name__=='__main__': unittest.main()
