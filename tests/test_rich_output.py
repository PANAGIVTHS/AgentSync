import importlib.util
import sys
import unittest

from helpers import CLI, Sandbox

HAVE_RICH = importlib.util.find_spec("rich") is not None


@unittest.skipUnless(HAVE_RICH, "Rich not installed for this Python (install.sh puts it in the CLI's venv)")
class RichOutputTest(unittest.TestCase):
    """The same commands, with Rich enabled: output must still carry the facts."""

    def setUp(self):
        self.s = Sandbox()
        self.user = self.s.repo("myrepo", {"src/a.txt": "one\n"})

    def tearDown(self):
        self.s.cleanup()

    def agent(self, *args, check=True):
        return self.s.run(sys.executable, CLI, *args, cwd=self.user, check=check, AGENT_PLAIN="0", NO_COLOR="1", COLUMNS="160")

    def test_tables_and_panels(self):
        self.agent("share", "--yes")
        work = self.s.root / "Agent" / "work" / "myrepo"
        self.s.git(work, "switch", "-q", "-c", "agent/odd-name")
        (work / "src" / "a.txt").write_text("two\n")
        self.s.git(work, "commit", "-q", "-am", "agent: change [bold]not markup[/bold]")
        handoff = self.s.root / "Agent" / "handoffs" / "myrepo" / "odd-name.md"
        handoff.write_text("# task\n[red]Fixed it.[/red]\n")

        listing = self.agent("list").stdout
        self.assertIn("myrepo", listing)
        self.assertIn("Shared repos", listing)

        pulled = self.agent("pull").stdout
        self.assertIn("agent/odd-name", pulled)
        self.assertIn("[bold]not markup[/bold]", pulled)

        review = self.agent("review", "odd-name", "--no-open").stdout
        self.assertIn("Handoff", review)
        self.assertIn("[red]Fixed it.[/red]", review)

        self.assertIn("unknown project", self.agent("review", "x", "--project", "nope", check=False).stderr)

    def test_help_renders(self):
        out = self.agent("--help").stdout
        self.assertIn("merge", out)
        merge_help = self.agent("merge", "--help").stdout
        self.assertIn("--commit", merge_help)
        self.assertNotIn("--edit", merge_help)  # old spelling still works, but isn't advertised


if __name__ == "__main__":
    unittest.main()
