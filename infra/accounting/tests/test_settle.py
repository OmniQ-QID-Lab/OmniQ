"""Tests of infra/accounting/settle_sessions.py on a synthetic dsh session tree (offline).

Run: python3 -m unittest discover -s infra/accounting/tests -v
Optional: RA_TRAJECTORIES_DIR=<folder with published run folders> also checks, for every run found there, that the
ledger re-priced at the paper's rates equals the run's token_usage.json cost.
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import settle_sessions as ss  # noqa: E402

try:
    import zstandard
except ImportError:
    zstandard = None
HAVE_ZSTD = bool(zstandard) or bool(shutil.which("zstd"))
RATES = (0.15, 0.003, 0.60)


def compress(data, frames=1):
    """zstd frames of data (python zstandard, else the zstd program)."""
    step = max(1, -(-len(data) // frames))
    out = b""
    for i in range(0, len(data), step):
        part = data[i:i + step]
        if zstandard:
            out += zstandard.ZstdCompressor().compress(part)
        else:
            out += subprocess.run(["zstd", "-q", "-c"], input=part, stdout=subprocess.PIPE, check=True).stdout
    return out


def U(i, o, c, r):
    return {"inputTokens": i, "outputTokens": o, "cacheReadTokens": c, "reasoningTokens": r,
            "cacheWriteTokens": 0, "totalTokens": i + o + c}


def msg(u, stream=True):
    """A V3 assistant/message: usage in data.usage and repeated by the last usage chunk of data.stream."""
    d = {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}, "usage": u}
    if stream:
        d["stream"] = [{"type": "chunk", "chunk": {"type": "text-delta", "delta": "x"}},
                       {"type": "chunk", "chunk": {"type": "usage", "usage": u}}]
    return {"type": "assistant/message", "seq": 3, "time": 1790000000000, "data": d}


def msg_stream_only(u):
    return {"type": "assistant/message", "data": {"message": {"role": "assistant", "content": []},
                                                  "stream": [{"type": "chunk", "chunk": {"type": "usage", "usage": U(1, 1, 1, 1)}},
                                                             {"type": "chunk", "chunk": {"type": "usage", "usage": u}}]}}


def attempt(u=None):
    st = [{"type": "chunk", "chunk": {"type": "error", "message": "stream closed"}}]
    if u:
        st.append({"type": "chunk", "chunk": {"type": "usage", "usage": u}})
    return {"type": "assistant/attempt", "data": {"stream": st}}


def jsonl(records):
    return ("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n").encode()


HEADER = {"type": "session", "version": 3, "id": "s", "cwd": "/workspace", "createdAt": 1}
# (call folder, session folder, file name, records, frames); usages chosen so that every role has a distinct total
SESSIONS = [
    ("01_plan", "--workspace--/session-aaa", "session.v3.jsonl.zstd",
     [HEADER, msg(U(100, 50, 1000, 20)), msg_stream_only(U(10, 5, 100, 2)), attempt(U(7, 0, 0, 0)), attempt(),
      {"type": "tool/result", "data": {"x": 1}}, "not json", "42", {"type": "assistant/message", "data": "odd"}], 1),
    ("02_exec_u1", "--workspace-u1--/session-bbb", "session.v3.jsonl.zstd",
     [HEADER, msg(U(200, 80, 5000, 40)), msg(U(300, 90, 6000, 50))], 2),
    ("02_exec_u1", "--workspace-u1--/ccc-subagent", "session.v3.jsonl.zstd", [HEADER, msg(U(40, 10, 400, 5))], 1),
    ("03_critic_u1", "--workspace--/session-ddd", "session.jsonl",
     [{"type": "session", "version": 0}, {"type": "assistant/message", "data": {"usage": U(60, 20, 600, 10)}}], 0),
    ("04_report_render", "--workspace--/session-eee", "session.v3.jsonl.zstd", [HEADER, msg(U(30, 30, 300, 3))], 1),
    ("05_scout1_u1", "--workspace-u1--/session-fff", "session.v3.jsonl.zstd", [HEADER, msg(U(5, 5, 50, 1))], 1),
]
EXPECT = {  # role: (input, output, cached, reasoning, usage_calls, retried_attempts)
    "plan": (117, 55, 1100, 22, 2, 2),     # the record whose data is not an object is skipped, not counted
    "exec": (540, 180, 11400, 95, 3, 0),
    "critic": (60, 20, 600, 10, 1, 0),
    "report": (30, 30, 300, 3, 1, 0),
    "scout": (5, 5, 50, 1, 1, 0),
}


def build_run(root, with_sessions=True, plain=False):
    """A run folder with _pece/ledger.jsonl, token_usage.json, report.md and (optionally) .dsh_ra3 sessions."""
    os.makedirs(os.path.join(root, "_pece"))
    if with_sessions:
        for call, sess, name, recs, frames in SESSIONS:
            d = os.path.join(root, ".dsh_ra3", "calls", call, "sessions", sess)
            os.makedirs(d, exist_ok=True)
            data = jsonl(recs)
            if frames and not plain:
                data = compress(data, frames)
            else:
                name = name.replace(".zstd", "")
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        tmp = os.path.join(root, ".dsh_ra3", "calls", "02_exec_u1", "sessions", "--workspace-u1--", "session-bbb",
                           "session.v3.jsonl.zstd.a1b2.tmp")
        with open(tmp, "wb") as f:
            f.write(jsonl([msg(U(99999, 0, 0, 0))]))
        os.makedirs(os.path.join(root, ".dsh_ra3", "calls", "06_plan"))          # a call folder without a session
    ledger = []   # the in-run accounting: the main sessions only (the sub-agent is missing, as in the published runs)
    for tag, u, att in (("plan", (117, 1100, 55, 22), 2), ("exec_u1", (500, 11000, 170, 90), 0),
                        ("critic_u1", (60, 600, 20, 10), 0), ("report_render", (30, 300, 30, 3), 0),
                        ("scout1_u1", (5, 50, 5, 1), 0)):
        ledger.append({"kind": "call", "tag": tag, "tokens": u[0] + u[1] + u[2], "retried_attempts": att,
                       "usage": {"input": u[0], "cached": u[1], "output": u[2], "reasoning": u[3]},
                       "cost_usd": round(ss.price(u[0], u[1], u[2], RATES), 4)})
    with open(os.path.join(root, "_pece", "ledger.jsonl"), "w") as f:
        f.write(json.dumps({"kind": "episode_start"}) + "\n")
        for e in ledger:
            f.write(json.dumps(e) + "\n")
        f.write("an agent's note\n")
    with open(os.path.join(root, "token_usage.json"), "w") as f:
        json.dump({"cost_usd": round(sum(e["cost_usd"] for e in ledger), 4)}, f)
    open(os.path.join(root, "report.md"), "w").close()
    return root


def run_main(args):
    fd, out = tempfile.mkstemp(suffix=".out")
    os.close(fd)
    try:
        rc = ss.main(list(args) + ["--out", out])
        with open(out) as f:
            text = f.read()
    finally:
        os.remove(out)
    return rc, text


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


@unittest.skipUnless(HAVE_ZSTD, "needs the zstd program or the python zstandard package")
class TestSettlement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra_settle_test_")
        self.run = build_run(os.path.join(self.tmp, "demo_p1"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def total(self):
        t = [sum(EXPECT[r][i] for r in EXPECT) for i in range(6)]
        return dict(zip(("in", "out", "cached", "reasoning", "calls", "attempts"), t))

    def test_totals_and_cost(self):
        rc, text = run_main([self.run])
        self.assertEqual(rc, 0)
        r = rows(text)[0]
        t = self.total()
        self.assertEqual((r["run"], r["role"], r["basis"]), ("demo_p1", "all", "sessions"))
        self.assertEqual([int(r[k]) for k in ("inputTokens", "outputTokens", "cacheReadTokens", "reasoningTokens",
                                               "usage_calls", "retried_attempts")],
                         [t["in"], t["out"], t["cached"], t["reasoning"], t["calls"], t["attempts"]])
        self.assertEqual(int(r["tokens"]), t["in"] + t["cached"] + t["out"])
        self.assertAlmostEqual(float(r["cost_usd"]), ss.price(t["in"], t["cached"], t["out"], RATES), places=4)
        self.assertEqual((r["sessions"], r["subagent_sessions"], r["session_files"], r["undecodable_files"], r["done"]),
                         ("6", "1", "6", "0", "1"))

    def test_by_role(self):
        rc, text = run_main([self.run, "--by-role"])
        got = dict((r["role"], r) for r in rows(text))
        for role, (i, o, c, rs, calls, att) in EXPECT.items():
            r = got[role]
            self.assertEqual([int(r[k]) for k in ("inputTokens", "outputTokens", "cacheReadTokens", "reasoningTokens",
                                                   "usage_calls", "retried_attempts")], [i, o, c, rs, calls, att], role)
        self.assertEqual(got["exec"]["subagent_sessions"], "1")
        self.assertEqual(got["other"]["inputTokens"], "0")

    def test_ledger_cross_check(self):
        rc, text = run_main([self.run])
        r = rows(text)[0]
        self.assertEqual(int(r["ledger_calls"]), 5)
        sub = U(40, 10, 400, 5)
        self.assertEqual(int(r["tokens"]) - int(r["ledger_tokens"]),
                         sub["inputTokens"] + sub["cacheReadTokens"] + sub["outputTokens"],
                         "the settlement exceeds the ledger by exactly the sub-agent session")
        self.assertAlmostEqual(float(r["ledger_cost_usd"]), float(r["token_usage_cost_usd"]), places=3)

    def test_zip_of_dataset_task_folder(self):
        """The dataset layout: tasks/<task>/run/ plus transcripts.zip with decoded session.jsonl members."""
        task = os.path.join(self.tmp, "paper-01_p1")
        plain = build_run(os.path.join(self.tmp, "plain_src"), plain=True)
        shutil.copytree(os.path.join(plain, "_pece"), os.path.join(task, "run", "_pece"))
        with zipfile.ZipFile(os.path.join(task, "transcripts.zip"), "w") as z:
            for dp, dn, fn in os.walk(os.path.join(plain, ".dsh_ra3")):
                for n in fn:
                    full = os.path.join(dp, n)
                    z.write(full, os.path.relpath(full, plain))
        a = rows(run_main([self.run])[1])[0]
        for path in (task, os.path.join(task, "transcripts.zip")):
            rc, text = run_main([path])
            b = rows(text)[0]
            self.assertEqual(rc, 0)
            for k in ("inputTokens", "outputTokens", "cacheReadTokens", "reasoningTokens", "tokens", "usage_calls",
                      "retried_attempts", "sessions", "subagent_sessions", "session_files", "cost_usd", "ledger_calls"):
                self.assertEqual(a[k], b[k], (path, k))

    def test_dsh_folder_input(self):
        a = rows(run_main([self.run])[1])[0]
        b = rows(run_main([os.path.join(self.run, ".dsh_ra3")])[1])[0]
        self.assertEqual(a["cost_usd"], b["cost_usd"])
        self.assertEqual(b["run"], "demo_p1")

    def test_undecodable_file_is_never_zero(self):
        bad = os.path.join(self.run, ".dsh_ra3", "calls", "07_exec_u2", "sessions", "--workspace-u2--", "session-ggg")
        os.makedirs(bad)
        with open(os.path.join(bad, "session.v3.jsonl.zstd"), "wb") as f:
            f.write(ss.ZSTD_MAGIC + b"\x00garbage" * 10)
        rc, text = run_main([self.run])
        r = rows(text)[0]
        self.assertEqual(rc, 1)
        self.assertEqual((r["undecodable_files"], r["session_files"]), ("1", "7"))
        self.assertEqual(int(r["inputTokens"]), self.total()["in"], "the other files are still settled")

    def test_json_output(self):
        rc, text = run_main([self.run, "--by-role", "--format", "json"])
        doc = json.loads(text)
        self.assertEqual(doc["rates_usd_per_mtok"]["uncached_input"], 0.15)
        run = doc["runs"][0]
        self.assertEqual(run["basis"], "sessions")
        self.assertEqual(run["roles"]["critic"]["inputTokens"], 60)
        self.assertIsInstance(run["cost_usd"], float)


class TestWithoutSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra_settle_test_")
        self.run = build_run(os.path.join(self.tmp, "demo_p2"), with_sessions=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_sessions_is_an_error_without_fallback(self):
        rc, text = run_main([self.run])
        self.assertEqual(rc, 1)
        r = rows(text)[0]
        self.assertEqual((r["basis"], r["cost_usd"], r["session_files"]), ("none", "", "0"))

    def test_ledger_fallback(self):
        rc, text = run_main([self.run, "--ledger-fallback", "--by-role"])
        self.assertEqual(rc, 0)
        got = dict((r["role"], r) for r in rows(text))
        a = got["all"]
        self.assertEqual(a["basis"], "ledger")
        self.assertEqual((int(a["inputTokens"]), int(a["cacheReadTokens"]), int(a["outputTokens"])), (712, 13050, 280))
        self.assertAlmostEqual(float(a["cost_usd"]), float(a["token_usage_cost_usd"]), places=3)
        self.assertEqual(got["exec"]["ledger_calls"], "1")
        self.assertEqual(got["exec"]["inputTokens"], "500")

    def test_rates(self):
        rc, text = run_main([self.run, "--ledger-fallback", "--rates", "1,1,1"])
        a = rows(text)[0]
        self.assertAlmostEqual(float(a["cost_usd"]), (712 + 13050 + 280) / 1e6, places=4)
        with open(ss.DEFAULT_RATES) as f:
            card = json.load(f)
        self.assertEqual((card["uncached_input"], card["cached_input"], card["output"]), RATES)
        self.assertEqual(ss.main([self.run, "--rates", "not-a-file"]), 2)

    def test_missing_path(self):
        rc, text = run_main([os.path.join(self.tmp, "nope")])
        self.assertEqual(rc, 1)


class TestRoles(unittest.TestCase):
    def test_role_of(self):
        cases = {"plan": "plan", "replan": "plan", "exec_u1": "exec", "fix_u1": "exec", "cont_u2": "exec",
                 "resume_u3_cd": "exec", "restart_u1": "exec", "bfix_u1": "exec", "critic_u1": "critic",
                 "criticcp_u1": "critic", "criticd_u4": "critic", "criticsel_u1": "critic", "criticfmt_u1": "critic",
                 "scout2_u2": "scout", "report_render": "report", "mystery": "other"}
        for tag, role in cases.items():
            self.assertEqual(ss.role_of(tag), role, tag)
        self.assertEqual(ss.tag_of_call_folder("18_resume_u3_cd_num"), "resume_u3_cd_num")


@unittest.skipUnless(os.environ.get("RA_TRAJECTORIES_DIR"), "set RA_TRAJECTORIES_DIR to check published runs")
class TestPublishedRuns(unittest.TestCase):
    def test_ledger_matches_token_usage(self):
        root = os.environ["RA_TRAJECTORIES_DIR"]
        runs = []
        for dp, dn, fn in os.walk(root):
            if "token_usage.json" in fn and os.path.isfile(os.path.join(dp, "_pece", "ledger.jsonl")):
                runs.append(dp)
                dn[:] = []
        self.assertTrue(runs, "no run folders under " + root)
        for run in sorted(runs):
            rc, text = run_main([run, "--ledger-fallback"])
            r = rows(text)[0]
            calls = int(r["ledger_calls"])
            # token_usage.json sums per-call costs rounded to 4 decimals: allow half a unit of rounding per call
            self.assertLessEqual(abs(float(r["ledger_cost_usd"]) - float(r["token_usage_cost_usd"])),
                                 0.00005 * calls + 1e-6, run)


if __name__ == "__main__":
    unittest.main()
