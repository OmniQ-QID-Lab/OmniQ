
# ---------------------------------------------------------------- the resource pool: one admission rule; the GPU is a token
def _units_rss_gb():
    """Measured resident memory (GB) of every process of THIS episode's units (PECE_UNIT marker) - what the running units really use."""
    needle, tot = ("PECE_UNIT=%s:" % RUN_ID).encode(), 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/environ" % pid, "rb") as f:
                if needle not in f.read():
                    continue
            with open("/proc/%s/status" % pid) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        tot += int(line.split()[1])
                        break
        except Exception:
            pass
    return tot / (1024.0 * 1024.0)

RSS_CACHE = {"gb": 0.0, "t": 0.0, "live": False}

def rss_sampler():
    """Daemon: the units' measured memory every 30 s, outside every lock (a /proc read can block on a stuck process)."""
    RSS_CACHE["live"] = True
    while not STOP.is_set():
        try:
            RSS_CACHE["gb"], RSS_CACHE["t"] = _units_rss_gb(), time.time()
        except Exception:
            pass
        STOP.wait(30)

def _rss_now():
    """The sampler's figure; an inline scan only where no sampler runs (the offline tests)."""
    return RSS_CACHE["gb"] if RSS_CACHE["live"] else _units_rss_gb()

class ResourcePool:
    """Cores + memory of the node's allocation across the running units (no CPU pinning). A request gets its declaration when the declarations
    fit. Otherwise - a repair round at once, a first launch after POOL_WAIT_S of readiness - it gets min(declared, max(2, free)) cores (cores may
    be oversubscribed) and its declared memory when the node's MEASURED use leaves room for it; a first launch waits for real memory, a repair
    round takes what is really free (at least 4 GB). A ready unit never starves behind inflated declarations."""
    def __init__(self, ncpus, mem_gb):
        self.lock = threading.RLock()
        self.ncpus = max(1, int(ncpus))
        self.mem_gb = int(mem_gb) if (mem_gb and mem_gb > 0) else 0
        self.grants = {}

    def clamp(self, unit):
        try:
            cores = int(unit.get("cores") or self.ncpus)
        except Exception:
            cores = self.ncpus
        cores = min(max(1, cores), self.ncpus)
        try:
            mem = float(unit.get("mem_gb") or (self.mem_gb or 1))
        except Exception:
            mem = float(self.mem_gb or 1)
        mem = max(1, int(round(mem)))
        if self.mem_gb > 0:
            mem = min(mem, self.mem_gb)
        return cores, mem

    def used(self):
        with self.lock:
            return (sum(g["cores"] for g in self.grants.values()), sum(g["mem_gb"] for g in self.grants.values()))

    def acquire(self, uid, unit, waited_s=0.0, first=True):
        """Non-blocking; the grant dict, or None (a first launch that does not fit and either waited less than POOL_WAIT_S or finds no real memory)."""
        cores, mem = self.clamp(unit)
        with self.lock:
            if uid in self.grants:
                return self.grants[uid]
            uc, um = self.used()
            fc, fm = self.ncpus - uc, ((self.mem_gb - um) if self.mem_gb > 0 else mem)
            if cores <= fc and mem <= fm:
                g = {"uid": uid, "cores": cores, "mem_gb": mem, "t": time.time()}
            elif (not first) or waited_s >= POOL_WAIT_S:
                c = max(1, min(cores, max(2, fc)))
                if self.mem_gb <= 0 or mem <= fm:
                    m, real = mem, None
                else:
                    real = self.mem_gb * 0.9 - _rss_now()
                    if real >= mem:
                        m = mem
                    elif not first:
                        m = max(4, min(mem, int(real)))
                    else:
                        return None
                g = {"uid": uid, "cores": c, "mem_gb": m, "t": time.time(), "shared": True, "reduced": bool(c < cores or m < mem),
                     "measured_free_gb": (round(real, 1) if real is not None else None)}
            else:
                return None
            self.grants[uid] = g
        if g.get("shared"):
            ledger({"kind": "pool_shared", "unit": uid, "declared": [cores, mem], "cores": g["cores"], "mem_gb": g["mem_gb"], "first": first,
                    "waited_s": int(waited_s), "pool_used": list(self.used()), "measured_free_gb": g.get("measured_free_gb")})
        return g

    def acquire_free(self, uid, want=2):
        """Up to `want` free cores now (at least 1) - the report agent starts even while flushing units hold grants."""
        with self.lock:
            if uid in self.grants:
                return self.grants[uid]
            uc, um = self.used()
            n = max(1, min(want, self.ncpus - uc))
            g = {"uid": uid, "cores": n, "mem_gb": max(1, (self.mem_gb - um) if self.mem_gb > 0 else 1), "t": time.time(), "shared": self.ncpus - uc < 1}
            self.grants[uid] = g
            return g

    def release(self, uid):
        with self.lock:
            g = self.grants.pop(uid, None)
        if g:
            with SCHED_CV:
                SCHED_CV.notify_all()
        return g

POOL = None
SCHED_CV = threading.Condition(STATE_LOCK)
THREADS = {}
TERMINAL = ("done", "failed", "blocked", "flushed")

# ---------------------------------------------------------------- scheduling helpers

def unit_by_id(uid, plan=None):
    for u in (plan if plan is not None else RUN["plan"]):
        if isinstance(u, dict) and u.get("id") == uid:
            return u
    return None


def deps_done(u):
    """Readiness: every dependency is done (the critic has spoken) or passes the FILE GATE (a running unit whose valid handoff names the files this
    unit needs, unchanged for FILE_GATE_S). The dependent then starts PROVISIONALLY (its prompt says so) instead of idling through the upstream's
    fix rounds. Nothing else counts."""
    for d in (u.get("depends_on") or []):
        du = unit_by_id(d)
        if du is None:
            return False
        if du.get("status") == "done" or (du.get("status") == "running" and _file_gate_ok(u, d, du)):
            continue
        return False
    return True

def _file_gate_ok(u, d, du):
    """FILE GATE: the upstream d has a valid handoff and every file u needs from it exists (a pattern: at least one match) and has been unchanged
    for FILE_GATE_S -> the dependent may start on it PROVISIONALLY (its audit follows; its inputs are fingerprinted)."""
    if not du.get("handoff_valid_at"):
        return False
    paths = need_paths((u.get("needs") or {}).get(d), d)
    if not paths:
        return False
    now_ = time.time()
    for p in paths:
        pat = re.sub(r"<[^<>/]*>", "*", p)
        hits = []
        for alt in _brace_expand(_ws_path(pat)):
            hits += glob.glob(alt) if _has_glob(alt) else ([alt] if os.path.exists(alt) else [])
        if not hits:
            return False
        for h in hits[:200]:
            try:
                if now_ - os.stat(h).st_mtime < FILE_GATE_S:
                    return False
            except OSError:
                return False
    return True

def provisional_deps(u):
    """{dep id: "file"} for the dependencies of u that run without a final verdict (they passed the file gate)."""
    out = {}
    for d in (u.get("depends_on") or []):
        du = unit_by_id(d) or {}
        if du.get("status") == "running" and _file_gate_ok(u, d, du):
            out[d] = "file"
    return out


def _g13_pass(gates):
    """G1 and G3 pass (a B for a located G2 code error qualifies for the incremental re-audit; dependents still need _g123_pass)."""
    return bool(gates) and all(str((gates or {}).get(k, "")).lower() == "pass" for k in ("G1", "G3"))


def _g123_pass(gates):
    """G1, G2 and G3 all pass in the critic's GATES line (a grade B whose fix list is the only open item)."""
    return bool(gates) and all(str((gates or {}).get(k, "")).lower() == "pass" for k in ("G1", "G2", "G3"))


def downstream_ids(uid, plan):
    """Transitive dependents of uid in plan order."""
    out, changed = [], True
    while changed:
        changed = False
        for u in plan:
            if isinstance(u, dict) and u.get("id") not in out and u.get("id") != uid and any(d == uid or d in out for d in (u.get("depends_on") or [])):
                out.append(u["id"])
                changed = True
    return out


def _prev_verdict_note(u):
    """The newest archived round's critic verdict (grade + first defect lines) for a unit flushed before its redo was audited."""
    for a in reversed(u.get("archives") or []):
        txt = read(os.path.join(_ws_path(a), "critic_report.md"))
        if not txt:
            continue
        g = re.match(r"\s*GRADE:\s*([ABCDW])", txt)
        m = re.search(r"(?:FIX LIST|WHAT IS WRONG|RESIDUAL)(.*?)(?:\n[A-Z][A-Z /]{3,}\n|\nFIX_PLAN:|\Z)", txt, re.S)
        body = " ".join(l.strip() for l in (m.group(1) if m else txt[:600]).strip().splitlines() if l.strip())[:400]
        return "redo flushed before its audit; the previous round (%s) was graded %s - its defects: %s" % (a, g.group(1) if g else "?", body or "(see its critic_report.md)")
    return ""

def _gap_note(uid):
    """The unmet rows of a unit settled DONE WITH GAP (its critic's G3 line / missed ANCHORS rows), for dependents and the report."""
    txt = read(os.path.join(unit_dir(uid), "critic_report.md"))
    rows = _anchor_missed(txt)
    if not rows:
        mm = re.search(r"G3=hit[^\n]*", txt)
        rows = [mm.group(0)] if mm else ["(see its critic_report.md)"]
    return " ; ".join(r[:200] for r in rows[:6])

def settle(u, status, reason, grade, rounds):
    """The terminal transition of a unit: status + grade + ledger, and EVERY settle stops the unit's processes (a job left running could rewrite files
    its dependents have started to read). failed: its produce dependents are blocked and one branch replan is requested. done: done
    dependents whose inputs changed are reopened. gap = done with its unmet rows stated (the data flow on)."""
    uid = u["id"]
    gap = status == "gap"
    status = "done" if gap else status
    try:   # stop the unit's processes BEFORE the status flips (no dependent can launch on files a leftover still writes)
        killed = reap_leftovers(uid)
        if killed:
            ledger({"kind": "settle_reaped", "unit": uid, "status": status, "n": len(killed)})
    except Exception as e:
        log("%s: reap at settle failed: %s" % (uid, e))
    with SCHED_CV:
        u["status"], u["t_settle"], u["settle_reason"] = status, time.time(), reason
        u.pop("waiting", None)
        u.pop("reopen", None)
        try:
            if status == "flushed":
                note = u.pop("override_note", None) or _prev_verdict_note(u)
                if note:
                    u["flush_note"] = note
                    ledger({"kind": "flush_unaudited", "unit": uid, "note": note[:400]})
            if status in ("done", "failed"):
                RUN["grades"][uid] = ("A" if (status == "done" and not gap and reason in ("A", "reaudit_A")) else (grade or ("B" if status == "done" else "U")))
            if gap:
                u["gap"] = True
                u["gap_unaudited"] = str(reason).startswith("gap_unaudited")
                u["gap_note"] = _gap_note(uid)
                ledger({"kind": "unit_gap", "unit": uid, "grade": grade, "rounds": rounds, "reason": reason, "note": u["gap_note"][:600]})
                log("%s: DONE WITH GAP (grade %s, %s): the data flow on; unmet: %s" % (uid, grade, reason, u["gap_note"][:300]))
            elif status == "done" and not str(reason).startswith("reopen"):
                u.pop("gap", None)
            if status == "failed" and uid not in RUN["failed"]:
                RUN["failed"].append(uid)
        except Exception as e:
            log("%s: settle bookkeeping failed: %s" % (uid, e))
        ledger({"kind": "settle", "unit": uid, "status": status, "reason": reason, "grade": RUN["grades"].get(uid), "rounds": rounds, "gap": gap})
        log("%s -> %s (%s; grade %s, %d repair round(s))" % (uid, ("DONE WITH GAP" if gap else status.upper()), reason, RUN["grades"].get(uid), rounds))
        SCHED_CV.notify_all()
    if POOL:
        POOL.release(uid)
    if status == "failed":
        try:
            if os.path.isdir(unit_dir(uid)):
                ensure_residual(uid, "retired: %s" % reason)
        except Exception as e:
            log("%s: residual at retirement failed: %s" % (uid, e))
        with SCHED_CV:
            RUN["retired"].append(uid)
            blocked = []
            for y in downstream_ids(uid, RUN["plan"]):
                yu = unit_by_id(y)
                if yu is None or yu.get("type") == "render" or yu.get("status") in ("running",) + TERMINAL:
                    continue
                yu["status"], yu["blocked_by"] = "blocked", uid
                blocked.append(y)
                ledger({"kind": "settle", "unit": y, "status": "blocked", "reason": "upstream %s retired" % uid, "upstream": uid})
            if RUN["replans"] < 1 and not OVERRIDE.is_set() and usable_left() >= MIN_ATTEMPT_S and blocked:
                RUN["replans"] += 1
                RUN["replan_req"] = {"uid": uid, "reason": reason, "blocked": blocked}
                ledger({"kind": "replan_trigger", "unit": uid, "reason": reason, "blocked": blocked, "replan": RUN["replans"]})
            elif blocked:
                ledger({"kind": "replan_cap", "unit": uid, "reason": reason, "blocked": blocked, "why": "override" if OVERRIDE.is_set() else "no_time_or_cap"})
            SCHED_CV.notify_all()
    if status == "done" and u.get("type") == "produce":
        try:
            propagate_inputs(exclude=uid)
        except Exception as e:
            log("%s: propagation failed: %s" % (uid, e))
    checkpoint()

def override_hold(u, g, action, why):
    """After the override a verdict never restarts, archives, retires or re-opens a unit - anything but a settled done becomes a note."""
    if not OVERRIDE.is_set() or action in ("done", "gap"):
        return False
    u["override_note"] = "the audit at the override graded %s (%s) - not acted on; the unit's files stay as they were" % (g, why)
    ledger({"kind": "override_verdict_held", "unit": u.get("id"), "grade": g, "action": action, "reason": why})
    return True
