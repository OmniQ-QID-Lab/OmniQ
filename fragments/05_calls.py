
# ---------------------------------------------------------------- the call layer: one dsh session per call, one outcome classification
_TAG_SUFFIX = re.compile(r"(_wrapup|_gwretry\d*|_only)+$")


def _tag_uid(tag):
    """Unit id carried by a call tag (exec_u1, critic2_u1, rethink_u2r_wrapup ...); None for plan/replan."""
    t = _TAG_SUFFIX.sub("", tag or "")
    if re.match(r"(plan|replan)", t):
        return None
    mm = re.match(r"[a-z]+\d*_(.+)$", t)
    return mm.group(1) if mm else None

def _role_of(tag):
    """Call family used by the interrupt / override / patch machinery: exec (every executor-family session), critic (audits, checkpoints,
    selection panels), scout, plan, report."""
    t = tag or ""
    if t.startswith("critic"):
        return "critic"
    if t.startswith("scout"):
        return "scout"
    if re.match(r"(exec|fix|cont|resume|restart|bfix)", t):
        return "exec"
    if re.match(r"(plan|replan)", t):
        return "plan"
    if t.startswith("report"):
        return "report"
    return "other"

def time_ledger(uid=None):
    """Information-only clock line injected into every role prompt (no caps, no orders)."""
    el = time.time() - T0
    line = ("TIME LEDGER: wall %.1fh; elapsed %.1fh; remaining %.1fh "
            "(report reserve %d min = the report cost; the orchestrator overrides "
            "into report mode when remaining <= report cost + %d min%s)."
            % (WALL / 3600.0, el / 3600.0, remaining() / 3600.0, report_cost_min(),
               FLUSH_WINDOW // 60,
               "; OVERRIDE ACTIVE - report mode" if OVERRIDE.is_set() else ""))
    try:
        pl = json.loads(read(os.path.join(PD, "plan.json"), "null")) or []
        if isinstance(pl, list) and pl:
            line += " Units done: %d/%d." % (
                sum(1 for x in pl if isinstance(x, dict) and x.get("status") == "done"), len(pl))
    except Exception:
        pass
    if uid:
        att, spent, waited, gseq = 0, 0.0, 0.0, []
        for ln in read(os.path.join(PD, "ledger.jsonl")).splitlines():
            try:   # the ledger lies in /workspace: a line an agent appended (not an object, odd types) is skipped
                e = json.loads(ln)
                if not isinstance(e, dict):
                    continue
                if e.get("kind") == "call" and re.search(r"_%s(_|$)" % re.escape(uid), str(e.get("tag") or "")):
                    att, spent = att + 1, spent + float(e.get("dur") or 0)
                elif e.get("kind") == "wait_end" and e.get("unit") == uid:
                    waited += float(e.get("seconds") or 0)   # every wait (however it ended) counts as the unit's time
                elif e.get("kind") == "critic" and e.get("unit") == uid:
                    gseq.append(str(e.get("grade")))
            except Exception:
                continue
        used = spent + waited
        line += (" THIS UNIT %s: used %.1fh of the wall (%d%%; %d calls %.1fh + waits %.1fh), critic grades so far: %s."
                 % (uid, used / 3600.0, 100.0 * used / max(1.0, WALL), att, spent / 3600.0, waited / 3600.0, ",".join(gseq) or "(none)"))
    return line


def _grant_line(grant):
    if not grant:
        return ""
    return ("RESOURCE GRANT: cores=%d mem=%d GB - what your unit may use on this node (other units run concurrently; OMP/MKL/OPENBLAS threads "
            "are set to %d; no CPU pinning, so keep to that many threads yourself). Size worker counts to %d and keep peak memory under %d GB.%s"
            % (grant["cores"], grant["mem_gb"], grant["cores"], grant["cores"], grant["mem_gb"],
               (" The node is busy (shared grant): expect less throughput and stay strictly under the memory figure.") if grant.get("shared") else ""))

_GPU_CACHE = {}


def gpu_facts():
    """The node's GPUs as [(name, mem_gb)] - RA3SR_GPU_FACTS overrides ("none" or "name, 40960 MiB; ..."), else nvidia-smi
    (20 s cap); [] when there is none. Cached for the episode."""
    if "v" in _GPU_CACHE:
        return _GPU_CACHE["v"]
    raw = os.environ.get("RA3SR_GPU_FACTS")
    if raw is None:
        try:
            raw = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20).stdout
        except Exception:
            raw = ""
    out = []
    for part in re.split(r"[;\n]", raw or ""):
        part = part.strip()
        if not part or part.lower() == "none":
            continue
        name, _, mem = part.partition(",")
        m = re.search(r"([\d.]+)\s*MiB", mem)
        out.append((name.strip(), int(round(float(m.group(1)) / 1024.0)) if m else None))
    _GPU_CACHE["v"] = out
    return out


def node_line():
    """One NODE line in every prompt header: cores, memory and whether the node has a GPU."""
    g = gpu_facts()
    if g:
        cnt = {}
        for k in g:
            cnt[k] = cnt.get(k, 0) + 1
        desc = ", ".join("%d x %s%s" % (c, n, (" (%d GB)" % m) if m else "") for (n, m), c in cnt.items())
        gl = ("GPUs: %s - visible to every unit and SHARED by the units running at the same time (check its free memory before a large "
              "allocation); when a unit's heavy step can run on a GPU, time both paths and use the faster." % desc)
    else:
        gl = "GPU: none (nvidia-smi finds no device) - plan and run on the CPU."
    return "NODE: %s cores, %s GB RAM; %s" % (NCPUS, MEM_GB, gl)


def _interrupt_kill(p, mode):
    """flush (mode "parent"): stop the dsh parent ONLY (a SIGTERM would make dsh stop its detached tool trees - a sweep writing checkpoints
    keeps writing); any other mode: the whole tree."""
    if mode != "parent":
        _dsh_kill_tree(p)
        return
    try:
        p.kill()
        p.wait(timeout=10)
    except Exception:
        pass

def interrupt_calls(pred, reason, mode="tree"):
    """Ask the watchdog of every live call matching pred(callinfo) to stop it (wrap-ups are never interrupted)."""
    with CALL_LOCK:
        tags = [tag for tag, c in list(ACTIVE_CALLS.items()) if not tag.endswith("_wrapup") and tag not in INTERRUPT_REQ and pred(c)]
        for tag in tags:
            INTERRUPT_REQ[tag] = (reason, mode)
    for t in tags:
        log("interrupt requested: %s (%s, %s)" % (t, reason, mode))
    return tags


def _assemble(prompt, tag, grant, resume_sid):
    """The header every role sees (clock, node, grant, task book, continuation) + the backend hints; rebuilt for every attempt."""
    body = prompt + ("" if VISION_OK else TEXT_ONLY_HINT) + DSH_EVIDENCE_HINT + (DSH_SESSION_HINT if _role_of(tag) == "exec" else "")
    hdr = [time_ledger(_tag_uid(tag)), node_line()]
    if grant:
        hdr.append(_grant_line(grant))
    cs = task_book()
    if cs:
        hdr.append(cs)
    if resume_sid:
        hdr.append(_dsh_continuation(resume_sid, tag))
    return "\n\n".join(hdr) + "\n\n" + body

def infra_dead(tag, reason):
    """The backend stays dead: the episode is an INFRA failure (the rerun policy re-issues it clean). Every thread winds down through the
    override; no report is attempted on a dead backend."""
    if INFRA["dead"]:
        return
    INFRA["dead"] = True
    if REPORT["started"] and os.path.exists(os.path.join(WS, "report.md")):   # a delivered report is never voided: calls stop, no marker
        ledger({"kind": "infra_after_report", "tag": tag, "reason": reason})
        log("backend dead after report.md was written (%s) - no further calls, the episode ends normally" % reason)
        return
    try:
        mk = os.path.join(WS, ".infra_failure")
        if not os.path.exists(mk):
            with open(mk, "w") as f:
                f.write("%s | tag=%s node=%s\n" % (reason, tag, os.uname().nodename))
    except Exception as e:
        log(".infra_failure write failed: %s" % e)
    ledger({"kind": "infra_failure", "tag": tag, "reason": reason})
    log("INFRA FAILURE: %s (last call %s) - the episode winds down for a clean rerun" % (reason, tag))
    try:
        fire_override("infra")
    except Exception as e:
        log("override on infra failure: %s" % e)

def run_call(prompt, timeout, tag, resume_sid=None, wrapup=None, grant=None, cwd=None, slice_ext=None, wrap_t=None, _attempt=0):
    """One role invocation (one dsh session). Returns dict(out, sid, tokens, rc, resumed, timed_out, interrupted, infra, t0, t1).
    rc: 0 turn ended, -1 time limit, -2 orchestrator error, -5 interrupted (flush / override) or refused (a stop signal), other = dsh exit code.
    wrapup: a pens-down prompt - on timeout (critic / planner / report) a fresh session writes the output file with it.
    grant: {cores, mem_gb[, gpu]} -> RESOURCE GRANT header line + thread environment.
    slice_ext: the unit id when `timeout` is a SLICE boundary - a productive unit gets SLICE_EXT_S more (up to SLICE_EXT_MAX times) unless its
    checkpoint is due or the usable wall is short.
    A failed call that produced nothing (0 tokens or an endpoint error, and for executors no file written) is INFRA: held and retried after
    INFRA_HOLDS; still dead, the backend is probed for up to INFRA_PROBE_S: it answers -> one more try (still dead: infra=True, the caller
    holds); it never answers -> the episode is an infra failure (a planner call: at once). A call may run into the flush window - the override
    interrupts it there."""
    if ABORT.is_set():   # the run is being stopped: no new session
        return {"out": "", "sid": None, "tokens": 0, "rc": -5, "resumed": bool(resume_sid), "timed_out": False, "interrupted": "stop", "infra": False,
                "t0": time.time(), "t1": time.time()}
    if OVERRIDE.is_set() and not REPORT["started"] and not tag.endswith("_wrapup"):   # the flush window starts nothing new
        ledger({"kind": "call_refused", "tag": tag, "why": "override"})
        return {"out": "", "sid": None, "tokens": 0, "rc": -5, "resumed": bool(resume_sid), "timed_out": False, "interrupted": "override", "infra": False,
                "t0": time.time(), "t1": time.time()}
    full = _assemble(prompt, tag, grant, resume_sid)
    cap = remaining() - report_reserve_s() - CLAMP_MARGIN_S   # the only clock clamp: never into the report reservation, never past the wall
    timeout = int(min(timeout, max(600, cap)))
    timeout = int(max(60, min(timeout, remaining() - 120)))
    with CALL_LOCK:
        CALLS["n"] += 1
        callno = CALLS["n"]
    t_start = time.time()
    os.makedirs(os.path.join(PD, "logs"), exist_ok=True)
    logf = os.path.join(PD, "logs", "%02d_%s.log" % (callno, tag))
    _dsh_setup()
    home = _dsh_call_home(callno, tag)
    patch = _critic_patch_path() if _role_of(tag) == "critic" else DSH_PATCH
    cmd = _dsh_argv_cmd(["dsh", "--profile", "headless", "--patch", patch, _argv_safe(full)], home, _argv_safe(full))
    log("dsh[%s]%s timeout=%ds%s" % (tag, " continuation(fresh session)" if resume_sid else "", timeout,
                                     (" grant=%dc/%dGB%s" % (grant["cores"], grant["mem_gb"], " shared" if grant.get("shared") else "")) if grant else ""))
    env = _dsh_env(home)
    env["PECE_CALL_TAG"] = _call_marker(tag)   # inherited by dsh's detached tool trees
    uid = _tag_uid(tag)
    if uid:
        env["PECE_UNIT"] = _unit_marker(uid)   # inherited by everything the session starts (the unit's leftovers)
    if grant:
        for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "JULIA_NUM_THREADS"):
            env[k] = str(grant["cores"])
        env["PECE_GRANT_CORES"], env["PECE_GRANT_MEM_GB"] = str(grant["cores"]), str(grant["mem_gb"])
    rc, intr, n_ext = None, None, 0
    with CALL_LOCK:
        ACTIVE_CALLS[tag] = {"tag": tag, "uid": uid, "role": _role_of(tag), "t0": t_start, "p": None}
        if OVERRIDE.is_set() and not REPORT["started"] and not tag.endswith("_wrapup"):   # the override fired while this call was assembled
            INTERRUPT_REQ[tag] = ("flush", "parent") if _role_of(tag) == "exec" else ("override", "tree")
    try:
        with open(logf, "wb") as lf:
            p = subprocess.Popen(cmd, cwd=(cwd if cwd and os.path.isdir(cwd) else WS), env=env, stdout=lf, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            p.pece_tag = tag
            if ABORT.is_set():   # the stop began while this session started (its sweep may be over): the session ends at once
                try:
                    os.killpg(p.pid, 9)
                except Exception:
                    pass
            UNIT_PIDS.setdefault(uid or "-", []).append(p.pid)
            with CALL_LOCK:
                ACTIVE_CALLS[tag]["p"] = p
            try:
                pgid = os.getpgid(p.pid)
            except Exception:
                pgid = None
            deadline = t_start + timeout
            cpu_prev, cpu_prev_t = _call_cpu(pgid, tag), time.time()
            while True:
                try:
                    rc = p.wait(timeout=POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                t_now = time.time()
                with CALL_LOCK:
                    intr = INTERRUPT_REQ.pop(tag, None)
                if intr:
                    _interrupt_kill(p, intr[1]); rc = -5
                    with open(logf, "a") as f:
                        f.write("\n<<ORCH INTERRUPT %s (%s): session stopped for wrap-up>>" % intr)
                    break
                if slice_ext and t_now - cpu_prev_t >= SLICE_CPU_TICK_S:
                    cpu_prev, cpu_prev_t = _call_cpu(pgid, tag), t_now
                if t_now > deadline:
                    if slice_ext:   # a slice boundary is a look, not a cut: a productive unit gets more time while the wall allows
                        why = _slice_progress(slice_ext, t_now, _call_cpu(pgid, tag) - cpu_prev, n_ext)
                        due = checkpoint_due(slice_ext)
                        if why and n_ext < SLICE_EXT_MAX and _slice_room(t_now - t_start) and not due:
                            n_ext += 1
                            deadline += SLICE_EXT_S
                            ledger({"kind": "slice_extended", "unit": slice_ext, "tag": tag, "n": n_ext, "ext_s": SLICE_EXT_S, "dur": int(t_now - t_start), "why": why})
                            log("%s: slice boundary at %d min, unit productive (%s) -> extended by %d min (%d/%d)"
                                % (slice_ext, int(t_now - t_start) // 60, why, SLICE_EXT_S // 60, n_ext, SLICE_EXT_MAX))
                            continue
                        if why:
                            log("%s: slice boundary at %d min, unit productive (%s) but %s -> cut" % (slice_ext, int(t_now - t_start) // 60, why,
                                ("its checkpoint is due (%s)" % due) if due else "no room / extensions used up"))
                    _dsh_kill_tree(p); rc = -1
                    with open(logf, "a") as f:
                        f.write("\n<<ORCH TIMEOUT>>")
                    break
    except Exception as e:
        rc = -2
        try:
            _dsh_kill_tree(p)
        except Exception:
            pass
        with open(logf, "a") as f:
            f.write("\n<<ORCH ERROR %s>>" % e)
    finally:
        with CALL_LOCK:
            ACTIVE_CALLS.pop(tag, None)
            INTERRUPT_REQ.pop(tag, None)
    try:   # the call is over: a session with no process left is forgotten at once (its id could be recycled)
        if not _session_alive(p.pid):
            ids = UNIT_PIDS.get(uid or "-")
            if ids is not None and p.pid in ids:
                ids.remove(p.pid)
    except Exception:   # also: no session was started
        pass
    out = read(logf)
    sid = None
    try:
        sid = _dsh_session_of(home)
    except Exception:
        pass
    tokens, dsh_u = 0, None
    try:
        dsh_u = _dsh_usage_detail(home)
        tokens = dsh_u["inputTokens"] + dsh_u.get("cacheReadTokens", 0) + dsh_u["outputTokens"]   # dsh 0.1.5: input is the uncached part, output holds the reasoning
    except Exception:
        pass
    used_file = os.path.isfile(os.path.join(home or "", "prompt.txt"))
    if used_file and (rc == 97 or "DSH_ARGV_WRAPPER_FAILED" in (out or "")[-4000:]) and not DSH_ARGV["off"] and not DSH_ARGV.get("ok"):   # the prompt-file launcher failed -> argv
        DSH_ARGV["off"] = True
        ledger({"kind": "dsh_argv_fallback", "tag": tag, "rc": rc, "error": (out or "")[-300:]})
        log("dsh prompt-file launcher failed on %s (rc=%s, no tokens) -> prompts go on the command line for this episode" % (tag, rc))
        return run_call(prompt, timeout, tag, resume_sid, wrapup=wrapup, grant=grant, cwd=cwd, slice_ext=slice_ext, wrap_t=wrap_t, _attempt=_attempt)
    if used_file and rc == 0 and tokens:
        DSH_ARGV["ok"] = True
    t_end = time.time()
    if sid:
        with CALL_LOCK:
            DSH_SESSIONS[sid] = {"home": home, "tag": tag, "uid": uid, "t0": t_start, "t1": t_end, "rc": rc, "ended": _dsh_ended(rc, intr)}
    ev = {"kind": "call", "tag": tag, "dur": int(t_end - t_start), "tokens": tokens, "rc": rc, "resumed": bool(resume_sid),
          "interrupted": (intr[0] if intr else None)}
    if n_ext:
        ev["slice_ext"] = n_ext
    if resume_sid:
        ev["resume_mode"] = "fresh"
    if dsh_u is not None:
        ev["retried_attempts"] = dsh_u["retried_attempts"]
        ev["usage"] = {"input": dsh_u["inputTokens"], "cached": dsh_u.get("cacheReadTokens", 0), "output": dsh_u["outputTokens"],
                       "reasoning": dsh_u["reasoningTokens"]}
        ev["cost_usd"] = round((dsh_u["inputTokens"] * RATES[0] + dsh_u.get("cacheReadTokens", 0) * RATES[1] + dsh_u["outputTokens"] * RATES[2]) / 1e6, 4)
    ledger(ev)
    res = {"out": out, "sid": sid, "tokens": tokens, "rc": rc, "resumed": bool(resume_sid), "timed_out": rc == -1,
           "interrupted": (intr[0] if intr else None), "infra": False, "t0": t_start, "t1": t_end}
    # timeout -> pens-down: a fresh session writes the role's output file (critic, planner, report, scouts; executors hand off at slices)
    if rc == -1 and wrapup and sid and not tag.endswith("_wrapup") and remaining() > WRAP_MIN_S:
        log("timeout on %s -> pens-down wrap-up" % tag)
        w = run_call(wrapup, slot(wrap_t or WRAPUP_T, floor=120), tag + "_wrapup", resume_sid=sid, cwd=cwd)
        res.update({"out": out + "\n" + w["out"], "tokens": tokens + w["tokens"], "rc": w["rc"], "resumed": True, "timed_out": True})
        return res
    # interrupted (flush / override) -> the reason picks the pens-down prompt and its window
    if rc == -5 and not tag.endswith("_wrapup"):
        reason = intr[0]
        ledger({"kind": "interrupt", "tag": tag, "unit": uid, "reason": reason, "mode": intr[1]})
        if reason == "flush" and _role_of(tag) == "exec":
            wp, wt = WRAP_FLUSH.format(uid=uid or "?"), FLUSH_WINDOW - 60
        else:
            wp, wt = wrapup, 600
        if wp and sid and remaining() > 240 and reason != "infra" and _role_of(tag) != "scout" and not tag.startswith("criticsel"):
            log("%s interrupted (%s) -> pens-down wrap-up (%ds)" % (tag, reason, wt))
            w = run_call(wp, wt, tag + "_wrapup", resume_sid=sid, cwd=cwd)
            res.update({"out": out + "\n" + w["out"], "tokens": tokens + w["tokens"], "rc": w["rc"], "resumed": True})
        return res
    # INFRA: a failed call that produced NOTHING (0 tokens) -> hold and retry the same prompt (fresh clock header), in report mode too; then probe
    try:   # while the unit's registered job lives, files under the unit are the job's writes, not the call's (as for slices)
        job_alive = bool(uid) and bool(_waiting_of(uid)) and _pid_alive(int(_waiting_of(uid)["pid"]))
    except Exception:
        job_alive = False
    wrote = bool(uid) and _role_of(tag) == "exec" and not job_alive and _dir_activity(unit_dir(uid), t_start) > t_start   # the call itself wrote files
    dead_call = (not tokens and not wrote and not INFRA["dead"]
                 and (rc not in (0, -1, -5) or (rc == 0 and _role_of(tag) == "exec")))   # an executor turn with no model use is not a turn
    if dead_call and (not OVERRIDE.is_set() or _role_of(tag) == "report"):
        if _attempt < len(INFRA_HOLDS):
            hold = INFRA_HOLDS[_attempt]
            if remaining() > hold + 300:
                ledger({"kind": "infra_retry", "tag": tag, "rc": rc, "n": _attempt + 1, "hold_s": hold})
                log("%s: dead call (rc=%s, 0 tokens) -> hold %d s, retry %d/%d" % (tag, rc, hold, _attempt + 1, len(INFRA_HOLDS)))
                if _role_of(tag) == "report":
                    time.sleep(hold)
                elif OVERRIDE.wait(hold):   # the override ends the hold: nothing is re-issued into the flush window
                    return res
                return run_call(prompt, timeout, re.sub(r"_gwretry\d*$", "", tag) + "_gwretry%d" % (_attempt + 1), resume_sid, wrapup=wrapup,
                                grant=grant, cwd=cwd, slice_ext=slice_ext, wrap_t=wrap_t, _attempt=_attempt + 1)
        if _attempt == len(INFRA_HOLDS):   # the retries are spent: probe the backend before any verdict on the episode
            t_p = time.time()
            alive = gw_ok()
            while not alive and time.time() - t_p < INFRA_PROBE_S and remaining() > 900 and not INFRA["dead"] and not OVERRIDE.is_set():
                time.sleep(min(300, max(1, INFRA_PROBE_S // 12)))
                alive = gw_ok()
            ledger({"kind": "infra_probe", "tag": tag, "alive": alive, "probed_s": int(time.time() - t_p)})
            if not alive and not OVERRIDE.is_set():
                infra_dead(tag, "backend unreachable: %s dead after %d retries and %d s of gateway probes" % (tag, len(INFRA_HOLDS), int(time.time() - t_p)))
            elif alive and remaining() > 900 and not OVERRIDE.is_set():
                return run_call(prompt, timeout, re.sub(r"_gwretry\d*$", "", tag) + "_gwretry%d" % (_attempt + 1), resume_sid, wrapup=wrapup,
                                grant=grant, cwd=cwd, slice_ext=slice_ext, wrap_t=wrap_t, _attempt=_attempt + 1)
        res["infra"] = True
        ledger({"kind": "infra_call_dead", "tag": tag, "rc": rc})
        if tag.split("_")[0] == "plan" and not INFRA["dead"]:   # the FIRST plan only: a dead replan leaves its branch blocked
            infra_dead(tag, "the planner's calls stay dead (0 tokens) while the gateway answers probes")
    return res

def slot(want, floor=300):
    """Clamp a call slot to the wall actually left (minus a 900 s epilogue margin)."""
    return int(max(floor, min(want, remaining() - 900)))
