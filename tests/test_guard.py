import json
import subprocess
import unittest

from helpers import BIN


def guard(command):
    p = subprocess.run([str(BIN / "agent-guard")], input=json.dumps({"tool_input": {"command": command}}),
                       text=True, capture_output=True)
    return p.returncode, p.stdout, p.stderr


class GuardTest(unittest.TestCase):
    def test_blocks_push_forms(self):
        for cmd in ("git push", "git push desktop agent/x", "git -C /work/p push",
                    "cd /work/p && git push -f", "GIT_TRACE=1 git push", "bash -c 'git push origin'",
                    "git -c http.extraHeader=x push", "echo hi; git push --force-with-lease",
                    "/usr/bin/git push", "timeout 5 git push"):
            code, out, err = guard(cmd)
            self.assertEqual(code, 2, cmd)
            self.assertIn("never pushes", err)

    def test_allows_everything_else_silently(self):
        for cmd in ("git status", "git commit -m 'push the button'", "echo git push",
                    "git log --grep push", "rg push src/", "git stash push", "ls"):
            code, out, err = guard(cmd)
            self.assertEqual((code, out, err), (0, "", ""), cmd)

    def test_bad_input_is_ignored(self):
        p = subprocess.run([str(BIN / "agent-guard")], input="not json", text=True, capture_output=True)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
