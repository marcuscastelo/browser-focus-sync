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
    def test_without_configuration_or_log_falls_back_to_hid(self):
        with patch.object(m,'DESKFLOW_LOG',''):
            self.assertIsNone(m.seconds_on_other_screen())
        with patch.object(m,'DESKFLOW_LOG',str(self.log)),patch.object(m,'DESKFLOW_SCREEN','polaris'):
            self.assertIsNone(m.seconds_on_other_screen())

if __name__=='__main__': unittest.main()
