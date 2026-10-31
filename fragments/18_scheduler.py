
# ---------------------------------------------------------------- the scheduler (main thread): launches, reopens, the branch replan
PLAN_KEYS = ("id", "type", "goal", "deliverables", "done_criteria", "depends_on", "needs", "cores", "mem_gb", "alloc_min", "scale_knobs",
             "report_cost_min", "notes")
def launch_unit(u, start="exec"):
    """Scheduler side: grant + directory + spec + input fingerprints + thread. Caller holds STATE_LOCK."""
    uid = u["id"]
    th0 = THREADS.get(uid)
    if th0 is not None and th0.is_alive():   # one thread per unit, always (a replan or a reopen never starts a second one)
        return False
    if start == "exec":
        waited = time.time() - float(u.get("ready_since") or time.time())
        g = POOL.acquire(uid, u, waited_s=waited, first=True)
        if g is None:
            return False
    else:
        g = None
    u["status"], u["t_launch"] = "running", time.time()
    u.pop("t_settle", None)
    if start == "exec":
        write_spec(u)
        prov = provisional_deps(u)
        for d in prov:
            du_ = unit_by_id(d)
            if du_ is not None:
                du_["released_deps"] = True
    else:
        prov = {}
    running = [x.get("id") for x in RUN["plan"] if x.get("status") == "running" and x.get("id") != uid]
    ledger({"kind": "launch", "unit": uid, "type": u.get("type"), "dir": "/workspace/%s/" % uid, "cores": (g or {}).get("cores"), "mem_gb": (g or {}).get("mem_gb"),
            "provisional": prov, "depends_on": list(u.get("depends_on") or []), "concurrent_with": running,
            "pool_used": list(POOL.used()), "remaining_s": int(remaining()), "start": start})
    log("LAUNCH %s (%s%s) in /workspace/%s/%s%s" % (uid, u.get("type"), "" if start == "exec" else ", REOPENED for a delta audit", uid,
        (" grant %dc/%dGB" % (g["cores"], g["mem_gb"])) if g else "", (" | running: %s" % ",".join(running)) if running else ""))
    LAST_LOOK[uid] = time.time()
    th = threading.Thread(target=unit_worker, args=(u, g, start), name="unit-%s" % uid, daemon=True)
    THREADS[uid] = th
    th.start()
    return True

def reblock_pending(plan):
    """A pending unit whose dependency retired (failed / blocked) while the planner ran would wait forever - it is blocked."""
    st = dict((x.get("id"), x.get("status")) for x in plan if isinstance(x, dict))
    out, changed = [], True
    while changed:
        changed = False
        for x in plan:
            if isinstance(x, dict) and not x.get("status") and x.get("type") != "render":
                bad = [d for d in (x.get("depends_on") or []) if st.get(d) in ("failed", "blocked")]
                if bad:
                    x["status"], x["blocked_by"] = "blocked", bad[0]
                    st[x.get("id")] = "blocked"
                    out.append(x.get("id"))
                    changed = True
                    ledger({"kind": "settle", "unit": x.get("id"), "status": "blocked", "reason": "upstream %s retired during the replan" % bad[0], "upstream": bad[0]})
    return out


def do_replan(req):
    """ONE planner call revises the branch of a retired unit while everything else keeps running. Done units and their
    directories are final; running units keep their live objects; the retired unit stays in the plan as history (id retired, directory kept
    for reference); blocked units named in the new plan become pending again; units the plan dropped are gone."""
    with STATE_LOCK:
        checkpoint()
        snapshot = json.loads(json.dumps(RUN["plan"]))
    newplan = get_plan(replan=req, old_plan=snapshot)
    if not newplan:
        ledger({"kind": "replan_failed", "unit": req["uid"], "blocked": req.get("blocked")})
        log("replan after %s produced no valid plan - the branch stays blocked: %s" % (req["uid"], req.get("blocked") or "-"))
        return False
    with STATE_LOCK:
        live = dict((x.get("id"), x) for x in RUN["plan"] if isinstance(x, dict))
        for i, nu in enumerate(newplan):
            ou = live.get(nu.get("id"))
            if ou is not None and ou.get("status") in ("done", "running") + HISTORY:
                newplan[i] = ou   # keep object identity: a running thread updates this dict
                continue
            for k in [k for k in list(nu) if k not in PLAN_KEYS]:   # a new or pending unit carries only planner keys (no copied orchestrator state)
                nu.pop(k, None)
            RUN["grades"].pop(nu.get("id"), None)
            while nu.get("id") in RUN["failed"]:
                RUN["failed"].remove(nu.get("id"))
        for ou in RUN["plan"]:   # a dropped retired unit stays in the plan as history
            if isinstance(ou, dict) and ou.get("status") in HISTORY and ou.get("id") not in [x.get("id") for x in newplan]:
                newplan.insert(0, ou)
        reblock_pending(newplan)
        RUN["plan"] = newplan
        ledger({"kind": "replan_applied", "unit": req["uid"], "units": [x.get("id") for x in newplan],
                "pending": [x.get("id") for x in newplan if not x.get("status") and x.get("type") != "render"],
                "running": [x.get("id") for x in newplan if x.get("status") == "running"], "done": [x.get("id") for x in newplan if x.get("status") == "done"]})
        log("branch replan applied: %s" % [x.get("id") + ("[%s]" % x["status"] if x.get("status") else "") for x in newplan])
        SCHED_CV.notify_all()
    checkpoint()
    return True

def _reopen_due(u):
    """A done produce unit flagged by propagation, with the wall for a delta audit and a fix, not reopened too often."""
    return (u.get("status") == "done" and u.get("reopen") and u.get("type") == "produce" and not OVERRIDE.is_set()
            and usable_left() >= REOPEN_MIN_S and int(u.get("reopens") or 0) < REOPEN_MAX)

def schedule_loop():
    """DAG scheduler (main thread). Returns 'normal' when every non-render unit is terminal (and no reopen is due), 'override' when the
    override fired."""
    while True:
        with SCHED_CV:
            for uid, th in list(THREADS.items()):
                if not th.is_alive():
                    THREADS.pop(uid, None)
            if OVERRIDE.is_set():
                return "override"
            req = RUN.pop("replan_req", None)
        if req:
            do_replan(req)
            continue
        with SCHED_CV:
            plan = RUN["plan"]
            others = [u for u in plan if isinstance(u, dict) and u.get("type") != "render"]
            for u in others:
                if u.get("status") == "done" and u.get("reopen") and u["id"] not in THREADS:
                    if _reopen_due(u):
                        u["reopens"] = int(u.get("reopens") or 0) + 1
                        u["reopen_why"] = u.pop("reopen").get("why")
                        ledger({"kind": "reopen", "unit": u["id"], "why": str(u["reopen_why"])[:300], "n": u["reopens"]})
                        launch_unit(u, start="reaudit")
                    else:
                        ledger({"kind": "reopen_skipped", "unit": u["id"], "why": str(u.pop("reopen").get("why"))[:200],
                                "usable_s": int(usable_left()), "reopens": int(u.get("reopens") or 0)})
            if all(u.get("status") in TERMINAL for u in others) and not THREADS:
                return "normal"
            for u in others:
                if u.get("status") not in ("running",) + TERMINAL and deps_done(u) and not u.get("ready_since"):
                    u["ready_since"] = time.time()
            ready = [u for u in others if u.get("status") not in ("running",) + TERMINAL and deps_done(u)]
            ready.sort(key=lambda x: float(x.get("ready_since") or 0))
            if not any([launch_unit(u) for u in ready]):
                SCHED_CV.wait(SCHED_TICK)

# ---------------------------------------------------------------- the override (the single time trigger) and the live clock
OVERRIDE_T0 = {"t": None}

def fire_override(reason="wall"):
    """Idempotent. Executors are told to flush (their dsh parent stops, their computations keep writing for FLUSH_WINDOW); critics, planners and
    scouts are stopped."""
    if OVERRIDE.is_set():
        return
    OVERRIDE.set()
    OVERRIDE_T0["t"] = time.time()
    with CALL_LOCK:
        active = [dict((k, v) for k, v in c.items() if k != "p") for c in ACTIVE_CALLS.values()]
    pending = "(state busy)"
    if STATE_LOCK.acquire(timeout=5):   # the trigger never waits on a busy scheduler
        try:
            pending = [u.get("id") for u in RUN["plan"] if isinstance(u, dict) and u.get("status") not in TERMINAL and u.get("type") != "render"]
        finally:
            STATE_LOCK.release()
    ledger({"kind": "override", "reason": reason, "remaining_s": int(remaining()), "report_cost_min": report_cost_min(), "flush_window_s": FLUSH_WINDOW,
            "active_calls": [c["tag"] for c in active], "unsettled": pending})
    log("OVERRIDE (%s): remaining %dm, report cost %dm -> report mode; flushing %s" % (reason, remaining() // 60, report_cost_min(), pending or "-"))
    ledger({"kind": "flush", "units": pending})
    interrupt_calls(lambda c: c.get("role") == "exec", "flush", "parent")
    interrupt_calls(lambda c: c.get("role") in ("critic", "plan", "scout"), "override", "tree")
    if STATE_LOCK.acquire(timeout=5):
        try:
            SCHED_CV.notify_all()
        finally:
            STATE_LOCK.release()

def write_clock():
    """/workspace/_pece/clock.json - the live clock every executor can `cat` (spare_s = usable_left, the one usable-time figure)."""
    try:
        units = "(state busy)"
        if STATE_LOCK.acquire(timeout=5):   # the clock never waits on a busy scheduler
            try:
                units = dict((x.get("id"), {"status": x.get("status"), "launched_at": int(x.get("t_launch") or 0),
                                            "used_s": int(time.time() - x["t_launch"]) if x.get("t_launch") and x.get("status") == "running" else None,
                                            "waiting": bool(x.get("waiting"))})
                             for x in RUN["plan"] if isinstance(x, dict) and x.get("t_launch"))
            finally:
                STATE_LOCK.release()
        c = {"ts": int(time.time()), "clock": now(), "wall_s": WALL, "elapsed_s": int(time.time() - T0), "remaining_s": int(remaining()),
             "report_reserve_s": int(report_reserve_s()), "spare_s": int(max(0, usable_left())), "override": OVERRIDE.is_set(),
             "note": "spare_s = wall left for computation before the run wraps up; a run longer than spare_s cannot finish", "units": units}
        _atomic_json_dump(c, os.path.join(PD, "clock.json"))
    except Exception as e:
        log("clock.json write failed: %s" % e)

def override_thread():
    write_clock()
    while not STOP.is_set():
        STOP.wait(OVERRIDE_TICK)
        if STOP.is_set() or REPORT["started"]:
            return
        if override_due():   # checked first: nothing on this thread may delay the trigger
            try:
                fire_override("wall")
            except Exception as e:
                log("override handler error: %s" % e)
            write_clock()
            return
        write_clock()

def flush_wait():
    """After the override: up to FLUSH_WINDOW (from the override instant) for the flushing threads to settle, then move on regardless."""
    t_end = (OVERRIDE_T0["t"] or time.time()) + FLUSH_WINDOW
    for th in list(THREADS.values()):
        th.join(max(0, t_end - time.time()))
    still = [uid for uid, th in THREADS.items() if th.is_alive()]
    ledger({"kind": "flush_done", "waited_s": int(time.time() - (OVERRIDE_T0["t"] or time.time())), "still_running": still})
    for uid in still:   # nothing of an unsettled unit runs into the report
        try:
            reap_leftovers(uid)
        except Exception:
            pass
    if still:
        log("flush window over; still running (their processes stopped): %s" % still)
