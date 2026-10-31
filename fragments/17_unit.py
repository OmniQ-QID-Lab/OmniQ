
# ---------------------------------------------------------------- the executor's prompts
def exec_prompt(u, restart=None):
    """EXEC prompt (budget at launch = the usable wall) + the upstream units; restart = the record of the archived round (preamble + ONE ruling
    block: the scout plan when scouting ran, else the previous critic's REDUCIBLE ruling and the budget note)."""
    uid = u["id"]
    budget = max(0.0, usable_left())
    deps = [d for d in (u.get("depends_on") or []) if unit_by_id(d) is not None]
    up = ""
    if deps:
        prov = provisional_deps(u)
        up = (" UPSTREAM UNITS you depend on (%s): %s - read each /workspace/<id>/handoff.json FIRST, then open the files it lists; the files are the "
              "truth, the prose is a claim." % ("all final" if not prov else "%s not final" % ", ".join(prov),
                 "; ".join("%s -> /workspace/%s/%s" % (d, d, PROVISIONAL_FILE_TAG if d in prov else "") for d in deps)))
    body = EXEC_PROMPT.format(uid=uid, upstream=up, budget_s=int(budget), budget_h=budget / 3600.0,
                              knobs=json.dumps(u.get("scale_knobs") or [], ensure_ascii=False)[:400]) + _exec_notes(u) + bank_start_block(u, restart)
    if restart:
        cp_, cd_ = restart.get("copied") or [], restart.get("code") or []
        note = restart.get("note") or ""
        body = RESTART_PREAMBLE.format(uid=uid, grade=restart.get("grade"), archive=restart["archive"], round=restart.get("round"),
                                       code=((" and the previous round's CODE files (%d copied back as your starting point - fix or redesign exactly the parts "
                                              "residual.md faults and keep the rest; when the scouting note or residual.md says the route itself is wrong, that "
                                              "code is reference only)" % len(cd_)) if cd_ else " only (an otherwise empty directory)"),
                                       copied=(" (%d file(s): %s)" % (len(cp_), ", ".join(cp_[:12]) + (" ..." if len(cp_) > 12 else ""))) if cp_ else " (none were listed)",
                                       note=(" " + note) if note else "") + body
    return body

def _exec_notes(u):
    """Change notices for the executor: readings changed and upstream files changed since the unit launched (orchestrator facts)."""
    n = change_notice(u)
    return ("\n\nCHANGE NOTICE (orchestrator facts - act on them in this session):\n" + n) if n else ""

def _handoff_sig(uid):
    p = os.path.join(unit_dir(uid), "handoff.json")
    try:
        with open(p, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None

def call_outcome(uid, r, sig_before):
    """HANDOFF (a normal turn end with a handoff.json written in the call, or a cut call whose new handoff registers a live job) | HANDOFF_BAD
    (written in the call, unparseable) | CUT (a time limit, a stream cut, a crash - even after writing a handoff) | CUT_NOHAND (the turn ended
    without writing handoff.json) | INFRA | INTERRUPTED. A CUT is continued, never audited (an mtime rule alone would audit
    half-finished sessions that rewrote the handoff mid-way)."""
    if r.get("interrupted") or OVERRIDE.is_set():
        return "INTERRUPTED"
    if r.get("infra"):
        return "INFRA"
    sig = _handoff_sig(uid)
    if sig is not None and sig != sig_before:
        try:
            h = json.load(open(os.path.join(unit_dir(uid), "handoff.json")))
        except Exception:
            return "HANDOFF_BAD"
        if r.get("rc") == 0:
            return "HANDOFF"
        wt = waiting_spec(h)[0] if isinstance(h, dict) else None
        if wt and _pid_alive_unit(wt["pid"], uid):
            return "HANDOFF"
        return "CUT"
    if r.get("rc") == 0 and sig is not None:   # a normal turn end that confirms a handoff written in this round (after the unit's last look)
        try:
            if os.stat(os.path.join(unit_dir(uid), "handoff.json")).st_mtime >= LAST_LOOK.get(uid, T0):
                h = json.load(open(os.path.join(unit_dir(uid), "handoff.json")))
                wt = waiting_spec(h)[0] if isinstance(h, dict) else None
                if not (wt and _stale_wait(uid, wt)):   # a registration already served is the previous narrative, not a handoff
                    return "HANDOFF"
        except Exception:
            pass
    return "CUT_NOHAND" if r.get("rc") == 0 else "CUT"

def _cut_why(kind, r):
    if kind == "CUT_NOHAND":
        return ("your previous session ended its turn but /workspace/{uid}/handoff.json was not (re)written - if the work is complete, rewrite "
                "handoff.json now (that ends your turn); otherwise finish it first.")
    if r.get("rc") == -1:
        return "your previous session reached its time limit while you were still working."
    return "your previous session ended abnormally (exit %s) - check what it left." % r.get("rc")

def _fix_carry(S):
    """A fix round's instructions, carried into every continuation of that round (they stay in force until the next verdict)."""
    return ("\n\nTHE FIX ROUND'S INSTRUCTIONS (still in force - finish them):\n" + S["fix_extra"][:12000]) if S.get("fix_extra") else ""

# ---------------------------------------------------------------- decide: the ONE close rule (only time closes a unit)
def settle_in_place(g, gates, S, u):
    """No wall for another attempt: the unit settles where it is - done (B with every gate passing), gap (provenance verified by its last full
    audit: its data flow with the unmet rows stated), gap_unaudited (a valid handoff whose provenance no audit verified), else failed. A reopened
    unit returns to done."""
    if S.get("reopened"):
        return "done", "reopen_unfinished"
    if S.get("cp_c"):   # the latest look (a checkpoint) found a defect in the current data: no audit verified them
        return ("gap", "gap_unaudited_checkpoint") if u.get("handoff_valid_at") else ("failed", "no_time")
    gates = gates or S.get("last_gates")
    if g == "B" and _g123_pass(gates):
        return "done", "b_all_pass_no_time"
    if (gates or {}).get("G1") == "pass":
        return "gap", "gap_no_time"
    if (gates or {}).get("G1") == "hit":   # untraceable data are never passed on as a gap
        return "failed", "g1_no_time"
    if u.get("handoff_valid_at"):
        return "gap", "gap_unaudited"
    return "failed", "no_time"

def _deliv_sig(u):
    """[files, bytes, newest mtime] over everything the unit's planned deliverables name (a pattern counts its matches, as the mech check expands
    it) - the B loop guard's "new data"."""
    n = tot = newest = 0
    for d in (u.get("deliverables") or []):
        real = _ws_path(d).rstrip("/")
        for p in (_pattern_hits(real) or [real]):
            q = _quick_sig(p)
            if not q:
                continue
            if len(q) == 3:
                n, tot, newest = n + q[0], tot + q[1], max(newest, q[2])
            else:
                n, tot, newest = n + 1, tot + q[0], max(newest, q[1])
    return [n, tot, newest]

def decide(g, S, gates, u):
    """-> (action, why). A -> done; D -> failed (task-book grounds). B -> fix in place while LAST_ACT_S is usable (a budget note when the fix is
    longer); the same gate hit standing B_SAME_HIT_MAX rounds in a row with no new data is handled as a C. C -> scouting + a fresh attempt while
    SCOUT_NEED_S is usable ('cpath'), else a fix in place while LAST_ACT_S is usable ('fix_c'). A reopened unit is never archived (its C is fixed
    in place). Otherwise the unit settles in place."""
    ul = usable_left()
    if g == "A":
        return "done", ("reaudit_A" if S.get("reopened") else "A")
    if g == "D":
        return ("done", "reopen_D_noted") if S.get("reopened") else ("failed", "D")
    if g == "B":
        hit = tuple(k for k in ("G1", "G2", "G3") if (gates or {}).get(k) == "hit")
        sig = _deliv_sig(u)   # "new data" = a planned deliverable changed (bookkeeping excluded)
        if hit and hit == S.get("b_hit") and sig == S.get("b_sig"):
            S["b_hit_n"] += 1
        else:
            S["b_hit"], S["b_hit_n"], S["b_sig"] = (hit or None), (1 if hit else 0), sig
        if not (hit and S["b_hit_n"] >= B_SAME_HIT_MAX):
            return ("fix", "B") if ul >= LAST_ACT_S else settle_in_place(g, gates, S, u)
        S["b_hit"], S["b_hit_n"] = None, 0
        g = "C"
    if S.get("reopened"):
        return ("fix_c", "reopen_C_in_place") if ul >= LAST_ACT_S else settle_in_place(g, gates, S, u)
    if scouting_due(u):
        return "cpath", "C"
    if ul >= LAST_ACT_S:
        return "fix_c", "C_late"
    return settle_in_place(g, gates, S, u)

# ---------------------------------------------------------------- the unit's life cycle (one thread per unit) - an explicit state machine
def _grant(uid, u, grant):
    return grant if grant is not None else POOL.acquire(uid, u, first=False)

def _budget_note(S, u):
    """The one budget note (fix rounds and fresh attempts alike): the critic's estimate exceeds the usable wall."""
    need = float(S.get("est") or 0) * 60
    if not math.isfinite(need) or need <= usable_left():   # a non-finite estimate is no estimate
        return ""
    return "\n\n" + BUDGET_NOTE.format(est=int(need // 60), spare=int(usable_left() // 60), knobs=_scale_hint(u)).strip()

def _mark_best(u, g, gates):
    """The unit's best validated round so far: the latest audit with G1 and G2 passing (grade A > B > C)."""
    if not (gates and gates.get("G1") == "pass" and gates.get("G2") == "pass" and g in ("A", "B", "C")):
        return
    rank = {"A": 3, "B": 2, "C": 1}
    b = u.get("_best") or {}
    if rank[g] >= rank.get(b.get("grade"), 0):
        u["_best"] = {"round": int(u.get("restarts") or 0), "grade": g, "t": int(time.time()), "dir": "/workspace/%s" % u["id"]}

def unit_worker(u, grant, start="exec"):
    """exec -> HANDOFF: mech -> wait on a live job | audit;  CUT: continue (a checkpoint when due, or when a finished job's verification was cut)
    -> decide -> fix in place / scouted fresh attempt / settle. start="reaudit": a done unit reopened for a changed input begins with a delta
    audit of the change and is never archived. Outcomes: done / gap / failed / flushed (a reopened unit returns to done)."""
    uid = u["id"]
    reopened = start == "reaudit"
    S = {"mode": "fresh", "extra": None, "sid": None, "rounds": 0, "mechs": 0, "nohand": 0, "doa": 0, "infra": 0, "est": None, "last_full_b": False,
         "last_gates": None, "events": set(), "restart": None, "why_cont": "", "b_hit": None, "b_hit_n": 0, "b_sig": None, "reopened": reopened,
         "t_call": None}
    prev_grade = RUN["grades"].get(uid)
    outcome, reason, g, gates = "failed", "thread_error", None, None
    job, cp_reason = None, None
    step = "audit_reopen" if reopened else "exec"
    LAST_LOOK.setdefault(uid, time.time())
    if not reopened:
        try:
            record_inputs(u)   # the consumed inputs' fingerprints at launch (hashing outside the scheduler's lock)
        except Exception as e:
            log("%s: input fingerprints failed: %s" % (uid, e))
    def flushed():
        return ("done", "reopen_override") if reopened else ("flushed", "override")
    try:
        while True:   # an orchestrator fault inside the unit's loop is never charged to the unit - logged, then the unit continues (twice at most)
            try:
                while True:
                    if OVERRIDE.is_set():
                        outcome, reason = flushed()
                        break
                    if step == "exec":
                        grant = _grant(uid, u, grant)
                        if S["mode"] == "fix":   # the round's instructions stay in force through its continuations until the next verdict
                            S["fix_extra"] = S.get("extra") or ""
                        tag, prompt, resume = _exec_call_spec(u, S)
                        was_resume = S["mode"] == "resume"
                        sig0 = _handoff_sig(uid)
                        fix_t = int(min(EXEC_SLICE_S, max(BFIX_MIN_S, 2 * float(S.get("est") or 0) * 60)))
                        exec_t = fix_t if S["mode"] == "fix" else EXEC_SLICE_S
                        sliced = exec_t < remaining() - report_reserve_s() - CLAMP_MARGIN_S   # every executor call: a cut is continued, never audited
                        r = run_call(prompt, exec_t, tag, resume_sid=(S["sid"] if resume else None), wrapup=None,
                                     grant=grant, cwd=unit_dir(uid), slice_ext=(uid if sliced else None))   # an unsliced call ends at the override (flush)
                        S["t_call"] = r.get("t0")
                        S["sid"] = r.get("sid") or S["sid"]
                        EXEC_SIDS[uid] = S["sid"]
                        kind = call_outcome(uid, r, sig0)
                        ledger({"kind": "exec", "unit": uid, "mode": S["mode"], "round": S["rounds"], "tokens": r.get("tokens"), "rc": r.get("rc"),
                                "interrupted": r.get("interrupted"), "dur": int((r.get("t1") or time.time()) - (r.get("t0") or time.time())), "outcome": kind})
                        if kind == "INTERRUPTED":
                            outcome, reason = flushed()
                            break
                        if kind == "INFRA":
                            S["infra"] += 1
                            if INFRA["dead"]:
                                outcome, reason = flushed()
                                break
                            if S["mode"] == "fresh-restart":   # the retry re-issues the fresh attempt's prompt
                                S["mode"] = "restart"
                            ledger({"kind": "infra_unit_hold", "unit": uid, "cycle": S["infra"]})
                            OVERRIDE.wait(min(600, max(5, INFRA_PROBE_S // 6)))
                            continue
                        S["infra"] = 0
                        if kind != "HANDOFF" and (r.get("t1") or 0) - (r.get("t0") or 0) < 60:
                            S["spin"] = S.get("spin", 0) + 1
                            if S["spin"] >= SPIN_N:
                                ledger({"kind": "spin_hold", "unit": uid, "calls": S["spin"], "hold_s": SPIN_HOLD_S})
                                log("%s: %d executor calls in a row ended within a minute without a handoff -> hold %d s" % (uid, S["spin"], SPIN_HOLD_S))
                                S["spin"] = 0
                                OVERRIDE.wait(SPIN_HOLD_S)
                                if OVERRIDE.is_set():
                                    outcome, reason = flushed()
                                    break
                        else:
                            S["spin"] = 0
                        if not _live_unit_pids(uid):
                            POOL.release(uid)
                            grant = None
                        if kind in ("CUT", "CUT_NOHAND"):
                            S["nohand"] = S["nohand"] + 1 if kind == "CUT_NOHAND" else 0
                            S["events"] = {"slice_cut"}
                            ledger({"kind": "slice", "unit": uid, "mode": S["mode"], "rc": r.get("rc"), "outcome": kind})
                            if was_resume and usable_left() >= LAST_ACT_S:   # the job's results are on disk: look at them now, never a blind continuation
                                step, cp_reason, job = "check", "after_job", None
                                continue
                            cp_reason = checkpoint_due(uid)
                            if cp_reason:
                                step, job = "check", current_job(uid)
                                continue
                            if S["nohand"] >= NOHAND_MAX and _handoff_sig(uid):
                                log("%s: %d turns in a row ended without a new handoff -> the audit decides on what exists" % (uid, S["nohand"]))
                                S["nohand"] = 0
                                step = "mech"
                                continue
                            S["mode"], S["why_cont"] = "continue", _cut_why(kind, r).replace("{uid}", uid) + _fix_carry(S)
                            continue
                        S["nohand"] = 0
                        step = "mech"
                        continue
                    if step == "mech":
                        mech = mech_check(uid, u, S.get("t_call"))
                        if mech.get("all_ok"):
                            u["handoff_valid_at"] = time.time()
                        else:
                            u.pop("handoff_valid_at", None)
                        checkpoint()
                        job = current_job(uid, implicit=False)
                        if job and not mech.get("handoff_errors"):
                            step = "wait"
                            continue
                        if mech.get("stopped"):
                            reap_leftovers(uid)   # the executor asked for a verdict: nothing unregistered keeps the unit waiting
                            step = "audit"
                            continue
                        if not mech.get("all_ok"):
                            S["mechs"] += 1
                            if S["mechs"] <= MECH_MAX:
                                machine_critic(uid, mech, "B")
                                S["mode"], S["extra"], S["events"], S["est"] = "fix", FIX_PROMPT.format(uid=uid, grade="B (mechanical check)", fixes=extract_fixes(uid) + mech_notes(mech, "\n\n")), mech_events(mech), 10
                                step = "exec"
                                continue
                            log("%s: %d mechanical fix rounds without a valid handoff -> the audit decides" % (uid, S["mechs"]))
                            step = "audit"
                            continue
                        live = _live_unit_pids(uid)
                        if live and _data_grew(unit_dir(uid), time.time() - 900):
                            job = current_job(uid)   # an unregistered computation still writes data: waited on (the checkpoint applies)
                            step = "wait" if job else "audit"
                            continue
                        if live:
                            reap_leftovers(uid)
                        step = "audit"
                        continue
                    if step == "wait":
                        how, secs, cp = wait_job(uid, u, job)
                        if how == "override":   # the job keeps the executors' flush window to write its last points
                            t_end = (OVERRIDE_T0["t"] or time.time()) + FLUSH_WINDOW - 180
                            while time.time() < t_end and not job_ended(uid, job):
                                time.sleep(max(1, min(20, WAIT_TICK)))
                            outcome, reason = flushed()
                            break
                        if how == "check":
                            step, cp_reason = "check", cp
                            continue
                        if how in ("exited", "stalled") and secs < 300 and not job.get("implicit"):
                            S["doa"] += 1
                        else:
                            S["doa"] = 0
                        if S["doa"] >= 2 and usable_left() >= LAST_ACT_S:   # two registrations dead on arrival: a look instead of a third resume
                            S["doa"] = 0
                            step, cp_reason, job = "check", "after_job", None
                            continue
                        S["mode"], S["extra"] = "resume", resume_text(uid, job, how, secs)
                        S["events"] = {"stalled"} if how == "stalled" else ({"orphan"} if how == "orphan" else set())
                        job = None
                        step = "exec"
                        continue
                    if step == "check":
                        vg = run_checkpoint(uid, u, cp_reason, job)
                        S["events"] = set()
                        if vg in ("W", "C") and job is not None and job_ended(uid, job):
                            ledger({"kind": "checkpoint_superseded", "unit": uid, "grade": vg})
                            vg = "ended"   # the job finished during the look: its complete results get a resume and a full audit
                        if vg == "D":   # task-book grounds are ruled by a full audit (never a delta); a running job is kept for a possible scouting
                            S["last_full_b"], S["d_job"] = False, (job if (job and not job_ended(uid, job)) else None)
                            step = "audit"
                            continue
                        if vg == "W":
                            job = job if (job and not job_ended(uid, job)) else current_job(uid)
                            if job:
                                step = "wait"
                                continue
                            S["mode"], S["why_cont"] = "continue", "the checkpoint found the unit on track - continue the plan and hand off." + _fix_carry(S)
                            step = "exec"
                            continue
                        if vg in ("B", "ended"):
                            fp = parse_fix_plan(uid) or {}
                            S["est"] = fp.get("est_min")
                            if vg == "ended" or (job is not None and job_ended(uid, job)):
                                S["mode"] = "resume"
                                S["extra"] = resume_text(uid, job, "marker" if os.path.exists(_ws_path(job["done_marker"])) else "exited", 0) + (
                                    "\n\nCHECKPOINT NOTES (taken while the job was finishing - check them against the complete results):\n" + extract_fixes(uid)[:3000])
                            else:
                                S["mode"], S["extra"] = "fix", FIX_PROMPT.format(uid=uid, grade="B (checkpoint)", fixes=extract_fixes(uid)) + _budget_note(S, u)
                            S["last_full_b"] = False
                            job = job if (job and not job_ended(uid, job)) else None
                            step = "exec"
                            continue
                        g, gates = "C", None
                        S["c_job"] = job if (job and not job_ended(uid, job)) else None
                        S["cp_c"] = True   # the latest look is a checkpoint C: at the end the unit's current data count as unaudited
                        step = "decide"
                        continue
                    if step in ("audit", "audit_reopen"):
                        if step == "audit_reopen":
                            notice = ((u.get("reopen_why") or "") + "\n" + change_notice(u)).strip()
                            g = run_critic(uid, delta=True, notice=notice, why_delta="it was graded done, and since then an input it consumed changed (the notice below). "
                                           "Verify whether its results depend on the change; its data stay in place.")
                        else:
                            g = run_critic(uid, delta=(S["last_full_b"] and (S["mode"] == "fix" or bool(S.get("fix_extra")))))
                            if g is None and not OVERRIDE.is_set():
                                g = run_critic(uid)
                        if g is None and OVERRIDE.is_set():
                            outcome, reason = flushed()
                            break
                        if g is None:
                            g = machine_critic(uid, {"fails": [NO_VERDICT_FIX]}, "B")
                            gates = None
                        else:
                            gates = parse_gates(uid)
                            S["last_gates"] = gates
                            _mark_best(u, g, gates)
                        S["last_full_b"] = bool(g == "B" and _g13_pass(gates))
                        S["c_job"], S["cp_c"], S["fix_extra"] = S.pop("d_job", None), False, ""
                        step = "decide"
                        continue
                    if step == "decide":
                        fp = parse_fix_plan(uid) or {}
                        S["est"] = fp.get("est_min")
                        action, why = decide(g, S, gates, u)
                        if SMOKE_FORCE_C and not RUN.get("smoke_forced") and not reopened and g in ("A", "B") and action in ("done", "fix"):
                            RUN["smoke_forced"] = True
                            ensure_residual(uid, "smoke test: grade %s handled as C to exercise the scouting" % g)
                            ledger({"kind": "smoke_forced_c", "unit": uid, "grade_was": g, "action_was": action})
                            g, action, why = "C", ("cpath" if scouting_due(u) else "fix_c"), "C"
                        ledger({"kind": "fix_plan", "unit": uid, "grade": g, "est_min": S["est"], "round": S["rounds"], "mechs": S["mechs"], "action": action,
                                "reason": why, "usable_s": int(usable_left()), "remaining_s": int(remaining()), "reopened": reopened})
                        if override_hold(u, g, action, why):
                            outcome, reason = flushed()
                            break
                        if action in ("done", "gap", "failed"):
                            outcome, reason = action, why
                            break
                        S["rounds"] += 1
                        if action == "fix":
                            S["mode"], S["extra"], S["events"] = "fix", FIX_PROMPT.format(uid=uid, grade=g, fixes=extract_fixes(uid)) + _budget_note(S, u), set()
                            step = "exec"
                            continue
                        if action == "fix_c":
                            S["mode"] = "fix"
                            S["extra"] = FIX_PROMPT.format(uid=uid, grade="C - fixed IN PLACE (%s): address what the residual names, keep the data that hold" % why,
                                                           fixes=(read(os.path.join(unit_dir(uid), "residual.md"))[:5000] or extract_fixes(uid))) + _budget_note(S, u)
                            S["events"], S["last_full_b"] = {"restart"}, False
                            step = "exec"
                            continue
                        # cpath: scouting first (the unit's directory is read in place; a running job keeps running), then a fresh attempt only if one fits
                        cjob = S.pop("c_job", None)
                        if not cjob:
                            reap_leftovers(uid)
                            POOL.release(uid)   # nothing of the unit runs: the scouts take the unit's share (never counted twice)
                            grant = None
                        ensure_residual(uid, "grade C (round %d)" % S["rounds"])
                        sgrant = POOL.acquire(uid + "~scout", u, first=False)
                        try:
                            sc = scout_round(u, sgrant, cjob)
                        finally:
                            POOL.release(uid + "~scout")
                        if OVERRIDE.is_set():
                            outcome, reason = flushed()
                            break
                        scout_readings(uid, sc)   # before any archive: the evidence files are where the scouts saw them
                        if sc.get("winner") and usable_left() >= MIN_ATTEMPT_S + 600:
                            prov, pnote = _prov_restart(u, S["rounds"], g)
                            rinfo = restart_unit(u, S["rounds"], g, pnote, keep_deliverables=prov)
                            scout_seed(uid, sc)
                            rinfo["note"] = ((rinfo.get("note") or "") + " " + scout_note(uid, sc)).strip()
                            rinfo["note"] += _budget_note(S, u)
                            grant = _grant(uid, u, None)
                            S.update({"mode": "restart", "restart": rinfo, "sid": None, "last_full_b": False, "events": set(), "b_hit": None, "b_hit_n": 0,
                                      "last_gates": None, "cp_c": False, "fix_extra": ""})
                            job = None
                            step = "exec"
                            continue
                        if sc.get("winner"):   # no fresh attempt fits: the winner's route is applied in place (its files seeded, its readings propagated)
                            scout_seed(uid, sc)
                            snote = scout_note(uid, sc)
                        else:
                            snote = sc.get("note") or ""
                        if usable_left() < LAST_ACT_S:
                            outcome, reason = settle_in_place(g, gates, S, u)
                            break
                        running = ("\n\nThe unit's job (pid %s) is still running: keep it only if the route above still needs its output, else stop it."
                                   % cjob.get("pid")) if (cjob and not job_ended(uid, cjob)) else ""
                        S["mode"] = "fix"
                        S["extra"] = FIX_PROMPT.format(uid=uid, grade="C - fixed IN PLACE (%s)" % ("the scouted route; no fresh attempt fits" if sc.get("winner") else "no scouted route"),
                                                       fixes=(read(os.path.join(unit_dir(uid), "residual.md"))[:5000] or extract_fixes(uid))) + (
                                                       ("\n\n" + snote) if snote else "") + running + _budget_note(S, u)
                        S["events"], S["last_full_b"] = {"restart"}, False
                        job = None
                        step = "exec"
                        continue
                break
            except Exception as e:
                import traceback
                log("unit thread %s crashed: %s\n%s" % (uid, e, traceback.format_exc()[-1500:]))
                S["faults"] = S.get("faults", 0) + 1
                rec = not reopened and S["faults"] <= 2 and not OVERRIDE.is_set() and usable_left() >= LAST_ACT_S
                ledger({"kind": "unit_thread_error", "unit": uid, "error": str(e)[:300], "recovered": rec, "n": S["faults"]})
                if rec:
                    try:
                        if POOL:
                            POOL.release(uid + "~scout")
                    except Exception:
                        pass
                    log("%s: orchestrator fault %d - the unit continues (a fault is never charged to the unit)" % (uid, S["faults"]))
                    S["mode"], S["why_cont"], S["events"] = "continue", FAULT_WHY + _fix_carry(S), set()
                    job, cp_reason, step = None, None, "exec"
                    continue
                if reopened:
                    outcome, reason = "done", "reopen_thread_error"
                elif u.get("handoff_valid_at") and (S.get("last_gates") or {}).get("G1") == "pass" and not S.get("cp_c"):
                    outcome, reason = "gap", "thread_error_kept"
                else:
                    outcome, reason = "failed", "thread_error"
                g = g or "U"
                break
    finally:
        try:
            if POOL:
                POOL.release(uid)
                POOL.release(uid + "~scout")
        finally:
            settle(u, outcome, reason, (prev_grade if (reopened and outcome == "done" and reason != "reaudit_A") else g), S["rounds"])

def _exec_call_spec(u, S):
    """(tag, prompt, resume?) of the next executor-family call from the unit's state."""
    uid = u["id"]
    m = S["mode"]
    if m == "fresh":
        return "exec_%s" % uid, exec_prompt(u), False
    if m == "restart":
        S["mode"] = "fresh-restart"
        return "restart_%s" % uid, exec_prompt(u, restart=S["restart"]), False
    if m == "continue":
        return "cont_%s" % uid, CONTINUE_PROMPT.format(uid=uid, why=S.get("why_cont") or "", notes=_exec_notes(u)) + bank_round_block(uid, S.get("events") or {"slice_cut"}, cite=bool(S.get("fix_extra"))), True
    if m == "resume":
        return "resume_%s" % uid, (S.get("extra") or "") + _exec_notes(u) + bank_round_block(uid, S.get("events") or set(), cite=False), True
    return "fix_%s" % uid, (S.get("extra") or "") + _exec_notes(u) + bank_round_block(uid, S.get("events") or set(), cite=True), True
