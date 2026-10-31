"""The timeline renderer residual_agents/trace.py (trace.json, timeline.html, timeline.txt from a run's ledger).
Two short offline episodes (a plain one and one with scouting and a restart) produce real ledgers. Checks: the episode end writes
the three files; trace.json says generated_by "trace.py (RA)" and the harness version of episode_start (or its "harness" field,
or nothing, when the ledger lacks them); a re-render from the ledger is byte-identical; `ra trace` and `python trace.py` work.
Optional:
  RA_TEST_TRAJECTORIES=DIR   every published run under DIR (unpacked) renders
Usage: python3 tests/test_trace.py
"""
import glob, importlib.util, json, os, re, shutil, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from e2e_lib import *

TRACE = os.path.join(REPO, "residual_agents", "trace.py")
OUTPUTS = ("trace.json", "timeline.html", "timeline.txt")
GEN_RX = re.compile(r"trace\.py \([^)]*\)")
SCRATCH = tempfile.mkdtemp(prefix="ra_trace_")

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

tr = load(TRACE, "ra_trace_under_test")

def render(mod, pece, out):
    mod.build(pece, out_dir=out, plan_path=os.path.join(pece, "plan.json"))
    return dict((f, open(os.path.join(out, f), encoding="utf-8").read()) for f in OUTPUTS)

# ---------------------------------------------------------------- two offline episodes
PLAN = [{"tag": r"^plan$", "do": [{"plan": plan_obj()}]}]
REPORT = [{"tag": r"^report_render$", "do": [{"report": True}]}, {"tag": r"^critic_render$", "do": [A("render")]}]
happy = run_scen("trace_happy", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
scout = run_scen("trace_scout", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "nth": [1], "do": [G("C", ids="R3")]},
                                        {"tag": r"^scout\d_u1$", "do": [{"candidate": {"hypothesis": "an index bug", "supports": True}}]},
                                        {"tag": r"^criticsel\d_u1$", "do": [{"select": {"rank": "1 2 3", "also": "none"}}]},
                                        {"tag": r"^restart_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"WALLTIME_SEC": str(9 * 3600)})
LEDGERS = []
for name, R in (("happy", happy), ("scout", scout)):
    pece = os.path.join(R["ws"], "_pece")
    LEDGERS.append(pece)
    check(name + ":episode_ok", R["rc"] == 0 and bool(kinds(R, "episode_end")), "rc=%s\n%s" % (R["rc"], R["out"][-1500:]))
    check(name + ":written_at_episode_end", all(os.path.isfile(os.path.join(pece, f)) for f in OUTPUTS) and "trace written:" in R["out"],
          [f for f in OUTPUTS if not os.path.isfile(os.path.join(pece, f))])
    try:
        t = json.load(open(os.path.join(pece, "trace.json")))
    except Exception as e:
        t = {"error": repr(e)}
    check(name + ":generated_by", t.get("generated_by") == "trace.py (RA)", t.get("generated_by"))
    check(name + ":header", t.get("version") == "v61" and t.get("episode") == kinds(R, "episode_start")[0]["episode"] and t.get("paper") == "99"
          and t.get("grades") == {"u1": "A", "render": "A"}, dict((k, t.get(k)) for k in ("version", "episode", "paper", "grades")))
    lanes = dict((l["name"], l) for l in t.get("lanes", []))
    check(name + ":lanes", {"u1", "planner", "override"} <= set(lanes) and lanes["u1"].get("status") == "done" and lanes["u1"].get("grade") == "A",
          sorted(lanes))
    n_calls = len(kinds(R, "call"))
    check(name + ":every_call_is_an_event", sum(1 for e in t.get("events", []) if e.get("kind") == "call") == n_calls,
          (n_calls, sum(1 for e in t.get("events", []) if e.get("kind") == "call")))
    again = render(tr, pece, os.path.join(SCRATCH, name + "_again"))
    same = [f for f in OUTPUTS if again[f] == open(os.path.join(pece, f), encoding="utf-8").read()]
    check(name + ":re_render_identical", same == list(OUTPUTS), same)
    txt = again["timeline.txt"]
    check(name + ":text_timeline", txt.startswith(t.get("episode", "?")) and "harness v61" in txt and "verdict chain" in txt, txt[:300])
    check(name + ":html_timeline", again["timeline.html"].lstrip().lower().startswith("<!doctype html") and "<svg" in again["timeline.html"]
          and "harness v61" in again["timeline.html"], again["timeline.html"][:200])
t = json.load(open(os.path.join(LEDGERS[1], "trace.json")))
n_scout_calls = sum(1 for e in kinds(scout, "call") if str(e.get("tag") or "").startswith("scout"))
scout_events = [e for e in t["events"] if e.get("kind") == "call" and e.get("mode") == "scout"]
u1_labels = [str(s.get("label")) for l in t["lanes"] if l["name"] == "u1" for s in l["segments"]]
check("scout:scouting_and_restart_shown", n_scout_calls >= 3 and len(scout_events) == n_scout_calls
      and any(e.get("kind") == "restart" for e in t["events"]) and any("restart" in x for x in u1_labels),
      (n_scout_calls, len(scout_events), [e.get("kind") for e in t["events"]][:40], u1_labels))

# ---------------------------------------------------------------- the version label falls back to the "harness" field
def edited_ledger(name, drop):
    d = os.path.join(SCRATCH, name, "_pece")
    os.makedirs(d)
    with open(os.path.join(LEDGERS[0], "ledger.jsonl")) as f, open(os.path.join(d, "ledger.jsonl"), "w") as g:
        for line in f:
            e = json.loads(line)
            if e.get("kind") == "episode_start":
                for k in drop:
                    e.pop(k, None)
            g.write(json.dumps(e) + "\n")
    shutil.copy(os.path.join(LEDGERS[0], "plan.json"), d)
    return d
d1 = edited_ledger("no_version", ("version",))
d2 = edited_ledger("no_version_no_harness", ("version", "harness"))
v1 = json.load(open(tr.build(d1)[0])).get("version")
v2 = json.load(open(tr.build(d2)[0])).get("version")
check("version_falls_back_to_harness", v1 == "ra3" and v2 == "", (v1, v2))

# ---------------------------------------------------------------- the command lines
out = os.path.join(SCRATCH, "cli_out")
e = strip_env(dict(os.environ))
e["PYTHONPATH"] = REPO
p = subprocess.run([sys.executable, "-m", "residual_agents", "trace", happy["ws"], "--out", out], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
check("ra_trace_cli", p.returncode == 0 and sorted(os.listdir(out)) == sorted(OUTPUTS)
      and open(os.path.join(out, "trace.json")).read() == open(os.path.join(LEDGERS[0], "trace.json")).read(), p.stdout[-500:])
out2 = os.path.join(SCRATCH, "script_out")
p = subprocess.run([sys.executable, TRACE, "--out", out2, happy["ws"]], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
written = glob.glob(os.path.join(out2, "*", "trace.json"))
check("trace_script_cli", p.returncode == 0 and p.stdout.decode().startswith("OK ") and len(written) == 1, p.stdout[-500:])
p = subprocess.run([sys.executable, TRACE], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
check("trace_script_usage", p.returncode == 2 and b"Usage:" in p.stdout, p.stdout[:300])
p = subprocess.run([sys.executable, TRACE, os.path.join(SCRATCH, "nowhere")], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
check("trace_script_missing_ledger_fails", p.returncode == 1 and p.stdout.decode().startswith("FAIL "), p.stdout[:300])

# ---------------------------------------------------------------- published runs (optional)
traj = os.environ.get("RA_TEST_TRAJECTORIES")
pub = sorted(os.path.dirname(x) for x in glob.glob(os.path.join(traj, "**", "_pece", "ledger.jsonl"), recursive=True)) if traj and os.path.isdir(traj) else []
if pub:
    bad = []
    for i, pece in enumerate(pub):
        try:
            got = render(tr, pece, os.path.join(SCRATCH, "pub", "%03d" % i))
            if json.loads(got["trace.json"]).get("generated_by") != "trace.py (RA)":
                bad.append((i, "generated_by"))
        except Exception as x:
            bad.append((i, repr(x)[:200]))
    print("     (%d published runs rendered)" % len(pub))
    check("published_runs_render", not bad, bad[:5])
else:
    skip("published_runs_render", "set RA_TEST_TRAJECTORIES to a folder of published runs")

check("no_network", not NET_SEEN, NET_SEEN[:5])   # the two episodes above attempted no socket or urllib call
cleanup(failed=bool(FAILS))
if os.environ.get("RA_TEST_KEEP") == "1":
    print("kept: %s" % SCRATCH)
else:
    shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(finish())
