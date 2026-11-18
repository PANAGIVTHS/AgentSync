import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from helpers import BIN

FAKE_CLAUDE = """#!/bin/sh
case "$1" in
  auth) echo '{ "loggedIn": true }' ;;
  remote-control) echo "$* in $(pwd)" >> "$LOG"; sleep 300 ;;
esac
"""


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class SupervisorTest(unittest.TestCase):
    def test_one_remote_control_per_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "bin").mkdir()
            fake = t / "bin" / "claude"
            fake.write_text(FAKE_CLAUDE)
            fake.chmod(0o755)
            for name in ("alpha", "beta"):
                (t / "work" / name / ".git").mkdir(parents=True)
            session = f"suptest{os.getpid()}"
            env = dict(os.environ, PATH=f"{t / 'bin'}:{os.environ['PATH']}", LOG=str(t / "log"),
                       AGENT_WORK=str(t / "work"), AGENT_TMUX_SESSION=session, AGENT_POLL_SECONDS="1")
            proc = subprocess.Popen([str(BIN / "agent-supervisor")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def windows():
                p = subprocess.run(["tmux", "list-windows", "-t", session, "-F", "#W"],
                                   capture_output=True, text=True)
                return set(p.stdout.split())

            try:
                for _ in range(50):
                    if {"rc-alpha", "rc-beta"} <= windows():
                        break
                    time.sleep(0.1)
                self.assertTrue({"rc-alpha", "rc-beta"} <= windows())
                time.sleep(0.3)
                self.assertIn("remote-control --name alpha", (t / "log").read_text())
                shutil.rmtree(t / "work" / "beta")
                for _ in range(50):
                    if "rc-beta" not in windows():
                        break
                    time.sleep(0.1)
                self.assertNotIn("rc-beta", windows())
                self.assertIn("rc-alpha", windows())
            finally:
                proc.kill()
                subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


if __name__ == "__main__":
    unittest.main()
