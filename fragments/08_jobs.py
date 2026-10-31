
# ---------------------------------------------------------------- waiting on a unit's running job (registered, or implicit = live unregistered processes)
LAST_WAIT_FACTS = {}     # uid -> measured facts of the last wait (resume prompts)
WAIT_RESUMED = {}        # uid -> {(pid, done_marker)} registrations already waited on and resumed; the same block left in handoff.json is stale
_PID_NOTED = set()

def _stale_wait(uid, wt):
    """A "waiting" block the orchestrator already waited on and resumed the executor for, whose job is over (done marker present or pid
    dead) - the executor left it in handoff.json; honouring it again loops the unit through zero-second waits and past the critic."""
    if not wt or wt.get("implicit"):
        return False
    if (int(wt["pid"]), str(wt["done_marker"])) not in WAIT_RESUMED.get(uid, set()):
        return False
    return os.path.exists(_ws_path(wt["done_marker"])) or not _pid_alive_unit(wt["pid"], uid)

def _waiting_of(uid):
    """The unit's registration (handoff.json "waiting", validated), with the live pid of bg.pid preferred over a hand-copied one."""
    try:
        wt = waiting_spec(json.load(open(os.path.join(unit_dir(uid), "handoff.json"))))[0]
    except Exception:
        return None
    try:
        fp = int(read(os.path.join(unit_dir(uid), "bg.pid"), "").split()[0])
    except Exception:
        fp = None
    if wt and fp and fp > 1 and fp != wt["pid"]:
        fa, ha = _pid_alive(fp), _pid_alive(wt["pid"])
        pick = fp if fa else (wt["pid"] if ha else fp)
        if (uid, wt["pid"], fp) not in _PID_NOTED:   # logged once per pair
            _PID_NOTED.add((uid, wt["pid"], fp))
            ledger({"kind": "wait_pid_from_file", "unit": uid, "handoff_pid": wt["pid"], "file_pid": fp, "chosen": pick, "file_alive": fa, "handoff_alive": ha})
        wt["pid"] = pick
    return wt

def _dir_activity(root, newer_than, limit=6000):
    """Newest mtime under root (bounded scan; returns early once something newer than `newer_than` is seen)."""
    newest, seen, stack = 0.0, 0, [root]
    while stack and seen < limit:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    try:
                        st = e.stat(follow_symlinks=False)
                    except Exception:
                        continue
                    if e.is_dir(follow_symlinks=False) and e.name not in (".dsh_ra3", "__pycache__", ".git", "node_modules"):
                        stack.append(e.path)
                    if st.st_mtime > newest:
                        newest = st.st_mtime
                    if newest > newer_than or seen >= limit:
                        return newest
        except Exception:
            pass
    return newest


def _wait_facts_text(uid):
    f = LAST_WAIT_FACTS.get(uid) or {}
    if not f:
        return "(no wait facts)"
    return ("%d min waited, bound extended %d time(s), last visible progress %d min ago, %d live process(es) of this unit at the end%s, bg.log %s"
            % (f.get("secs", 0) // 60, f.get("extensions", 0), f.get("quiet_s", 0) // 60, f.get("live_pids", 0),
               " (the registered pid was dead; the orchestrator waited on the unit's own processes)" if f.get("adopted") else "",
               ("%d bytes" % f["log_bytes"]) if f.get("log_bytes", -1) >= 0 else "absent"))


def _prog_sig(pats):
    """(path, size, mtime) of every file matching the declared progress patterns - any change = the job advanced."""
    sig = []
    for pat0 in pats:
        files = []
        for pat in _brace_expand(pat0):   # brace lists: data/{a,b}/*.json
            for p in sorted(glob.glob(pat))[:400]:
                if os.path.isdir(p):   # a directory entry = the files inside it (a directory's mtime does not move when a file in it grows)
                    try:
                        files += [os.path.join(p, x) for x in sorted(os.listdir(p))[:400]]
                    except OSError:
                        pass
                else:
                    files.append(p)
        for p in files[:400]:
            try:
                st = os.stat(p)
                sig.append((p, st.st_size, int(st.st_mtime)))
            except OSError:
                pass
    return tuple(sig)

_DATA_EXT = (".npz", ".npy", ".json", ".jsonl", ".csv", ".h5", ".hdf5", ".pkl", ".pickle", ".dat", ".parquet", ".mat")


def _data_grew(ud, since, logp=None, limit=6000):
    """Did any DATA file under the unit (result formats; not logs, not bookkeeping, not audit dirs) change after `since`?
    The second signal before a declared-progress stall ends a wait (a live job can write its data while the declared progress
    files stay quiet)."""
    skip = {os.path.join(ud, x) for x in ("handoff.json", "mech.json", "bg.pid", "spec.md")}
    if logp:
        skip.add(logp)
    seen, stack = 0, [ud]
    while stack and seen < limit:
        d = stack.pop()
        if "/audit_r" in d or d.endswith("/_pece") or "/.dsh_ra3" in d:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    if e.path in skip or not e.name.lower().endswith(_DATA_EXT):
                        continue
                    try:
                        if e.stat(follow_symlinks=False).st_mtime > since:
                            return True
                    except OSError:
                        pass
        except OSError:
            pass
    return False

# ---------------------------------------------------------------- the unit's running job (registered, or implicit) and the wait on it
JOB_T0 = {}   # (uid, pid, marker) -> the first wait start of that job


def current_job(uid, implicit=True):
    """The unit's running job: its registration (unless already served), else - implicit=True - its live unregistered processes. None when nothing
    of the unit runs."""
    wt = _waiting_of(uid)
    if wt and _stale_wait(uid, wt):
        wt = None
    if wt:
        job = dict(wt)
        job["implicit"] = False
    elif implicit:
        live = _live_unit_pids(uid)
        if not live:
            return None
        job = {"pid": live[0], "pids": live, "done_marker": "/workspace/%s/.implicit_job_never_marked" % uid, "expect_min": 0,
               "resume_note": "processes the session left running without registering them", "implicit": True}
    else:
        return None
    key = (uid, int(job["pid"]), str(job["done_marker"]))
    job["t0"] = JOB_T0.setdefault(key, time.time())
    return job

def job_ended(uid, job):
    """The job finished (its marker) or none of its processes (or of the unit's processes, for an implicit job) lives."""
    if not job:
        return True
    if os.path.exists(_ws_path(job["done_marker"])):
        return True
    pids = [int(x) for x in (job.get("pids") or [job["pid"]])]
    return all(not _pid_alive_unit(p, uid) for p in pids) and not (job.get("implicit") and _live_unit_pids(uid))

def wait_job(uid, u, job):
    """Wait on the unit's running job with no model call; nothing is killed. Returns (how, secs, checkpoint reason):
    marker | exited | stalled | orphan | check | override. Progress = done marker | log growth | declared progress files | any file under the
    unit (until a declared pattern matched); CPU alone is not progress. A registered pid without the unit's marker counts as dead (recycled).
    The checkpoint interrupts the wait - registered and implicit jobs alike."""
    pid, marker = int(job["pid"]), _ws_path(job["done_marker"])
    logp = _ws_path(job["log"]) if job.get("log") else os.path.join(unit_dir(uid), "bg.log")
    prog = [_ws_path(x) for x in (job.get("progress") or [])]
    ud = unit_dir(uid)
    pids = [int(x) for x in (job.get("pids") or [pid])]
    gone = lambda: all(not _pid_alive_unit(p, uid) for p in pids)
    pid_ok = not gone()
    live = lambda: _live_unit_pids(uid)
    t0 = quiet0 = last_log = time.time()
    u["waiting"] = {"pid": pid, "since": int(t0), "expect_min": job.get("expect_min"), "implicit": bool(job.get("implicit"))}
    ledger({"kind": "wait_start", "unit": uid, "pid": pid, "pid_ok": pid_ok, "expect_min": job.get("expect_min"), "implicit": bool(job.get("implicit")),
            "pids": pids[:20], "done_marker": job["done_marker"], "resume_note": str(job.get("resume_note") or "")[:200], "progress": len(prog),
            "job_age_s": int(t0 - job.get("t0", t0))})
    log("%s WAITING for its %s job (pid %d%s%s; no model call until it ends or a look is due)" % (
        uid, "unregistered" if job.get("implicit") else "registered", pid, "" if pid_ok else " NOT ALIVE",
        (", declared %.0f min" % float(job["expect_min"])) if job.get("expect_min") else ""))
    adopted, t_adopt = [], t0
    if not pid_ok:
        adopted = live()
        ledger({"kind": "wait_pid_invalid", "unit": uid, "pid": pid, "adopted": adopted[:20]})
        if adopted:
            ledger({"kind": "wait_pid_adopted", "unit": uid, "registered": pid, "pids": adopted[:20]})
    lv = list(adopted)
    lsz, newest, tick_n = -1, 0.0, 0
    prog_sig, prog_seen, prog_gap_max, prog_last_t, prog_stall_s = None, False, 0.0, time.time(), WAIT_PROG_STALL_S
    how, cp = None, None
    while True:
        now_ = time.time()
        try:   # the log can vanish between two calls (a job rotating it)
            sz = os.path.getsize(logp)
        except OSError:
            sz = -1
        if sz > lsz:
            lsz, quiet0 = sz, now_
        if prog:
            ps_ = _prog_sig(prog)
            if ps_:
                prog_seen = True
            if ps_ != prog_sig:
                if prog_sig is not None and ps_:
                    prog_gap_max = max(prog_gap_max, now_ - prog_last_t)
                    prog_stall_s = int(max(WAIT_PROG_STALL_S, min(2.0 * prog_gap_max, max(WAIT_PROG_STALL_S, usable_left() // 4))))
                prog_last_t = now_
                prog_sig, quiet0 = ps_, now_
        if not (prog and prog_seen):
            nw = _dir_activity(ud, newest)
            if nw > newest:
                newest, quiet0 = nw, now_
        if tick_n % 5 == 0:
            lv = live()
        tick_n += 1
        if os.path.exists(marker):
            how = "marker"
        elif OVERRIDE.is_set():
            how = "override"
        elif pid_ok and gone():
            lv = live()
            if lv:
                adopted, pid_ok, t_adopt = list(lv), False, now_
                ledger({"kind": "wait_pid_adopted", "unit": uid, "registered": pid, "pids": adopted[:20], "after_exit": True})
            else:
                how = "exited"
        if how is None:
            quiet = now_ - quiet0
            running = (pid_ok and not gone()) or bool(lv)
            if prog and prog_seen and quiet >= prog_stall_s and not _data_grew(ud, now_ - prog_stall_s, logp):
                how = "stalled"
            elif prog and prog_seen and quiet >= prog_stall_s:
                quiet0 = now_
            elif not running and job.get("implicit"):
                how = "exited"
            elif not running and quiet >= WAIT_STALL_S:
                how = "stalled"
            elif not running and not pid_ok and now_ - t0 >= WAIT_DEAD_GRACE_S and quiet >= WAIT_DEAD_GRACE_S:
                how = "stalled"
            elif running and not pid_ok and adopted and not job.get("implicit") and now_ - t_adopt >= WAIT_ADOPT_MAX_S:
                how = "orphan"
        if how is None:
            cp = checkpoint_due(uid, job, now_)
            if cp:
                how = "check"
        if how:
            break
        if now_ - last_log >= 900:
            last_log = now_
            log("%s still waiting: %d min; pid %d %s, %d live unit process(es)" % (uid, (now_ - t0) // 60, pid, "alive" if pid_ok and not gone() else "gone", len(lv)))
        OVERRIDE.wait(WAIT_TICK)
    secs = int(time.time() - t0)
    LAST_WAIT_FACTS[uid] = {"secs": secs, "extensions": 0, "quiet_s": int(time.time() - quiet0), "live_pids": len(lv), "adopted": bool(adopted), "log_bytes": lsz}
    u.pop("waiting", None)
    if how in ("marker", "exited", "stalled", "orphan") and not job.get("implicit"):
        WAIT_RESUMED.setdefault(uid, set()).add((pid, str(job["done_marker"])))
    ledger({"kind": "wait_end", "unit": uid, "how": how, "seconds": secs, "pid": pid, "checkpoint": cp, "live": len(lv), "implicit": bool(job.get("implicit"))})
    if how in ("marker", "exited"):
        ledger({"kind": "wait_done", "unit": uid, "seconds": secs, "how": how, "pid": pid})
    log("%s wait ended after %d min: %s%s" % (uid, secs // 60, how, (" (%s)" % cp) if cp else ""))
    return how, secs, cp


def resume_text(uid, job, how, secs):
    em = job.get("expect_min")
    return RESUME_PROMPT.format(uid=uid, pid=job.get("pid"), expect=("an unregistered run" if job.get("implicit") else "%.0f min" % float(em or 0)),
                                how=_HOW_TEXT.get(how, how), mins=secs // 60, facts=_wait_facts_text(uid), note=str(job.get("resume_note") or "(none)")[:400])
