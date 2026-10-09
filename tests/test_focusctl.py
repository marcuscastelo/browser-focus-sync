import contextlib
import io
import unittest
from unittest.mock import patch

import focusctl


class FocusctlTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["focusctl.py", *args]), patch("socket.socket") as sock, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = focusctl.main()
        return code, out.getvalue(), err.getvalue(), sock

    def test_help_is_local(self):
        for flag in ("-h", "--help"):
            code, out, _, sock = self.run_cli(flag)
            self.assertEqual(code, 0)
            self.assertIn("mac-active", out)
            sock.assert_not_called()

    def test_unknown_option_is_not_sent(self):
        code, _, err, sock = self.run_cli("--status")
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)
        sock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
