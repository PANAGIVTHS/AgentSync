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

    def agent_commit(self, branch, rel, text, new_branch=True):
        """Plays the agent: commit on agent/<task> in its clone, with a co-author trailer."""
        work = self.agent_home / "work" / "myrepo"
        if new_branch:
            self.s.git(work, "switch", "-q", "-c", branch)
        (work / rel).write_text(text)
        self.s.git(work, "add", "-A")
        self.s.git(work, "commit", "-q", "-m", f"agent: {rel}", "-m",
                   "Co-Authored-By: Claude <noreply@anthropic.com>")

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
        (self.user / "src" / "a.txt").write_text("changed\n")            # unstaged edit
        (self.user / "staged.txt").write_text("staged\n")
        self.s.git(self.user, "add", "staged.txt")                         # staged new file
        (self.user / "src" / "new.sv").write_text("module m; endmodule\n")  # untracked new file
        (self.user / ".gitignore").write_text("build/\n")
        (self.user / "build").mkdir()
        (self.user / "build" / "out.bit").write_text("ignored\n")        # ignored
        (self.user / ".env").write_text("TOKEN=secret\n")                 # secret, not in .gitignore
        index_before = self.s.git(self.user, "diff", "--cached", "--name-only").stdout
        out = self.agent("push", "--wip").stdout
        self.assertIn("desktop-wip/main", out)
        self.assertIn("including 3 new files", out)                        # staged.txt, new.sv, .gitignore
        mirror = self.agent_home / "mirrors" / "myrepo.git"
        files = self.s.git(mirror, "ls-tree", "-r", "--name-only", "refs/wip/main").stdout.split()
        self.assertEqual(sorted(files), [".gitignore", "src/a.txt", "src/new.sv", "staged.txt"])  # no build/, no .env
        self.assertEqual(self.s.git(mirror, "show", "refs/wip/main:src/a.txt").stdout, "changed\n")
        # Your staging area and history are untouched.
        self.assertEqual(self.s.git(self.user, "diff", "--cached", "--name-only").stdout, index_before)
        self.assertIn("?? src/new.sv", self.s.git(self.user, "status", "--porcelain").stdout)
        self.assertEqual(self.s.git(self.user, "log", "--oneline").stdout.count("\n"), 1)
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
        self.assertIn("written", out)

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

        out = self.agent("merge", "uart-fix").stdout
        self.assertIn("staged in your repo, not committed", out)
        self.assertEqual((self.user / "src" / "a.txt").read_text(), "two\n")
        self.assertFalse(wt.exists())
        self.assertIn("staged; commit to finish", self.agent("pull").stdout)
        self.s.git(self.user, "commit", "-q", "--no-edit")
        self.assertIn("merged", self.agent("pull").stdout)

    def last_commit(self, fmt):
        return self.s.git(self.user, "log", "-1", f"--format={fmt}").stdout

    def test_merge_is_a_squash_authored_by_you_without_coauthors(self):
        self.share()
        self.agent_commit("agent/split", "src/a.txt", "two\n")
        self.agent("merge", "split")
        self.assertNotEqual(self.s.git(self.user, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode, 0)
        msg = (self.user / ".git" / "SQUASH_MSG").read_text()
        self.assertEqual(msg, "agent: src/a.txt\n")
        self.s.git(self.user, "commit", "-q", "--no-edit")   # what VS Code's Commit does with the pre-filled message
        self.assertEqual(len(self.last_commit("%P").split()), 1)  # no merge commit
        self.assertEqual(self.last_commit("%an <%ae>").strip(), "Tester <t@example.com>")
        self.assertNotIn("Co-Authored-By", self.last_commit("%B"))
        self.assertNotIn("Claude", self.s.git(self.user, "log", "--format=%an%n%B", "main").stdout)
        self.assertFalse((self.user / ".git" / "SQUASH_MSG").exists())
        self.assertIn("merged", self.agent("pull").stdout)
        mirror = self.agent_home / "mirrors" / "myrepo.git"
        self.assertEqual(self.s.git(mirror, "rev-parse", "refs/merged/split").stdout,
                         self.s.git(self.user, "rev-parse", "refs/remotes/agent/split").stdout)
        self.assertIn("already merged", self.agent("merge", "split").stdout)

    def test_merge_commit_flag_and_multi_commit_message(self):
        self.share()
        self.agent_commit("agent/fir-pipeline", "src/a.txt", "two\n")
        self.agent_commit("agent/fir-pipeline", "b.txt", "b\n", new_branch=False)
        out = self.agent("merge", "fir-pipeline", "--commit").stdout
        self.assertIn("committed as you: Fir pipeline", out)
        self.assertEqual(self.last_commit("%B").strip(), "Fir pipeline\n\n- agent: src/a.txt\n- agent: b.txt")
        self.assertIn("merged", self.agent("pull").stdout)

    def test_abandoned_squash_is_forgotten(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "two\n")
        self.agent("merge", "x")
        self.assertIn("already in progress", self.agent("merge", "x", check=False).stderr)
        self.s.git(self.user, "reset", "-q", "--merge")
        self.assertFalse((self.user / ".git" / "SQUASH_MSG").exists())
        self.assertIn("1 new", self.agent("pull").stdout)
        self.assertIn("staged in your repo", self.agent("merge", "x").stdout)

    def test_follow_up_commits_after_a_merge(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "two\n")
        self.agent("merge", "x", "--commit")
        # The agent adds to the same task without rebasing first.
        self.agent_commit("agent/x", "c.txt", "c\n", new_branch=False)
        self.assertIn("1 new since merge", self.agent("pull").stdout)
        review = self.agent("review", "x", "--no-open").stdout
        self.assertIn("new since you last merged", review)
        wt = self.agent_home / "review" / "myrepo" / "x"
        self.assertEqual(self.s.git(wt, "diff", "--cached", "--name-only").stdout.split(), ["c.txt"])
        self.agent("merge", "x", "--commit")
        self.assertEqual(self.s.git(self.user, "show", "--name-only", "--format=", "HEAD").stdout.split(), ["c.txt"])
        self.assertEqual((self.user / "src" / "a.txt").read_text(), "two\n")
        self.assertIn("merged", self.agent("pull").stdout)

    def test_real_merge_is_opt_in(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "two\n")
        self.agent("merge", "x", "--merge")
        self.assertEqual(len(self.last_commit("%P").split()), 2)
        self.assertIn("merged", self.agent("pull").stdout)

    def test_merge_conflict_leaves_real_merge_state(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "agent\n")
        (self.user / "src" / "a.txt").write_text("user\n")
        self.s.git(self.user, "commit", "-q", "-am", "user edit")
        p = self.agent("merge", "x", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Merge Changes", p.stderr)
        self.assertIn("git reset --merge", p.stdout)
        # Unmerged index entries: this is what VS Code's merge editor needs.
        self.assertIn("src/a.txt", self.s.git(self.user, "diff", "--name-only", "--diff-filter=U").stdout)
        self.s.git(self.user, "reset", "-q", "--merge")

    def test_review_unknown_task(self):
        self.share()
        self.assertIn("no agent branch", self.agent("review", "nope", "--no-open", check=False).stderr)

    def test_edits_in_review_copy_are_carried_into_merge(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "agent\n")
        self.agent("review", "x", "--no-open")
        wt = self.agent_home / "review" / "myrepo" / "x"
        (wt / "src" / "a.txt").write_text("agent, renamed by me\n")
        (wt / "NEW.md").write_text("my note\n")
        out = self.agent("merge", "x").stdout
        self.assertIn("applied the edits you made in the review copy", out)
        self.assertEqual((self.user / "src" / "a.txt").read_text(), "agent, renamed by me\n")
        self.assertEqual((self.user / "NEW.md").read_text(), "my note\n")
        self.assertFalse(wt.exists())

    def test_rerunning_review_keeps_your_edits(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "agent\n")
        self.agent("review", "x", "--no-open")
        wt = self.agent_home / "review" / "myrepo" / "x"
        (wt / "src" / "a.txt").write_text("mine\n")
        out = self.agent("review", "x", "--no-open").stdout
        self.assertIn("carried your edits", out)
        self.assertEqual((wt / "src" / "a.txt").read_text(), "mine\n")

    def test_closing_review_saves_your_edits_as_a_patch(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "agent\n")
        self.agent("review", "x", "--no-open")
        wt = self.agent_home / "review" / "myrepo" / "x"
        (wt / "src" / "a.txt").write_text("mine\n")
        p = self.agent("review", "x", "--close")
        self.assertIn("saved them to", p.stderr)
        patches = list((self.agent_home / "review" / "myrepo").glob("x-your-edits-*.patch"))
        self.assertEqual(len(patches), 1)
        self.assertIn("+mine", patches[0].read_text())
        # An untouched review copy closes without a patch.
        self.agent("review", "x", "--no-open")
        self.assertNotIn("saved", self.agent("review", "x", "--close").stderr)

    def test_merge_modes_are_exclusive(self):
        self.share()
        self.assertNotEqual(self.agent("merge", "x", "--commit", "--merge", check=False).returncode, 0)

    def agent_rebuilds_history(self, task, files):
        """Plays the agent: a branch with no history in common with yours."""
        work = self.agent_home / "work" / "myrepo"
        self.s.git(work, "switch", "-q", "--orphan", f"agent/{task}")
        self.s.git(work, "rm", "-rq", "--cached", "--ignore-unmatch", ".")
        for f in list(work.iterdir()):
            if f.name != ".git":
                (f.unlink() if f.is_file() else __import__("shutil").rmtree(f))
        for rel, text in files.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text)
        self.s.git(work, "add", "-A")
        self.s.git(work, "commit", "-q", "-m", "Rebuild history")

    def test_unrelated_history_merge_explains_overwrite(self):
        self.share()
        self.agent_rebuilds_history("rebuild-history", {"src/a.txt": "new\n"})
        p = self.agent("merge", "rebuild-history", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("agent pull rebuild-history --overwrite", p.stderr)
        self.assertIn("no shared history", self.agent("review", "rebuild-history", "--no-open").stdout)

    def test_pull_overwrite_takes_the_agents_files_exactly(self):
        self.share()
        self.agent_rebuilds_history("rebuild-history", {"src/a.txt": "agent\n", "src/b.txt": "b\n"})
        # Your mess: a staged file, an edit, a deletion, a new file, and a secret.
        (self.user / "staged.txt").write_text("staged\n")
        self.s.git(self.user, "add", "staged.txt")
        (self.user / "src" / "a.txt").write_text("mine\n")
        (self.user / "loose.txt").write_text("loose\n")
        (self.user / ".env").write_text("TOKEN=1\n")
        out = self.agent("pull", "rebuild-history", "--overwrite").stdout
        self.assertIn("match agent/rebuild-history exactly", out)
        files = sorted(str(p.relative_to(self.user)) for p in self.user.rglob("*")
                       if p.is_file() and ".git" not in p.parts)
        self.assertEqual(files, [".env", "src/a.txt", "src/b.txt"])        # your .env is never deleted
        self.assertEqual((self.user / "src" / "a.txt").read_text(), "agent\n")
        self.assertEqual(self.s.git(self.user, "diff", "--name-only").stdout, "")  # all staged
        self.s.git(self.user, "commit", "-q", "--no-edit")
        self.assertEqual(self.last_commit("%s").strip(), "Rebuild history")
        self.assertEqual(len(self.last_commit("%P").split()), 1)
        self.assertIn("merged", self.agent("pull").stdout)
        # Your old work was saved and can be restored.
        backup = self.s.git(self.user, "for-each-ref", "--format=%(refname)", "refs/agent-backup/").stdout.split()
        self.assertEqual(len(backup), 1)
        self.s.git(self.user, "restore", f"--source={backup[0]}", "--worktree", "--", ".")
        self.assertEqual((self.user / "src" / "a.txt").read_text(), "mine\n")
        self.assertEqual((self.user / "loose.txt").read_text(), "loose\n")

    def test_pull_overwrite_replaces_a_half_finished_merge(self):
        self.share()
        self.agent_commit("agent/x", "src/a.txt", "agent\n")
        (self.user / "src" / "a.txt").write_text("user\n")
        self.s.git(self.user, "commit", "-q", "-am", "user edit")
        self.assertEqual(self.agent("merge", "x", check=False).returncode, 1)   # conflict
        self.agent("pull", "x", "--overwrite")
        self.assertEqual((self.user / "src" / "a.txt").read_text(), "agent\n")
        self.assertEqual(self.s.git(self.user, "diff", "--name-only", "--diff-filter=U").stdout, "")

    def test_pull_overwrite_needs_a_task(self):
        self.share()
        self.assertIn("say which task", self.agent("pull", "--overwrite", check=False).stderr)

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
        self.assertIn(f"AGENT_TOOLS_DIR='{self.agent_home / 'tools'}'", env)
        tools = self.agent_home / "tools"
        self.assertEqual((tools / "env.sh").read_text(), (ROOT / "tools-template" / "env.sh").read_text())
        self.assertTrue((tools / "bin").is_dir() and (tools / "manifest.json").is_file())
        # Re-running keeps the vault and the manifest, and is safe.
        (tools / "manifest.json").write_text('{"tools": [{"name": "kept"}]}')
        self.agent("setup", "--no-build", "--no-start")
        self.assertIn("kept", (tools / "manifest.json").read_text())
        # `agent` alone works without Rich too.
        self.assertIn("agent checkup", self.agent().stdout)


if __name__ == "__main__":
    unittest.main()
