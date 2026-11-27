import importlib.util
import re
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

    def test_help_box_edges_line_up_in_colour(self):
        # rich-argparse colours the usage with escape codes; they must not count as text width.
        out = self.s.run(sys.executable, CLI, "pull", "--help", cwd=self.user,
                         AGENT_PLAIN="0", FORCE_COLOR="1", COLUMNS="80").stdout
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        widths = {len(line) for line in plain.splitlines() if line.startswith(("│", "╭", "╰"))}
        self.assertEqual(len(widths), 1, plain)
        self.assertNotIn("Usage: ", plain)


if __name__ == "__main__":
    unittest.main()
