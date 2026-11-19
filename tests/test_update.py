import shutil
import unittest

from helpers import ROOT, Sandbox


class UpdateTest(unittest.TestCase):
    """`agent update` on a full and a shallow install clone, without Docker or install.sh."""

    def setUp(self):
        self.s = Sandbox()
        src = self.s.root / "src"
        (src / "cli").mkdir(parents=True)
        shutil.copy2(ROOT / "cli" / "agent.py", src / "cli" / "agent.py")
        (src / "container").mkdir()
        (src / "container" / "Dockerfile").write_text("FROM scratch\n")
        self.s.git(src, "init", "-q")
        self.s.git(src, "add", "-A")
        self.s.git(src, "commit", "-q", "-m", "v1")
        self.upstream = self.s.root / "upstream.git"
        self.s.run("git", "clone", "-q", "--bare", src, self.upstream)
        self.src = src

    def tearDown(self):
        self.s.cleanup()

    def install(self, shallow):
        dest = self.s.root / ("shallow" if shallow else "full")
        args = ["--depth", "1"] if shallow else []
        self.s.run("git", "clone", "-q", *args, f"file://{self.upstream}", dest)
        return dest

    def release(self, rel, text, msg):
        f = self.src / rel
        f.write_text(text)
        self.s.git(self.src, "commit", "-q", "-am", msg)
        self.s.git(self.src, "push", "-q", self.upstream, "HEAD:main")

    def agent(self, install, *args, check=True):
        return self.s.run("python3", install / "cli" / "agent.py", "update", *args, cwd=self.s.root, check=check)

    def test_full_clone(self):
        inst = self.install(shallow=False)
        self.assertIn("already up to date", self.agent(inst).stdout)
        self.release("container/Dockerfile", "FROM scratch\n# new\n", "image tweak")
        out = self.agent(inst, "--check").stdout
        self.assertIn("image tweak", out)
        self.assertIn("the image will be rebuilt", out)
        self.assertNotIn("# new", (inst / "container" / "Dockerfile").read_text())  # --check changes nothing
        self.assertIn("updated to", self.agent(inst, "--no-install").stdout)
        self.assertIn("# new", (inst / "container" / "Dockerfile").read_text())

    def test_shallow_clone_keeps_one_version(self):
        inst = self.install(shallow=True)
        old = self.s.git(inst, "rev-parse", "HEAD").stdout.strip()
        self.release("cli/agent.py", (self.src / "cli" / "agent.py").read_text() + "\n# v2\n", "cli only")
        out = self.agent(inst, "--no-install").stdout
        self.assertIn("shallow copy", out)
        self.assertIn("the image stays as it is", out)
        self.assertTrue((inst / "cli" / "agent.py").read_text().endswith("# v2\n"))
        self.assertEqual(self.s.git(inst, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertNotEqual(self.s.git(inst, "cat-file", "-t", old, check=False).returncode, 0)  # old version gone

    def test_local_changes_block_update(self):
        inst = self.install(shallow=False)
        self.release("container/Dockerfile", "FROM scratch\n# new\n", "change")
        (inst / "container" / "Dockerfile").write_text("mine\n")
        p = self.agent(inst, "--no-install", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("local changes", p.stderr)


if __name__ == "__main__":
    unittest.main()
