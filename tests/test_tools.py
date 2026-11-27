import hashlib
import json
import os
import re
import shutil
import unittest

from helpers import BIN, ROOT, Sandbox

TOOLS = str(BIN / "tools")
ENV_SH = ROOT / "tools-template" / "env.sh"

FAKE_CURL = """#!/bin/sh
# Writes $FAKE_BODY to the -o file; logs its arguments.
echo "$*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
printf '%s' "$FAKE_BODY" > "$out"
"""


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.root = self.s.root / "tools"
        self.root.mkdir()
        shutil.copy(ENV_SH, self.root / "env.sh")
        self.bin = self.s.root / "fakebin"
        self.bin.mkdir()
        (self.bin / "curl").write_text(FAKE_CURL)
        (self.bin / "curl").chmod(0o755)
        self.log = self.s.root / "curl.log"

    def tearDown(self):
        self.s.cleanup()

    def tools(self, *args, check=True, **env):
        env = {"AGENT_TOOLS": str(self.root), "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_LOG": str(self.log), **env}
        return self.s.run(TOOLS, *args, check=check, **env)

    def manifest(self):
        return json.loads((self.root / "manifest.json").read_text())["tools"]

    def test_fetch_verifies_checksum(self):
        body = "toolchain bytes"
        good = hashlib.sha256(body.encode()).hexdigest()
        out = self.tools("fetch", "https://example.com/rel/cad.tgz", "--sha256", good, FAKE_BODY=body).stdout
        self.assertIn("saved /tools/downloads/cad.tgz", out)
        self.assertIn("--proto =https", self.log.read_text())
        self.assertEqual((self.root / "downloads" / "cad.tgz").read_text(), body)

        bad = self.tools("fetch", "https://example.com/rel/evil.tgz", "--sha256", "0" * 64,
                         FAKE_BODY="tampered", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("checksum mismatch", bad.stderr)
        self.assertEqual([p.name for p in (self.root / "downloads").iterdir()], ["cad.tgz"])  # nothing left behind

        self.assertIn("only over https", self.tools("fetch", "http://example.com/x", "--sha256", good,
                                                     check=False).stderr)
        self.assertIn("--no-checksum", self.tools("fetch", "https://example.com/x", check=False).stderr)

    def test_record_list_check_rm(self):
        tool = self.root / "opt" / "oss-cad-suite" / "bin"
        tool.mkdir(parents=True)
        (tool / "verilator").write_text("#!/bin/sh\n")
        (self.root / "bin").mkdir(exist_ok=True)
        os.symlink("/tools/opt/oss-cad-suite/bin/verilator", self.root / "bin" / "verilator")
        out = self.tools("record", "oss-cad-suite", "--version", "2026-11-01", "--source",
                         "https://github.com/YosysHQ/oss-cad-suite-build/releases/x.tgz", "--sha256", "a" * 64,
                         "--path", "/tools/opt/oss-cad-suite", "--path", "bin/verilator", "--project", "gpgpu").stdout
        self.assertIn("recorded oss-cad-suite", out)
        entry = self.manifest()[0]
        self.assertEqual(entry["paths"], ["/tools/bin/verilator", "/tools/opt/oss-cad-suite"])
        self.assertEqual(entry["project"], "gpgpu")
        self.assertIn("oss-cad-suite 2026-11-01 [gpgpu]", self.tools("list").stdout)
        self.assertIn("ok: 1 tools", self.tools("check").stdout)

        # Refusals: outside /tools, missing files, no checksum for a download, unknown sources.
        self.assertIn("isn't inside /tools", self.tools("record", "x", "--version", "1", "--source", "local",
                                                        "--note", "n", "--path", "/home/agent/x", check=False).stderr)
        self.assertIn("isn't inside /tools", self.tools("record", "x", "--version", "1", "--source", "local",
                                                        "--note", "n", "--path", "/tools/../etc", check=False).stderr)
        self.assertIn("doesn't exist", self.tools("record", "x", "--version", "1", "--source", "pip:x",
                                                  "--path", "/tools/opt/nope", check=False).stderr)
        self.assertIn("needs --sha256", self.tools("record", "x", "--version", "1", "--source", "https://e.com/x",
                                                   "--path", "/tools/opt/oss-cad-suite", check=False).stderr)
        self.assertIn("--source must be", self.tools("record", "x", "--version", "1", "--source", "http://e.com",
                                                     "--path", "/tools/opt/oss-cad-suite", check=False).stderr)

        # Something unrecorded, a link out of the folder, a setuid file.
        (self.root / "opt" / "mystery").mkdir()
        os.symlink("/home/agent/.ssh", self.root / "bin" / "keys")
        os.symlink("../../../../home/you", self.root / "opt" / "oss-cad-suite" / "up")
        os.symlink("/usr/bin/python3", self.root / "bin" / "py")  # fine: a system path in the container
        suid = self.root / "opt" / "oss-cad-suite" / "bin" / "su-ish"
        suid.write_text("x")
        suid.chmod(0o4755)
        p = self.tools("check", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("not in the manifest", p.stdout)
        self.assertIn("/tools/opt/mystery", p.stdout)
        self.assertIn("/tools/bin/keys -> /home/agent/.ssh", p.stdout)
        self.assertIn("/tools/opt/oss-cad-suite/up -> ../../../../home/you", p.stdout)
        self.assertNotIn("/tools/bin/py ->", p.stdout)
        self.assertIn("setuid/setgid file", p.stdout)

        self.tools("rm", "oss-cad-suite", "--delete")
        self.assertEqual(self.manifest(), [])
        self.assertFalse((self.root / "opt" / "oss-cad-suite").exists())
        self.assertFalse(os.path.lexists(self.root / "bin" / "verilator"))

    def test_unverified_and_managed_sources(self):
        (self.root / "python").mkdir()
        self.tools("record", "uv", "--version", "0.9", "--source", "pip:uv", "--path", "/tools/python")
        (self.root / "opt" / "thing").mkdir(parents=True)
        out = self.tools("record", "thing", "--version", "1", "--source", "https://e.com/t.tgz",
                         "--no-checksum", "no checksum published", "--path", "/tools/opt/thing").stdout
        self.assertIn("UNVERIFIED", out)
        p = self.tools("check")  # warnings only: exit 0
        self.assertIn("installed without a checksum: thing", p.stdout)
        self.assertNotIn("uv", p.stdout)

    def test_manifest_link_is_refused(self):
        secret = self.s.root / "secret.json"
        secret.write_text('{"tools": [{"name": "x"}]}')
        os.symlink(secret, self.root / "manifest.json")
        self.assertIn("isn't a regular file", self.tools("list", check=False).stderr)


class EnvShTest(unittest.TestCase):
    """What every Bash command in the container sees (BASH_ENV=/tools/env.sh)."""

    def setUp(self):
        self.s = Sandbox()
        self.root = self.s.root / "tools"
        for d in ("bin", "env.d", "jdks"):
            (self.root / d).mkdir(parents=True)
        shutil.copy(ENV_SH, self.root / "env.sh")

    def tearDown(self):
        self.s.cleanup()

    def bash(self, script, login=False, path=None):
        env = {"BASH_ENV": str(self.root / "env.sh"), "AGENT_TOOLS": str(self.root), "JAVA_HOME": ""}
        if path:
            env["PATH"] = path
        return self.s.run("bash", "-lc" if login else "-c", script, **env)

    def test_quiet_and_snippets_apply_on_the_next_command(self):
        self.assertEqual(self.bash("true").stdout + self.bash("true").stderr, "")
        (self.root / "env.d" / "cad.sh").write_text('export CAD_HOME="$AGENT_TOOLS/opt/cad"\n')
        self.assertEqual(self.bash('printf %s "$CAD_HOME"').stdout, f"{self.root}/opt/cad")
        self.assertEqual(self.bash('printf %s "$CAD_HOME"', login=True).stdout, f"{self.root}/opt/cad")

    def test_java_home_follows_current(self):
        self.assertEqual(self.bash('printf %s "${JAVA_HOME:-unset}"').stdout, "unset")
        java = self.root / "jdks" / "jdk-21" / "bin"
        java.mkdir(parents=True)
        (java / "java").write_text("#!/bin/sh\n")
        (java / "java").chmod(0o755)
        os.symlink("jdk-21", self.root / "jdks" / "current")
        self.assertEqual(self.bash('printf %s "$JAVA_HOME"').stdout, f"{self.root}/jdks/current")

    def test_new_program_works_with_a_frozen_path(self):
        # Claude Code restores the PATH it saw when the chat started before each command.
        # /tools/bin is in that PATH from the image, so a program added mid-chat is found.
        frozen = f"/usr/bin:/bin:{self.root}/bin"
        tool = self.root / "bin" / "newtool"
        tool.write_text("#!/bin/sh\necho hi\n")
        tool.chmod(0o755)
        self.assertEqual(self.bash(f'export PATH="{frozen}"; newtool').stdout.strip(), "hi")

    def test_image_puts_tools_after_the_system_and_sets_bash_env(self):
        dockerfile = (ROOT / "container" / "Dockerfile").read_text()
        path = re.search(r"ENV PATH=(\S+)", dockerfile).group(1).split(":")
        self.assertLess(path.index("$PATH"), path.index("/tools/bin"))  # can't shadow git, python3, …
        for needed in ("BASH_ENV=/tools/env.sh", "PYTHONUSERBASE=/tools/python", "NPM_CONFIG_PREFIX=/tools/npm",
                       "UV_TOOL_BIN_DIR=/tools/bin", "python3-pip", "python3-venv"):
            self.assertIn(needed, dockerfile)
        self.assertIn("${AGENT_TOOLS_DIR}:/tools", (ROOT / "container" / "compose.yaml").read_text())


if __name__ == "__main__":
    unittest.main()
