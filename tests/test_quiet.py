import unittest

from helpers import BIN, Sandbox

QUIET = str(BIN / "quiet")


class QuietTest(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.logs = self.s.root / "logs"

    def tearDown(self):
        self.s.cleanup()

    def quiet(self, *args):
        return self.s.run(QUIET, *args, check=False, QUIET_LOG_DIR=str(self.logs))

    def test_failure_summary(self):
        script = ('for i in $(seq 1 500); do echo "noise $i"; done; '
                  'echo "%Error: rtl/alu.sv:12:5: bad width"; echo "%Error: rtl/alu.sv:40:5: bad width"; '
                  'echo "0 errors in lint"; echo "Ran 45 tests"; echo "FAILED (failures=2)"; exit 3')
        f = self.s.root / "noisy.sh"
        f.write_text(script.replace("; ", "\n") + "\n")
        p = self.quiet("-n", "3", "--", "bash", str(f))
        self.assertEqual(p.returncode, 3)
        out = p.stdout
        self.assertIn("exit 3", out)
        self.assertIn("505 lines", out)
        self.assertIn("501: %Error: rtl/alu.sv:12:5: bad width  (×2)", out)
        errors = out.split("errors (")[1].split("tail")[0]
        self.assertNotIn("0 errors in lint", errors)  # "0 errors" isn't an error
        self.assertIn("Ran 45 tests", out.split("errors (")[0])
        self.assertNotIn("noise 250", out)
        self.assertLess(len(out.splitlines()), 20)
        log = next(self.logs.glob("*.log")).read_text()
        self.assertIn("noise 250", log)  # the full output is kept

    def test_short_output_passes_through(self):
        p = self.quiet("--", "printf", "a\\nb\\n")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.splitlines()[2:], ["  a", "  b"])

    def test_missing_command(self):
        self.assertEqual(self.quiet("--", "no-such-cmd-xyz").returncode, 127)


if __name__ == "__main__":
    unittest.main()
