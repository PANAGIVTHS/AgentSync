import importlib.util
import json
import unittest

from helpers import CLI, Sandbox

FAKE_DOCKER = r"""#!/bin/sh
# Just enough of docker for the storage commands. Logs every call.
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "info -f")
    case "$3" in *DockerRootDir*) echo "$FAKE_ROOT" ;; *) echo "Debian GNU/Linux 12" ;; esac ;;
  "images -q") printf 'sha256:old1\nsha256:other\n' ;;
  "image inspect")
    case "$4" in
      *Entrypoint*) echo 'sha256:old1 1073741824 ["/opt/agent/bin/agent-supervisor"]'
                    echo 'sha256:other 5000 ["/bin/sh"]' ;;
      *) echo 900000000 ;;
    esac ;;
  "system df") printf 'Images\t2GB\t1GB\nBuild Cache\t1.5GB\t1.5GB\n' ;;
  "ps -a") echo "12MB (virtual 1GB)" ;;
  "inspect -f") echo false ;;
esac
exit 0
"""


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.bin = self.s.root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.root = self.s.root / "docker-root"
        self.root.mkdir()
        self.log = self.s.root / "docker.log"
        self.config = self.s.home / ".config" / "agent" / "config.json"
        self.user = self.s.repo("myrepo", {"a.txt": "one\n"})

    def tearDown(self):
        self.s.cleanup()

    def agent(self, *args, check=True, **env):
        import os
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "FAKE_ROOT": str(self.root), "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", **env}
        return self.s.run("python3", CLI, *args, cwd=self.user, check=check, **env)

    def test_storage_report_and_clean(self):
        out = self.agent("storage").stdout
        self.assertIn("Old agent images", out)
        self.assertIn("1, up to 1.0 GB", out)
        self.assertIn("Build cache", out)
        self.assertIn("agent storage clean --build-cache", out)
        self.agent("storage", "clean")
        log = self.log.read_text()
        self.assertIn("rmi sha256:old1", log)
        self.assertNotIn("sha256:other", log.split("rmi", 1)[1])  # never someone else's image
        self.assertNotIn("builder prune", log)
        self.agent("storage", "clean", "--build-cache", "--yes")
        self.assertIn("builder prune -f", self.log.read_text())

    def test_docker_move_prints_commands(self):
        out = self.agent("storage", "docker-move", "/data/docker").stdout
        self.assertIn(f"rsync -aHAX --info=progress2 {self.root}/ /data/docker/", out)
        self.assertIn('"data-root": "/data/docker"', out)

    def test_setup_home_is_remembered(self):
        target = self.s.root / "data" / "Agent"
        self.agent("setup", "--no-build", "--no-start", "--home", str(target), AGENT_HOME="")
        self.assertEqual(json.loads(self.config.read_text())["home"], str(target))
        self.assertIn(f"AGENT_WORK_DIR='{target / 'work'}'", (target / ".env").read_text())
        # Later commands (and the auto-push hooks) find it without AGENT_HOME.
        self.agent("share", "--yes", AGENT_HOME="")
        self.assertTrue((target / "mirrors" / "myrepo.git").is_dir())

    def test_move_data(self):
        old, new = self.s.root / "old" / "Agent", self.s.root / "new" / "Agent"
        self.agent("setup", "--no-build", "--no-start", "--home", str(old), AGENT_HOME="")
        self.agent("share", "--yes", AGENT_HOME="")
        self.agent("storage", "move", str(new), "--yes", AGENT_HOME="")
        self.assertFalse(old.exists())
        self.assertEqual(json.loads(self.config.read_text())["home"], str(new))
        self.assertEqual(self.s.git(self.user, "remote", "get-url", "agent").stdout.strip(),
                         str(new / "work" / "myrepo"))
        self.assertIn(str(new), (new / ".env").read_text())
        self.assertIn("myrepo", self.agent("list", AGENT_HOME="").stdout)
        self.assertIn("already set up", self.agent("setup", "--no-build", "--no-start", "--home", str(old),
                                                   AGENT_HOME="", check=False).stderr)


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.user = self.s.repo("myrepo", {"a.txt": "one\n"})
        self.s.run("python3", CLI, "share", "--yes", cwd=self.user)
        work = self.s.root / "Agent" / "work" / "myrepo"
        self.s.git(work, "switch", "-q", "-c", "agent/uart-fix")
        (work / "a.txt").write_text("two\n")
        self.s.git(work, "commit", "-q", "-am", "fix the uart")
        self.s.run("python3", CLI, "pull", cwd=self.user)

    def tearDown(self):
        self.s.cleanup()

    def complete(self, *words, shell="bash"):
        out = self.s.run("python3", CLI, "__complete", shell, *words, cwd=self.user).stdout
        return [line.split("\t") for line in out.splitlines()]

    def test_commands_tasks_options(self):
        self.assertEqual([c[0] for c in self.complete("re")], ["restart", "review"])
        self.assertIn(["uart-fix", "fix the uart", "task"], self.complete("merge", ""))
        self.assertIn("--commit", [c[0] for c in self.complete("merge", "--")])
        self.assertEqual([c[0] for c in self.complete("review", "--project", "")], ["myrepo"])
        self.assertIn("uart-fix", [c[0] for c in self.complete("review", "--project", "myrepo", "u")])
        self.assertEqual([c[0] for c in self.complete("storage", "")], ["clean", "move", "docker-move"])
        self.assertEqual(self.complete("share", ""), [[":dirs", "", ""]])
        self.assertIn("install", [c[0] for c in self.complete("completion", "")])
        self.assertNotIn("__complete", [c[0] for c in self.complete("")])
        # zsh gets everything and does its own matching.
        self.assertIn("pull", [c[0] for c in self.complete("re", shell="zsh")])

    def test_bash_script(self):
        bindir = self.s.root / "bin"
        bindir.mkdir()
        (bindir / "agent").write_text(f'#!/bin/sh\nexec python3 {CLI} "$@"\n')
        (bindir / "agent").chmod(0o755)
        script = self.s.run("python3", CLI, "completion", "bash").stdout
        probe = script + '\nCOMP_WORDS=(agent merge u); COMP_CWORD=2; _agent; printf "%s\\n" "${COMPREPLY[@]}"\n'
        import os
        out = self.s.run("bash", "-c", probe, cwd=self.user, PATH=f"{bindir}:{os.environ['PATH']}").stdout
        self.assertEqual(out.split(), ["uart-fix"])

    def test_install(self):
        self.s.run("python3", CLI, "completion", "install", "--shell", "zsh", cwd=self.user,
                   XDG_DATA_HOME="", ZDOTDIR="")
        target = self.s.home / ".local" / "share" / "agent" / "completion.zsh"
        self.assertIn("compdef _agent agent", target.read_text())
        rc = (self.s.home / ".zshrc").read_text()
        self.assertIn(str(target), rc)
        self.s.run("python3", CLI, "completion", "install", "--shell", "zsh", cwd=self.user,
                   XDG_DATA_HOME="", ZDOTDIR="")
        self.assertEqual((self.s.home / ".zshrc").read_text(), rc)  # added once


@unittest.skipUnless(importlib.util.find_spec("rich") is not None, "Rich not installed for this Python")
class PickHomeTest(unittest.TestCase):
    def test_suggests_disk_with_most_space(self):
        spec = importlib.util.spec_from_file_location("agent_cli", CLI)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        cands = m.candidate_locations()
        self.assertTrue(cands)
        m.docker_root = lambda: None
        m.ask = lambda q, default="": default
        best = max(cands, key=lambda c: c["free"])["path"]
        self.assertEqual(m.pick_home(), best)


if __name__ == "__main__":
    unittest.main()
