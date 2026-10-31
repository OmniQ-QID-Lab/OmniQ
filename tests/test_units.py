"""Unit tests of the pure policy functions (no model, no subprocess of dsh): prompts, hygiene, parser, validator, repair, decide, outcome
classifier, checkpoint triggers, bank retrieval, reading changes, pool admission, vendored filter, clocks.
They load residual_agents/orchestrator.py (RA_TEST_ORCH: another build) with the shipped memories (RA_TEST_NOTES: another folder).
The caller's RA3_*, RA3SR_*, PECE_*, RA_*, DEEPSEEK_* and DSH_* variables are removed first; the tests run the paper's proxy route
(RA_DSH_SHIM=1). Usage: python3 tests/test_units.py"""
import glob, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from e2e_lib import CLAUDE_MD, plan_obj, unit, ORCH, NOTES, strip_env
strip_env(os.environ)
os.environ.update({"PECE_NCPUS": "16", "PECE_MEM_GB": "64", "WALLTIME_SEC": str(24 * 3600), "RA3SR_GPU_FACTS": "none", "RUN_ID": "tu_%d" % os.getpid(),
                   "RA3SR_NOTES_DIR": NOTES, "MODEL": "deepseek/deepseek-v4.1-flash", "RA_DSH_SHIM": "1"})
NET = []   # every socket or urllib call this process attempts (none expected)
def _net_audit(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg", "urllib.Request"):
        where = args[1] if event in ("socket.connect", "socket.sendto", "socket.sendmsg") and len(args) > 1 else (args[0] if args else None)
        if event in ("socket.getaddrinfo", "urllib.Request") or not isinstance(where, (str, bytes)):
            NET.append("%s %s" % (event, str(where)[:200]))
if hasattr(sys, "addaudithook"):
    sys.addaudithook(_net_audit)
spec = importlib.util.spec_from_file_location("ra_orchestrator_units", ORCH)
sr = importlib.util.module_from_spec(spec); sys.modules["ra_orchestrator_units"] = sr; spec.loader.exec_module(sr)
ROOT = tempfile.mkdtemp(prefix="ra_units_"); WS = os.path.join(ROOT, "ws"); os.makedirs(os.path.join(WS, "_pece"))
sr.WS, sr.PD, sr.STORE = WS, os.path.join(WS, "_pece"), os.path.join(ROOT, "store"); sr.STATE_F = os.path.join(sr.PD, "state.json"); os.makedirs(sr.STORE)
open(os.path.join(WS, "CLAUDE.md"), "w").write(CLAUDE_MD)
FAILS = []
def check(name, cond, detail=""):
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else ": " + str(detail)[:1500]), flush=True)
    if not cond:
        FAILS.append(name)
FORB = ["PTM", "Clifford", "shadow", "Pauli", "iPEPO", "CTMRG", "QTCI", "Eagle", "BP-TNS", "DMRG", "PEPS", "Trotter", "VQE", "RXX", "CPTP", "UIRS",
        "cross-talk", "crosstalk", "stabilizer", "Qiskit"]

# ---- prompts format and stay generic
fmt = {
 "EXEC_PROMPT": dict(uid="u1", upstream="", budget_s=3600, budget_h=1.0, knobs="[]"),
 "CRITIC_PROMPT": dict(uid="u1", handoff="H", mech="M", changes="", previous="P", tool_min=10),
 "CRITIC_CONTRACT": dict(uid="u1"),
 "CRITIC_DELTA_PROMPT": dict(uid="u1", why="w", prev_dir="audit_r1", fixes="f", changed="none", handoff="h", mech="m", tool_min=10),
 "CHECKPOINT_PROMPT": dict(uid="u1", situation="s", activity="a", mech="m", previous="p", usable_end="12:00"),
 "SCOUT_PROMPT": dict(k=1, K=3, uid="u1", archive="/workspace/u1", running="", sdir="/workspace/_pece/scouts/u1.s1/s1", spec="/workspace/u1/spec.md",
                      angle="A", tried="", poc_min=30, cores=2, mins=45, bank=""),
 "SELECT_PROMPT": dict(uid="u1", n=3, dirs="d", archive="/workspace/u1", spec="s", spare_min=300, out="/workspace/_pece/scouts/u1.s1/select1.md"),
 "REPLAN_PROMPT": dict(uid="u1", reason="D", blocked="u2", status_table="t", residual="r", done_block="d", retired="u1"),
 "REPORT_PROMPT": dict(mode="NORMAL", uid="render", mode_note="n", status_table="t", sources="s"),
 "RESTART_PREAMBLE": dict(uid="u1", grade="C", archive="/workspace/u1.r1", round=1, code="", copied="", note=""),
 "CONTINUE_PROMPT": dict(uid="u1", why="w", notes=""), "FIX_PROMPT": dict(uid="u1", grade="B", fixes="f"),
 "RESUME_PROMPT": dict(uid="u1", pid=1, expect="5 min", how="h", mins=1, facts="f", note="n"), "WRAP_FLUSH": dict(uid="u1"),
 "WRAP_CRITIC": dict(uid="u1"), "WRAP_CHECK": dict(uid="u1"), "BUDGET_NOTE": dict(est=100, spare=50, knobs="k"),
}
texts = []
for name, kw in fmt.items():
    try:
        t = getattr(sr, name).format(**kw)
        texts.append(t)
        check("format_" + name, "{" not in re.sub(r"\{\"[^}]*\}|\{\{|\}\}", "", t) or name in ("SCOUT_PROMPT", "CRITIC_PROMPT", "CRITIC_CONTRACT", "CHECKPOINT_PROMPT", "CRITIC_DELTA_PROMPT", "EXEC_PROMPT"), t[:200])
    except Exception as e:
        check("format_" + name, False, repr(e))
pp = sr.PLAN_PROMPT % {"ncpus": 16, "mem": 64, "rmin": 75, "maxu": 8}
texts.append(pp)
alltxt = "\n".join(texts) + "\n".join(open(os.path.join(NOTES, f)).read() for f in sorted(os.listdir(NOTES))) + " ".join(sr.SCOUT_ANGLES)
bad = [w for w in FORB if re.search(re.escape(w), alltxt, re.I)] + (["MPS"] if re.search(r"\bMPS\b", alltxt) else [])
check("hygiene_forbidden", not bad, bad)
check("hygiene_paper_refs", not re.search(r"\bp\d{2}\b|\bFig(ure)?\.? ?\d|chi ~|nine measurement", alltxt), re.findall(r"\bp\d{2}\b|\bFig(?:ure)?\.? ?\d", alltxt)[:5])
check("flush_text", "REPORT MODE" not in sr.WRAP_FLUSH and "/workspace/{uid}/" in sr.WRAP_FLUSH and "do NOT write" in sr.WRAP_FLUSH, sr.WRAP_FLUSH)
check("critic_one_trailer", '"residual_ids"' in sr.CRITIC_PROMPT and "RESIDUAL_IDS:" not in sr.CRITIC_PROMPT and "anchors_met" not in sr.CRITIC_PROMPT and "GRADE: W" not in sr.CRITIC_PROMPT, "")
check("critic_b_rule", "A G1 hit is never B" in sr.CRITIC_PROMPT and "forbids B" not in sr.CRITIC_PROMPT and "bounded extension" in sr.CRITIC_PROMPT, "")
check("exec_no_false_claims", "is killed" not in sr.EXEC_PROMPT and "NEVER dropped" in sr.EXEC_PROMPT and "READINGS.md" in sr.EXEC_PROMPT, "")
check("session_hint_exec_only", sr.DSH_SESSION_HINT not in sr._assemble("x", "critic_u1", None, None) and sr.DSH_SESSION_HINT in sr._assemble("x", "exec_u1", None, None), "")
check("plan_prompt_fields", '"requirements"' in pp and '"readings"' in pp and '"check_by"' in pp and '"decisions"' not in pp, "")
check("prompt_sizes", len(sr.EXEC_PROMPT) < 6500 and len(sr.CRITIC_PROMPT) < 8500 and len(sr.CHECKPOINT_PROMPT) < 3500 and len(pp) < 9500,
      [len(sr.EXEC_PROMPT), len(sr.CRITIC_PROMPT), len(sr.CHECKPOINT_PROMPT), len(pp)])

# ---- the shared needs-path extractor
cases = {"/workspace/u1/data/a.npz and /workspace/u1/code.py - both": ["/workspace/u1/data/a.npz", "/workspace/u1/code.py"],
         "/workspace/u1/a.npz plus /workspace/u1/b.npz": ["/workspace/u1/a.npz", "/workspace/u1/b.npz"],
         "/workspace/u1/a.npz (plus /workspace/u1/b.npz)": ["/workspace/u1/a.npz", "/workspace/u1/b.npz"],
         "/workspace/u1/a.npz + /workspace/u1/b.npz, /workspace/u1/c/": ["/workspace/u1/a.npz", "/workspace/u1/b.npz", "/workspace/u1/c/"],
         "/workspace/u1/data/<key>.json - per key": ["/workspace/u1/data/<key>.json"], "/workspace/u1/handoff.json only": [],
         "/workspace/u1/t_{a,b}.npz.": ["/workspace/u1/t_{a,b}.npz"]}
for txt, want in cases.items():
    check("need_paths %s" % txt[:30], sr.need_paths(txt, "u1") == want, sr.need_paths(txt, "u1"))

# ---- the validator (soft contract errors while strict; warnings on the last attempt; numbers moved to notes)
sr.RUN["plan"] = []
o = plan_obj()
e, w, units, rq, rd = sr.validate_plan(json.loads(json.dumps(o)))
check("validate_ok", not e and units[-1]["id"] == "render" and len(rq) == 1 and rd and rd[0]["id"] == "K1", (e, w))
o2 = plan_obj(); o2.pop("requirements")
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o2)), strict=True)
check("validate_req_missing_soft", any("requirements" in x for x in e), e)
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o2)), strict=False)
check("validate_req_missing_last", not e and any("requirements" in x for x in w), (e, w))
o3 = plan_obj(units=[unit("u0", cores=12, alloc=200), unit("u1", deps=["u0"])], reqs=[{"id": "Q1", "what": "core rows", "owner": "u1", "core": True}])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o3)))
check("validate_core_first", any("core row Q1" in x for x in e), e)
o3b = plan_obj(units=[unit("u0", cores=8, alloc=180), unit("u1", deps=["u0"])], reqs=[{"id": "Q1", "what": "core rows", "owner": "u1", "core": True}])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o3b)))
check("validate_core_first_180_ok", not any("core row" in x for x in e), e)
o4 = plan_obj(units=[unit("u1", goal="compute the series; if it does not fit, drop the largest size and mark it MISSING")])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o4)))
check("validate_drop_language", any("drop a required row" in x for x in e), e)
o4b = plan_obj(units=[unit("u1", goal="compute the series; never drop a required size")])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o4b)))
check("validate_negated_drop_ok", not any("drop a required row" in x for x in e), e)
o5 = plan_obj(units=[unit("u1", goal="compute with method A; if the gauge step fails, fall back to the library default")])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o5)))
check("validate_fallback_language", any("fallback" in x for x in e), e)
o6 = plan_obj(); o6["units"][0]["done_criteria"] = ["error below 0.01 at every point", "files exist (F5)"]
e, w, units, *_ = sr.validate_plan(json.loads(json.dumps(o6)))
check("validate_numbers_to_notes", not e and units[0]["done_criteria"] == ["files exist (F5)"] and units[0].get("notes"), (e, units[0]))
o7 = plan_obj(units=[unit("u1", alloc=900)])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o7)))
check("validate_alloc_third", any("third" in x for x in e), e)
e, w, units, rq, rd = sr.validate_plan(json.loads(json.dumps(o["units"])))
check("validate_bare_array", units is not None and any("requirements" in x for x in e), e)
bad = {"units": [{"id": "u1", "type": "produce", "goal": "g", "deliverables": ["/workspace/other/x.npz"], "done_criteria": ["x"], "depends_on": ["zz"], "cores": 99, "mem_gb": 999}]}
rp = sr._repair_plan(bad)
e, w, units, *_ = sr.validate_plan(rp, strict=False)
check("repair_plan", rp and not e and units[0]["deliverables"] == ["/workspace/u1/data/"] and units[0]["depends_on"] == [] and units[-1]["id"] == "render", (e, rp))
e, w, units, *_ = sr.validate_plan({"requirements": [], "readings": [], "units": sr._minimal_plan()}, strict=False)
check("minimal_plan_valid", not e, e)

# ---- the replan: running (incl. reopened) units keep their live object; a retired dependency of the render unit is dropped, not an error
live_u1 = unit("u1"); live_u1["status"] = "running"; live_u1["reopens"] = 1; live_u1["reopen_why"] = "K1 changed"
dead_u2 = unit("u2"); dead_u2["status"] = "failed"
sr.RUN["plan"] = [live_u1, dead_u2, dict(unit("u3", deps=["u2"]), status="blocked"), {"id": "render", "type": "render", "goal": "g",
                  "deliverables": ["/workspace/render/report.md"], "done_criteria": ["x"], "depends_on": ["u1", "u2", "u3"], "cores": 2, "mem_gb": 4}]
newp = plan_obj(units=[dict(unit("u1"), goal="rewritten by the planner"), unit("u4")], reqs=[{"id": "Q1", "what": "rows", "owner": "u4", "core": True}])
newp["units"][-1]["depends_on"] = ["u1", "u2", "u4"]
e, w, units, *_ = sr.validate_plan(json.loads(json.dumps(newp)), old_plan=json.loads(json.dumps(sr.RUN["plan"])))
check("replan_render_retired_dep_dropped", not any("RETIRED" in x for x in e) and units and units[-1]["depends_on"] == ["u1", "u4"], (e, units and units[-1]))
check("replan_running_restored", units and [x for x in units if x["id"] == "u1"][0]["goal"] == live_u1["goal"], e)
_gp = sr.get_plan
sr.get_plan = lambda replan=None, old_plan=None: units
try:
    ok = sr.do_replan({"uid": "u2", "blocked": ["u3"], "reason": "D"})
finally:
    sr.get_plan = _gp
check("replan_keeps_object_identity", ok and [x for x in sr.RUN["plan"] if x.get("id") == "u1"][0] is live_u1, [x.get("id") for x in sr.RUN["plan"]])
check("replan_retired_kept_as_history", [x for x in sr.RUN["plan"] if x.get("id") == "u2"][0]["status"] == "failed", "")
sr.RUN["plan"] = []

# ---- decide: only time closes a unit
class T:
    pass
def mkS(**kw):
    S = {"b_hit": None, "b_hit_n": 0, "b_sig": None, "reopened": False, "est": 30, "last_gates": None}
    S.update(kw); return S
u = {"id": "ud", "handoff_valid_at": 1}
os.makedirs(os.path.join(WS, "ud"), exist_ok=True)
GP = {"G1": "pass", "G2": "pass", "G3": "pass"}; GH = {"G1": "pass", "G2": "hit", "G3": "pass"}; G3H = {"G1": "pass", "G2": "pass", "G3": "hit"}
def with_usable(sec, fn):
    old = sr.usable_left
    sr.usable_left = lambda: sec
    try:
        return fn()
    finally:
        sr.usable_left = old
H = 3600
check("decide_A", with_usable(10 * H, lambda: sr.decide("A", mkS(), GP, u)) == ("done", "A"), "")
check("decide_D", with_usable(10 * H, lambda: sr.decide("D", mkS(), GP, u)) == ("failed", "D"), "")
check("decide_B_fix", with_usable(10 * H, lambda: sr.decide("B", mkS(), GH, u)) == ("fix", "B"), "")
check("decide_B_long_fix_still_fix", with_usable(1 * H, lambda: sr.decide("B", mkS(est=600), GH, u)) == ("fix", "B"), "")
check("decide_B_no_time", with_usable(600, lambda: sr.decide("B", mkS(), GP, u)) == ("done", "b_all_pass_no_time"), "")
GH1 = {"G1": "hit", "G2": "pass", "G3": "pass"}
check("decide_late_G1_hit_fails", with_usable(600, lambda: sr.decide("C", mkS(last_gates=GH1), None, u)) == ("failed", "g1_no_time"), "")
check("decide_late_checkpoint_C_unaudited", with_usable(600, lambda: sr.decide("C", mkS(last_gates=GP, cp_c=True), None, u)) == ("gap", "gap_unaudited_checkpoint"), "")
check("decide_late_checkpoint_C_no_handoff", with_usable(600, lambda: sr.decide("C", mkS(last_gates=GP, cp_c=True), None, {"id": "ud"})) == ("failed", "no_time"), "")
S = mkS()
r1 = with_usable(10 * H, lambda: sr.decide("B", S, GH, u)); r2 = with_usable(10 * H, lambda: sr.decide("B", S, GH, u)); r3 = with_usable(10 * H, lambda: sr.decide("B", S, GH, u))
check("decide_B_loop_guard", r1 == ("fix", "B") and r2 == ("fix", "B") and r3[0] == "cpath", (r1, r2, r3))
ud = {"id": "ud", "handoff_valid_at": 1, "deliverables": ["/workspace/ud/data/", "/workspace/ud/src/solver.py"]}
os.makedirs(os.path.join(WS, "ud", "data"), exist_ok=True); os.makedirs(os.path.join(WS, "ud", "src"), exist_ok=True)
open(os.path.join(WS, "ud", "src", "solver.py"), "w").write("x = 1\n")
S = mkS(); sr.decide("B", S, GH, ud); sr.decide("B", S, GH, ud)
open(os.path.join(WS, "ud", "data", "new.npz"), "w").write("x")
r3 = with_usable(10 * H, lambda: sr.decide("B", S, GH, ud))
check("decide_B_loop_guard_data_grew", r3 == ("fix", "B"), r3)
S = mkS(); sr.decide("B", S, GH, ud); sr.decide("B", S, GH, ud)
open(os.path.join(WS, "ud", "handoff.json"), "w").write("{}"); open(os.path.join(WS, "ud", "critic_report.md"), "w").write("GRADE: B")
r3 = with_usable(10 * H, lambda: sr.decide("B", S, GH, ud))
check("decide_B_loop_guard_bookkeeping_is_not_data", r3[0] == "cpath", r3)
check("decide_C_scout", with_usable(sr.SCOUT_NEED_S + 60, lambda: sr.decide("C", mkS(), G3H, u)) == ("cpath", "C"), "")
check("decide_C_late_in_place", with_usable(2 * H, lambda: sr.decide("C", mkS(), G3H, u)) == ("fix_c", "C_late"), "")
check("decide_C_no_time_gap", with_usable(600, lambda: sr.decide("C", mkS(), G3H, u)) == ("gap", "gap_no_time"), "")
check("decide_C_no_time_unaudited", with_usable(600, lambda: sr.decide("C", mkS(), None, u)) == ("gap", "gap_unaudited"), "")
check("decide_C_no_time_failed", with_usable(600, lambda: sr.decide("C", mkS(), None, {"id": "ud"})) == ("failed", "no_time"), "")
check("decide_C_uses_last_full_audit", with_usable(600, lambda: sr.decide("C", mkS(last_gates=GP), None, {"id": "ud"})) == ("gap", "gap_no_time"), "")
check("decide_reopen_C_in_place", with_usable(10 * H, lambda: sr.decide("C", mkS(reopened=True), G3H, u)) == ("fix_c", "reopen_C_in_place"), "")
check("decide_reopen_no_time_done", with_usable(600, lambda: sr.decide("C", mkS(reopened=True), G3H, u)) == ("done", "reopen_unfinished"), "")
check("decide_reopen_D", with_usable(10 * H, lambda: sr.decide("D", mkS(reopened=True), G3H, u)) == ("done", "reopen_D_noted"), "")
check("no_counter_close", with_usable(10 * H, lambda: [sr.decide("B", mkS(), GP, u) for _ in range(8)])[-1] == ("fix", "B"), "")

# ---- the call outcome classifier
os.makedirs(os.path.join(WS, "uc"), exist_ok=True)
hp = os.path.join(WS, "uc", "handoff.json")
def outcome(rc, write=None, **kw):
    s0 = sr._handoff_sig("uc")
    if write is not None:
        open(hp, "w").write(write)
    r = {"rc": rc, "interrupted": None, "infra": False}; r.update(kw)
    return sr.call_outcome("uc", r, s0)
good = json.dumps({"uid": "uc", "deliverables": [], "stopped": None})
check("outcome_handoff", outcome(0, good) == "HANDOFF", "")
sr.LAST_LOOK["uc"] = time.time() + 1   # a verdict after that handoff: the next session must hand off again
check("outcome_cut_nohand", outcome(0) == "CUT_NOHAND", "")
sr.LAST_LOOK["uc"] = time.time() - 5   # the session re-wrote the SAME handoff since the last look and ended cleanly -> a handoff
check("outcome_fresh_identical_handoff", outcome(0, good) == "HANDOFF", "")
check("outcome_cut_after_handoff", outcome(-1, json.dumps({"uid": "uc", "deliverables": [1]})) == "CUT", "")
check("outcome_handoff_bad", outcome(0, "{bad json,}") == "HANDOFF_BAD", "")
env = dict(os.environ); env["PECE_UNIT"] = sr._unit_marker("uc")
pj = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=env)
time.sleep(0.3)
check("outcome_cut_with_live_registration", outcome(-1, json.dumps({"uid": "uc", "deliverables": [], "waiting": {"pid": pj.pid, "done_marker": "/workspace/uc/bg.done", "expect_min": 5}})) == "HANDOFF", "")
pf = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(0.3)
check("outcome_cut_with_foreign_pid", outcome(-1, json.dumps({"uid": "uc", "deliverables": [], "waiting": {"pid": pf.pid, "done_marker": "/workspace/uc/bg.done", "expect_min": 6}})) == "CUT", "")
check("pid_marker_check", sr._pid_alive_unit(pj.pid, "uc") and not sr._pid_alive_unit(pf.pid, "uc"), "")
pj.kill(); pf.kill()
check("outcome_interrupted", outcome(-5, interrupted="flush") == "INTERRUPTED", "")
check("outcome_infra", outcome(1, infra=True) == "INFRA", "")

# ---- a pid registered by one unit that belongs to another unit is never its job and never killed on its behalf
os.makedirs(os.path.join(WS, "ua"), exist_ok=True)
envb = dict(os.environ); envb["PECE_UNIT"] = sr._unit_marker("ub")
pb = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=envb, start_new_session=True)
time.sleep(0.3)
open(os.path.join(WS, "ua", "handoff.json"), "w").write(json.dumps({"uid": "ua", "deliverables": [], "waiting": {"pid": pb.pid, "done_marker": "/workspace/ua/bg.done", "expect_min": 5}}))
jb = sr.current_job("ua", implicit=False)
check("foreign_pid_job_counts_dead", jb is None or sr.job_ended("ua", jb), jb)
sr.reap_leftovers("ua"); sr.reap_leftovers("ua", kill_pid=pb.pid if sr._pid_is_units(pb.pid, "ua") else None)
time.sleep(0.2)
check("foreign_pid_survives_reap", sr._pid_alive(pb.pid), "")
envo = dict(os.environ); envo["PECE_UNIT"] = sr._unit_marker("ua")
po = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=envo, start_new_session=True)   # (the orchestrator's own group is exempt)
time.sleep(0.3)
sr.reap_leftovers("ua"); time.sleep(0.3)
check("own_pid_reaped", not sr._pid_alive(po.pid) and sr._pid_alive(pb.pid), "")
pb.kill(); po.kill()

# ---- a dead call is a failed call with 0 tokens only (a busy endpoint with tokens is a cut, never an infra death)
check("dead_call_rule_zero_tokens", "dead_call = (not tokens and not wrote and not INFRA[\"dead\"]" in open(sr.__file__).read(), "")

# ---- the checkpoint trigger
sr.LAST_LOOK["uk"] = time.time()
job = {"pid": 123, "done_marker": "/workspace/uk/bg.done", "expect_min": 10 ** 6, "implicit": False, "t0": time.time()}
check("cp_past_end", with_usable(10 * H, lambda: sr.checkpoint_due("uk", job)) == "past_end", "")
sr.CP_DONE["uk"] = {(sr._job_key(job), "past_end")}
check("cp_past_end_once", with_usable(10 * H, lambda: sr.checkpoint_due("uk", job)) is None, "")
sr.LAST_LOOK["uk"] = time.time() - sr.CP_S - 5
check("cp_interval", with_usable(10 * H, lambda: sr.checkpoint_due("uk", job)) == "interval", "")
check("cp_implicit_interval", with_usable(10 * H, lambda: sr.checkpoint_due("uk", dict(job, implicit=True))) == "interval", "")
check("cp_floor", with_usable(sr.LAST_ACT_S - 60, lambda: sr.checkpoint_due("uk", job)) is None, "")
sr.OVERRIDE.set()
check("cp_override", with_usable(10 * H, lambda: sr.checkpoint_due("uk", job)) is None, "")
sr.OVERRIDE.clear()

# ---- the residual bank: 10 entries, IDs only from the trailer
sr.load_notes()
ents = sr.bank_entries()
check("bank_10", len(ents) == 10 and [e["id"] for e in ents] == list(range(1, 11)) and sum(1 for e in ents if "always" in e["keys"]) == 1, [e["id"] for e in ents])
check("bank_sizes", all(len(e["text"]) <= 700 for e in ents), [len(e["text"]) for e in ents])
rp = os.path.join(ROOT, "rep.md")
open(rp, "w").write('GRADE: B\nthe lattice reflection R1 and probe R2 row 3\nFIX LIST\n- see R9\nFIX_PLAN: {"est_min": 5, "steps": [], "residual_ids": ["R3", "R8", "R99"], "reading_change": null}\n')
check("bank_ids_from_trailer", sr.bank_cited_ids(rp) == [3, 8], sr.bank_cited_ids(rp))
open(rp, "w").write('GRADE: B\nR1 R2 mentioned\nRESIDUAL_IDS: R5\nFIX_PLAN: {"est_min": 5, "steps": []}\n')
check("bank_ids_only_from_fix_plan", sr.bank_cited_ids(rp) == [], sr.bank_cited_ids(rp))
open(rp, "w").write('GRADE: B\nsee R4 and R6\nFIX_PLAN: {"est_min": 5, "steps": []}\n')
check("bank_ids_none_without_trailer", sr.bank_cited_ids(rp) == [], sr.bank_cited_ids(rp))
blk = sr.bank_start_block({"id": "ub", "goal": "validate the exact case"}, None)
check("bank_start_always_first", blk.startswith("\n\n" + sr.BANK_START_TITLE) and "R1 VALIDATE" in blk, blk[:200])

# ---- reading changes: evidence required, one change per reading, propagation to done units
sr.RUN["readings"] = [{"id": "K1", "F": "F7", "reading": "J = 1 as written", "version": 1}]
sr.write_readings(sr.RUN["readings"])
os.makedirs(os.path.join(WS, "ur", "probes"), exist_ok=True)
open(os.path.join(WS, "ur", "probes", "probe_k1.txt"), "w").write("decides")
open(os.path.join(WS, "ur", "critic_report.md"), "w").write('GRADE: B\nFIX_PLAN: {"est_min": 5, "steps": [], "residual_ids": [], "reading_change": {"id": "K1", "reading": "J = 1/2", "evidence": "probe_k1.txt"}}\n')
check("reading_change_parsed", sr.parse_reading_change("ur") == [("K1", "J = 1/2", "probe_k1.txt")], sr.parse_reading_change("ur"))
open(os.path.join(WS, "ur", "critic_report.md"), "w").write('GRADE: B\nFIX_PLAN: {"est_min": 5, "steps": [], "residual_ids": [], "reading_change": {"id": "K1", "reading": "J = 2", "evidence": "missing.txt"}}\n')
check("reading_change_needs_evidence", sr.parse_reading_change("ur") == [], sr.parse_reading_change("ur"))
ud1 = {"id": "ud1", "type": "produce", "status": "done", "depends_on": []}
sr.RUN["plan"] = [ud1]
sr.record_inputs(ud1)
check("inputs_include_readings", "/workspace/READINGS.md" in (ud1.get("inputs") or {}), ud1.get("inputs"))
ok1 = sr.apply_reading_change("K1", "J = 1/2", "probe_k1.txt", "ur", "critic")
check("reading_change_applied", ok1 and sr.RUN["readings"][0]["version"] == 2 and "J = 1/2" in open(os.path.join(WS, "READINGS.md")).read(), sr.RUN["readings"])
check("reading_change_reopens_done", bool(ud1.get("reopen")) and "READINGS CHANGED" in ud1["reopen"]["why"], ud1.get("reopen"))
ok2 = sr.apply_reading_change("K1", "J = 2", "probe_other.txt", "ux", "critic")
check("reading_change_once", not ok2 and sr.RUN["readings"][0]["reading"] == "J = 1/2", sr.RUN["readings"])
sig_before = dict(ud1["inputs"]["/workspace/READINGS.md"])
sr.record_inputs(ud1)
os.utime(os.path.join(WS, "READINGS.md"))
check("same_content_no_change", sr.upstream_changes(ud1) == [], sr.upstream_changes(ud1))

# ---- the pool: cores may be oversubscribed, memory only by measured use
P = sr.ResourcePool(16, 64)
sr.POOL = P
g1 = P.acquire("a", {"cores": 13, "mem_gb": 40})
check("pool_full_fit", g1 and g1["cores"] == 13 and not g1.get("shared"), g1)
check("pool_first_waits", P.acquire("b", {"cores": 10, "mem_gb": 20}, waited_s=10) is None, "")
sr._units_rss_gb = lambda: 5.0
g2 = P.acquire("b", {"cores": 10, "mem_gb": 20}, waited_s=sr.POOL_WAIT_S + 1)
check("pool_after_wait_oversub_cores_real_mem", g2 and g2["cores"] == 3 and g2["mem_gb"] == 20 and g2["shared"], g2)
sr._units_rss_gb = lambda: 55.0
check("pool_first_waits_for_real_memory", P.acquire("c", {"cores": 4, "mem_gb": 30}, waited_s=sr.POOL_WAIT_S + 1) is None, "")
g4 = P.acquire("d", {"cores": 4, "mem_gb": 30}, first=False)
check("pool_repair_takes_real_free", g4 and g4["mem_gb"] == 4 and g4["cores"] == 2, g4)
P.release("a"); P.release("b"); P.release("d")
check("pool_release", P.used() == (0, 0), P.used())

# ---- vendored trees never compile, never copy back
vd = os.path.join(WS, "uv"); os.makedirs(os.path.join(vd, "lib", "pkg", "sub"), exist_ok=True); os.makedirs(os.path.join(vd, "lib", "pkg-1.0.dist-info"), exist_ok=True)
open(os.path.join(vd, "lib", "pkg", "sub", "broken.py"), "w").write("def (:\n")
open(os.path.join(vd, "mine.py"), "w").write("x = 1\n")
check("vendored_skip", [os.path.basename(x) for x in sr._changed_py(vd, 0)] == ["mine.py"], sr._changed_py(vd, 0))

# ---- regression cases: one test per fixed path
# the loop guard sees new data in pattern deliverables
up = {"id": "up", "handoff_valid_at": 1, "deliverables": ["/workspace/up/data/E_*.npz", "/workspace/up/data/F_L{8,16}.npz"]}
os.makedirs(os.path.join(WS, "up", "data"), exist_ok=True)
S = mkS(); r1 = with_usable(10 * H, lambda: sr.decide("B", S, GH, up))
open(os.path.join(WS, "up", "data", "E_1.npz"), "w").write("1")
r2 = with_usable(10 * H, lambda: sr.decide("B", S, GH, up))
open(os.path.join(WS, "up", "data", "F_L8.npz"), "w").write("1")
r3 = with_usable(10 * H, lambda: sr.decide("B", S, GH, up))
check("decide_B_loop_guard_patterns", [r1, r2, r3] == [("fix", "B")] * 3, [r1, r2, r3])
# past_end compares the job's END with the usable end
sr.LAST_LOOK["upe"] = time.time(); sr.CP_DONE.pop("upe", None)
old_job = {"pid": 7, "done_marker": "/workspace/upe/bg.done", "expect_min": 600, "implicit": False, "t0": time.time() - 590 * 60}
check("cp_past_end_uses_end_time", with_usable(2 * H, lambda: sr.checkpoint_due("upe", old_job)) is None, "")
new_job = dict(old_job, t0=time.time())
check("cp_past_end_new_long_job", with_usable(2 * H, lambda: sr.checkpoint_due("upe", new_job)) == "past_end", "")
# the delta prompt carries the full rules; no assembled critic text asks for the old trailer lines
check("delta_has_rules", all(x in sr.CRITIC_RULES for x in ("(G1) provenance", "A G1 hit is never B", "EXPLICITLY permits at reduced scale", "root cause is not located")), "")
crit_txt = sr.CRITIC_PROMPT + sr.CRITIC_RULES + sr.CRITIC_CONTRACT + sr.CRITIC_DELTA_PROMPT + sr.CHECKPOINT_PROMPT + sr.BANK_INDEX_TITLE + sr.notes_block("critic_notes", sr.CRITIC_NOTES_TITLE, index=True)
check("one_trailer_only", "RESIDUAL_IDS" not in crit_txt and "READING_CHANGE" not in crit_txt and "residual_ids" in sr.BANK_INDEX_TITLE, "")
sr.write_readings([{"id": "K1", "reading": "J = 1", "version": 1}])
check("readings_no_trailer_line", "READING_CHANGE" not in open(os.path.join(WS, "READINGS.md")).read() and not glob.glob(os.path.join(WS, "READINGS.md.tmp*")), "")
# a replan keeps the live readings (an in-run change survives), new ids are added
sr.RUN["readings"] = [{"id": "K1", "reading": "J = 1/2", "version": 2, "changed": "J = 1/2 (was J = 1)"}]
sr._accept_plan([unit("ux")], [], [{"id": "K1", "reading": "J = 1"}, {"id": "K2", "reading": "h = 0"}], {}, {}, 1, [], {"uid": "uz"})
check("replan_keeps_readings", [x["reading"] for x in sr.RUN["readings"]] == ["J = 1/2", "h = 0"] and "J = 1/2" in open(os.path.join(WS, "READINGS.md")).read(), sr.RUN["readings"])
sr.RUN["readings"] = []; sr.RUN["plan"] = []
# call suffixes are reserved
o9 = plan_obj(units=[unit("u1_only")])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(o9)))
check("uid_suffix_reserved", any("_only" in x for x in e), e)
# planning budget: a first call + its pens-down leave room for one retry inside PLAN_TOTAL_S
check("plan_budget_room_for_retry", sr.PLAN_T + sr.PLAN_WRAP_FIRST_T + 600 <= sr.PLAN_TOTAL_S and sr.PLAN_WRAP_FIRST_T < sr.WRAPUP_T, (sr.PLAN_T, sr.PLAN_WRAP_FIRST_T, sr.PLAN_TOTAL_S))
# panel: the DISQUALIFIED line is parsed like ADOPT (valid numbers only, "none" = empty)
pr = sr._parse_rank("RANK: 2 1 3\nADOPT: 1\nDISQUALIFIED: 2 7\ntext", {1, 2, 3})
check("parse_rank_disq", pr == ([2, 1, 3], [1], [2]), pr)
pr = sr._parse_rank("RANK: NONE\nADOPT: none\nDISQUALIFIED: none", {1, 2, 3})
check("parse_rank_none_disq", pr == ("NONE", [], []), pr)
pr = sr._parse_rank("RANK: 3 1\nADOPT: none", {1, 3})
check("parse_rank_no_disq_line", pr == ([3, 1], [], []), pr)
check("select_prompt_disq_line", "DISQUALIFIED: <the numbers" in sr.SELECT_PROMPT and "DISQUALIFIED: <numbers> | none" in sr.SELECT_WRAP and
      "a gate the current code misses is never that evidence" in sr.SELECT_PROMPT and "plan or fallback" in sr.SELECT_PROMPT, "")
# a scout's reading change needs an evidence file
sr.RUN["readings"] = [{"id": "K1", "reading": "J = 1", "version": 1}]
sd = os.path.join(ROOT, "scoutdir"); os.makedirs(sd, exist_ok=True); os.makedirs(os.path.join(WS, "us"), exist_ok=True)
sc = {"winner": 1, "adopt": [], "res": {1: {"dir": "/workspace/_pece/scouts/us.s1/s1", "real": sd, "cand": {"hypothesis": "h", "poc": {}, "supports": True,
      "reading_change": {"id": "K1", "reading": "J = 2", "evidence": "probe_missing.txt"}}}}}
sr.scout_readings("us", sc)
check("scout_reading_change_needs_evidence", sr.RUN["readings"][0]["reading"] == "J = 1", sr.RUN["readings"])
open(os.path.join(sd, "probe_k1.txt"), "w").write("decides")
sc["res"][1]["cand"]["reading_change"]["evidence"] = "probe_k1.txt"
sr.scout_readings("us", sc)
check("scout_reading_change_with_evidence", sr.RUN["readings"][0]["reading"] == "J = 2", sr.RUN["readings"])
sr.RUN["readings"] = []
# after the override no new session starts before the report (the flush window); the first-plan rule only
sr.OVERRIDE.set()
rr = sr.run_call("x", 60, "exec_u1")
check("no_new_call_after_override", rr["rc"] == -5 and rr["interrupted"] == "override" and rr["tokens"] == 0, rr)
sr.OVERRIDE.clear()
src = open(sr.__file__).read()
check("planner_death_first_plan_only", 'tag.split("_")[0] == "plan" and not INFRA["dead"]' in src, "")
# a recycled session id never kills a process marked for another unit
envx = dict(os.environ); envx["PECE_UNIT"] = sr._unit_marker("uother")
px = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=envx, start_new_session=True)
time.sleep(0.3)
sr.UNIT_PIDS["urec"] = [px.pid]   # px leads its own session: its sid equals a pid "urec" once recorded
sr.reap_leftovers("urec"); time.sleep(0.3)
check("sid_hit_spares_foreign_marker", sr._pid_alive(px.pid), "")
px.kill()
# a call's sweep covers its wrap-up and its endpoint retries
envw = dict(os.environ); envw["PECE_CALL_TAG"] = sr._call_marker("scout1_uk") + "_wrapup"
pw = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=envw, start_new_session=True)
time.sleep(0.3)
n_k = sr._kill_marked("scout1_uk", 9); time.sleep(0.3)
check("sweep_covers_wrapup", n_k == 1 and not sr._pid_alive(pw.pid), n_k)
# settle(failed) blocks the dependents even when the residual write fails
ua_ = unit("uf"); ua_["status"] = "running"; ub_ = unit("ug", deps=["uf"])
sr.RUN["plan"] = [ua_, ub_]; sr.RUN["failed"] = []; sr.RUN["retired"] = []
_er = sr.ensure_residual
sr.ensure_residual = lambda *a, **k: (_ for _ in ()).throw(IOError("disk full"))
try:
    sr.settle(ua_, "failed", "D", "D", 1)
finally:
    sr.ensure_residual = _er
check("settle_failed_blocks_despite_io_error", ub_.get("status") == "blocked", ub_.get("status"))
sr.RUN["plan"] = []; sr.RUN.pop("replan_req", None); sr.RUN["replans"] = 0
# a registration already served is not a fresh handoff
os.makedirs(os.path.join(WS, "usv"), exist_ok=True)
hsv = os.path.join(WS, "usv", "handoff.json")
open(hsv, "w").write(json.dumps({"uid": "usv", "deliverables": [], "waiting": {"pid": 999999, "done_marker": "/workspace/usv/bg.done", "expect_min": 5}}))
sr.WAIT_RESUMED.setdefault("usv", set()).add((999999, "/workspace/usv/bg.done"))
sr.LAST_LOOK["usv"] = time.time() - 60
check("served_registration_not_a_handoff", sr.call_outcome("usv", {"rc": 0, "interrupted": None, "infra": False}, sr._handoff_sig("usv")) == "CUT_NOHAND", "")

# ---- verification of the regression fixes
src = open(sr.__file__).read()
check("first_plan_full_pens_down", "PLAN_T, (300 if replan else PLAN_WRAP_FIRST_T)" in src and sr.PLAN_WRAP_FIRST_T >= 600 and "WRAP_EXEC" not in src, "")
check("fix_carry_persists", "FIX ROUND'S INSTRUCTIONS" in sr._fix_carry({"fix_extra": "- fix line 3"}) and sr._fix_carry({"fix_extra": ""}) == "", "")
_ur = sr._units_rss_gb
sr._units_rss_gb = lambda: (_ for _ in ()).throw(RuntimeError("an inline /proc scan while the sampler runs"))
sr.RSS_CACHE.update({"gb": 12.5, "live": True})
try:
    check("rss_cache_only_while_sampler_runs", sr._rss_now() == 12.5, "")
finally:
    sr.RSS_CACHE["live"] = False; sr._units_rss_gb = _ur
envw2 = dict(os.environ); envw2["PECE_CALL_TAG"] = sr._call_marker("criticsel2_uk") + "_wrapup_gwretry1"
pw2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=envw2, start_new_session=True)
time.sleep(0.3)
n_k2 = sr._kill_marked("criticsel2_uk", 9); time.sleep(0.3)
check("sweep_covers_wrapup_retry", n_k2 == 1 and not sr._pid_alive(pw2.pid), n_k2)
check("contract_plotted_window", "a quantity's plotted window" in sr.CRITIC_PROMPT and "a quantity's plotted window" in sr.CRITIC_RULES and "a quantity's plotted window" in sr.task_book(), "")
check("contract_D_never_removes_rows", "a D never removes, re-scopes or reduces a required row" in sr.CRITIC_RULES, "")
check("contract_reduced_scale_keeps_rows", "a reduced scale never removes a value, series or panel" in sr.CRITIC_RULES, "")

# ---- planning efficiency: compliance read as a violation, mechanical slips bounced
oA = plan_obj(units=[unit("u1", goal="compute the series; no required row may be dropped or shortened by any setting")])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(oA)))
check("negation_after_no_in_clause", not any("drop a required row" in x for x in e), e)
oB = plan_obj(units=[unit("u1", goal="compute with the named library; a dense-FFT substitute is a forbidden substitution")])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(oB)))
check("negation_after_phrase", not any("fallback" in x for x in e), e)
oC = plan_obj(units=[unit("u1", goal="compute the series; if it does not converge, drop the largest size")])
e, w, *_ = sr.validate_plan(json.loads(json.dumps(oC)))
check("conditional_drop_still_flagged", any("drop a required row" in x for x in e), e)
oD = plan_obj(units=[unit("u1"), unit("u2")])
rdr = oD["units"].pop(); rdr["depends_on"] = ["u1"]; oD["units"].insert(0, rdr)
e, w, units, *_ = sr.validate_plan(json.loads(json.dumps(oD)))
check("render_place_and_deps_mended", not e and units[-1]["id"] == "render" and set(units[-1]["depends_on"]) >= {"u1", "u2"}, (e, units and units[-1]))
oE = plan_obj(reqs=[{"id": "Q1", "what": "rows", "owner": "u1", "core": True}, {"id": "Q2", "what": "report.md and the figure files", "owner": "render", "core": True}])
e, w, units, rq, rd = sr.validate_plan(json.loads(json.dumps(oE)))
check("render_rows_accepted_noncore", not e and [r["id"] for r in rq] == ["Q1", "Q2"] and rq[1]["core"] is False, (e, rq))
check("fix_attempt_short", "budget, wrap = PLAN_FIX_T, 300" in open(sr.__file__).read() and "EDITING /workspace/_pece/plan.json in place" in open(sr.__file__).read()
      and "ended WITHOUT writing" in open(sr.__file__).read(), "")

# ---- mech.json lies in the unit's directory, where an agent can rewrite it (an executor's own script can rewrite it without n_deliv_ok;
# the checkpoint prompt must not raise KeyError and retire the unit) -> any foreign content reads as 'not available'
ud = sr.unit_dir("u9"); os.makedirs(ud, exist_ok=True)
mp = os.path.join(ud, "mech.json")
json.dump({"uid": "u9", "n_deliv_ok": 1, "n_deliv": 1, "n_py_ok": 0, "n_py": 0, "handoff_ok": True,
           "deliverables": [{"path": os.path.join(ud, "a.npz"), "ok": True, "check": "npz", "err": ""}], "py_compile": []}, open(mp, "w"))
check("mech_block_own_schema", "deliverables 1/1 OK" in sr.mech_block("u9"), sr.mech_block("u9"))
bad_forms = {"foreign_schema": {"uid": "u9", "generated_unix": 1, "deliverables": [{"path": "/workspace/u9/a.npz", "ok": True}]},
             "list": [1, 2], "null": None, "rows_without_keys": {"n_deliv_ok": 1, "n_deliv": 1, "n_py_ok": 0, "n_py": 0, "deliverables": [{}], "py_compile": []},
             "string_counts": {"n_deliv_ok": "1", "n_deliv": "1", "n_py_ok": 0, "n_py": 0, "deliverables": [], "py_compile": []}}
for nm, obj in bad_forms.items():
    json.dump(obj, open(mp, "w"))
    try:
        txt = sr.mech_block("u9")
        check("mech_block_foreign_" + nm, txt.startswith("MECH CHECK: not available"), txt)
    except Exception as e:
        check("mech_block_foreign_" + nm, False, repr(e))
open(mp, "w").write("{not json")
check("mech_block_unparseable", sr.mech_block("u9").startswith("MECH CHECK: not available"), "")
check("fault_note_generic", "internal fault" in sr.FAULT_WHY and "not a verdict" in sr.FAULT_WHY, sr.FAULT_WHY)

# ---- static audit: agent-written files in another shape never raise in the orchestrator
def _no_raise(name, fn, ok=lambda r: True):
    try:
        r = fn()
        check(name, ok(r), r)
    except Exception as e:
        check(name, False, repr(e))
ud8 = sr.unit_dir("u8"); os.makedirs(ud8, exist_ok=True)
json.dump({"fails": [{"path": "data/E.npz", "err": "missing"}, None]}, open(os.path.join(ud8, "mech.json"), "w"))
_no_raise("audit_ensure_residual_fail_objects", lambda: sr.ensure_residual("u8", "grade C"), lambda r: r == "orchestrator" and "data/E.npz" in open(os.path.join(ud8, "residual.md")).read())
os.remove(os.path.join(ud8, "residual.md")); json.dump({"fails": 2}, open(os.path.join(ud8, "mech.json"), "w"))
_no_raise("audit_ensure_residual_fail_number", lambda: sr.ensure_residual("u8", "grade C"))
json.dump([{"L": 8, "t_unit_s": 0.3}], open(os.path.join(ud8, "profile.json"), "w"))
_no_raise("audit_scale_hint_list_profile", lambda: sr._scale_hint({"id": "u8"}), lambda r: "SCALE KNOBS" in r)
for nm, w in (("nan", {"pid": "nan", "done_marker": "x", "expect_min": 5}), ("inf", {"pid": "inf", "done_marker": "x", "expect_min": 5}),
              ("huge", {"pid": 10 ** 30, "done_marker": "x", "expect_min": 5})):
    _no_raise("audit_waiting_pid_" + nm, lambda w=w: sr.waiting_spec({"waiting": w}), lambda r: r[0] is None and r[1])
_no_raise("audit_waiting_expect_inf_bounded", lambda: sr.waiting_spec({"waiting": {"pid": 4242, "done_marker": "/workspace/u8/bg.done", "expect_min": "inf"}}),
          lambda r: r[0] is None)
_no_raise("audit_waiting_expect_huge_clamped", lambda: sr.waiting_spec({"waiting": {"pid": 4242, "done_marker": "/workspace/u8/bg.done", "expect_min": 1e300}}),
          lambda r: r[0] is not None and r[0]["expect_min"] <= 1e5)
_no_raise("audit_brace_range_capped", lambda: sr._brace_expand("/workspace/u8/d{0..1000000000}.npz"), lambda r: 0 < len(r) <= 400)
led = os.path.join(sr.PD, "ledger.jsonl"); keep_led = open(led).read() if os.path.exists(led) else None
open(led, "a").write('"agent note"\n42\n{"kind": "call", "tag": null, "dur": "5"}\n')
_no_raise("audit_time_ledger_foreign_lines", lambda: sr.time_ledger("u8"), lambda r: "TIME LEDGER" in r)
if keep_led is None:
    os.remove(led)
else:
    open(led, "w").write(keep_led)
json.dump([{"path": "/workspace/u8/data/"}], open(os.path.join(ud8, "handoff.json"), "w"))
keep_plan = sr.RUN["plan"]; sr.RUN["plan"] = [{"id": "u8", "type": "produce", "status": "flushed"}]
_no_raise("audit_fallback_report_list_handoff", lambda: sr.write_fallback_report("render"))
sr.RUN["plan"] = keep_plan; os.remove(os.path.join(ud8, "handoff.json"))
_no_raise("audit_fix_plan_inf", lambda: (open(os.path.join(ud8, "critic_report.md"), "w").write('GRADE: B\nFIX_PLAN: {"est_min": 1e999}\n'), sr.parse_fix_plan("u8"))[1],
          lambda r: r is not None and r["est_min"] is None)
_no_raise("audit_budget_note_inf", lambda: sr._budget_note({"est": float("inf")}, {"id": "u8"}), lambda r: r == "")
_no_raise("audit_write_spec_notes_number", lambda: sr.write_spec({"id": "u7", "goal": "g", "deliverables": ["/workspace/u7/a"], "done_criteria": ["c"], "notes": 3}))
d8 = os.path.join(ud8, "data"); os.makedirs(d8, exist_ok=True); open(os.path.join(d8.encode(), b"bad\xffname.npz"), "w").write("x")
_no_raise("audit_content_sig_non_utf8", lambda: sr._content_sig(d8), lambda r: isinstance(r, str))
oO = plan_obj(units=[unit("u1"), unit("u2")], reqs=[{"id": "Q1", "what": "curve A", "owner": ["u1", "u2"], "core": True}, {"id": "Q2", "what": "curve B", "owner": ["u2"], "core": False}],
              reads=[{"id": "K1", "reading": "x", "check_by": ["u1"]}, {"id": "K2", "reading": "y", "check_by": ["u1", "u2"]}])
oO["units"][0]["notes"] = None
_no_raise("audit_validate_owner_list_is_error", lambda: sr.validate_plan(json.loads(json.dumps(oO))),
          lambda r: any("owner must be ONE unit id" in x for x in r[0]))
_no_raise("audit_validate_lists_normalised", lambda: sr.validate_plan(json.loads(json.dumps(oO)), strict=False),
          lambda r: r[3] is not None and [q["owner"] for q in r[3]] == ["u2"] and [x.get("check_by") for x in r[4]] == ["u1", None])
oR = json.loads(json.dumps(oO)); oR["units"][1]["depends_on"] = ["u1", {"id": "u1"}]
_no_raise("audit_repair_plan_odd_types", lambda: sr._repair_plan(oR), lambda r: [q["id"] for q in r["requirements"]] == [] and r["units"][1]["depends_on"] == ["u1"])

# ---- clocks
sr.RUN["plan"] = [{"id": "render", "type": "render", "report_cost_min": 30}]
sr.RUN.pop("report_cost_min", None)
check("report_reserve_never_lowered", sr.report_cost_min() == sr.REPORT_MIN == 75, sr.report_cost_min())
sr.RUN["report_cost_min"] = 80
check("report_reserve_frozen", sr.report_cost_min() == 80, "")
sr.REPORT["started"] = True
check("usable_zero_after_report", sr.usable_left() == 0, sr.usable_left())
sr.REPORT["started"] = False

# ---- no network: nothing above attempted a socket or urllib call
check("no_network", not NET, NET[:5])
if os.environ.get("RA_TEST_KEEP") == "1" or (os.environ.get("RA_TEST_KEEP") == "failed" and FAILS):
    print("kept: %s" % ROOT)
else:
    shutil.rmtree(ROOT, ignore_errors=True)
print("%d check(s) failed - %s" % (len(FAILS), "GREEN" if not FAILS else "RED " + str(FAILS)))
sys.exit(1 if FAILS else 0)
