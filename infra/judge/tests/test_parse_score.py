"""Tests of infra/judge/parse_score.py. Run: python3 -m unittest discover -s infra/judge/tests -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "parse_score.py")
sys.path.insert(0, os.path.dirname(HERE))
import parse_score as ps  # noqa: E402

VERDICT = "# Judge Result - Strict Mode\n\n## Score\n%s\n\n## Figure Analysis\n...\n## Issues Found\n- [NON-CRITICAL] x\n## Reason\ny\n"


class TestParseScore(unittest.TestCase):
    def test_scores(self):
        cases = {"1": 1.0, "0": 0.0, "1.0": 1.0, "0.5": 0.5, "  1  ": 1.0}
        for text, want in cases.items():
            self.assertEqual(ps.score_of_text(VERDICT % text), want, text)
        self.assertEqual(ps.score_of_text("## Score\n\n\n1\n"), 1.0, "blank lines before the number are allowed")
        for bad in ("## Score\n[0 or 1]\n", "## Score: 1\n", "Score\n1\n", "### Score\n1\n", "## Score\n.\n",
                    "## Score\n**1**\n", "no score at all"):
            self.assertIsNone(ps.score_of_text(bad), bad)

    def test_strict_success(self):
        self.assertEqual(ps.verdict(1.0), "PASS")
        for s in (0.0, 0.5, 0.99, 2.0):
            self.assertEqual(ps.verdict(s), "FAIL", s)
        self.assertEqual(ps.verdict(None), "UNPARSED")

    def test_cli(self):
        tmp = tempfile.mkdtemp(prefix="ra_parse_score_")
        try:
            for name, text in (("a", VERDICT % "1"), ("b", VERDICT % "0"), ("c", VERDICT % "0.5")):
                os.makedirs(os.path.join(tmp, name))
                with open(os.path.join(tmp, name, "judge_result.md"), "w") as f:
                    f.write(text)
            with open(os.path.join(tmp, "a", "judge_result.md.bak"), "w") as f:
                f.write(VERDICT % "0")
            r = subprocess.run([sys.executable, TOOL, "--json", tmp], stdout=subprocess.PIPE)
            doc = json.loads(r.stdout)
            self.assertEqual((doc["passed"], doc["failed"], doc["unparsed"], doc["total"]), (1, 2, 0, 3))
            self.assertEqual(r.returncode, 0)
            one = lambda n: subprocess.run([sys.executable, TOOL, os.path.join(tmp, n, "judge_result.md")],
                                           stdout=subprocess.PIPE).returncode
            self.assertEqual((one("a"), one("b"), one("c")), (0, 1, 1))
            with open(os.path.join(tmp, "a", "judge_result.md"), "w") as f:
                f.write("no score")
            self.assertEqual(one("a"), 2)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
