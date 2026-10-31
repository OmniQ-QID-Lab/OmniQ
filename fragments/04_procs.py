
# ---------------------------------------------------------------- the process registry: markers, session ids, the registered job's family

def _call_marker(tag):
    """Environment marker every process of one agent call inherits (the orchestrator and the agents share one PID namespace)."""
    return "%s:%s" % (RUN_ID, tag)


def _call_procs(pgid, tag, family=False):
    """[(pid, cpu_s, cmdline)] of every live process in the call's process group, plus every process carrying the call's PECE_CALL_TAG marker
    (dsh spawns tool commands detached). family: the markers of the call's retries (_gwretryN) and wrap-up count too."""
    marker = ("PECE_CALL_TAG=%s\0" % _call_marker(tag)).encode() if tag else None
    fam_rx = re.compile(re.escape(("PECE_CALL_TAG=%s" % _call_marker(tag)).encode()) + rb"(?:_gwretry\d+|_wrapup)*\0") if (tag and family) else None
    tck = float(os.sysconf("SC_CLK_TCK"))
    me = os.getpid()
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            st = open("/proc/%s/stat" % pid).read()
            f = st[st.rindex(")") + 2:].split()   # fields after comm
            hit = (pgid is not None and int(f[2]) == pgid)
            if not hit and marker:
                with open("/proc/%s/environ" % pid, "rb") as ef:
                    envb = ef.read()
                hit = bool(fam_rx.search(envb)) if fam_rx else marker in envb
            if not hit:
                continue
            cmd = open("/proc/%s/cmdline" % pid).read().replace("\0", " ").strip()
            out.append((int(pid), (int(f[11]) + int(f[12])) / tck, cmd))
        except Exception:
            pass
    return out


def _call_cpu(pgid, tag):
    return sum(c for _, c, _ in _call_procs(pgid, tag))


def _slice_progress(uid, t_now, cpu_delta, n_ext=0):
    """Why the executor slice of `uid` counts as productive at its boundary - a file under the unit written in the last
    SLICE_PROG_S, or (for the FIRST extension only) the call's processes burning CPU in the last sample window. '' = idle -> cut."""
    try:   # when the unit's registered job is alive its writes are the JOB's progress, not the session's
        wt = _waiting_of(uid)
        job_alive = bool(wt) and _pid_alive(int(wt["pid"]))
    except Exception:
        job_alive = False
    try:
        age = t_now - _dir_activity(unit_dir(uid), t_now - SLICE_PROG_S)
        if age < SLICE_PROG_S and not job_alive:
            return "files written %d min ago" % max(0, int(age // 60))
    except Exception:
        pass
    if cpu_delta >= STALL_CPU_S and n_ext == 0:   # CPU without output is progress once; after that only files count (a busy loop must not hold the slice open)
        return "%.0f CPU-s in the last sample window" % cpu_delta
    return ""


def _slice_room(elapsed):
    """May this executor call take SLICE_EXT_S more? Only while that much usable wall is left."""
    return usable_left() >= SLICE_EXT_S


def _pid_alive(pid):
    """Is pid a live (non-zombie) process?"""
    try:
        return (read("/proc/%d/stat" % int(pid)) or "Z").split(")")[-1].split()[0] != "Z"
    except Exception:
        return False


def _registered_family(uid):
    """The pids of the job the unit registered in handoff.json "waiting" - the pid itself, its descendants and every member of its
    process group (setsid makes it a group leader) - when that pid is alive; () otherwise. Spared by the marker sweep of a cut slice."""
    if not uid:
        return ()
    roots = set()
    try:
        wt = _waiting_of(uid)
        if wt:
            roots.add(int(wt["pid"]))
            roots.update(int(x) for x in (wt.get("pids") or []) if str(x).strip().isdigit())
    except Exception:
        pass
    try:   # the raw block (even when invalid: a null / string pid beside real "pids") and the executor's own bg*.pid files count too
        raw = (json.load(open(os.path.join(unit_dir(uid), "handoff.json"))).get("waiting") or {})
        if isinstance(raw, dict):
            for x in [raw.get("pid")] + list(raw.get("pids") or []):
                if str(x).strip().isdigit():
                    roots.add(int(str(x).strip()))
    except Exception:
        pass
    try:
        for f in glob.glob(os.path.join(unit_dir(uid), "bg*.pid"))[:20]:
            for tok in read(f, "").split()[:8]:
                if tok.isdigit():
                    roots.add(int(tok))
    except Exception:
        pass
    roots = set(p for p in roots if p > 1 and _pid_alive(p))
    if not roots:
        return ()
    ppid, pgid = {}, {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            f = read("/proc/%s/stat" % p).split(")")[-1].split()
            ppid[int(p)], pgid[int(p)] = int(f[1]), int(f[2])
        except Exception:
            pass
    fam = set(roots)
    groups = set(pgid.get(r) for r in roots if pgid.get(r) is not None)
    changed = True
    while changed:
        changed = False
        for p, pp in ppid.items():
            if p not in fam and (pp in fam or pgid.get(p) in groups):
                fam.add(p); changed = True
    return tuple(sorted(fam))


def _kill_marked(tag, sig=9, spare=()):
    """Kill every process still carrying the call's marker (dsh's detached tool trees after the parent is gone). `spare` pids
    (the unit's registered job and its family) are left alone."""
    n = 0
    spare = set(int(x) for x in (spare or ()))
    for pid, _, _ in _call_procs(None, tag, family=True):
        if pid in spare:
            continue
        try:
            os.kill(pid, sig)
            n += 1
        except Exception:
            pass
    return n


def _dsh_kill_tree(p):
    """dsh spawns every tool command DETACHED: SIGTERM first (dsh stops its own trees), then SIGKILL + marker sweep."""
    tag = getattr(p, "pece_tag", None)
    try:
        pgid = os.getpgid(p.pid)
    except Exception:
        pgid = None
    try:
        p.terminate()
    except Exception:
        pass
    try:
        p.wait(timeout=DSH_KILL_GRACE_S)
    except Exception:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass
    if pgid is not None:
        try:
            os.killpg(pgid, 9)
        except Exception:
            pass
    if tag:
        fam = _registered_family(_tag_uid(tag))   # the registered background job survives the cut of the session that started it
        n = _kill_marked(tag, 9, spare=fam)
        if n or fam:
            log("%s: %d detached process(es) killed by marker sweep%s" % (tag, n, ("; the registered job kept (%d process(es))" % len(fam)) if fam else ""))
        if fam:
            ledger({"kind": "sweep_spared_registered", "tag": tag, "unit": _tag_uid(tag), "pids": list(fam)[:30]})


def _unit_marker(uid): return "%s:%s" % (RUN_ID, uid)   # PECE_UNIT value inherited by everything the unit's sessions start


def reap_leftovers(uid, keep_pid=None, kill_pid=None, dry=False):
    """A turn (an exec or critic session, wrap-ups included) leaves no process behind: every live process in the session of
    one of the unit's dsh calls (unless it carries another unit's marker), in kill_pid's process group, or carrying the
    unit's PECE_UNIT marker is killed - never keep_pid's process group or the orchestrator's own. dry=True only lists them,
    and forgets the recorded session ids that no process holds any more (a recycled pid could make a later scan count an
    unrelated process as the unit's); the list is edited in place, never replaced (run_call appends to it)."""
    lst = UNIT_PIDS.get(uid) if dry else None
    snap = list(lst or []) if dry else UNIT_PIDS.pop(uid, [])   # a dry scan keeps the live session ids for the real sweep
    sids, live_sids = set(snap), set()
    needle, keep, kill, found = ("PECE_UNIT=%s\0" % _unit_marker(uid)).encode(), None, None, []
    try:
        keep = os.getpgid(int(keep_pid)) if keep_pid else None
        kill = os.getpgid(int(kill_pid)) if kill_pid else None
    except Exception:
        pass
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            f = read("/proc/%s/stat" % pid).split(")")[-1].split()   # state pgid=f[2] session=f[3]
            live_sids.add(int(f[3]))   # a session id stays in use while any member (a zombie too) exists
            if f[0] == "Z" or int(f[2]) == os.getpgid(0):
                continue
            hit = int(f[3]) in sids or int(f[2]) == kill
            with open("/proc/%s/environ" % pid, "rb") as ef:
                envb = ef.read()
            if hit:
                m = re.search(rb"PECE_UNIT=([^\0]*)\0", envb)
                hit = not (m and m.group(1) != _unit_marker(uid).encode())
            else:
                hit = needle in envb
            if hit and (keep is None or int(f[2]) != keep):
                found.append((int(pid), int(f[2]), read("/proc/%s/cmdline" % pid).replace("\0", " ").strip()[:90]))
        except Exception:
            pass
    if dry:
        for s in snap:   # only ids seen before the scan: a call that started during it is never dropped
            if s not in live_sids and lst is not None:
                try:
                    lst.remove(s)
                except ValueError:
                    pass
        return found
    for pid, pgid, _ in found:
        for fn, tgt in ((os.killpg, pgid), (os.kill, pid)):
            try: fn(tgt, 9)
            except Exception: pass
    if found:
        ledger({"kind": "leftover_killed", "unit": uid, "n": len(found), "pids": [p for p, _, _ in found][:50], "cmds": sorted(set(c for _, _, c in found))[:20], "kept_pgid": keep})
        log("%s: %d leftover process(es) killed after the session: %s" % (uid, len(found), "; ".join(c for _, _, c in found[:4])[:300]))
    return found


def _session_alive(sid):
    """Is any process (a zombie included: it still holds the id) a member of session `sid`?"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if int(read("/proc/%s/stat" % pid).split(")")[-1].split()[3]) == int(sid):
                return True
        except Exception:
            pass
    return False


def reap_critic(uid, *tags):
    """The critic's post-session cleanup: only the processes carrying the critic calls' own markers (its probes), never the unit's
    background production."""
    n = 0
    for t in tags:
        n += _kill_marked(t, 9)
    if n:
        ledger({"kind": "critic_probes_killed", "unit": uid, "n": n, "tags": list(tags)})
        log("%s: %d critic probe process(es) killed after the audit" % (uid, n))
    return n


def _pid_is_units(pid, uid):
    """Does pid carry this unit's PECE_UNIT marker? (a stale pid from an archived handoff may have been reused by another process)"""
    try:
        with open("/proc/%d/environ" % int(pid), "rb") as f:
            return ("PECE_UNIT=%s\0" % _unit_marker(uid)).encode() in f.read()
    except Exception:
        return False


def _pid_alive_unit(pid, uid):
    """Alive AND one of this unit's processes (its PECE_UNIT marker) - a recycled pid is a dead job."""
    return _pid_alive(pid) and _pid_is_units(pid, uid)

def _live_unit_pids(uid):
    try:
        return [p for p, _, _ in reap_leftovers(uid, dry=True) if _pid_alive(p)]
    except Exception:
        return []


def _sweep_run():
    """Stop every other process of this run: SIGKILL to each process whose environment carries PECE_CALL_TAG=<RUN_ID>: or
    PECE_UNIT=<RUN_ID>: (dsh sessions, the tool trees dsh starts detached, the units' jobs). The marker must open an environment
    entry and the trailing colon keeps out a longer run id that starts with this one (x_p10 never matches x_p101). Returns the
    number of processes signalled."""
    rx = re.compile(rb"(?:^|\0)PECE_(?:CALL_TAG|UNIT)=" + re.escape(RUN_ID.encode()) + rb":")
    me, n = os.getpid(), 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open("/proc/%s/environ" % pid, "rb") as f:
                hit = bool(rx.search(f.read()))
        except Exception:
            continue
        if hit:
            try:
                os.kill(int(pid), 9)
                n += 1
            except Exception:
                pass
    return n
