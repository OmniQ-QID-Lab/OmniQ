#!/usr/bin/env python3
"""Read the score of judge verdicts (judge_result.md) the way the paper counted them.

Usage:
    parse_score.py PATH [PATH ...] [--json]
PATH is a judge_result.md file or a folder searched recursively for files named judge_result.md (earlier verdicts
such as judge_result.md.bak are not read).

The score is the number on the line after the "## Score" heading, matched with the regular expression
    ^## Score\\s*\\n\\s*([0-9.]+)        (multiline)
Strict success: score == 1. Any other score, including a partial score such as 0.5, is a fail. A verdict without a
readable score is reported as unparsed.

Output: one line per verdict, "<score or ->  <PASS|FAIL|UNPARSED>  <path>", then a summary line; with --json, one JSON
document. Exit status for a single file: 0 pass, 1 fail, 2 unparsed. For several files: 0 when every verdict was
parsed, 2 otherwise.
"""
import argparse
import json
import os
import re
import sys

SCORE_RE = re.compile(r"^## Score\s*\n\s*([0-9.]+)", re.M)


def score_of_text(text):
    """The score as a float, or None when the verdict has no readable score."""
    m = SCORE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def score_of_file(path):
    try:
        with open(path, errors="replace") as f:
            return score_of_text(f.read())
    except (IOError, OSError):
        return None


def verdict(score):
    if score is None:
        return "UNPARSED"
    return "PASS" if score == 1.0 else "FAIL"


def find(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for dp, dn, fn in os.walk(p):
                dn.sort()
                if "judge_result.md" in fn:
                    out.append(os.path.join(dp, "judge_result.md"))
        else:
            out.append(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score of judge_result.md files (strict: pass only when score == 1).")
    ap.add_argument("paths", nargs="+", metavar="PATH")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    files = find(a.paths)
    rows = [{"path": f, "score": score_of_file(f)} for f in files]
    for r in rows:
        r["result"] = verdict(r["score"])
    n_pass = sum(1 for r in rows if r["result"] == "PASS")
    n_fail = sum(1 for r in rows if r["result"] == "FAIL")
    n_unp = sum(1 for r in rows if r["result"] == "UNPARSED")
    if a.json:
        json.dump({"verdicts": rows, "passed": n_pass, "failed": n_fail, "unparsed": n_unp, "total": len(rows)},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for r in rows:
            sys.stdout.write("%-5s  %-8s  %s\n" % ("-" if r["score"] is None else ("%g" % r["score"]), r["result"],
                                                  r["path"]))
        sys.stdout.write("passed %d of %d (failed %d, unparsed %d)\n" % (n_pass, len(rows), n_fail, n_unp))
    if not rows:
        return 2
    if len(rows) == 1:
        return {"PASS": 0, "FAIL": 1, "UNPARSED": 2}[rows[0]["result"]]
    return 0 if n_unp == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
