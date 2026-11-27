import json
import shutil
import unittest

from helpers import BIN, ROOT, Sandbox

VAULT = str(BIN / "vault")


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.vault = self.s.root / "vault"
        shutil.copytree(ROOT / "vault-template", self.vault)
        self.s.git(self.vault, "init", "-q")
        self.s.git(self.vault, "add", "-A")
        self.s.git(self.vault, "commit", "-q", "-m", "create")

    def tearDown(self):
        self.s.cleanup()

    def v(self, *args, check=True, input=None, cwd=None):
        return self.s.run(VAULT, *args, check=check, input=input, cwd=cwd, AGENT_VAULT=str(self.vault))

    def add(self, title, tags, body, project=None, check=True):
        args = ["add", title, "--tags", tags, "--stdin"] + (["--project", project] if project else [])
        return self.v(*args, input=body, check=check)

    def test_add_find_show(self):
        self.add("UART RX framing at high baud", "uart,timing,rx", "Sample at 16x; 921600 needs 14.7456 MHz.", "fpga")
        self.add("Python venv tips", "python,venv", "Use python -m venv .venv.")
        note = self.vault / "projects" / "fpga" / "uart-rx-framing-at-high-baud.md"
        text = note.read_text()
        self.assertIn("tags: [uart, timing, rx]", text)
        self.assertIn("project: fpga", text)
        out = self.v("find", "uart", "framing", "errors").stdout
        self.assertTrue(out.startswith("projects/fpga/uart-rx-framing-at-high-baud.md"), out)
        self.assertNotIn("venv", out)
        self.assertIn("Sample at 16x", self.v("show", "uart-rx-framing-at-high-baud").stdout)
        index = (self.vault / "Index.md").read_text()
        self.assertIn("[[projects/fpga/uart-rx-framing-at-high-baud|UART RX framing at high baud]] #uart", index)
        log = self.s.git(self.vault, "log", "--oneline").stdout
        self.assertIn("vault: add projects/fpga/uart-rx-framing-at-high-baud.md", log)

    def test_find_without_match_lists_known_tags(self):
        self.add("Python venv tips", "python,venv", "Use python -m venv .venv.")
        self.assertIn("Known tags: python, venv", self.v("find", "kubernetes").stdout)

    def test_duplicate_detection(self):
        self.add("UART baud", "uart,baud", "a")
        p = self.add("UART baud rates again", "uart,baud,rx", "b", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("topics/uart-baud.md", p.stdout)
        self.v("add", "UART baud rates again", "--tags", "uart,baud,rx", "--body", "b", "--force")

    def test_update_and_rm(self):
        self.add("Build steps", "build,make", "Run make.")
        self.v("update", "build-steps", "--append", "Then make test.", "--add-tags", "test", "--verified", "abc123")
        text = (self.vault / "topics" / "build-steps.md").read_text()
        self.assertIn("Run make.\nThen make test.", text)
        self.assertIn("tags: [build, make, test]", text)
        self.assertIn("verified: abc123", text)
        self.v("rm", "topics/build-steps.md")
        self.assertFalse((self.vault / "topics" / "build-steps.md").exists())
        self.assertIn("_No notes yet._", (self.vault / "Index.md").read_text())

    def test_reads_obsidian_style_list_frontmatter(self):
        (self.vault / "topics" / "handmade.md").write_text(
            "---\ntags:\n  - Cocotb\n  - '#sim'\naliases: [testbench]\n---\n# Cocotb basics\nUse make SIM=icarus.\n")
        out = self.v("find", "testbench", "cocotb").stdout
        self.assertIn("topics/handmade.md · Cocotb basics [cocotb, sim]", out)

    def test_suggest_hook_is_silent_unless_strong_match(self):
        self.add("UART RX framing", "uart,rx", "details", "fpga")
        hook = json.dumps({"prompt": "fix the uart framing errors please", "cwd": "/work/fpga"})
        out = self.v("suggest", "--hook", input=hook).stdout
        self.assertIn("projects/fpga/uart-rx-framing.md", out)
        quiet = json.dumps({"prompt": "rename the readme heading", "cwd": "/work/fpga"})
        self.assertEqual(self.v("suggest", "--hook", input=quiet).stdout, "")
        self.assertEqual(self.v("suggest", "--hook", input="garbage").stdout, "")

    def test_lint(self):
        self.add("A", "x,y", "a")
        self.v("add", "B", "--tags", "x,y", "--body", "b", "--force")
        self.assertIn("nearly the same tags", self.v("lint").stdout)

    def test_hints_stay_in_the_current_repo_and_need_real_evidence(self):
        self.add("GPGPU RTL test suites", "gpgpu,verilator,rtl,tests", "Run ./gpgpu rtl all.", "gpu")
        self.add("CLI testing gotchas", "tests,rich,cli", "Rich isn't on system python.", "agentsync")
        self.add("Verilator lint flags", "verilator,lint", "Use -Wall.")  # general: topics/

        def hint(prompt, repo):
            return self.v("suggest", "--hook", input=json.dumps({"prompt": prompt, "cwd": f"/work/{repo}"})).stdout

        out = hint("split the SM into WEU and LSU and run the rtl tests", "gpu")
        self.assertIn("gpgpu-rtl-test-suites", out)
        self.assertNotIn("cli-testing-gotchas", out)  # another repo's note
        self.assertEqual(hint("run the tests", "agentsync"), "")  # a generic word alone isn't enough
        self.assertIn("cli-testing-gotchas", hint("fix the rich completion test", "agentsync"))
        self.assertIn("verilator-lint-flags", hint("verilator lint warnings", "agentsync"))  # general notes everywhere
        self.assertNotIn("gpgpu-rtl", hint("verilator lint warnings", "agentsync"))

    def test_saving_points_out_what_doesnt_belong(self):
        out = self.add("Plotting", "matplotlib,plot", "matplotlib isn't installed in the agent container: "
                       "pip install --target <scratch>/py matplotlib.\nResults: 175 MFLOPS at n=2048.\n"
                       "Contact jason@example.com, linkedin.com/in/someone.", "ece445").stdout
        self.assertIn("check: container setup", out)
        self.assertIn("check: looks like results of one run", out)
        self.assertIn("check: personal details", out)
        clean = self.add("Make targets", "make,targets", "- make run SIZES=... RUNS=...\n- clone git@github.com:x/y",
                         "ece445").stdout
        self.assertNotIn("check:", clean)
        self.assertIn("ece445/plotting.md: container setup", self.v("lint").stdout)

    def test_lint_suggests_a_topic_for_knowledge_shared_across_repos(self):
        for repo in ("a", "b", "c"):
            self.add(f"Playwright in {repo}", f"playwright,{repo}-site", "Use the bundled Chromium.", repo)
        self.assertIn("tag 'playwright' in 3 repos", self.v("lint").stdout)
        self.add("Playwright", "playwright,chromium", "Use the bundled Chromium.", check=False)
        self.assertNotIn("tag 'playwright'", self.v("lint").stdout)

    def test_index_lists_recent_notes(self):
        self.add("UART RX framing", "uart,rx", "details", "fpga")
        index = (self.vault / "Index.md").read_text()
        self.assertIn("## Recently updated", index)
        self.assertIn("[[projects/fpga/uart-rx-framing|UART RX framing]]", index)

    def test_path_escape_refused(self):
        p = self.v("show", "../../etc/passwd", check=False)
        self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
