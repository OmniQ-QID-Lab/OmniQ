"""Tests of infra/judge/judge.sh and stage_run.py with a fake `claude` (offline; no container is started).
Run: python3 -m unittest discover -s infra/judge/tests -v
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGE_DIR = os.path.dirname(HERE)
JUDGE_SH = os.path.join(JUDGE_DIR, "judge.sh")
STAGE = os.path.join(JUDGE_DIR, "stage_run.py")
KEY = "judgekey-7"

FAKE_CLAUDE = r"""#!/bin/sh
if [ "$1" = "--version" ]; then echo "9.9.9 (fake)"; exit 0; fi
[ -e judge_result.md ] && echo stale > "$FAKE_DIR/stale"
[ -n "$ANTHROPIC_API_KEY" ] && echo present > "$FAKE_DIR/key"
printf '%s\n' "$@" > "$FAKE_DIR/args"
ls -a > "$FAKE_DIR/ls"
printf '# Judge Result - Strict Mode\n\n## Score\n%s\n\n## Figure Analysis\nok\n## Issues Found\n- [NON-CRITICAL] none\n## Reason\nfine\n' "$FAKE_SCORE" > judge_result.md
echo '{"type":"result","subtype":"success"}'
"""


def tree_digest(root):
    h = hashlib.sha256()
    for dp, dn, fn in sorted(os.walk(root)):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, root).encode())
            if os.path.islink(p):
                h.update(os.readlink(p).encode())
            else:
                with open(p, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


def write(path, text="x", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


class JudgeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra_judge_test_")
        t = self.tmp
        self.run = os.path.join(t, "runs", "demo_p7")
        write(os.path.join(self.run, "report.md"), "report")
        write(os.path.join(self.run, "reproduced_figure.png"), "png")
        write(os.path.join(self.run, "ENV.md"), "env of the run")
        write(os.path.join(self.run, "judge_result.md"), "## Score\n0\n")
        write(os.path.join(self.run, "judge_result.md.bak_old"), "## Score\n0\n")
        write(os.path.join(self.run, "JUDGE.md"), "stale criteria")
        write(os.path.join(self.run, "_pece", "ledger.jsonl"), "{}\n")
        write(os.path.join(self.run, "_pece", "snap", "x.json"), "{}")
        write(os.path.join(self.run, ".dsh_ra3", "calls", "01_plan", "prompt.txt"), "prompt")
        write(os.path.join(self.run, "u1", "venv", "pyvenv.cfg"), "home = /usr")
        write(os.path.join(self.run, "u1", "env2", "pyvenv.cfg"), "home = /usr")
        write(os.path.join(self.run, "u1", "lib", "pkg-1.0.dist-info", "METADATA"), "Name: pkg")
        write(os.path.join(self.run, "u1", "lib", "pkg", "__init__.py"), "")
        write(os.path.join(self.run, "u1", "library_notes", "notes.md"), "kept")
        write(os.path.join(self.run, "u1", "code.py"), "print(1)")
        write(os.path.join(self.run, "u1", "data", "judge_result.md"), "a unit file with that name is kept")
        write(os.path.join(self.run, ".claude", "settings.json"), "{}")
        os.symlink("code.py", os.path.join(self.run, "u1", "link.py"))
        self.judge_md = os.path.join(t, "task", "JUDGE.md")
        write(self.judge_md, "# Judge: STRICT MODE\n## Output Format\n## Score\n[0 or 1]\n")
        self.paper = os.path.join(t, "papers", "source.pdf")
        write(self.paper, "%PDF-1.4 fake")
        self.key_file = os.path.join(t, "key")
        write(self.key_file, KEY + "\n", 0o600)
        self.fake_dir = os.path.join(t, "fake")
        os.makedirs(self.fake_dir)
        self.fake = os.path.join(self.fake_dir, "claude")
        write(self.fake, FAKE_CLAUDE, 0o755)
        self.out = os.path.join(t, "verdicts")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def judge(self, *extra, score="1", env_extra=None):
        env = dict(os.environ, RA_JUDGE_CLAUDE=self.fake, FAKE_DIR=self.fake_dir, FAKE_SCORE=score)
        env.update(env_extra or {})
        args = ["bash", JUDGE_SH, "--run", self.run, "--judge-md", self.judge_md, "--paper", self.paper,
                "--paper-num", "7", "--out", self.out, "--key-file", self.key_file] + list(extra)
        return subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)


class TestJudgeHost(JudgeTestBase):
    def test_end_to_end_with_fake_claude(self):
        before = tree_digest(self.run)
        r = self.judge("--engine", "host")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(tree_digest(self.run), before, "the original run folder is never changed")
        j = os.path.join(self.out, "demo_p7")
        ws = os.path.join(j, "workspace")
        with open(os.path.join(j, "judge_result.md")) as f:
            verdict = f.read()
        self.assertIn("## Score\n1\n", verdict)
        self.assertIn("<!-- judge: model=claude-opus-4-8 cli=9.9.9 (fake) effort=high engine=host", verdict)
        self.assertIn("(an equivalent, not the original)", verdict)
        with open(os.path.join(j, "judge_meta.json")) as f:
            meta = json.load(f)
        self.assertEqual((meta["score"], meta["strict_pass"], meta["prompt_is_original"], meta["judge_model"],
                          meta["exit_code"], meta["allowed_hosts"]),
                         (1.0, True, False, "claude-opus-4-8", 0, ["api.anthropic.com"]))
        # what the judge saw
        self.assertFalse(os.path.exists(os.path.join(self.fake_dir, "stale")), "no earlier verdict in the judged copy")
        with open(os.path.join(self.fake_dir, "ls")) as f:
            listing = f.read().split()
        self.assertNotIn(".dsh_ra3", listing)
        self.assertNotIn(".claude", listing)
        self.assertNotIn("judge_result.md.bak_old", listing)
        with open(os.path.join(ws, "JUDGE.md")) as f:
            self.assertIn("STRICT MODE", f.read())
        with open(os.path.join(ws, "paper_7.pdf")) as f:
            self.assertEqual(f.read(), "%PDF-1.4 fake")
        with open(os.path.join(ws, "ENV.md")) as f:
            self.assertEqual(f.read(), "env of the run", "the run's own ENV.md is kept")
        for gone in ("_pece/snap", "u1/venv", "u1/env2", "u1/lib"):
            self.assertFalse(os.path.exists(os.path.join(ws, gone)), gone)
        for kept in ("u1/library_notes/notes.md", "u1/code.py", "u1/data/judge_result.md", "_pece/ledger.jsonl"):
            self.assertTrue(os.path.exists(os.path.join(ws, kept)), kept)
        self.assertTrue(os.path.islink(os.path.join(ws, "u1", "link.py")))
        with open(os.path.join(j, "staged_excluded.tsv")) as f:
            excluded = f.read()
        for item in (".dsh_ra3/", "_pece/snap/", "u1/venv/", "u1/env2/", "u1/lib/", ".claude/", "judge_result.md",
                     "JUDGE.md", "judge_result.md.bak_old"):
            self.assertIn(item + "\t", excluded)
        # the key
        with open(os.path.join(self.fake_dir, "key")) as f:
            self.assertEqual(f.read().strip(), "present")
        self.assertFalse(os.path.exists(os.path.join(j, ".judge.env")), "the env file is removed")
        for name in os.listdir(j):
            p = os.path.join(j, name)
            if os.path.isfile(p):
                with open(p, errors="replace") as f:
                    self.assertNotIn(KEY, f.read(), name)
        self.assertNotIn(KEY.encode(), r.stdout + r.stderr)
        # the prompt
        with open(os.path.join(JUDGE_DIR, "JUDGE_PROMPT.md")) as f:
            md = f.read()
        prompt = md.split("<!-- BEGIN PROMPT -->\n", 1)[1].split("\n<!-- END PROMPT -->", 1)[0]
        with open(os.path.join(self.fake_dir, "args")) as f:
            args = f.read().split("\n")
        self.assertEqual(args[0:2], ["-p", prompt.split("\n")[0]])
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn(b"PASS", r.stdout)

    def test_failing_verdict_still_exit_zero(self):
        r = self.judge("--engine", "host", score="0")
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"FAIL", r.stdout)
        with open(os.path.join(self.out, "demo_p7", "judge_meta.json")) as f:
            self.assertFalse(json.load(f)["strict_pass"])

    def test_refusals(self):
        self.assertEqual(self.judge("--engine", "host").returncode, 0)
        r = self.judge("--engine", "host")
        self.assertEqual(r.returncode, 2, "an existing verdict folder is never overwritten")
        os.remove(self.paper)
        self.assertEqual(self.judge("--engine", "host").returncode, 2)

    def test_missing_key(self):
        os.remove(self.key_file)
        r = self.judge("--engine", "host")
        self.assertEqual(r.returncode, 2)


class TestJudgeDryRun(JudgeTestBase):
    def test_docker_command(self):
        r = self.judge("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        self.assertIn("docker network create --internal ra-judge-", out)
        self.assertIn("--allow api.anthropic.com", out)
        self.assertIn("--cpus 4 --memory 16g", out)
        self.assertIn("timeout -s TERM -k 60 3600", out)
        self.assertIn("residual-agents-judge:1.0.0", out)
        self.assertIn("\\<prompt\\ of\\ JUDGE_PROMPT.md\\>", out)
        self.assertNotIn(KEY, out)

    def test_apptainer_command(self):
        r = self.judge("--dry-run", "--engine", "apptainer", "--image", "/images/judge.sif", "--allow-host", "api.example.com")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        self.assertIn("apptainer exec --containall --cleanenv", out)
        self.assertIn("--allow api.example.com", out)
        self.assertNotIn("api.anthropic.com", out)


class TestStageRun(unittest.TestCase):
    def test_size_limit_and_existing_destination(self):
        tmp = tempfile.mkdtemp(prefix="ra_stage_test_")
        try:
            src = os.path.join(tmp, "run")
            write(os.path.join(src, "big.bin"), "x" * 300000)
            write(os.path.join(src, "small.txt"), "y")
            os.chmod(os.path.join(src, "small.txt"), stat.S_IRUSR)
            dest = os.path.join(tmp, "dest")
            r = subprocess.run([sys.executable, STAGE, src, dest, "--max-mb", "0.1", "--excluded",
                                os.path.join(tmp, "ex.tsv")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertFalse(os.path.exists(os.path.join(dest, "big.bin")))
            self.assertTrue(os.access(os.path.join(dest, "small.txt"), os.W_OK), "copies are writable")
            with open(os.path.join(tmp, "ex.tsv")) as f:
                self.assertIn("big.bin\tlarger than the size limit\t300000", f.read())
            r = subprocess.run([sys.executable, STAGE, src, dest], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(r.returncode, 2)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
