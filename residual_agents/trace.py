#!/usr/bin/env python3
"""trace.py - the visible flow of one RA episode, rendered from its ledger.

Reads _pece/ledger.jsonl (+ plan.json / state.json when present) and writes, next to it:
  trace.json    - every event with an id, unit, role, start/end, tokens, cost, result and a caused_by chain
  timeline.html - a self-contained SVG Gantt chart: one row per unit + planner + report + override; segments coloured
                  by phase (pending / blocked on whom / exec / critic / repair / done / failed / blocked / flushed);
                  critic verdicts (A/B/C/D) on the segments; planner calls, override, foreign_write, frozen_modified,
                  kill, taint, gate ... as markers
  timeline.txt  - the same as text (one line per lane, one character per time bucket) + the marker list
Usage: python3 trace.py <run dir or its _pece dir> [more dirs...]        (writes into each _pece dir)
       python3 trace.py --out <dir> <run dir>                            (writes into <dir> instead)
       ra trace <run dir>                                                (the same through the RA command line)
As a module: build(pece_dir, out_dir=None, plan_path=None) -> [written files].
The KINDS table below maps ledger event kinds to markers (unknown kinds become plain markers); some entries only serve
ledgers written by earlier harness versions. A unit lane gets a "waiting" segment between wait_start and the end of the
wait (the unit's registered background job runs, no model call); resume_<uid> and restart_<uid> calls are exec-lane
segments labelled "resume" and "restart"; a retired unit's lane ends "RETIRED (failed)", its blocked dependents
"BLOCKED by <id>". Fields an older ledger lacks are computed, so every ledger renders.
No third-party dependencies (python3 >= 3.6)."""
import collections, html, json, os, re, sys

# ---------------------------------------------------------------- vocabulary
PHASE_COLOR = {"pending": "#e6e6e6", "blocked_on": "#d9d9d9", "exec": "#4a90d9", "bfix": "#8cbbe8", "rethink": "#2a5f9e",
               "wrapup": "#9fc5ea", "critic": "#f5a623", "wait_critic": "#fbe3b5", "repair_wait": "#cfcfcf",
               "done": "#3cb371", "failed": "#d0021b", "blocked": "#8b1a1a", "flushed": "#9b59b6", "skip": "#9a9a9a",
               "running": "#cccccc", "plan": "#50c8b4", "replan": "#2a9d8f", "compile": "#b8e986", "report": "#bd10e0",
               "report_critic": "#f5a623", "finalize": "#c77dff", "override": "#ff9f43", "flush": "#ffcf99", "rescue": "#7b8fe0",
               "waiting": "#f4d03f", "wrapped": "#d7bde2", "void": "#f0f0f0"}
PHASE_TXT = {"pending": ".", "blocked_on": ":", "exec": "=", "bfix": "b", "rethink": "r", "wrapup": "~", "critic": "c",
             "wait_critic": "w", "repair_wait": "-", "done": "D", "failed": "F", "blocked": "B", "flushed": "X", "skip": "S",
             "running": "?", "plan": "p", "replan": "R", "compile": "k", "report": "P", "report_critic": "c", "finalize": "P",
             "override": "O", "flush": "o", "rescue": "e", "waiting": "W", "wrapped": "x", "void": "v"}
# ledger kinds that become MARKERS on a lane (label template; {x} fields from the event)
KINDS = {
    "launch": ("launch", "launch {unit} ({cores}c)"), "mech": ("mech", "mech {ok_txt}{mech_note}"), "fix_plan": ("fix", "{grade} -> {action} (afford={affordable})"),
    "kill": ("kill", "KILL {reason}"), "taint": ("taint", "taint {reason}"), "taint_resolved": ("taint_ok", "taint lifted by {by}"),
    "taint_exhausted": ("taint", "taint budget spent"), "gate_downgraded": ("note", "{gate} downgraded"),
    "upstream_failed": ("notice", "upstream {upstream} failed"), "upstream_suspect": ("notice", "upstream {upstream} suspect"),
    "upstream_changed": ("notice", "upstream {upstream} changed"), "requeue": ("notice", "requeued"),
    "profile_gate": ("gate", "gate {gate_txt}"), "profile_gate_skipped": ("gate", "gate skipped"), "self_profile": ("gate", "self-timing {over_txt}"),
    "self_profile_missing": ("note", "no self-timing"), "rescope": ("note", "rescope {accepted}"), "repair_bounded": ("fix", "bounded redo {budget_min}m"),
    "pool_shared": ("note", "shared grant {cores}c"), "infra_retry": ("infra", "infra retry"), "infra_unit_dead": ("infra", "infra dead"),
    "gateway_wait": ("infra", "gateway wait"), "stall_wait": ("infra", "stall wait"), "gw_cooldown_wait": ("infra", "gateway cooldown"),
    "infra_failure": ("infra", "INFRA FUSE"), "unit_thread_error": ("infra", "thread error"),
    "foreign_write": ("foreign", "foreign write {change}: {base} -> {who}"), "frozen_modified": ("frozen", "FROZEN {base} of {owner} {status}"),
    "stray_write": ("stray", "stray writes: {n}"), "leftover_killed": ("leftover", "leftover killed: {n} process(es)"),
    "leftover_kept": ("leftover", "leftover KEPT: {n} process(es) -> implicit wait"),
    "wait_start": ("wait", "{wait_lead} job pid {pid} (expect {expect_min}m, bound {max_s}s)"), "wait_done": ("wait", "job ended ({how}) after {seconds}s"),
    "wait_timeout": ("wait_bad", "WAIT TIMEOUT after {seconds}s: job killed"), "wait_abort": ("override", "wait aborted ({reason}): job killed"),
    "archive": ("archive", "archived -> {dir} ({reason})"), "restart": ("restart", "RESTART round {round} (grade {grade}): residual by {residual_by}"),
    "replan_cap": ("replan", "replan cap ({replans}): the branch stays blocked"), "writes_outside_own_dir": ("outside", "writes outside own dir by {by}: {n}"),
    "d_rejected": ("fix", "D without a task-book ground -> taken as C"),
    "root_copy": ("report", "final files copied to the root: {files}"),
    "reap_deferred": ("note", "sweep deferred (job pid {pid} registered)"), "wait_pid_invalid": ("wait_bad", "registered pid {pid} does not exist: marker + bound only"),
    "wait_pid_from_file": ("wait", "bg.pid {file_pid} / handoff pid {handoff_pid} -> chosen {chosen} (alive: file {file_alive}, handoff {handoff_alive})"), "wait_stalled": ("wait_bad", "bg.log quiet for {quiet_s}s (information)"),
    "wait_extended": ("wait", "bound {from_s}s reached, job alive and writing -> extended once to {to_s}s"), "wait_unaffordable": ("wait_bad", "registration of {expect_min}min exceeds the {cap_s}s affordable -> not waited, executor told the budget"),
    "wait_stalled_end": ("wait_bad", "wait ended after {seconds}s: pid unverifiable, log quiet {quiet_s}s, no marker -> executor resumed"), "slice": ("note", "session slice {slice_s}s reached ({mode}) -> wrap-up asked for a handoff / registration"),
    "handoff_settled": ("note", "complete handoff.json on disk {quiet_s}s with nothing following -> turn ended by the orchestrator"),
    "frozen_restored": ("frozen_ok", "frozen {base} restored"), "freeze": ("freeze", "frozen {files} files"), "unfreeze": ("freeze", "unfrozen ({reason})"),
    "settle": ("settle", "{status} ({reason})"), "retire": ("retire", "retired: no replan ({why_no_replan})"),
    "replan_trigger": ("replan", "replan requested ({reason})"), "replan_applied": ("replan", "replan applied: {units}"),
    "replan_failed": ("replan", "replan failed"), "plan_invalid": ("plan_bad", "plan invalid (attempt {attempt})"),
    "plan": ("plan_ok", "plan {ok} attempt {attempt}{cp_note}: {units}"), "compile": ("compile", "compile (cache {cache_hit})"),
    "task_book": ("note", "task book {items} items"), "override": ("override", "OVERRIDE (remaining {remaining_s}s)"),
    "flush": ("override", "flush {units}"), "flush_done": ("override", "flush done ({waited_s}s)"), "interrupt": ("override", "interrupted ({reason})"),
    "report_start": ("report", "report start ({mode})"), "report": ("report", "report written ok={ok}"), "report_grade": ("fix", "report grade {grade}"),
    "finalize_report": ("report", "report.md retry"), "critic_skipped": ("note", "critic skipped (override)"),
    "rescue_start": ("rescue", "rescue"), "rescue_grade": ("fix", "rescue grade {grade}"), "live_compute_wait": ("infra", "compute wait"),
    "bus_out": ("bus", "bus {msg_kind}"), "resource_warn": ("note", "resource clamp"), "exec_nowrite": ("note", "nowrite"),
    "episode_start": ("episode", "start {version}"), "episode_end": ("episode", "end"), "trace": ("note", "trace"),
}   # kinds that only earlier harness versions wrote stay, so that their ledgers still render
MARK_COLOR = {"kill": "#d0021b", "taint": "#c0392b", "taint_ok": "#3cb371", "foreign": "#e67e22", "frozen": "#d0021b", "frozen_ok": "#3cb371",
              "freeze": "#3cb371", "settle": "#333", "retire": "#7f8c8d", "replan": "#2a9d8f", "plan_bad": "#d0021b", "plan_ok": "#50c8b4",
              "override": "#ff9f43", "infra": "#8e44ad", "gate": "#f39c12", "fix": "#555", "mech": "#555", "notice": "#95a5a6",
              "launch": "#4a90d9", "report": "#bd10e0", "note": "#aaa", "bus": "#bbb", "compile": "#b8e986", "episode": "#000", "rescue": "#7b8fe0",
              "wait": "#d4ac0d", "wait_bad": "#d0021b", "leftover": "#8e44ad", "stray": "#f0c27b",
              "archive": "#7f8c8d", "restart": "#2a5f9e", "dag": "#c0392b", "outside": "#f0c27b"}

def _load(pece):
    ev = []
    for ln in open(os.path.join(pece, "ledger.jsonl"), errors="replace"):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if isinstance(e, dict) and "kind" in e:
            ev.append(e)
    starts = [i for i, e in enumerate(ev) if e["kind"] == "episode_start"]
    if len(starts) > 1:   # a directory re-used by several episodes: the LAST episode is the one on disk
        ev = ev[starts[-1]:]
    return ev

def _tag_parts(tag):
    """(role, uid, mode, wrapup) of a call tag: exec/bfix/rethink/rescue -> exec lane of the unit, critic* -> critic,
    plan/replan/compile -> planner, report*/finalize -> report."""
    t = tag or ""
    wrap = t.endswith("_wrapup") or "_wrapup_" in t
    t = re.sub(r"(_wrapup|_gwretry\d*|_only)+$", "", t)
    m = re.match(r"^(exec|bfix|fix|cont|resume|restart|rethink|rescue|critic|criticd|criticcp|criticfmt|criticsel|scout|report|finalize|plan|replan|compile|rescope)\d*(?:_(.+))?$", t)
    if not m:
        return "other", None, t, wrap
    role, uid = m.group(1), m.group(2)
    if role in ("plan", "replan", "compile"):
        return "planner", None, role, wrap
    if role in ("critic", "criticd", "criticcp", "criticfmt", "criticsel", "rescope", "scout"):
        return "critic", uid, role, wrap
    if role in ("report", "finalize"):
        return "report", uid, role, wrap
    return "exec", uid, ("fresh" if role == "exec" else role), wrap

def _units_of(ev, plan):
    order = []
    by = {}
    for u in plan or []:
        if isinstance(u, dict) and u.get("id"):
            order.append(u["id"]); by[u["id"]] = u
    for e in ev:
        if e["kind"] in ("plan", "replan_applied"):
            for x in (e.get("units") or []):
                if x not in by:
                    order.append(x); by[x] = {"id": x}
    for e in ev:
        u = e.get("unit")
        if e["kind"] == "call":
            _, u, _, _ = _tag_parts(e.get("tag"))
        if u and u not in by:
            order.append(u); by[u] = {"id": u}
    return order, by

def _fmt(t):
    t = max(0, int(t))
    return "%d:%02d" % (t // 3600, (t % 3600) // 60)

def build(pece, out_dir=None, plan_path=None):
    """Write trace.json / timeline.html / timeline.txt for one cell. Returns the written paths."""
    pece = pece.rstrip("/")
    if os.path.basename(pece) != "_pece" and os.path.isdir(os.path.join(pece, "_pece")):
        pece = os.path.join(pece, "_pece")
    out_dir = out_dir or pece
    ev = _load(pece)
    if not ev:
        raise ValueError("no ledger events in %s" % pece)
    plan = None
    for pp in (plan_path, os.path.join(pece, "plan.json")):
        if pp and os.path.exists(pp):
            try:
                plan = json.load(open(pp))
                break
            except Exception:
                pass
    state = {}
    try:
        state = json.load(open(os.path.join(pece, "state.json")))
    except Exception:
        pass
    es = ev[0] if ev[0]["kind"] == "episode_start" else {}
    t0 = es.get("ts", ev[0]["ts"])
    t_end = max(e.get("ts", t0) for e in ev)
    wall = es.get("wall") or 0
    version = es.get("version") or es.get("harness") or ""
    order, by = _units_of(ev, plan)
    render_ids = set(x for x in order if by[x].get("type") == "render")
    final_grades = {}
    for e in ev:
        if e["kind"] == "episode_end":
            final_grades = e.get("grades") or {}
    if not final_grades:
        final_grades = state.get("grades") or {}
    # -------- events with ids and caused_by
    events = []
    last_of = {}          # unit -> last event id (chain within a unit)
    done_ev = {}          # unit -> event id of its done settle / grade A critic (older ledgers)
    fail_ev = {}          # unit -> event id of its failed settle / degrade
    replan_trig = None
    last_planner = None
    for i, e in enumerate(ev):
        k = e["kind"]
        eid = "e%d" % (i + 1)
        role, uid, mode, wrap = (None, e.get("unit"), None, False)
        if k == "foreign_write" and uid == "ambiguous":   # judged on nobody; drawn on the lane of the turn that saw it
            uid = e.get("turn_of")
        t1 = e.get("ts", t0)
        tstart = t1
        if k == "call":
            role, uid, mode, wrap = _tag_parts(e.get("tag"))
            tstart = t1 - (e.get("dur") or 0)
        elif k in ("plan", "plan_invalid", "compile", "replan_applied", "replan_failed", "task_book"):
            role = "planner"
        elif k in ("report_start", "report", "report_grade", "finalize_report"):
            role = "report"
        elif k in ("override", "flush", "flush_done", "episode_start", "episode_end", "infra_failure"):
            role = "episode"
        elif uid:
            role = "unit"
        if uid in render_ids and role in ("unit", "exec", "critic"):
            role = "report"
        res = None
        if k == "critic":
            res = e.get("grade")
        elif k == "call":
            res = "rc=%s%s" % (e.get("rc"), (" " + str(e.get("interrupted"))) if e.get("interrupted") else "")
        elif k in ("settle",):
            res = e.get("status")
        elif k == "fix_plan":
            res = e.get("action") or ("affordable" if e.get("affordable") else "unaffordable")
        elif k == "mech":
            res = "ok" if e.get("ok") else "FAIL"
        elif k == "plan":
            res = "ok" if e.get("ok") else "invalid"
        # caused_by
        cause = None
        if k == "launch":
            deps = e.get("depends_on") or (by.get(uid, {}).get("depends_on") or [])
            cause = [done_ev[d] for d in deps if d in done_ev] or ([last_planner] if last_planner else None)
        elif k == "call" and role == "exec":
            cause = [last_of[uid]] if uid in last_of else None
        elif k == "call" and role == "planner" and mode == "replan":
            cause = [replan_trig] if replan_trig else None
        elif k == "replan_trigger":
            cause = [fail_ev[uid]] if uid in fail_ev else ([last_of[uid]] if uid in last_of else None)
        elif k == "settle" and e.get("status") == "blocked":
            up = e.get("upstream")
            cause = [fail_ev[up]] if up in fail_ev else None
        elif k == "report_start":
            cause = [x for x in [last_planner] if x] or None
        elif uid and uid in last_of:
            cause = [last_of[uid]]
        elif role == "planner" and last_planner:
            cause = [last_planner]
        rec = {"id": eid, "kind": k, "unit": uid, "role": role, "t0": int(tstart - t0), "t1": int(t1 - t0),
               "dur": int(t1 - tstart), "tokens": e.get("tokens"), "cost_usd": e.get("cost_usd"), "result": res,
               "mode": mode, "wrapup": wrap, "caused_by": cause,
               "detail": dict((kk, vv) for kk, vv in e.items() if kk not in ("ts", "paper", "episode", "kind", "unit") and not isinstance(vv, (dict, list))
                              or kk in ("units", "fail", "gates", "trigger", "running", "pending", "archived", "void", "done", "files", "sample", "warnings"))}
        if k == "foreign_write":
            rec["detail"]["who"] = e.get("unit")
        events.append(rec)
        if uid:
            last_of[uid] = eid
            if k == "settle" and e.get("status") == "done":
                done_ev[uid] = eid
            if k == "settle" and e.get("status") == "failed":
                fail_ev[uid] = eid
            if k == "critic" and e.get("grade") == "A" and uid not in done_ev:
                done_ev[uid] = eid   # old harnesses: A = done
            if k in ("taint", "taint_exhausted") and uid not in fail_ev:
                fail_ev[uid] = eid
        if k == "replan_trigger":
            replan_trig = eid
        if k in ("plan", "compile"):
            last_planner = eid
    # -------- lanes
    lanes = []
    calls = [x for x in events if x["kind"] == "call"]
    verdicts = [x for x in events if x["kind"] == "critic"]
    T = t_end - t0

    def _grade_at(uid, t1):
        best = None
        for v in verdicts:
            if v["unit"] == uid and v["t1"] >= t1 - 3 and (best is None or v["t1"] < best["t1"]):
                best = v
        return best

    def markers_for(pred):
        M = []
        for x in events:
            if x["kind"] in ("call", "critic", "exec") or not pred(x):
                continue
            kind, tmpl = KINDS.get(x["kind"], ("note", x["kind"]))
            d = dict(x["detail"])
            d.update({"unit": x["unit"], "ok_txt": "ok" if d.get("ok") else "FAIL", "gate_txt": {True: "PASS", False: "OVER", None: "MISSING"}.get(d.get("ok")),
                      "over_txt": "OVER" if d.get("over") else "fits", "base": os.path.basename(str(d.get("path", ""))), "grade": d.get("grade", "?"),
                      # computed so that a ledger without the new fields renders exactly as before
                      "mech_note": ("%s%s" % ((" +%d note(s)" % len(d["warnings"])) if d.get("warnings") else "", " SLICE-CONTINUE" if d.get("slice_continue") else "")),
                      "cp_note": ((" cp=%smin" % d["critical_path_min"]) if d.get("critical_path_min") is not None else ""),
                      "wait_lead": ("implicit wait on" if d.get("implicit") else "waiting for")})
            try:
                label = tmpl.format_map(collections.defaultdict(lambda: "?", d))
            except Exception:
                label = x["kind"]
            M.append({"t": x["t1"], "kind": kind, "label": label[:90], "event": x["id"]})
        return M

    for uid in order:
        u = by.get(uid, {})
        segs = []
        spans = sorted([c for c in calls if c["unit"] == uid and c["role"] in ("exec", "critic", "report")], key=lambda c: c["t0"])
        first_launch = next((x["t1"] for x in events if x["kind"] == "launch" and x["unit"] == uid), None)
        status = u.get("status") or ("done" if final_grades.get(uid) == "A" else None)
        settle = [x for x in events if x["kind"] == "settle" and x["unit"] == uid]
        if settle:
            status = settle[-1]["result"]
        t_cursor = 0
        deps = u.get("depends_on") or []
        blocked_at = next((x["t1"] for x in events if x["kind"] == "settle" and x["unit"] == uid and x["result"] == "blocked"), None)
        if first_launch is not None:
            t_wait = first_launch if blocked_at is None or blocked_at > first_launch else blocked_at
            segs.append({"t0": 0, "t1": t_wait, "phase": "blocked_on" if deps else "pending",
                         "label": ("waits for %s" % ",".join(deps)) if deps else "pending"})
            if t_wait < first_launch:
                segs.append({"t0": t_wait, "t1": first_launch, "phase": "blocked", "label": "BLOCKED (upstream failed) until the replan"})
            t_cursor = first_launch
        mech_verdicts = [x for x in verdicts if x["unit"] == uid and x["detail"].get("by") == "mech"]
        for v in mech_verdicts:   # a machine verdict has no call: a thin critic segment in time order
            spans.append({"t0": v["t1"], "t1": v["t1"] + max(1, T // 400), "role": "mech", "mode": "mech", "result": "C", "wrapup": False,
                          "id": v["id"], "tokens": 0, "cost_usd": 0})
        for w0 in [x for x in events if x["kind"] == "wait_start" and x["unit"] == uid]:   # waiting segments
            w1 = next((x for x in events if x["kind"] in ("wait_done", "wait_timeout", "wait_abort", "wait_stalled_end") and x["unit"] == uid and x["t1"] >= w0["t1"]), None)
            spans.append({"t0": w0["t1"], "t1": max(w0["t1"] + 1, w1["t1"] if w1 else T), "role": "waiting", "mode": "waiting", "wrapup": False,
                          "result": (w1["kind"][5:] + ("" if w1["kind"] != "wait_done" else " " + str(w1["detail"].get("how", "")))) if w1 else "?",
                          "id": w0["id"], "tokens": 0, "cost_usd": 0})
        spans.sort(key=lambda c: c["t0"])
        for c in spans:
            if c["role"] == "waiting" and t_cursor < c["t0"] <= t_cursor + 120:
                c["t0"] = t_cursor   # the seconds of mech check between the turn and wait_start belong to the waiting segment
            if c["t0"] > t_cursor + 1:
                prev = segs[-1]["phase"] if segs else "pending"
                gap_phase = ("wait_critic" if (prev in ("exec", "bfix", "rethink", "wrapup") and c["role"] == "critic")
                             else ("pending" if not segs else "repair_wait"))
                segs.append({"t0": t_cursor, "t1": c["t0"], "phase": gap_phase, "label": "wait (critic)" if gap_phase == "wait_critic" else "wait"})
            if c["role"] == "waiting":
                lab, phase = "waiting (%s)" % c["result"], "waiting"
            elif c["role"] == "mech":
                lab, phase = "mech -> C", "critic"
            elif c["role"] == "critic":
                v = _grade_at(uid, c["t1"])
                lab = "critic %s" % (v["result"] if v else "?")
                phase = "critic"
            elif c["role"] == "report":
                lab, phase = "report", "report"
            else:
                phase = {"fresh": "exec", "bfix": "bfix", "rethink": "rethink", "restart": "rethink", "rescue": "rescue"}.get(c["mode"], "exec")
                lab = "%s%s" % (c["mode"], (" %s" % c["result"]) if c["result"] not in ("rc=0", None) else "")
            if c["wrapup"]:
                phase, lab = "wrapup", lab + " wrap-up"
            segs.append({"t0": c["t0"], "t1": max(c["t1"], c["t0"] + 1), "phase": phase, "label": lab, "event": c["id"],
                         "tokens": c["tokens"], "cost_usd": c["cost_usd"]})
            t_cursor = max(t_cursor, c["t1"])
        if status in ("blocked",):
            tb = settle[-1]["t1"] if settle else t_cursor
            if not segs:
                segs.append({"t0": 0, "t1": tb, "phase": "blocked_on", "label": "waits for %s" % ",".join(deps)})
            segs.append({"t0": tb, "t1": T, "phase": "blocked", "label": "BLOCKED by %s" % (settle[-1]["detail"].get("upstream") if settle else "?")})
        elif status:
            ph = status if status in PHASE_COLOR else "skip"
            segs.append({"t0": t_cursor, "t1": T, "phase": ph, "label": ("RETIRED (failed)" if status == "failed" else status.upper()) + ((" grade %s" % final_grades[uid]) if uid in final_grades else "")})
        elif spans:
            segs.append({"t0": t_cursor, "t1": T, "phase": "running", "label": "no terminal state recorded"})
        lanes.append({"name": uid, "type": u.get("type"), "status": status, "grade": final_grades.get(uid),
                      "segments": segs, "markers": markers_for(lambda x, uid=uid: x["unit"] == uid),
                      "cost_usd": round(sum(c["cost_usd"] or 0 for c in spans), 3), "tokens": sum(c["tokens"] or 0 for c in spans)})
    # planner lane
    segs = []
    for c in sorted([c for c in calls if c["role"] == "planner"], key=lambda c: c["t0"]):
        segs.append({"t0": c["t0"], "t1": max(c["t1"], c["t0"] + 1), "phase": "wrapup" if c["wrapup"] else c["mode"], "label": c["mode"] + (" wrap-up" if c["wrapup"] else ""),
                     "event": c["id"], "tokens": c["tokens"], "cost_usd": c["cost_usd"]})
    for x in events:
        if x["kind"] == "compile" and not any(s["phase"] == "compile" for s in segs):
            segs.append({"t0": x["t1"], "t1": x["t1"] + max(1, T // 400), "phase": "compile", "label": "compile", "event": x["id"]})
    lanes.append({"name": "planner", "type": "role", "segments": segs, "markers": markers_for(lambda x: x["role"] == "planner" and not x["unit"])})
    # report lane (report calls of the render unit and the finalize calls)
    segs = []
    for c in sorted([c for c in calls if c["role"] == "report" or (c["unit"] in render_ids)], key=lambda c: c["t0"]):
        if c["role"] == "critic":
            v = _grade_at(c["unit"], c["t1"])
            ph, lab = "report_critic", "critic %s" % (v["result"] if v else "?")
        else:
            ph, lab = ("finalize" if c["mode"] == "finalize" else "report"), (c["mode"] if c["mode"] not in ("fresh",) else "report")
        if c["wrapup"]:
            ph, lab = "wrapup", lab + " wrap-up"
        segs.append({"t0": c["t0"], "t1": max(c["t1"], c["t0"] + 1), "phase": ph, "label": lab, "event": c["id"], "tokens": c["tokens"], "cost_usd": c["cost_usd"]})
    if not any(l.get("type") == "render" and l["segments"] for l in lanes):
        lanes.append({"name": "report", "type": "role", "segments": segs,
                      "markers": markers_for(lambda x: x["role"] == "report" and x["kind"] != "settle")})
    else:
        for l in lanes:
            if l.get("type") == "render":
                l["markers"] += markers_for(lambda x: x["role"] == "report" and x["kind"] != "settle" and not x["unit"])
    # override lane
    segs = []
    ov = [x for x in events if x["kind"] == "override"]
    fd = [x for x in events if x["kind"] == "flush_done"]
    if ov:
        segs.append({"t0": ov[0]["t1"], "t1": (fd[0]["t1"] if fd else min(T, ov[0]["t1"] + 900)), "phase": "flush", "label": "flush window"})
        segs.append({"t0": (fd[0]["t1"] if fd else ov[0]["t1"]), "t1": T, "phase": "override", "label": "report mode (override)"})
    lanes.append({"name": "override", "type": "role", "segments": segs, "markers": markers_for(lambda x: x["role"] == "episode")})
    # unit lanes that are render units go to the report lane's neighbourhood: keep plan order but drop empty render lanes
    lanes = [l for l in lanes if not (l.get("type") == "render" and not l["segments"])]
    cost = round(sum(c["cost_usd"] or 0 for c in calls), 3)
    trace = {"episode": es.get("episode") or ev[0].get("episode"), "paper": ev[0].get("paper"), "version": version, "t0": t0, "t_end": t_end,
             "duration_s": T, "wall_s": wall, "grades": final_grades, "cost_usd": cost,
             "units": [{"id": uid, "type": by[uid].get("type"), "status": next((l["status"] for l in lanes if l["name"] == uid), None),
                        "grade": final_grades.get(uid)} for uid in order],
             "lanes": lanes, "events": events, "generated_by": "trace.py (RA)"}
    os.makedirs(out_dir, exist_ok=True)
    pj = os.path.join(out_dir, "trace.json")
    json.dump(trace, open(pj, "w"), indent=1)
    ph = os.path.join(out_dir, "timeline.html")
    open(ph, "w").write(render_html(trace))
    pt = os.path.join(out_dir, "timeline.txt")
    open(pt, "w").write(render_txt(trace))
    return [pj, ph, pt]

# ---------------------------------------------------------------- renderers
def render_html(tr):
    T = max(1, tr["duration_s"])
    W, LEFT, RH, TOP = 1400, 150, 24, 40
    scale = (W - LEFT - 20) / float(T)
    lanes = tr["lanes"]
    H = TOP + RH * len(lanes) + 30
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>%s timeline</title>" % html.escape(str(tr["episode"])),
           "<style>body{font-family:sans-serif;font-size:13px;margin:16px}svg text{font-size:11px}.leg span{display:inline-block;width:14px;height:12px;margin:0 4px -2px 10px;border:1px solid #888}"
           "table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #ccc;padding:2px 6px;text-align:left}</style></head><body>",
           "<h2>%s &mdash; paper %s &mdash; harness %s</h2>" % (html.escape(str(tr["episode"])), html.escape(str(tr["paper"])), html.escape(str(tr["version"]))),
           "<p>duration %s of wall %s; cost $%.2f; grades: %s</p>" % (_fmt(T), _fmt(tr["wall_s"]) if tr["wall_s"] else "?", tr["cost_usd"],
                                                                     html.escape(", ".join("%s=%s" % kv for kv in sorted(tr["grades"].items())) or "(none)")),
           "<svg width='%d' height='%d' xmlns='http://www.w3.org/2000/svg'>" % (W, H)]
    step = 3600 if T > 4 * 3600 else (900 if T > 3600 else 300)
    t = 0
    while t <= T:
        x = LEFT + t * scale
        out.append("<line x1='%.1f' y1='%d' x2='%.1f' y2='%d' stroke='#eee'/><text x='%.1f' y='%d' fill='#666'>%s</text>" % (x, TOP - 6, x, H - 20, x - 12, TOP - 10, _fmt(t)))
        t += step
    for i, lane in enumerate(lanes):
        y = TOP + i * RH
        out.append("<text x='4' y='%d' font-weight='bold'>%s</text>" % (y + 16, html.escape(lane["name"][:22])))
        out.append("<line x1='%d' y1='%d' x2='%d' y2='%d' stroke='#ddd'/>" % (LEFT, y + RH, W - 10, y + RH))
        for s in lane["segments"]:
            x0, x1 = LEFT + s["t0"] * scale, LEFT + s["t1"] * scale
            wdt = max(1.5, x1 - x0)
            col = PHASE_COLOR.get(s["phase"], "#999")
            extra = " stroke='#333' stroke-dasharray='3,2'" if s["phase"] in ("wrapup", "blocked") else ""
            tip = "%s: %s [%s - %s]%s" % (lane["name"], s["label"], _fmt(s["t0"]), _fmt(s["t1"]),
                                          (" $%.2f" % s["cost_usd"]) if s.get("cost_usd") else "")
            out.append("<rect x='%.1f' y='%d' width='%.1f' height='%d' fill='%s'%s><title>%s</title></rect>" % (x0, y + 3, wdt, RH - 6, col, extra, html.escape(tip)))
            if wdt > 34:
                out.append("<text x='%.1f' y='%d' fill='%s'>%s</text>" % (x0 + 3, y + 16, "#fff" if s["phase"] in ("exec", "rethink", "failed", "blocked", "flushed", "report", "done") else "#222",
                                                                          html.escape(s["label"][: max(3, int(wdt / 6.5))])))
        for m in lane["markers"]:
            x = LEFT + m["t"] * scale
            col = MARK_COLOR.get(m["kind"], "#aaa")
            big = m["kind"] in ("kill", "taint", "foreign", "frozen", "override", "replan", "plan_bad", "infra", "retire", "settle", "wait_bad", "leftover", "restart")
            r = 5 if big else 3
            out.append("<polygon points='%.1f,%d %.1f,%d %.1f,%d %.1f,%d' fill='%s' stroke='#222' stroke-width='0.5'><title>%s</title></polygon>"
                       % (x, y + 1, x + r, y + 1 + r, x, y + 1 + 2 * r, x - r, y + 1 + r, col, html.escape("%s @%s: %s" % (lane["name"], _fmt(m["t"]), m["label"]))))
    out.append("</svg>")
    out.append("<p class='leg'>" + "".join("<span style='background:%s'></span>%s" % (PHASE_COLOR[k], k) for k in
                                           ("pending", "blocked_on", "exec", "bfix", "rethink", "wrapup", "waiting", "critic", "wait_critic", "repair_wait", "done", "failed", "blocked", "wrapped", "void", "flushed", "skip", "plan", "replan", "report", "flush", "override")) + "</p>")
    out.append("<p class='leg'>markers: " + "".join("<span style='background:%s'></span>%s" % (MARK_COLOR[k], k) for k in
                                                     ("launch", "mech", "fix", "gate", "kill", "taint", "foreign", "stray", "outside", "frozen", "freeze", "archive", "restart", "settle", "retire", "replan", "plan_bad", "override", "infra", "wait", "wait_bad", "leftover", "notice")) + "</p>")
    out.append("<h3>Units</h3><table><tr><th>id</th><th>type</th><th>status</th><th>grade</th><th>calls cost</th><th>tokens</th></tr>")
    for l in lanes:
        if l.get("type") in ("role",):
            continue
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>$%.2f</td><td>%s</td></tr>" % (
            html.escape(l["name"]), html.escape(str(l.get("type"))), html.escape(str(l.get("status"))), html.escape(str(l.get("grade"))), l.get("cost_usd") or 0, l.get("tokens") or 0))
    out.append("</table><h3>Markers</h3><table><tr><th>time</th><th>lane</th><th>event</th></tr>")
    rows = sorted(((m["t"], l["name"], m["label"]) for l in lanes for m in l["markers"] if m["kind"] not in ("note", "bus", "launch", "mech")), key=lambda r: r[0])
    for t, n, lab in rows[:400]:
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (_fmt(t), html.escape(n), html.escape(lab)))
    out.append("</table><p style='color:#777'>generated by trace.py (RA) from _pece/ledger.jsonl; hover a bar or a marker for details.</p></body></html>")
    return "\n".join(out)

def render_txt(tr):
    T = max(1, tr["duration_s"])
    cols = 100
    L = ["%s  paper %s  harness %s  duration %s / wall %s  cost $%.2f" % (tr["episode"], tr["paper"], tr["version"], _fmt(T), _fmt(tr["wall_s"]) if tr["wall_s"] else "?", tr["cost_usd"]),
         "grades: %s" % (", ".join("%s=%s" % kv for kv in sorted(tr["grades"].items())) or "(none)"),
         "one column = %s;  %s" % (_fmt(T / float(cols)), "  ".join("%s=%s" % (v, k) for k, v in PHASE_TXT.items() if k in ("pending", "blocked_on", "exec", "bfix", "rethink", "wrapup", "waiting", "critic", "wait_critic", "done", "failed", "blocked", "wrapped", "void", "flushed", "skip", "plan", "replan", "report", "flush", "override"))),
         "%-22s|%s|" % ("", "".join(str(i // 10) if i % 10 == 0 else " " for i in range(cols)))]
    for lane in tr["lanes"]:
        row = [" "] * cols
        for s in lane["segments"]:
            a, b = int(s["t0"] * cols / T), max(int(s["t0"] * cols / T) + 1, int(s["t1"] * cols / T))
            for i in range(a, min(cols, b)):
                row[i] = PHASE_TXT.get(s["phase"], "?")
        for m in lane["markers"]:
            if m["kind"] in ("kill", "taint", "foreign", "frozen", "override", "replan", "plan_bad", "infra", "retire", "wait_bad", "leftover", "restart"):
                i = min(cols - 1, int(m["t"] * cols / T))
                row[i] = "!"
        L.append("%-22s|%s| %s" % (lane["name"][:22], "".join(row), (lane.get("status") or "") + ((" " + str(lane.get("grade"))) if lane.get("grade") else "")))
    L.append("")
    L.append("markers (! above):")
    rows = sorted(((m["t"], l["name"], m["label"]) for l in tr["lanes"] for m in l["markers"] if m["kind"] not in ("note", "bus", "launch", "mech")), key=lambda r: r[0])
    for t, n, lab in rows[:400]:
        L.append("  %s  %-14s %s" % (_fmt(t), n[:14], lab))
    L.append("")
    L.append("verdict chain (critic events):")
    for x in tr["events"]:
        if x["kind"] == "critic":
            L.append("  %s  %-12s %s (%s)%s" % (_fmt(x["t1"]), x["unit"], x["result"], x["detail"].get("by", "llm"), (" <- " + ",".join(x["caused_by"])) if x.get("caused_by") else ""))
    return "\n".join(L) + "\n"

def main(argv):
    out = None
    dirs = []
    i = 0
    while i < len(argv):
        if argv[i] == "--out":
            out = argv[i + 1]; i += 2
            continue
        dirs.append(argv[i]); i += 1
    if not dirs:
        print(__doc__)
        return 2
    rc = 0
    for d in dirs:
        try:
            files = build(d, out_dir=(os.path.join(out, os.path.basename(os.path.abspath(d.rstrip("/")).replace("/_pece", ""))) if out else None))
            print("OK %s -> %s" % (d, ", ".join(files)))
        except Exception as e:
            print("FAIL %s: %s" % (d, e))
            rc = 1
    return rc

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
