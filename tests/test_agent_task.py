import unittest

from helpers import BIN, ROOT, Sandbox

TASK = str(BIN / "agent-task")


class AgentTaskTest(unittest.TestCase):
    """Simulates the container: a user repo, its mirror, and the agent's clone."""

    def setUp(self):
        self.s = Sandbox()
        self.user = self.s.repo("user", {"src/a.txt": "one\n"})
        self.s.git(self.user, "branch", "feat/dsp")
        self.mirror = self.s.root / "mirror.git"
        self.s.run("git", "init", "-q", "--bare", self.mirror)
        self.s.git(self.mirror, "symbolic-ref", "HEAD", "refs/heads/main")
        self.push()
        self.work = self.s.root / "work" / "proj"
        self.s.run("git", "clone", "-q", "-o", "desktop", self.mirror, self.work)
        self.s.git(self.work, "config", "--add", "remote.desktop.fetch", "+refs/wip/*:refs/remotes/desktop-wip/*")
        self.s.git(self.work, "config", "core.hooksPath", str(ROOT / "container" / "githooks"))
        self.handoffs = self.s.root / "handoffs"

    def tearDown(self):
        self.s.cleanup()

    def push(self):
        self.s.git(self.user, "push", "-q", "--force", "--prune", self.mirror, "refs/heads/*:refs/heads/*")

    def task(self, *args, check=True):
        return self.s.run(TASK, *args, cwd=self.work, check=check, AGENT_HANDOFFS=str(self.handoffs))

    def commit(self, rel, text, msg):
        (self.work / rel).write_text(text)
        self.s.git(self.work, "add", "-A")
        return self.s.git(self.work, "commit", "-q", "-m", msg, check=False)

    def test_commit_outside_agent_branch_is_rejected(self):
        p = self.commit("x.txt", "x", "on main")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("commit only on agent/<task>", p.stderr)

    def test_start_resume_status_handoff(self):
        out = self.task("start", "UART Fix").stdout
        self.assertIn("created agent/uart-fix from the user's main", out)
        self.assertEqual(self.commit("src/a.txt", "two\n", "Fix uart").returncode, 0)

        # The user commits meanwhile; handoff rebases onto it.
        (self.user / "other.txt").write_text("user\n")
        self.s.git(self.user, "add", "-A")
        self.s.git(self.user, "commit", "-q", "-m", "user work")
        self.push()

        (self.work / "scratch.txt").write_text("wip")
        out = self.task("start", "hls", "--from", "feat/dsp").stdout
        self.assertIn("autosaved uncommitted work on agent/uart-fix", out)
        self.assertIn("created agent/hls from the user's feat/dsp", out)

        out = self.task("start", "uart-fix").stdout
        self.assertIn("resumed agent/uart-fix (base main: 2 commits ahead, 1 behind", out)

        status = self.task("status").stdout
        self.assertIn("* agent/uart-fix", status)
        self.assertIn("agent/hls", status)

        out = self.task("handoff").stdout
        self.assertIn("rebased onto the user's latest main", out)
        self.assertIn(str(self.handoffs / "proj" / "uart-fix.md"), out)
        self.assertTrue((self.work / "other.txt").exists())

    def test_handoff_refuses_dirty_tree_and_bad_base(self):
        self.task("start", "x")
        (self.work / "y.txt").write_text("y")
        self.assertIn("uncommitted changes", self.task("handoff", check=False).stderr)
        self.assertIn("no user branch 'nope'", self.task("start", "z", "--from", "nope", check=False).stderr)


if __name__ == "__main__":
    unittest.main()
