import json
import time
import unittest

from helpers import CLI, ROOT, Sandbox


class CliTest(unittest.TestCase):
    """Host CLI end to end, without Docker: share → agent commits → pull → review → merge → unshare."""

    def setUp(self):
        self.s = Sandbox()
        self.agent_home = self.s.root / "Agent"
        self.user = self.s.repo("myrepo", {"src/a.txt": "one\n"})

    def tearDown(self):
        self.s.cleanup()

    def agent(self, *args, check=True, cwd=None):
        return self.s.run("python3", CLI, *args, cwd=cwd or self.user, check=check)

    def agent_commit(self, branch, rel, text):
        """Plays the agent: commit on agent/<task> in its clone."""
        work = self.agent_home / "work" / "myrepo"
        self.s.git(work, "switch", "-q", "-c", branch)
        (work / rel).write_text(text)
        self.s.git(work, "add", "-A")
        self.s.git(work, "commit", "-q", "-m", f"agent: {rel}")

    def share(self):
        self.agent("share", "--yes")

    def test_share_is_dry_run_by_default(self):
        out = self.agent("share").stdout
        self.assertIn("Dry run", out)
        self.assertFalse((self.agent_home / "projects.json").exists())

    def test_share_layout_and_safety(self):
        self.share()
        mirror = self.agent_home / "mirrors" / "myrepo.git"
        work = self.agent_home / "work" / "myrepo"
        self.assertEqual(self.s.git(mirror, "rev-parse", "main").stdout, self.s.git(self.user, "rev-parse", "main").stdout)
        self.assertEqual(self.s.git(work, "remote", "get-url", "desktop").stdout.strip(), "/mirrors/myrepo.git")
        self.assertEqual(self.s.git(work, "config", "remote.desktop.pushurl").stdout.strip(), "no-push://disabled")
        self.assertEqual(self.s.git(self.user, "config", "remote.agent.pushurl").stdout.strip(), "no-push://disabled")
        self.assertIn("refs/heads/agent/*", self.s.git(self.user, "config", "remote.agent.fetch").stdout)
        # Objects are copied, not hard-linked into the mirror.
        self.assertFalse((work / ".git" / "objects" / "info" / "alternates").exists())
        reg = json.loads((self.agent_home / "projects.json").read_text())
        self.assertTrue(reg["myrepo"]["hooks"])
        self.assertIn("agent auto-push", (self.user / ".git" / "hooks" / "post-commit").read_text())
        self.assertIn("already shared", self.agent("share", "--yes", check=False).stderr)

    def test_auto_push_hook(self):
        self.share()
        (self.user / "b.txt").write_text("b\n")
        self.s.git(self.user, "add", "-A")
        self.s.git(self.user, "commit", "-q", "-m", "second")
        mirror = self.agent_home / "mirrors" / "myrepo.git"
        want = self.s.git(self.user, "rev-parse", "HEAD").stdout
        for _ in range(50):
            if self.s.git(mirror, "rev-parse", "main").stdout == want:
                break
            time.sleep(0.1)
        self.assertEqual(self.s.git(mirror, "rev-parse", "main").stdout, want)

    def test_push_wip(self):
        self.share()
        (self.user / "src" / "a.txt").write_text("changed\n")
        self.assertIn("desktop-wip/main", self.agent("push", "--wip").stdout)
        mirror = self.agent_home / "mirrors" / "myrepo.git"
        self.assertEqual(self.s.git(mirror, "show", "refs/wip/main:src/a.txt").stdout, "changed\n")
        # A normal push must not prune the WIP snapshot.
        self.agent("push")
        self.assertEqual(self.s.git(mirror, "rev-parse", "-q", "--verify", "refs/wip/main", check=False).returncode, 0)

    def test_pull_review_merge(self):
        self.share()
        self.agent_commit("agent/uart-fix", "src/a.txt", "two\n")
        handoff = self.agent_home / "handoffs" / "myrepo" / "uart-fix.md"
        handoff.write_text("# uart-fix\n## Summary\nFixed it.\x1b[31m\n")
        out = self.agent("pull").stdout
        self.assertIn("agent/uart-fix", out)
        self.assertIn("1 new", out)
        self.assertIn("handoff", out)

        out = self.agent("review", "uart-fix", "--no-open").stdout
        self.assertIn("Fixed it.", out)
        self.assertNotIn("\x1b", out)
        wt = self.agent_home / "review" / "myrepo" / "uart-fix"
        staged = self.s.git(wt, "diff", "--cached", "--name-only").stdout.split()
        self.assertEqual(staged, ["src/a.txt"])
        self.assertEqual(self.s.git(self.user, "branch", "--show-current").stdout.strip(), "main")

        (self.user / "dirty.txt").write_text("x")
        self.s.git(self.user, "add", "dirty.txt")
        self.assertIn("uncommitted changes", self.agent("merge", "uart-fix", check=False).stderr)
        self.s.git(self.user, "reset", "-q", "dirty.txt")

        self.agent("merge", "uart-fix")
        self.assertEqual((self.user / "src" / "a.txt").read_text(), "two\n")
        self.assertFalse(wt.exists())
        self.assertIn("merged", self.agent("pull").stdout)

    def test_merge_conflict_leaves_real_merge_state(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "agent\n")
        (self.user / "src" / "a.txt").write_text("user\n")
        self.s.git(self.user, "commit", "-q", "-am", "user edit")
        p = self.agent("merge", "x", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Merge Changes", p.stderr)
        # Unmerged index entries: this is what VS Code's merge editor needs.
        self.assertIn("src/a.txt", self.s.git(self.user, "diff", "--name-only", "--diff-filter=U").stdout)
        self.s.git(self.user, "merge", "--abort")

    def test_review_unknown_task(self):
        self.share()
        self.assertIn("no agent branch", self.agent("review", "nope", "--no-open", check=False).stderr)

    def test_unshare_cleans_up(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "agent\n")
        p = self.agent("unshare", "--yes")
        self.assertIn("unmerged agent branches will be lost: x", p.stderr)
        self.assertFalse((self.agent_home / "mirrors" / "myrepo.git").exists())
        self.assertFalse((self.agent_home / "work" / "myrepo").exists())
        self.assertFalse((self.user / ".git" / "hooks" / "post-commit").exists())
        self.assertNotEqual(self.s.git(self.user, "remote", "get-url", "agent", check=False).returncode, 0)
        self.assertEqual(json.loads((self.agent_home / "projects.json").read_text()), {})

    def test_existing_hook_is_preserved(self):
        hook = self.user / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\necho mine\n")
        self.share()
        self.assertIn("echo mine", hook.read_text())
        self.agent("unshare", "--yes")
        self.assertEqual(hook.read_text(), "#!/bin/sh\necho mine\n")

    def test_setup_without_docker(self):
        self.agent("setup", "--no-build", "--no-start")
        home = self.agent_home / "home"
        self.assertTrue((home / ".claude" / "CLAUDE.md").exists())
        self.assertTrue((home / ".claude" / "skills" / "vault" / "SKILL.md").exists())
        self.assertIn(".gitconfig.agent", (home / ".gitconfig").read_text())
        self.assertTrue((self.agent_home / "vault" / ".git").is_dir())
        env = (self.agent_home / ".env").read_text()
        self.assertIn(f"AGENT_WORK_DIR='{self.agent_home / 'work'}'", env)
        self.assertEqual((self.agent_home / "compose.yaml").read_text(), (ROOT / "container" / "compose.yaml").read_text())
        # Re-running keeps the vault and is safe.
        self.agent("setup", "--no-build", "--no-start")


if __name__ == "__main__":
    unittest.main()
