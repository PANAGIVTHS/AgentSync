import os
import unittest

from helpers import CLI, Sandbox

TOKEN = "ghp_" + "b" * 36


class ShareFolderTest(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.home = self.s.root / "Agent"

    def tearDown(self):
        self.s.cleanup()

    def agent(self, *args, cwd=None, check=True):
        return self.s.run("python3", CLI, *args, cwd=cwd or self.s.root, check=check,
                          XDG_CONFIG_HOME="", XDG_DATA_HOME="")

    def both(self, *args, **kw):
        p = self.agent(*args, **kw)
        return p.stdout + p.stderr  # warnings go to stderr in plain output

    def folder(self):
        f = self.s.root / "Coursework"
        for rel, text in {"notes.md": "hi\n", "src/main.c": "int main(){}\n", ".env": "PASSWORD=x\n",
                          ".env.example": "PASSWORD=\n", "keys/deploy.pem": "-----BEGIN\n",
                          "config.py": f"TOKEN = '{TOKEN}'\n", "node_modules/x/index.js": "x\n",
                          "private/diary.txt": "dear diary\n"}.items():
            (f / rel).parent.mkdir(parents=True, exist_ok=True)
            (f / rel).write_text(text)
        with open(f / "data.bin", "wb") as big:
            big.truncate(60 * 1024 * 1024)  # sparse: big on paper, no disk used
        return f

    def committed(self, repo):
        return self.s.git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()

    def test_plain_folder_dry_run_then_share(self):
        f = self.folder()
        dry = self.both("share", str(f), "--secret", str(f / "private"))
        self.assertIn("Your folder", dry)
        self.assertIn(".env  (looks secret)", dry)
        self.assertIn("keys/deploy.pem  (looks secret)", dry)
        self.assertNotIn(".env.example", dry)
        self.assertIn("private/", dry)
        self.assertIn("data.bin  (over 50.0 MB)", dry)
        self.assertIn("config.py:1  GitHub token", dry)
        self.assertNotIn("b" * 36, dry)  # masked
        self.assertIn("make it a git repo (local only", dry)
        self.assertFalse((f / ".git").exists())  # dry run changed nothing

        self.agent("share", str(f), "--secret", str(f / "private"), "--yes")
        self.assertTrue((f / ".git").is_dir())
        files = self.committed(f)
        self.assertIn("notes.md", files)
        self.assertIn("config.py", files)  # warned about, but you didn't exclude it
        self.assertIn(".env.example", files)
        for kept_out in (".env", "keys/deploy.pem", "private/diary.txt", "node_modules/x/index.js", "data.bin"):
            self.assertNotIn(kept_out, files)
        self.assertNotIn(".gitignore", files)  # nothing written into your folder's files
        work = self.home / "work" / "Coursework"
        self.assertTrue((work / "notes.md").exists())
        self.assertFalse((work / ".env").exists())
        self.assertEqual(self.s.git(f, "status", "--porcelain").stdout, "")
        listed = self.agent("secrets", cwd=f).stdout
        self.assertIn("private", listed)
        self.assertIn("kept out", listed)
        self.assertIn("stays a git repo", self.agent("unshare", "--project", "Coursework", "--yes").stdout)

    def test_refuses_the_home_folder(self):
        p = self.agent("share", str(self.s.home), check=False)
        self.assertIn("whole home folder", p.stderr)

    def test_existing_repo_secrets_and_history_warning(self):
        repo = self.s.repo("proj", {"app.py": "print(1)\n", ".env": "OLD=1\n"})
        (repo / "secrets.json").write_text("{}\n")
        (repo / ".gitignore").write_text("*.log\n")
        self.s.git(repo, "add", ".gitignore")
        self.s.git(repo, "commit", "-qm", "ignore logs")
        dry = self.both("share", cwd=repo)
        self.assertIn("secrets.json  (looks secret)", dry)
        self.assertIn("already committed, so the agent will see them in your history: .env", dry)
        self.agent("share", "--yes", cwd=repo)
        self.assertEqual((repo / ".gitignore").read_text(), "*.log\n")  # your files untouched
        self.assertEqual(self.s.git(repo, "status", "--porcelain").stdout, "")  # secrets.json is now ignored
        self.assertIn("/secrets.json", (repo / ".git" / "info" / "exclude").read_text())

    def test_secrets_command(self):
        repo = self.s.repo("proj", {"app.py": "print(1)\n", "creds.txt": "pw\n"})
        self.agent("share", "--yes", "--no-detect", cwd=repo)
        (repo / "local").mkdir()
        (repo / "local" / "token").write_text(TOKEN)
        out = self.agent("secrets", "add", "local", cwd=repo).stdout
        self.assertIn("kept from the agent: local", out)
        self.assertIn("/local/", (repo / ".git" / "info" / "exclude").read_text())

        refused = self.agent("secrets", "add", "creds.txt", cwd=repo, check=False)
        self.assertIn("already committed", refused.stderr)
        out = self.both("secrets", "add", "creds.txt", "--untrack", cwd=repo)
        self.assertIn("untracked; commit to finish", out)
        self.assertIn("D  creds.txt", self.s.git(repo, "status", "--porcelain").stdout)
        p = self.agent("secrets", "add", "creds.txt", "--untrack", cwd=repo)  # idempotent
        self.assertIn("committed before", p.stderr + p.stdout)

        # `agent push --wip` leaves secrets out of the snapshot it sends.
        (repo / "app.py").write_text("print(2)\n")
        self.agent("push", "--wip", cwd=repo)
        wip = self.s.git(self.home / "mirrors" / "proj.git", "ls-tree", "-r", "--name-only", "refs/wip/main").stdout
        self.assertIn("app.py", wip)
        self.assertNotIn("local/token", wip)

        listing = self.agent("secrets", cwd=repo).stdout
        self.assertIn("committed earlier", listing)
        self.agent("secrets", "rm", "local", cwd=repo)
        self.assertNotIn("/local/", (repo / ".git" / "info" / "exclude").read_text())
        self.assertIn("not in the secrets list", self.agent("secrets", "rm", "nope", cwd=repo, check=False).stderr)
        self.assertIn("isn't inside", self.agent("secrets", "add", "/etc/passwd", cwd=repo, check=False).stderr)

    def test_completion(self):
        def complete(*words):
            return self.agent("__complete", "bash", *words).stdout.split("\n")
        self.assertTrue(any(line.startswith(":files") for line in complete("secrets", "add", "")))
        self.assertTrue(any(line.startswith(":files") for line in complete("secrets", "add", "a", "")))
        self.assertTrue(any(line.startswith(":files") for line in complete("share", ".", "--secret", "")))
        self.assertEqual({line.split("\t")[0] for line in complete("secrets", "") if line} & {"list", "add", "rm"},
                         {"list", "add", "rm"})


if __name__ == "__main__":
    unittest.main()
