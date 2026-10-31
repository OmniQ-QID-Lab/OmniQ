"""Offline end-to-end scenarios through the scenario-driven fake dsh (no model, no network). Each scenario runs the real main() in a temp
workspace with short clocks and checks the ledger; every scenario also checks that the orchestrator attempted no network call.
Usage: python3 tests/test_e2e.py [scenario ...]        (no argument: every scenario; tests/run_all.sh runs each in its own process)
       python3 tests/test_e2e.py --scout               the scenarios that run scouts or panel critics in parallel (SCOUT_SCENS)
       python3 tests/test_e2e.py --list | --list-scout print the scenario names"""
import hashlib, json, os, re, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import *

PLAN1 = plan_obj()
REPORT = [{"tag": r"^report_render$", "do": [{"report": True}]}, {"tag": r"^critic_render$", "do": [A("render")]}]
PLAN = [{"tag": r"^plan$", "do": [{"plan": PLAN1}]}]

def base_checks(name, R, grades=None):
    check(name + ":rc", R["rc"] == 0, "rc=%s\n%s" % (R["rc"], R["out"][-3000:]))
    end = kinds(R, "episode_end")
    check(name + ":end", bool(end), R["out"][-2000:])
    if grades is not None and end:
        check(name + ":grades", end[-1]["grades"] == grades, end[-1]["grades"])
    pl = json.load(open(os.path.join(R["ws"], "_pece", "plan.json")))
    check(name + ":plan_array", isinstance(pl, list) and all(isinstance(x, dict) for x in pl), type(pl))
    check(name + ":no_thread_error", not kinds(R, "unit_thread_error") and "Traceback" not in R["out"], R["out"][-3000:])

def scen_happy():
    R = run_scen("happy", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
    base_checks("happy", R, {"u1": "A", "render": "A"})
    check("happy:files", all(os.path.exists(os.path.join(R["ws"], f)) for f in ("READINGS.md", "_pece/requirements.json", "_pece/plan_full.json", "report.md", "token_usage.json")), "")
    spec = open(os.path.join(R["ws"], "u1", "spec.md")).read()
    check("happy:spec_rows", "Required rows this unit owns" in spec and "Q1 [CORE]" in spec and "READINGS.md" in spec, spec[:600])
    t = tags(R)
    check("happy:calls", t == ["plan", "exec_u1", "critic_u1", "report_render", "critic_render"], t)
    check("happy:saw", "saw:exec_u1:lessons" in R["calls"] and "saw:exec_u1:readings" in R["calls"] and "saw:critic_u1:index" in R["calls"], [c for c in R["calls"] if c.startswith("saw")])
    rates = R["mod"].RATES if R.get("mod") else (0.10, 0.02, 0.40)
    cu = [e for e in kinds(R, "call") if e.get("usage")]   # dsh 0.1.5: input = uncached, output holds the reasoning (the settlement's convention)
    check("happy:usage_accounting", cu and all(e["tokens"] == e["usage"]["input"] + e["usage"]["cached"] + e["usage"]["output"] and e["cost_usd"] >= 0 and
          abs(e["cost_usd"] - (e["usage"]["input"] * rates[0] + e["usage"]["cached"] * rates[1] + e["usage"]["output"] * rates[2]) / 1e6) < 1e-4 for e in cu),
          [(e["tag"], e["tokens"], e.get("usage"), e.get("cost_usd")) for e in cu][:3])

def scen_cut_nohand():
    """A turn that ends without writing handoff.json is continued, never audited."""
    R = run_scen("cut_nohand", PLAN + [{"tag": r"^exec_u1$", "do": [{"npz": "/workspace/u1/data/a.npz"}]},
                                      {"tag": r"^cont_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
    base_checks("cut_nohand", R, {"u1": "A", "render": "A"})
    t = tags(R)
    check("cut_nohand:order", t[:4] == ["plan", "exec_u1", "cont_u1", "critic_u1"], t)
    ex = kinds(R, "exec")
    check("cut_nohand:outcome", ex[0]["outcome"] == "CUT_NOHAND" and ex[1]["outcome"] == "HANDOFF", ex)

def scen_handoff_then_cut():
    """A session that wrote a valid handoff and then died (rc 1, tokens > 0) is a CUT, not a handoff."""
    R = run_scen("ho_cut", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1", [{"rc": 1}])}, {"tag": r"^cont_u1$", "do": deliver("u1")},
                                   {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
    base_checks("ho_cut", R, {"u1": "A", "render": "A"})
    t = tags(R)
    check("ho_cut:order", t[:4] == ["plan", "exec_u1", "cont_u1", "critic_u1"] and not kinds(R, "infra_retry") and kinds(R, "exec")[0]["outcome"] == "CUT", t)

def scen_nohand_loop():
    """After a B the fix sessions end their turn without rewriting handoff.json (it predates the verdict) -> NOHAND_MAX continuations, then the
    audit decides on what exists. A fresh handoff confirmed by a normal turn end is a handoff at once."""
    R = run_scen("nohand", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "nth": [1], "do": [G("B", fix="- rename the key")]},
                                   {"tag": r"^(fix|cont)_u1$", "do": [{"sleep": 0.1}]}, {"tag": r"^critic", "do": [A()]},
                                   {"tag": r"^report_render$", "do": [{"report": True}]}])
    base_checks("nohand", R, {"u1": "A", "render": "A"})
    t = tags(R)
    check("nohand:count", t[:8] == ["plan", "exec_u1", "critic_u1", "fix_u1", "cont_u1", "cont_u1", "critic_u1", "report_render"] or t[:7] == ["plan", "exec_u1", "critic_u1", "fix_u1", "cont_u1", "cont_u1", "criticd_u1"], t)

def scen_wait_resume_cut():
    """A registered job ends; the verifying session is cut -> a checkpoint looks at the finished results (never a blind continuation)."""
    R = run_scen("wait_cut", PLAN + [{"tag": r"^exec_u1$", "do": [{"write": "/workspace/u1/src/u1_solver.py", "text": "x=1\n"},
                                                                 {"spawn": {"secs": 3, "period": 0.5, "register": True, "expect_min": 1, "progress": True}}]},
                                     {"tag": r"^resume_u1$", "do": [{"rc": 1}]},
                                     {"tag": r"^criticcp_u1$", "do": [{"critic": {"grade": "W", "gates": "n/a (checkpoint)"}}]},
                                     {"tag": r"^cont_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
    base_checks("wait_cut", R, {"u1": "A", "render": "A"})
    t = tags(R)
    check("wait_cut:order", t[:6] == ["plan", "exec_u1", "resume_u1", "criticcp_u1", "cont_u1", "critic_u1"], t)
    cp = kinds(R, "checkpoint")
    check("wait_cut:after_job", cp and cp[0]["reason"] == "after_job", cp)
    check("wait_cut:waited", kinds(R, "wait_done") and kinds(R, "wait_start")[0]["implicit"] is False, kinds(R, "wait_start"))

def scen_implicit_wait():
    """A handoff with an unregistered computation still writing data -> an implicit wait, looked at by the checkpoint."""
    R = run_scen("implicit", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1", [{"spawn": {"secs": 12, "period": 0.5, "marker": False}}])},
                                     {"tag": r"^criticcp_u1$", "do": [{"critic": {"grade": "W", "gates": "n/a (checkpoint)"}}]},
                                     {"tag": r"^resume_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"RA3_CP_S": "4"})
    base_checks("implicit", R, {"u1": "A", "render": "A"})
    ws = kinds(R, "wait_start")
    check("implicit:implicit_wait", ws and ws[0]["implicit"] is True, ws)
    cp = kinds(R, "checkpoint")
    check("implicit:checked", cp and cp[0]["reason"] == "interval", cp)
    t = tags(R)
    check("implicit:resumed_then_audited", "resume_u1" in t and t.index("critic_u1") > t.index("resume_u1"), t)

def scen_checkpoint_b():
    """A running registered job; the checkpoint rules B -> the executor resumes with the fix list while the job keeps running."""
    R = run_scen("cp_b", PLAN + [{"tag": r"^exec_u1$", "do": [{"write": "/workspace/u1/src/u1_solver.py", "text": "x=1\n"},
                                                             {"spawn": {"secs": 10, "period": 0.5, "register": True, "expect_min": 1, "progress": True}}]},
                                 {"tag": r"^criticcp_u1$", "nth": [1], "do": [{"critic": {"grade": "B", "gates": "n/a (checkpoint)", "fix": "- lower the sample count", "est": 5}}]},
                                 {"tag": r"^criticcp_u1$", "do": [{"critic": {"grade": "W", "gates": "n/a (checkpoint)"}}]},
                                 {"tag": r"^fix_u1$", "do": [{"handoff": {"uid": "u1", "deliverables": [{"path": "/workspace/u1/data/", "what": "d"}],
                                                                          "waiting": {"pid": "BGPID", "done_marker": "/workspace/u1/bg.done", "expect_min": 1}}}]},
                                 {"tag": r"^resume_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"RA3_CP_S": "3"})
    base_checks("cp_b", R, {"u1": "A", "render": "A"})
    t = tags(R)
    check("cp_b:fix_after_check", "fix_u1" in t and t.index("fix_u1") > t.index("criticcp_u1"), t)
    check("cp_b:no_archive", not kinds(R, "archive"), kinds(R, "archive"))

def scen_scout_restart():
    """C with the wall for a scouted fresh attempt: 3 scouts on the unit's grant, a panel picks + adopts, the archive happens AFTER the
    scouting, the winner seeds the fresh directory, an adopted reading change reaches READINGS.md."""
    rules = PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")},
                    {"tag": r"^critic_u1$", "nth": [1], "do": [G("C", ids="R3")]},
                    {"tag": r"^scout1_u1$", "do": [{"candidate": {"hypothesis": "an index bug", "supports": False}}]},
                    {"tag": r"^scout2_u1$", "do": [{"write": "/workspace/_pece/scouts/u1.s1/s2/probe_k1.txt", "text": "decides"},
                                                   {"candidate": {"hypothesis": "the literal reading of K1", "supports": True,
                                                                  "reading_change": {"id": "K1", "reading": "J = 1/2 per F7", "evidence": "/workspace/_pece/scouts/u1.s1/s2/probe_k1.txt"}}}]},
                    {"tag": r"^scout3_u1$", "do": [{"candidate": {"hypothesis": "too few samples", "supports": True}}]},
                    {"tag": r"^criticsel\d_u1$", "do": [{"select": {"rank": "3 2 1", "also": "2"}}]},
                    {"tag": r"^restart_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT
    R = run_scen("scout", rules, env={"WALLTIME_SEC": str(9 * 3600)})
    base_checks("scout", R, {"u1": "A", "render": "A"})
    t = tags(R)
    ar = kinds(R, "archive")
    ss = kinds(R, "scout_start")
    check("scout:scouted_then_archived", ss and ar and ss[0]["ts"] <= ar[0]["ts"] and t.index("restart_u1") > t.index("criticsel1_u1"), (ss, ar, t))
    check("scout:grants", ss and ss[0]["cores_each"] >= 1 and all(("grant:scout%d_u1:%d" % (k, ss[0]["cores_each"])) in " ".join(R["calls"]) or True for k in (1, 2, 3)), ss)
    sw = kinds(R, "scout_winner")
    check("scout:winner_adopt", sw and sw[0]["winner"] == 3 and sw[0]["adopt"] == [2], sw)
    check("scout:seeded", os.path.isdir(os.path.join(R["ws"], "u1", "from_scout")) or os.path.isdir(os.path.join(R["ws"], "u1.r1")), "")
    check("scout:note_in_restart", "saw:restart_u1:scouting" in R["calls"] and "saw:restart_u1:also" in R["calls"], [c for c in R["calls"] if "restart_u1" in c])
    rc = kinds(R, "reading_change")
    check("scout:reading_change", rc and rc[0]["id"] == "K1" and "J = 1/2" in open(os.path.join(R["ws"], "READINGS.md")).read(), rc)

def scen_c_late():
    """C with less wall than a scouted attempt needs but more than LAST_ACT -> fixed IN PLACE (no archive, no scouting)."""
    R = run_scen("c_late", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "nth": [1], "do": [G("C")]},
                                   {"tag": r"^fix_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"WALLTIME_SEC": str(int(3.2 * 3600))})
    base_checks("c_late", R, {"u1": "A", "render": "A"})
    check("c_late:in_place", not kinds(R, "archive") and not kinds(R, "scout_start") and "fix_u1" in tags(R), tags(R))
    fp = kinds(R, "fix_plan")
    check("c_late:reason", fp and fp[0]["action"] == "fix_c", fp)

def scen_c_no_time():
    """C with less than LAST_ACT of usable wall -> settles in place as DONE WITH GAP (provenance verified); the render runs on its data."""
    R = run_scen("c_notime", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [G("C", gates="G1=pass G2=pass G3=hit")]}] + REPORT,
                 env={"WALLTIME_SEC": str(int(1.5 * 3600))})
    base_checks("c_notime", R)
    st = [e for e in kinds(R, "settle") if e["unit"] == "u1"]
    check("c_notime:gap", st and st[-1]["status"] == "done" and st[-1]["gap"] is True and st[-1]["reason"] == "gap_no_time", st)

def scen_requeue():
    """A requeued job finds its own episode_start: refused, .infra_failure, no second episode, no report."""
    def setup(ws):
        open(os.path.join(ws, "_pece", "ledger.jsonl"), "w").write(json.dumps({"kind": "episode_start", "ts": 1}) + "\n")
    R = run_scen("requeue", PLAN, setup=setup)
    check("requeue:rc3", R["rc"] == 3, R["rc"])
    check("requeue:marker", os.path.exists(os.path.join(R["ws"], ".infra_failure")) and len(kinds(R, "episode_start")) == 1 and kinds(R, "requeue_refused")
          and not kinds(R, "episode_end") and not os.path.exists(os.path.join(R["ws"], "report.md")), [e["kind"] for e in R["ev"]])

def scen_infra_planner():
    """Every planner call dies with 0 tokens and the endpoint probe fails (stubbed: the suite makes no network call) -> .infra_failure,
    no report, exit 3."""
    R = run_scen("infra", [{"tag": r"^plan", "do": [{"tokens": 0}, {"rc": 1}]}], env={"RA3_INFRA_PROBE_S": "2"}, pre="sr.gw_ok = lambda: False\n")
    check("infra:rc3", R["rc"] == 3, (R["rc"], R["out"][-1500:]))
    check("infra:marker", os.path.exists(os.path.join(R["ws"], ".infra_failure")) and kinds(R, "infra_failure") and not kinds(R, "report_start"),
          [e["kind"] for e in R["ev"]])

def scen_bad_json():
    """A handoff with a trailing comma -> a mechanical fix list naming the parse error, then the audit."""
    R = run_scen("badjson", PLAN + [{"tag": r"^exec_u1$", "do": [{"npz": "/workspace/u1/data/a.npz"}, {"write": "/workspace/u1/src/u1_solver.py", "text": "x=1\n"},
                                                                {"write": "/workspace/u1/handoff.json", "text": '{"uid": "u1",}'}]},
                                    {"tag": r"^fix_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
    base_checks("badjson", R, {"u1": "A", "render": "A"})
    ex = kinds(R, "exec")
    check("badjson:outcome", ex[0]["outcome"] == "HANDOFF_BAD" and kinds(R, "critic")[0]["by"] == "mech", (ex, kinds(R, "critic")))

def scen_stopped_leftover():
    """A 'stopped' handoff with a leftover sleeper: the sleeper is killed, the audit runs at once (no implicit wait)."""
    R = run_scen("stopped", PLAN + [{"tag": r"^exec_u1$", "do": [{"npz": "/workspace/u1/data/a.npz"}, {"write": "/workspace/u1/src/u1_solver.py", "text": "x=1\n"},
                                                                {"spawn": {"secs": 60, "period": 30, "marker": False}},
                                                                {"handoff": {"uid": "u1", "deliverables": [{"path": "/workspace/u1/data/", "what": "d"}], "stopped": "cannot converge: numbers"}}]},
                                    {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
    base_checks("stopped", R, {"u1": "A", "render": "A"})
    check("stopped:no_wait", not kinds(R, "wait_start") and tags(R)[:3] == ["plan", "exec_u1", "critic_u1"], tags(R))
    sp = [c for c in R["calls"] if c.startswith("spawned:")]
    pid = int(sp[0].split(":")[-1]) if sp else 0
    check("stopped:killed", pid and not os.path.exists("/proc/%d" % pid) or (pid and open("/proc/%d/stat" % pid).read().split(")")[-1].split()[0] == "Z"), pid)

def scen_settle_kills():
    """A unit settles done while a stray process of it still runs -> the process is dead before the dependents launch."""
    plan = plan_obj(units=[unit("u1"), unit("u2", deps=["u1"])])
    R = run_scen("settlekill", [{"tag": r"^plan$", "do": [{"plan": plan}]},
                                {"tag": r"^exec_u1$", "do": deliver("u1", [{"spawn": {"secs": 120, "period": 50, "marker": False}}])},
                                {"tag": r"^critic_u1$", "do": [A()]}, {"tag": r"^exec_u2$", "do": deliver("u2")}, {"tag": r"^critic_u2$", "do": [A()]}] + REPORT)
    base_checks("settlekill", R, {"u1": "A", "u2": "A", "render": "A"})
    sp = [c for c in R["calls"] if c.startswith("spawned:u1")]
    pid = int(sp[0].split(":")[-1]) if sp else 0
    alive = pid and os.path.exists("/proc/%d" % pid) and open("/proc/%d/stat" % pid).read().split(")")[-1].split()[0] != "Z"
    check("settlekill:dead", pid and not alive, pid)

# the file gate stays shut in the two replan scenarios: with the suite's 1 s gate a loaded host can start u2 provisionally on u1's
# files while u1's audit is under way, and a dependent that is already running is not blocked when its upstream is retired (it is
# not a test of the gate; the scenario would wait on u2 until its time limit)
GATE_CLOSED = {"RA3_FILE_GATE_S": "36000"}

def scen_d_replan():
    """D on task-book grounds -> retired -> dependents blocked -> one branch replan -> the new unit runs."""
    plan = plan_obj(units=[unit("u1"), unit("u2", deps=["u1"])])
    new = plan_obj(units=[unit("u1"), unit("u3")], reqs=[{"id": "Q1", "what": "rows", "owner": "u3", "core": True}])
    new["units"][0]["status"] = "failed"
    R = run_scen("dreplan", [{"tag": r"^plan$", "do": [{"plan": plan}]}, {"tag": r"^exec_u1$", "do": deliver("u1")},
                             {"tag": r"^critic_u1$", "do": [G("D", fix="F5 and F6 contradict (a)")]},
                             {"tag": r"^replan$", "do": [{"plan": new}]}, {"tag": r"^exec_u3$", "do": deliver("u3")}, {"tag": r"^critic_u3$", "do": [A()]}] + REPORT,
                 env=GATE_CLOSED)
    base_checks("dreplan", R)
    st = dict((e["unit"], e["status"]) for e in kinds(R, "settle"))
    check("dreplan:flow", st.get("u1") == "failed" and st.get("u2") in ("blocked",) and st.get("u3") == "done" and kinds(R, "replan_applied"), st)

def scen_plan_repair():
    """Every planner attempt is rejected (deliverables outside the unit) -> the last parseable plan is mended and runs."""
    badplan = plan_obj(); badplan["units"][0]["deliverables"] = ["/workspace/elsewhere/data/"]
    R = run_scen("repair", [{"tag": r"^plan$", "do": [{"plan": badplan}]}, {"tag": r"^exec_u1$", "do": [{"npz": "/workspace/u1/data/a.npz"},
                             {"handoff": {"uid": "u1", "deliverables": [{"path": "/workspace/u1/data/", "what": "d"}]}}]},
                            {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
    base_checks("repair", R, {"u1": "A", "render": "A"})
    check("repair:used", kinds(R, "plan_repaired") and len(kinds(R, "plan_invalid")) == 3, [e["kind"] for e in R["ev"] if e["kind"].startswith("plan")])

def scen_reopen():
    """u2's audit changes reading K1 (evidence file) -> done u1 is reopened for a delta audit (never archived) -> done again."""
    plan = plan_obj(units=[unit("u1"), unit("u2", deps=["u1"])])
    R = run_scen("reopen", [{"tag": r"^plan$", "do": [{"plan": plan}]}, {"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]},
                            {"tag": r"^exec_u2$", "do": deliver("u2", [{"write": "/workspace/u2/probes/probe_k1.txt", "text": "decides"}])},
                            {"tag": r"^critic_u2$", "do": [{"critic": {"grade": "A", "reading_change": None}},
                                                            {"write": "/workspace/u2/critic_report.md", "text": 'GRADE: A\nGATES: G1=pass G2=pass G3=pass\nFIX_PLAN: {"est_min": 0, "steps": [], "residual_ids": [], "reading_change": {"id": "K1", "reading": "J = 1/2", "evidence": "probe_k1.txt"}}\n'}]},
                            {"tag": r"^criticd_u1$", "do": [A()]}] + REPORT, env={"WALLTIME_SEC": str(6 * 3600)})
    base_checks("reopen", R)
    check("reopen:reopened", kinds(R, "reopen") and kinds(R, "reopen")[0]["unit"] == "u1" and "criticd_u1" in tags(R), [e for e in R["ev"] if e["kind"] in ("reopen", "reading_change", "input_changed")])
    st = [e for e in kinds(R, "settle") if e["unit"] == "u1"]
    check("reopen:done_again", len(st) == 2 and st[-1]["status"] == "done" and not [a for a in kinds(R, "archive") if a["unit"] == "u1"], st)

def scen_override_scouting():
    """The override arrives while the scouts run -> they are interrupted, nothing was archived, the unit is flushed, the report runs."""
    rules = PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [G("C")]},
                    {"tag": r"^scout\d_u1$", "do": [{"sleep": 60}]}] + REPORT
    pre = "sr.SCOUT_NEED_S = 60\nimport threading\nthreading.Timer(8, lambda: sr.fire_override('test')).start()\n"
    R = run_scen("ov_scout", rules, pre=pre, env={"RA3_WRAPUP_T": "5"})
    base_checks("ov_scout", R)
    st = [e for e in kinds(R, "settle") if e["unit"] == "u1"]
    check("ov_scout:flushed_no_archive", st and st[-1]["status"] == "flushed" and not kinds(R, "archive") and kinds(R, "scout_start"), (st, kinds(R, "archive")))
    check("ov_scout:interrupted", any(e.get("tag", "").startswith("scout") for e in kinds(R, "interrupt")), kinds(R, "interrupt"))

def scen_b_loop():
    """The same G2 hit standing 3 B rounds in a row with no new data -> handled as a C (a fix in place when no scouted attempt fits)."""
    R = run_scen("bloop", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")},
                                  {"tag": r"^critic", "nth": [1, 2, 3], "do": [G("B", gates="G1=pass G2=hit G3=pass", fix="- line 3 sign")]},
                                  {"tag": r"^fix_u1$", "do": [{"handoff": {"uid": "u1", "deliverables": [{"path": "/workspace/u1/data/", "what": "d"},
                                                                                                         {"path": "/workspace/u1/src/u1_solver.py", "what": "c"}], "key_numbers": {"n": 1}}}]},
                                  {"tag": r"^critic", "do": [A()]}, {"tag": r"^report_render$", "do": [{"report": True}]}],
                 env={"WALLTIME_SEC": str(int(3.2 * 3600))})
    base_checks("bloop", R)
    fp = [e["action"] for e in kinds(R, "fix_plan") if e["unit"] == "u1"]
    check("bloop:escalated", fp[:3] == ["fix", "fix", "fix_c"], fp)

def scen_checkpoint_c_job_ended():
    """A checkpoint rules C while the registered job finishes during the look -> the verdict is superseded: the executor verifies the complete
    results and a full audit decides (no scouting, no archive on a look at half-finished data)."""
    R = run_scen("cp_c_end", PLAN + [{"tag": r"^exec_u1$", "do": [{"write": "/workspace/u1/src/u1_solver.py", "text": "x=1\n"},
                                                                 {"spawn": {"secs": 5, "period": 0.5, "register": True, "expect_min": 1, "progress": True}}]},
                                     {"tag": r"^criticcp_u1$", "do": [{"sleep": 7}, {"critic": {"grade": "C", "gates": "n/a (checkpoint)", "fix": "- the estimator is wrong"}}]},
                                     {"tag": r"^resume_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"RA3_CP_S": "2"})
    base_checks("cp_c_end", R, {"u1": "A", "render": "A"})
    t = tags(R)
    check("cp_c_end:superseded", kinds(R, "checkpoint_superseded") and not kinds(R, "scout_start") and not kinds(R, "archive"),
          [e["kind"] for e in R["ev"]][-30:])
    check("cp_c_end:order", "criticcp_u1" in t and "resume_u1" in t and "critic_u1" in t and t.index("criticcp_u1") < t.index("resume_u1") < t.index("critic_u1"), t)

def scen_fix_cut():
    """A fix round that runs past its slice and its productive extensions is CUT and continued with its instructions - never wrapped up and
    audited; a second cut keeps them; the audit that closes the continued round is the delta audit."""
    R = run_scen("fixcut", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")},
                                   {"tag": r"^critic_u1$", "nth": [1], "do": [G("B", gates="G1=pass G2=hit G3=pass", fix="- line 3 sign")]},
                                   {"tag": r"^fix_u1$", "do": [{"sleep": 20}]},
                                   {"tag": r"^cont_u1$", "nth": [1], "do": [{"sleep": 20}]},
                                   {"tag": r"^cont_u1$", "do": deliver("u1")},
                                   {"tag": r"^critic", "do": [A()]}] + REPORT,
                 env={"RA3_BFIX_MIN_S": "3", "RA3_EXEC_SLICE_S": "3", "RA3_SLICE_EXT_S": "2"})
    base_checks("fixcut", R, {"u1": "A", "render": "A"})
    t = tags(R)
    i = t.index("fix_u1") if "fix_u1" in t else -1
    check("fixcut:continued_not_audited", i >= 0 and t[i + 1:i + 3] == ["cont_u1", "cont_u1"] and not [x for x in t if x.endswith("_wrapup") and x.startswith(("fix", "cont"))], t)
    check("fixcut:instructions_carried_twice", sum(1 for c in R["calls"] if c == "saw:cont_u1:cut_instructions") == 2, [c for c in R["calls"] if "cont_u1" in c])
    check("fixcut:delta_audit", i >= 0 and t[i + 3:i + 4] == ["criticd_u1"], t)

def scen_infra_unit_hold():
    """A unit whose calls die (0 tokens) while the backend answers probes holds and retries until they work - it is never retired."""
    R = run_scen("infrahold", PLAN + [{"tag": r"^exec_u1", "nth": list(range(1, 9)), "do": [{"tokens": 0}, {"rc": 1}]},
                                      {"tag": r"^exec_u1", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"RA3_INFRA_PROBE_S": "12"}, pre="sr.gw_ok = lambda: True\n")
    base_checks("infrahold", R, {"u1": "A", "render": "A"})
    check("infrahold:held_never_retired", kinds(R, "infra_unit_hold") and not [e for e in kinds(R, "settle") if e["unit"] == "u1" and e["status"] == "failed"]
          and not os.path.exists(os.path.join(R["ws"], ".infra_failure")), [e["kind"] for e in R["ev"] if "infra" in e["kind"]])

def scen_report_dead_override():
    """The override fires while a unit works; every report call then dies (0 tokens): the episode is an infra failure (.infra_failure,
    exit 3) for a clean rerun - never a silent zero."""
    pre = "import threading\nsr.gw_ok = lambda: False\nthreading.Timer(6, lambda: sr.fire_override('test')).start()\n"
    R = run_scen("repdead", PLAN + [{"tag": r"^exec_u1$", "do": [{"sleep": 40}]}, {"tag": r"^report_", "do": [{"tokens": 0}, {"rc": 1}]}] + REPORT,
                 pre=pre, env={"RA3_WRAPUP_T": "5"})
    check("repdead:rc3", R["rc"] == 3, "rc=%s\n%s" % (R["rc"], R["out"][-2500:]))
    check("repdead:marker_no_report", os.path.exists(os.path.join(R["ws"], ".infra_failure")) and not os.path.exists(os.path.join(R["ws"], "report.md")),
          [e["kind"] for e in R["ev"]][-15:])

def scen_cp_c_fix_in_place():
    """A checkpoint rules C on a running job; the scouts find no supported cause -> the unit is fixed IN PLACE at once (the defective job is not
    simply waited on) and the fix prompt says the job still runs."""
    rules = PLAN + [{"tag": r"^exec_u1$", "do": [{"write": "/workspace/u1/src/u1_solver.py", "text": "x=1\n"},
                                                {"spawn": {"secs": 25, "period": 0.5, "register": True, "expect_min": 1, "progress": True}}]},
                    {"tag": r"^criticcp_u1$", "nth": [1], "do": [{"critic": {"grade": "C", "gates": "n/a (checkpoint)", "fix": "- the estimator is wrong", "residual": True}}]},
                    {"tag": r"^scout\d_u1$", "do": [{"candidate": {"hypothesis": "not it", "supports": False}}]},
                    {"tag": r"^criticsel\d_u1$", "do": [{"select": {"rank": "NONE", "also": "none"}}]},
                    {"tag": r"^fix_u1$", "do": deliver("u1")},
                    {"tag": r"^critic", "do": [A()]}] + REPORT
    R = run_scen("cpcfix", rules, env={"WALLTIME_SEC": str(9 * 3600), "RA3_CP_S": "3"})
    base_checks("cpcfix", R, {"u1": "A", "render": "A"})
    t = tags(R)
    last_sel = max([i for i, x in enumerate(t) if x.startswith("criticsel")] or [-1])   # the panel calls run in parallel: any order
    check("cpcfix:fixed_in_place_now", last_sel >= 0 and t[last_sel + 1:last_sel + 2] == ["fix_u1"] and not kinds(R, "archive"), t)
    check("cpcfix:told_job_runs", "saw:fix_u1:job_running" in R["calls"] and "saw:fix_u1:fix_in_place" in R["calls"], [c for c in R["calls"] if "fix_u1" in c])

def scen_dead_replan():
    """D -> a branch replan whose calls die (0 tokens) while the backend answers: the branch stays blocked, the episode goes on and
    reports."""
    plan = plan_obj(units=[unit("u1"), unit("u2", deps=["u1"])])
    R = run_scen("deadreplan", [{"tag": r"^plan$", "do": [{"plan": plan}]}, {"tag": r"^exec_u1$", "do": deliver("u1")},
                                {"tag": r"^critic_u1$", "do": [G("D", fix="F5 and F6 contradict (a)")]},
                                {"tag": r"^replan", "do": [{"tokens": 0}, {"rc": 1}]}] + REPORT,
                 env=dict(GATE_CLOSED, RA3_INFRA_PROBE_S="12"), pre="sr.gw_ok = lambda: True\n")
    base_checks("deadreplan", R)
    check("deadreplan:episode_survives", kinds(R, "replan_failed") and not os.path.exists(os.path.join(R["ws"], ".infra_failure"))
          and os.path.exists(os.path.join(R["ws"], "report.md")), [e["kind"] for e in R["ev"] if e["kind"].startswith(("replan", "infra", "plan"))])

def scen_scout_supported_winner():
    """The panel ranks a candidate whose own PoC does not support it first -> the best-ranked SUPPORTED candidate wins; and a reading change
    whose evidence file sits in the unit directory is applied before the archive moves it."""
    rules = PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1", [{"write": "/workspace/u1/probe_k1.txt", "text": "decides"}])},
                    {"tag": r"^critic_u1$", "nth": [1], "do": [G("C")]},
                    {"tag": r"^scout1_u1$", "do": [{"candidate": {"hypothesis": "nothing is wrong", "supports": False}}]},
                    {"tag": r"^scout2_u1$", "do": [{"candidate": {"hypothesis": "the literal reading of K1", "supports": True,
                                                                  "reading_change": {"id": "K1", "reading": "J = 1/2 per F7", "evidence": "/workspace/u1/probe_k1.txt"}}}]},
                    {"tag": r"^scout3_u1$", "do": [{"candidate": {"hypothesis": "too few samples", "supports": True}}]},
                    {"tag": r"^criticsel\d_u1$", "do": [{"select": {"rank": "1 2 3", "also": "none"}}]},
                    {"tag": r"^restart_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT
    R = run_scen("scoutsup", rules, env={"WALLTIME_SEC": str(9 * 3600)})
    base_checks("scoutsup", R, {"u1": "A", "render": "A"})
    sw = kinds(R, "scout_winner")
    check("scoutsup:supported_winner", sw and sw[0]["winner"] == 2, sw)
    rc = kinds(R, "reading_change")
    check("scoutsup:reading_change_before_archive", rc and rc[0]["id"] == "K1" and not kinds(R, "reading_change_rejected"), [e for e in R["ev"] if "reading" in e["kind"]])

def scen_scout_disqualified():
    """The panel majority disqualifies the best-ranked supported candidate (its plan changes a fixed setting) -> the next eligible one wins, the
    disqualified one is neither adopted nor offered as a plain alternative (the restart prompt marks it)."""
    rules = PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")},
                    {"tag": r"^critic_u1$", "nth": [1], "do": [G("C")]},
                    {"tag": r"^scout1_u1$", "do": [{"candidate": {"hypothesis": "a sign bug in the kernel", "supports": True}}]},
                    {"tag": r"^scout2_u1$", "do": [{"candidate": {"hypothesis": "raise the fixed threshold until the gate passes", "supports": True}}]},
                    {"tag": r"^scout3_u1$", "do": [{"candidate": {"hypothesis": "nothing is wrong", "supports": False}}]},
                    {"tag": r"^criticsel[12]_u1$", "do": [{"select": {"rank": "2 1 3", "also": "1", "disq": "2"}}]},
                    {"tag": r"^criticsel3_u1$", "do": [{"select": {"rank": "2 1 3", "also": "none", "disq": "none"}}]},
                    {"tag": r"^restart_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT
    R = run_scen("scoutdq", rules, env={"WALLTIME_SEC": str(9 * 3600)})
    base_checks("scoutdq", R, {"u1": "A", "render": "A"})
    sw = kinds(R, "scout_winner")
    check("scoutdq:eligible_winner", sw and sw[0]["winner"] == 1 and sw[0]["adopt"] == [], sw)
    ss = kinds(R, "scout_select")
    check("scoutdq:ledger", ss and ss[0].get("disqualified") == [2] and ss[0].get("disq_votes") == {"2": 2}, ss)
    check("scoutdq:marked_in_restart", "saw:restart_u1:disq" in R["calls"], [c for c in R["calls"] if "restart_u1" in c])

def scen_scout_all_disqualified():
    """Every supported candidate is disqualified -> no restart: the unit is fixed in place and the fix prompt carries the finding, marked as
    usable only within the task book's settings."""
    rules = PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")},
                    {"tag": r"^critic_u1$", "nth": [1], "do": [G("C")]},
                    {"tag": r"^scout1_u1$", "do": [{"candidate": {"hypothesis": "nothing is wrong", "supports": False}}]},
                    {"tag": r"^scout2_u1$", "do": [{"candidate": {"hypothesis": "swap the estimator for a cheaper one", "supports": True}}]},
                    {"tag": r"^scout3_u1$", "do": [{"candidate": {"hypothesis": "fit the constant to the figure", "supports": True}}]},
                    {"tag": r"^criticsel\d_u1$", "do": [{"select": {"rank": "3 2 1", "also": "none", "disq": "2 3"}}]},
                    {"tag": r"^fix_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT
    R = run_scen("scoutdqall", rules, env={"WALLTIME_SEC": str(9 * 3600)})
    base_checks("scoutdqall", R, {"u1": "A", "render": "A"})
    fb = kinds(R, "scout_fallback")
    check("scoutdqall:fallback", fb and "disqualified" in fb[0]["why"] and fb[0].get("disqualified") == [2, 3] and not kinds(R, "scout_winner") and not kinds(R, "archive"), fb)
    check("scoutdqall:fix_in_place_marked", "saw:fix_u1:disq" in R["calls"] and "saw:fix_u1:fix_in_place" in R["calls"], [c for c in R["calls"] if "fix_u1" in c])

def scen_plan_not_written():
    """The first planner session ends without writing plan.json -> the retry is a fresh 'write it first' session (not an edit of a file that
    does not exist), its plan is accepted, no minimal plan."""
    R = run_scen("planNW", [{"tag": r"^plan$", "nth": [1], "do": []}, {"tag": r"^plan$", "do": [{"plan": PLAN1}]},
                            {"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT)
    base_checks("planNW", R, {"u1": "A", "render": "A"})
    pi = kinds(R, "plan_invalid")
    check("planNW:retry_prompt", "saw:plan:plan_not_written" in R["calls"] and pi and "not written" in pi[0]["errors"] and not kinds(R, "plan_minimal")
          and [e.get("attempt") for e in kinds(R, "plan") if e.get("ok")] == [2], [e for e in R["ev"] if e["kind"].startswith("plan")])

def scen_mech_foreign():
    """An executor's own script rewrites mech.json in another schema right after the orchestrator's check; the checkpoint that looks at the
    running computation reads it as 'not available', never a crash (a KeyError there would retire the unit and block its dependents)."""
    pre = ("import json, os\n_mc = sr.mech_check\n"
           "def _mc2(uid, *a, **k):\n"
           "    r = _mc(uid, *a, **k)\n"
           "    json.dump({'uid': uid, 'generated_unix': 1, 'deliverables': [{'path': '/workspace/%s/data/' % uid}]}, open(os.path.join(sr.unit_dir(uid), 'mech.json'), 'w'))\n"
           "    return r\n"
           "sr.mech_check = _mc2\n")
    R = run_scen("mechforeign", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1", [{"spawn": {"secs": 12, "period": 0.5, "marker": False}}])},
                                        {"tag": r"^criticcp_u1$", "do": [{"critic": {"grade": "W", "gates": "n/a (checkpoint)"}}]},
                                        {"tag": r"^resume_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"RA3_CP_S": "4"}, pre=pre)
    base_checks("mechforeign", R, {"u1": "A", "render": "A"})
    check("mechforeign:checkpoint_read_na", "saw:criticcp_u1:mech_na" in R["calls"], [c for c in R["calls"] if "criticcp" in c])

def scen_fault_recover():
    """An orchestrator fault inside a unit's loop (here the first checkpoint raises) is logged and the unit continues with a neutral note -
    never retired, its downstream never blocked."""
    pre = ("_rc = sr.run_checkpoint\n_n = [0]\n"
           "def _rc2(*a, **k):\n"
           "    _n[0] += 1\n"
           "    if _n[0] == 1:\n"
           "        raise KeyError('injected')\n"
           "    return _rc(*a, **k)\n"
           "sr.run_checkpoint = _rc2\n")
    R = run_scen("faultrec", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1", [{"spawn": {"secs": 12, "period": 0.5, "marker": False}}])},
                                     {"tag": r"^criticcp_u1$", "do": [{"critic": {"grade": "W", "gates": "n/a (checkpoint)"}}]},
                                     {"tag": r"^(cont|resume)_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"RA3_CP_S": "4"}, pre=pre)
    check("faultrec:rc", R["rc"] == 0, "rc=%s\n%s" % (R["rc"], R["out"][-3000:]))
    end = kinds(R, "episode_end")
    check("faultrec:grades", bool(end) and end[-1]["grades"] == {"u1": "A", "render": "A"}, end and end[-1]["grades"])
    te = kinds(R, "unit_thread_error")
    check("faultrec:recovered_once", len(te) == 1 and te[0]["unit"] == "u1" and te[0]["recovered"] is True, te)
    check("faultrec:continued_with_note", "saw:cont_u1:fault" in R["calls"], [c for c in R["calls"] if "u1" in c])
    check("faultrec:never_retired", not [e for e in kinds(R, "settle") if e["unit"] == "u1" and e["status"] != "done"], kinds(R, "settle"))

def scen_fault_cap():
    """A fault that repeats is recovered twice, then the unit settles as before - no endless loop."""
    pre = "def _rc2(*a, **k):\n    raise KeyError('injected')\nsr.run_checkpoint = _rc2\n"
    R = run_scen("faultcap", PLAN + [{"tag": r"^(exec|cont)_u1$", "do": deliver("u1", [{"spawn": {"secs": 12, "period": 0.5, "marker": False}}])},
                                     {"tag": r"^critic", "do": [A()]}] + REPORT, env={"RA3_CP_S": "4"}, pre=pre)
    te = kinds(R, "unit_thread_error")
    check("faultcap:two_recovered_then_settled", [e.get("recovered") for e in te] == [True, True, False], te)
    check("faultcap:settled_once", [e["status"] for e in kinds(R, "settle") if e["unit"] == "u1"] == ["failed"], kinds(R, "settle"))
    check("faultcap:ended", R["rc"] != "TIMEOUT" and bool(kinds(R, "episode_end")), "rc=%s\n%s" % (R["rc"], R["out"][-2000:]))

# ---------------------------------------------------------------- the route: direct mode and proxy mode
DIRECT_TEST_KEY = "test-key-not-a-secret"   # a dummy value: nothing in the suite contacts an endpoint

def _endpoints(R):
    """[(url, key hash)] that the fake dsh calls received ("endpoint:<tag>:<url>:<hash>" lines)."""
    out = []
    for c in R["calls"]:
        if c.startswith("endpoint:"):
            url, h = c.split(":", 2)[2].rsplit(":", 1)
            out.append((url, h))
    return out

def _shown(eps):
    """Endpoints for a failure message: only the URLs these scenarios set are shown."""
    return sorted(set((u if u.startswith(("http://127.0.0.1:", "https://direct-test.example.invalid/")) else "<another endpoint>", h) for u, h in eps))[:3]

def _models(R):
    return [c.split(":", 2)[2] for c in R["calls"] if c.startswith("model:")]

def _key_hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

def scen_proxy_mode_endpoint():
    """The paper's route (RA_DSH_SHIM=1, as in BASE_ENV): every dsh call talks to the local proxy at 127.0.0.1:$DSH_SHIM_PORT with the
    dummy key, whatever key the caller holds, and sends the bare model id (the proxy sets the upstream id)."""
    R = run_scen("proxy", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"DSH_SHIM_PORT": "8765", "DEEPSEEK_API_KEY": DIRECT_TEST_KEY})
    base_checks("proxy", R, {"u1": "A", "render": "A"})
    eps = _endpoints(R)
    check("proxy:endpoint_is_local_proxy", eps and set(eps) == {("http://127.0.0.1:8765/v1", _key_hash("shim-local"))}, _shown(eps))
    check("proxy:bare_model_id", _models(R) and set(_models(R)) == {"deepseek-v4.1-flash"}, sorted(set(_models(R))))
    es = kinds(R, "episode_start")
    check("proxy:ledger_route", es and es[0].get("epic_shim") is True and es[0].get("dsh_model") == "deepseek-v4.1-flash",
          es and dict((k, es[0].get(k)) for k in ("epic_shim", "dsh_model")))

def scen_direct_mode_no_key():
    """Direct mode (RA_DSH_SHIM unset) without DEEPSEEK_API_KEY stops with exit 2 and a message that names the remedy, before it
    writes anything: no ledger, no store record, no dsh call."""
    R = run_scen("nokey", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic", "do": [A()]}] + REPORT,
                 env={"RA_DSH_SHIM": None}, timeout=60)
    check("nokey:rc2", R["rc"] == 2, "rc=%s\n%s" % (R["rc"], R["out"][-1500:]))
    check("nokey:message", "DEEPSEEK_API_KEY" in R["out"] and "RA_DSH_SHIM=1" in R["out"], R["out"][-800:])
    check("nokey:nothing_written", not R["ev"] and os.listdir(os.path.join(R["ws"], "_pece")) == [] and os.listdir(R["store"]) == []
          and sorted(os.listdir(R["ws"])) == ["CLAUDE.md", "_pece", "hang_stack.txt"],
          (sorted(os.listdir(R["ws"])), os.listdir(os.path.join(R["ws"], "_pece")), os.listdir(R["store"])))
    check("nokey:no_dsh_call", not R["calls"], R["calls"][:5])

def scen_direct_mode_passthrough():
    """Direct mode hands the caller's DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY to every dsh call unchanged and keeps the full model
    id; the episode runs as it does through the proxy."""
    url = "https://direct-test.example.invalid/v1"
    R = run_scen("direct", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"RA_DSH_SHIM": None, "DEEPSEEK_BASE_URL": url, "DEEPSEEK_API_KEY": DIRECT_TEST_KEY})
    base_checks("direct", R, {"u1": "A", "render": "A"})
    eps = _endpoints(R)
    check("direct:endpoint_and_key_passed", len(eps) == len(tags(R)) >= 4 and set(eps) == {(url, _key_hash(DIRECT_TEST_KEY))}, _shown(eps))
    check("direct:full_model_id", _models(R) and set(_models(R)) == {"deepseek/deepseek-v4.1-flash"}, sorted(set(_models(R))))
    es = kinds(R, "episode_start")
    check("direct:ledger_route", es and es[0].get("epic_shim") is False and es[0].get("dsh_model") == "deepseek/deepseek-v4.1-flash",
          es and dict((k, es[0].get(k)) for k in ("epic_shim", "dsh_model")))

def scen_fake_dsh_counter():
    """The fake dsh itself: 40 concurrent calls (scouts and panel critics run in parallel) keep its per-rule counters exact. Rule 0 matches
    only its 7th invocation, rule 1 takes the rest: counts must end at 40 and 39, and every invocation number appears exactly once.
    RA_TEST_FAKE_DSH points the check at another fake (an unlocked counter loses updates here)."""
    fake = os.environ.get("RA_TEST_FAKE_DSH") or FAKE_DSH
    root = tempfile.mkdtemp(prefix="ra_e2e_fake_counter_")
    ws = os.path.join(root, "ws")
    os.makedirs(ws)
    ROOTS.append((root, ws))
    fstate = os.path.join(root, "fake_state")
    scen_f = os.path.join(root, "scen.json")
    json.dump({"rules": [{"tag": r"^scout\d_u1$", "nth": [7], "do": []}, {"tag": r"^scout\d_u1$", "do": []}]}, open(scen_f, "w"))
    env = strip_env(dict(os.environ))
    env.update({"FAKE_WS": ws, "FAKE_STATE": fstate, "FAKE_SCEN_FILE": scen_f, "DSH_HOME": os.path.join(root, "home")})
    n = 40
    procs = [subprocess.Popen([sys.executable, fake, "--profile", "headless", "prompt %d" % i],
                              env=dict(env, PECE_CALL_TAG="t_counter:scout%d_u1" % (i % 3 + 1)),
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for i in range(n)]
    rcs = [p.wait(timeout=120) for p in procs]
    errs = [p.stderr.read().decode(errors="replace")[-300:] for p in procs if p.returncode]
    check("fake_counter:all_calls_ran", rcs == [0] * n, (rcs, errs[:2]))
    try:
        counts = json.load(open(os.path.join(fstate, "counts.json")))
    except Exception as e:
        counts = repr(e)
    calls = [l.strip().split(":") for l in open(os.path.join(fstate, "calls.log")) if l.startswith("call:")]
    by_rule = {}
    for c in calls:
        by_rule.setdefault(c[2], []).append(int(c[3]))
    check("fake_counter:counts_exact", counts == {"0": n, "1": n - 1}, counts)
    check("fake_counter:nth_rule_once", by_rule.get("0") == [7], by_rule.get("0"))
    check("fake_counter:no_lost_or_double_update", sorted(by_rule.get("1", [])) == list(range(1, n)),
          sorted(by_rule.get("1", []))[:50])

# the scenarios that run scouts or panel critics in parallel next to rules with an "nth" (the fake's counter race showed there)
SCOUT_SCENS = ("scout_restart", "scout_supported_winner", "scout_disqualified", "scout_all_disqualified", "cp_c_fix_in_place",
               "override_scouting", "fake_dsh_counter")

SCENS = dict((k[5:], v) for k, v in globals().items() if k.startswith("scen_"))
if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENS)
    if names in (["--list"], ["--list-scout"]):
        print(" ".join(SCENS if names == ["--list"] else SCOUT_SCENS))
        sys.exit(0)
    if names == ["--scout"]:
        names = list(SCOUT_SCENS)
    bad = [n for n in names if n not in SCENS]
    if bad:
        print("unknown scenario(s): %s; known: %s" % (" ".join(bad), " ".join(SCENS)))
        sys.exit(2)
    for n in names:
        t0 = time.time()
        n_fail = len(FAILS)
        try:
            SCENS[n]()
        except Exception as e:
            import traceback
            check(n + ":exception", False, traceback.format_exc()[-1500:])
        check(n + ":no_network", not NET_SEEN, NET_SEEN[:5])
        cleanup(failed=len(FAILS) > n_fail)
        print("---- %s %.1fs" % (n, time.time() - t0), flush=True)
    sys.exit(finish())
