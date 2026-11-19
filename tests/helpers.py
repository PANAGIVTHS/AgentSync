"""Shared test helpers: throwaway git repos with an isolated HOME."""
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "container" / "bin"
CLI = ROOT / "cli" / "agent.py"


class Sandbox:
    """A temp dir acting as HOME, with a git identity and no global hooks."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-test-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = Tester\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=str(self.home), AGENT_HOME=str(self.root / "Agent"), AGENT_PLAIN="1")
        env.update(extra)
        return env

    def run(self, *cmd, cwd=None, check=True, input=None, **env):
        p = subprocess.run([str(c) for c in cmd], cwd=cwd, env=self.env(**env), text=True,
                           capture_output=True, input=input)
        if check and p.returncode:
            raise AssertionError(f"{cmd} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
        return p

    def git(self, repo, *args, check=True):
        return self.run("git", "-C", repo, *args, check=check)

    def repo(self, name="proj", files=None):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "-q")
        for rel, text in (files or {"README.md": "hello\n"}).items():
            f = path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "initial")
        return path

    def cleanup(self):
        # Auto-push hooks run in the background; give them a moment to finish.
        import shutil
        import time
        for _ in range(20):
            try:
                self.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.1)
        shutil.rmtree(self.root, ignore_errors=True)
