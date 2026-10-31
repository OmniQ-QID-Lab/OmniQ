
# ---------------------------------------------------------------- the report agent (the render unit)
def unit_status_table():
    with STATE_LOCK:
        plan = list(RUN["plan"])
        grades = dict(RUN["grades"])
    L = ["UNIT STATUS TABLE (orchestrator truth; id | type | status | critic grade | directory | notes):"]
    for u in plan:
        if not isinstance(u, dict):
            continue
        uid = u.get("id")
        st = u.get("status") or "not started"
        d = "/workspace/%s/" % uid
        notes = []
        if st == "blocked":
            notes.append("blocked: upstream %s retired - never ran, no data" % u.get("blocked_by"))
        elif st == "failed":
            notes.append("retired: its data were not verified by an audit - usable only as the rules say (plotted as unaudited, listed under LIMITATIONS); residual.md in its directory")
        elif st == "flushed":
            notes.append("flushed at the override (partial, unaudited)")
        elif st == "running":
            notes.append("still running")
        if st == "done" and u.get("gap"):
            notes.append("DONE WITH GAP: %s; unmet: %s - plot its data as it is and state the miss; never fill, rescale or calibrate it"
                         % (("its last look (a checkpoint) found a defect in its current data - its critic_report.md says which; plot it marked unaudited"
                             if u.get("settle_reason") == "gap_unaudited_checkpoint" else "its provenance was NOT verified by an audit - plot it marked unaudited")
                            if u.get("gap_unaudited") else "provenance verified",
                            str(u.get("gap_note", ""))[:300]))
        b = u.get("_best") or {}
        if b.get("dir") and (st != "done" or u.get("gap")):
            if b["dir"] != "/workspace/%s" % uid:
                notes.append("BEST VALIDATED ROUND: %s/ (grade %s, G1 and G2 passed) - prefer its data where the live directory is incomplete or unaudited"
                             % (b["dir"], b.get("grade")))
            else:
                notes.append("LAST VALIDATED STATE: this directory passed G1 and G2 at its audit of %s (grade %s); files written after that audit are unaudited"
                             % (time.strftime("%H:%M", time.localtime(b.get("t") or 0)), b.get("grade")))
        if st == "flushed" and u.get("flush_note"):
            notes.append(u["flush_note"][:400])
        if u.get("restarts"):
            notes.append("restarted %d time(s); earlier rounds: %s" % (u["restarts"], ", ".join(u.get("archives") or [])))
        rows = owned_rows(uid)
        if rows:
            notes.append("owns required rows %s" % ", ".join(str(r.get("id")) for r in rows))
        try:
            mj = json.load(open(os.path.join(_ws_path(d), "mech.json")))
            notes.append("mech %d/%d deliverables OK%s" % (mj.get("n_deliv_ok", 0), mj.get("n_deliv", 0), (", stopped: %s" % str(mj.get("stopped"))[:120]) if mj.get("stopped") else ""))
        except Exception:
            pass
        for fn in ("handoff.json", "profile.json"):
            if os.path.exists(os.path.join(_ws_path(d), fn)):
                notes.append(fn)
        if str(u.get("settle_reason") or "").startswith("reopen") and u.get("settle_reason") != "reaudit_A":
            notes.append("REOPENED after an input change and not re-verified (%s) - its critic_report.md says what the change affects" % u["settle_reason"])
        L.append("  %s | %s | %s | %s | %s | %s" % (uid, u.get("type"), st, grades.get(uid, "-"), d, "; ".join(notes) or "-"))
    conf = RUN.get("reading_conflicts") or []
    if conf:
        L.append("READING CONFLICTS (a second change of an already-changed reading was refused - list them under CONFLICTS): "
                 + "; ".join("%s -> %s (evidence %s, by %s)" % (c.get("id"), c.get("reading"), c.get("evidence"), c.get("by")) for c in conf[:8]))
    return "\n".join(L)

def report_prompt(rid, mode, mode_note):
    with STATE_LOCK:
        done = [u.get("id") for u in RUN["plan"] if isinstance(u, dict) and u.get("status") == "done" and u.get("id") != rid]
        other = [u for u in RUN["plan"] if isinstance(u, dict) and u.get("status") in ("flushed", "failed") and u.get("type") != "render"]
    src = ["SOURCES - done units (verified; DONE WITH GAP units state their unmet rows):"]
    src += ["  %s -> /workspace/%s/ : read /workspace/%s/handoff.json first (%s)" % (d, d, d, _handoff_summary(d, 300)) for d in done] or ["  (none)"]
    if other:
        src.append("FLUSHED / RETIRED units (unaudited - usable only as the rules say):")
        src += ["  %s (%s) -> /workspace/%s/ (handoff.json %s)" % (u["id"], u.get("status"), u["id"], "present" if os.path.exists(os.path.join(unit_dir(u["id"]), "handoff.json")) else "missing") for u in other]
    return REPORT_PROMPT.format(mode=mode, uid=rid, mode_note=mode_note, status_table=unit_status_table(), sources="\n".join(src)) + bank_report_block()

def _report_t():
    """The report call leaves about 27 min before the wall, so a timeout still gets a real pens-down session."""
    want = max(FIN_T, report_cost_min() * 60 + 600)
    return int(max(600, min(want, remaining() - 1620)))

def root_copy(rid):
    """The final files are collected at /workspace/ root: copy report.md and reproduced_figure* from the render directory when
    the agent left them only there, or left a newer version there."""
    rd, n = unit_dir(rid), []
    for fn in sorted(os.listdir(rd)) if os.path.isdir(rd) else []:
        if fn == "report.md" or (fn.startswith("reproduced_figure") and fn.endswith((".png", ".pdf"))):
            src, dst = os.path.join(rd, fn), os.path.join(WS, fn)
            try:
                if os.path.isfile(src) and (not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst) + 1):
                    shutil.copy2(src, dst)
                    n.append(fn)
            except OSError as e:
                log("root copy of %s failed: %s" % (fn, e))
    if n:
        ledger({"kind": "root_copy", "unit": rid, "files": n})
        log("report agent left %s only in /workspace/%s/ -> copied to /workspace/" % (", ".join(n), rid))
    return n


def _render_critic(rid):
    """The render unit's audit - it runs after the override too (the report step is past it)."""
    return run_critic(rid, force=True) or run_critic(rid, force=True)

def run_report(mode):
    """The report agent executes the render unit in /workspace/render/, then its audit (normal and override mode) while the wall allows it: at
    most 3 verdict rounds, B fixed in place, C redone while the wall allows."""
    global POOL
    if POOL is None:   # the last-resort report after an early failure
        POOL = ResourcePool(NCPUS, MEM_GB)
    with STATE_LOCK:
        rend = next((u for u in RUN["plan"] if isinstance(u, dict) and u.get("type") == "render"), None)
        REPORT["started"], REPORT["reason"], REPORT["t"] = True, mode, int(time.time())
        rid = rend["id"] if rend else "render"
        rd = unit_dir(rid)
        os.makedirs(rd, exist_ok=True)
        if rend:
            rend["status"], rend["t_launch"] = "running", time.time()
            write_spec(rend)
    ledger({"kind": "report_start", "mode": mode, "unit": rid, "dir": "/workspace/%s/" % rid, "remaining_s": int(remaining()),
            "report_cost_min": report_cost_min(), "grades": dict(RUN["grades"]), "failed": list(RUN["failed"]),
            "blocked": [u.get("id") for u in RUN["plan"] if isinstance(u, dict) and u.get("status") == "blocked"],
            "flushed": [u.get("id") for u in RUN["plan"] if isinstance(u, dict) and u.get("status") == "flushed"]})
    log("REPORT AGENT (%s) on %s: remaining %dm" % (mode, rid, remaining() // 60))
    checkpoint()
    grant = (POOL.acquire(rid, rend, first=False) if rend else None) or POOL.acquire_free(rid, want=2)
    mode_note = REPORT_MODE_OVERRIDE if mode == "override" else REPORT_MODE_NORMAL
    t_r0 = time.time()
    try:
        r = run_call(report_prompt(rid, mode.upper(), mode_note), _report_t(), "report_%s" % rid, wrapup=WRAP_FIN, grant=grant, cwd=rd)
    finally:
        POOL.release(rid)
    sid = r["sid"]
    root_copy(rid)
    mech = mech_check(rid, rend, t_r0) if rend else None
    ledger({"kind": "report", "mode": mode, "unit": rid, "tokens": r["tokens"], "rc": r["rc"], "ok": deliverables_ok(), "handoff": os.path.exists(os.path.join(rd, "handoff.json"))})
    r2 = None
    if not os.path.exists(os.path.join(WS, "report.md")) and remaining() > 420 and not INFRA["dead"]:
        log("report.md still missing after the report agent -> pointed report-only retry")
        r2 = run_call("Write ONLY /workspace/report.md now, using the figures and data ALREADY on disk under /workspace: the task-book items F1..Fn one "
                      "by one (met / partial / missing, evidence file), the method actually implemented, the parameters used, the results compared to "
                      "the paper's target values, and an honest LIMITATIONS section. Do NOT compute anything and do NOT touch any other file. Then stop.",
                      max(240, min(1800, int(remaining() - 360))), "report_%s_only" % rid, resume_sid=sid, wrapup=WRAP_FIN, cwd=rd)
        root_copy(rid)
        ledger({"kind": "finalize_report", "tokens": r2["tokens"], "rc": r2["rc"], "ok": os.path.exists(os.path.join(WS, "report.md"))})
    if not os.path.exists(os.path.join(WS, "report.md")) and not INFRA["dead"]:
        if (r2 if r2 is not None else r).get("infra"):   # the last attempt decides: a backend that answered the retry is alive
            infra_dead("report_%s" % rid, "the report calls stay dead (0 tokens) in the report window")
        else:
            write_fallback_report(rid)
    g = None
    if rend and not INFRA["dead"] and remaining() > 1500:   # the render audit (normal and override mode alike) while the wall allows it
        rounds = 0
        while remaining() > 1500:
            if mech and not mech["all_ok"]:
                g = machine_critic(rid, mech)
            else:
                g = _render_critic(rid)
            fp = parse_fix_plan(rid) or {}
            est = float(fp.get("est_min") or 0)
            left = remaining() - 900
            action = ("done" if g == "A" else "keep" if (g in (None, "D") or rounds >= 3) else
                      "fix" if (g == "B" and left >= max(est * 60, 1200)) else "restart" if (g == "C" and left >= max(est * 60, 2700)) else "keep")
            ledger({"kind": "report_grade", "round": rounds, "grade": g, "action": action, "est_min": fp.get("est_min"), "remaining_s": int(remaining())})
            if action in ("done", "keep"):
                break
            rounds += 1
            grant = POOL.acquire(rid, rend, first=False) or POOL.acquire_free(rid, want=2)
            t_r0 = time.time()
            try:
                if action == "fix":
                    rf = run_call(FIX_PROMPT.format(uid=rid, grade=g, fixes=extract_fixes(rid)) + bank_round_block(rid), min(int(max(1200, 2 * est * 60)), FIN_T),
                                  "fix_%s" % rid, resume_sid=sid, wrapup=WRAP_FIN, grant=grant, cwd=rd)
                    sid = rf["sid"] or sid
                else:
                    rs = restart_unit(rend, rounds, g)
                    rf = run_call(report_prompt(rid, "NORMAL (RESTART after critic grade %s)" % g, REPORT_MODE_NORMAL + " The previous render (archived at %s/) "
                                                "was graded %s: read %s/residual.md and critic_report.md there first and fix every verified defect." % (rs["archive"], g, rs["archive"])),
                                  _report_t(), "restart_%s" % rid, wrapup=WRAP_FIN, grant=grant, cwd=rd)
                    sid = rf["sid"]
            finally:
                POOL.release(rid)
            root_copy(rid)
            mech = mech_check(rid, rend, t_r0)
        with STATE_LOCK:
            RUN["grades"][rid] = g or "U"
    elif rend:
        with STATE_LOCK:
            RUN["grades"].setdefault(rid, "U")
    with STATE_LOCK:
        if rend:
            rend["status"] = "done" if RUN["grades"].get(rid) in ("A", "B") else "failed"
    checkpoint()
    return g

def write_fallback_report(rid):
    """The report agent did not deliver and the backend answers: /workspace/report.md from the orchestrator's own records (said so first), so
    the episode never ends without a report."""
    figs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(WS, "reproduced_figure*")))
    L = ["# Report (written by the orchestrator: the report agent did not deliver report.md)", "",
         "The figures and data below are what the run's units left on disk; nothing here was checked by the report agent.", "",
         "## Figures", ""] + (["- /workspace/%s" % f for f in figs] or ["- none rendered"]) + ["", "## Units", "", unit_status_table(), ""]
    for u in RUN["plan"]:
        if not isinstance(u, dict) or u.get("type") == "render":
            continue
        try:
            h = json.load(open(os.path.join(unit_dir(u["id"]), "handoff.json")))
        except Exception:
            continue
        if not isinstance(h, dict):
            continue
        L += ["### %s (%s)" % (u["id"], u.get("status")), "key numbers: %s" % json.dumps(h.get("key_numbers"), ensure_ascii=False)[:1500],
              "known limits: %s" % json.dumps(h.get("known_limits"), ensure_ascii=False)[:800], ""]
    try:
        with open(os.path.join(WS, "report.md"), "w") as f:
            f.write("\n".join(L) + "\n")
        ledger({"kind": "fallback_report", "unit": rid, "figures": figs})
        log("report.md written by the orchestrator (the report agent did not deliver)")
    except Exception as e:
        log("fallback report failed: %s" % e)

def build_digest():
    """Deterministic workflow summary: call timeline, billing, grade sequences, time by role, notable events."""
    events = []
    for line in read(os.path.join(PD, "ledger.jsonl")).splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        if isinstance(e, dict):   # the ledger lies in /workspace: a line an agent appended that is not an object is skipped
            events.append(e)
    calls = [e for e in events if e.get("kind") == "call"]
    L = ["# Episode digest - %s (paper %s)" % (RUN_ID, PAPER), "",
         "wall=%ds spent=%ds deliverables=%s calls=%d" %
         (WALL, int(time.time() - T0), deliverables_ok(), len(calls)), "",
         "## Call timeline (# | tag | minutes | tokens | rc)"]
    role_min = {}
    for i, c in enumerate(calls, 1):
        mins = c.get("dur", 0) / 60.0
        role = re.split(r"[_\d]", c["tag"], 1)[0]
        role_min[role] = role_min.get(role, 0) + mins
        L.append("%02d | %-24s | %5.1f | %8d | rc=%s%s" %
                 (i, c["tag"], mins, c.get("tokens") or 0, c.get("rc"), "  <-WRAPUP" if c["tag"].endswith("_wrapup") else ""))
    bl = [c for c in calls if c.get("usage")]
    if bl:
        tot = dict((k, sum((c["usage"] or {}).get(k, 0) for c in bl)) for k in ("input", "cached", "output", "reasoning"))
        L += ["", "## Usage (dsh session usage x RA3SR_RATES $/Mtok uncached %.3g / cached %.3g / output %.3g)" % RATES,
              "calls=%d uncached in=%d cached in=%d out=%d (reasoning inside: %d) cost=$%.2f"
              % (len(bl), tot["input"], tot["cached"], tot["output"], tot["reasoning"], sum(c.get("cost_usd") or 0 for c in bl)),
              "top calls by cost: " + ", ".join("%s $%.2f" % (c["tag"], c.get("cost_usd") or 0)
                                                 for c in sorted(bl, key=lambda c: -(c.get("cost_usd") or 0))[:5])]
    L += ["", "## Time by role (minutes)"] + ["%-10s %6.1f" % (k, v) for k, v in sorted(role_min.items(), key=lambda x: -x[1])]
    L += ["", "## Unit grade sequence"] + ["%s -> %s (%s%s)" % (e.get("unit"), e.get("grade"), e.get("by", "llm"), ", residual" if e.get("residual") else "")
                                          for e in events if e.get("kind") == "critic"]
    nk = ("finalize_report", "plan", "plan_invalid", "plan_minimal", "replan_trigger", "replan_applied", "replan_failed", "replan_cap", "d_rejected", "archive",
          "restart", "settle", "writes_outside_own_dir", "fix_plan", "override", "flush", "interrupt", "launch", "report", "report_start", "root_copy",
          "pool_shared", "infra_retry", "infra_call_dead", "infra_unit_dead", "infra_failure", "critic_skipped", "leftover_killed", "settle_reaped",
          "wait_start", "wait_end", "wait_check", "checkpoint", "scout_winner", "scout_fallback", "reading_change", "upstream_changed", "reopen",
          "reopen_skipped", "unit_gap", "critic_regrade", "requeue_refused")
    rs = [e for e in events if e.get("kind") == "report_start"]
    if rs:
        idle = float(rs[-1].get("remaining_s") or 0)
        L += ["", "idle wall at report start: %.1fh (%d%% of the %.1fh wall)" % (idle / 3600.0, int(100 * idle / max(WALL, 1)), WALL / 3600.0)]
    notable = [e for e in events if e.get("kind") in nk]
    if notable:
        L += ["", "## Notable events"] + ["%s %s %s" % (e["kind"], e.get("unit") or "", e.get("reason") or e.get("status") or e.get("tag") or e.get("dir") or e.get("units", ""))
                                        for e in notable]
    txt = "\n".join(L) + "\n"
    open(os.path.join(PD, "digest.md"), "w").write(txt)
    return txt


def deliverables_ok():
    """All figure files CLAUDE.md names explicitly + report.md, at /workspace/ root (where the pipeline collects them)."""
    required = set(re.findall(r"reproduced_figure[A-Za-z0-9_\-]*\.png", read(os.path.join(WS, "CLAUDE.md"))))
    figs_ok = all(os.path.exists(os.path.join(WS, f)) for f in required) if required else \
        any(f.startswith("reproduced_figure") and f.endswith(".png") for f in os.listdir(WS))
    return figs_ok and os.path.exists(os.path.join(WS, "report.md"))


def build_trace():
    """trace.py next to this file renders _pece/trace.json, timeline.html and timeline.txt from the ledger."""
    tp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trace.py")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ra_trace", tp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = mod.build(PD, plan_path=os.path.join(PD, "plan.json"))
        log("trace written: %s" % ", ".join(os.path.basename(x) for x in out))
        return out
    except Exception as e:
        log("trace generation failed (%s): %s" % (tp, e))
        return []

def epilogue():
    """token_usage.json, digest.md, the episode record in the store, ledger episode_end, trace."""
    with STATE_LOCK:
        grades, failed = dict(RUN["grades"]), list(RUN["failed"])
    total, cost = 0, 0.0
    bt = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    for line in read(os.path.join(PD, "ledger.jsonl")).splitlines():
        try:   # a ledger line an agent appended (not an object, odd types) is skipped
            e = json.loads(line)
            if isinstance(e, dict) and e.get("kind") == "call":
                n_tok, n_cost = int(e.get("tokens") or 0), float(e.get("cost_usd") or 0)
                us = dict((k, int((e.get("usage") or {}).get(k, 0) or 0)) for k in bt)
                total, cost = total + n_tok, cost + n_cost
                for k in bt:
                    bt[k] += us[k]
        except Exception:
            continue
    try:
        json.dump({"agent": "pece-dsh", "harness": "ra3_%s" % ORCH_VERSION, "model": MODEL, "total_tokens": total, "billed_input_tokens": bt["input"],
                   "billed_cached_tokens": bt["cached"], "billed_output_tokens": bt["output"], "billed_reasoning_tokens": bt["reasoning"],
                   "cost_usd": round(cost, 4), "rates_per_mtok": {"uncached_input": RATES[0], "cached_input": RATES[1], "output": RATES[2]},
                   "cost_note": "estimated: per-call dsh session usage x RA3SR_RATES (settle from .dsh_ra3 sessions for the billed figure)"},
                  open(os.path.join(WS, "token_usage.json"), "w"), indent=2)
    except Exception as e:
        log("token_usage.json write failed: %s" % e)
    ed = os.path.join(STORE, "episodes", RUN_ID)
    try:   # the digest in its own step: a ledger line it cannot read never costs the episode record below
        build_digest()
    except Exception as e:
        log("digest failed: %s" % e)
    try:
        os.makedirs(os.path.join(ed, "critic"), exist_ok=True)
        if os.path.exists(os.path.join(PD, "digest.md")):
            shutil.copy(os.path.join(PD, "digest.md"), ed)
        json.dump({"paper": PAPER, "workdir": os.environ.get("HOST_WORK_DIR", ""), "grades": grades, "failed": failed, "ts": int(time.time()),
                   "override": OVERRIDE.is_set(), "report": REPORT, "infra_dead": INFRA["dead"]}, open(os.path.join(ed, "meta.json"), "w"), indent=2)
        for uid in grades:
            p = os.path.join(unit_dir(uid), "critic_report.md")
            if os.path.exists(p):
                shutil.copy(p, os.path.join(ed, "critic", "%s_critic_report.md" % uid))
    except Exception as e:
        log("episode record failed: %s" % e)
    ledger({"kind": "episode_end", "grades": grades, "failed": failed, "deliverables": deliverables_ok(), "calls": CALLS["n"], "override": OVERRIDE.is_set(),
            "report_mode": REPORT.get("reason"), "cost_usd": round(cost, 4), "infra_dead": INFRA["dead"]})
    log("episode done: grades=%s deliverables=%s calls=%d override=%s cost=$%.2f" % (grades, deliverables_ok(), CALLS["n"], OVERRIDE.is_set(), cost))
    build_trace()

# ---------------------------------------------------------------- main
def _requeued():
    """A scheduler requeue restarts the job in the SAME workspace: an episode that already started here is never run twice."""
    try:
        for line in read(os.path.join(PD, "ledger.jsonl")).splitlines():
            if '"episode_start"' in line and json.loads(line).get("kind") == "episode_start":
                return True
    except Exception:
        pass
    return False

def _on_stop(signum, frame=None):
    """Handler of SIGTERM, SIGINT and SIGHUP. No new dsh session starts (ABORT; STOP and OVERRIDE wind the threads down), every
    process of the run is killed (_sweep_run: twice, half a second apart, and a third time when the second still found one), the
    ledger records orchestrator_stopped and the process exits with 128 + signum. No report is written. A second signal during the
    stop is ignored. Every write is guarded: the handler runs in the main thread between two bytecodes."""
    if ABORT.is_set():
        return
    ABORT.set()
    STOP.set()
    OVERRIDE.set()
    swept = []
    for i in range(3):
        if i:
            time.sleep(0.5)
        try:
            swept.append(_sweep_run())
        except Exception:
            swept.append(0)
        if i and not swept[-1]:
            break
    try:
        name = signal.Signals(signum).name
    except Exception:
        name = str(signum)
    try:
        ledger({"kind": "orchestrator_stopped", "signal": name, "swept": swept, "calls": sorted(ACTIVE_CALLS)[:20]})
    except Exception:
        pass
    try:
        log("STOPPED by %s: %d process(es) of this run killed; no report is written" % (name, sum(swept)))
    except Exception:
        pass
    os._exit(128 + signum)

def _install_stop_handlers():
    """_on_stop for SIGTERM, SIGINT and SIGHUP, except for a signal the caller already ignores (under nohup SIGHUP stays ignored).
    Only the main thread can install handlers; elsewhere nothing is installed. Returns the names of the signals now handled."""
    done = []
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        try:
            if sig is None or signal.getsignal(sig) == signal.SIG_IGN:
                continue
            signal.signal(sig, _on_stop)
            done.append(name)
        except (ValueError, OSError):
            pass
    return done

def _main_body():
    global POOL
    if not os.path.isdir(NOTES_DIR):   # no memory folder: stop before any write (a folder that lacks some files is the ablation switch)
        log("CONFIGURATION ERROR: the memory folder %s does not exist. Set RA3SR_NOTES_DIR to a folder with planner_notes.md, "
            "critic_notes.md, residual_bank.md and libraries.md (the shipped ones are in residual_agents/memories/). Nothing was written."
            % NOTES_DIR)
        sys.exit(2)
    if not _shim_on() and not os.environ.get("DEEPSEEK_API_KEY", "").strip():   # direct mode without a key: stop before any write
        log("CONFIGURATION ERROR: direct mode (RA_DSH_SHIM is not 1) needs DEEPSEEK_API_KEY (DEEPSEEK_BASE_URL defaults to "
            "https://api.deepseek.com); set it, or run through the local proxy with RA_DSH_SHIM=1. Nothing was written.")
        sys.exit(2)
    _install_stop_handlers()   # a stop signal kills every process of the run (an ignored signal stays ignored)
    os.makedirs(PD, exist_ok=True)
    if _requeued():
        mk = os.path.join(WS, ".infra_failure")
        try:
            with open(mk, "a") as f:
                f.write("requeue: a second episode_start in the same workspace refused | node=%s\n" % os.uname().nodename)
        except Exception:
            pass
        ledger({"kind": "requeue_refused", "reason": "the ledger already holds an episode_start"})
        log("REQUEUE REFUSED: this workspace already ran an episode - exiting (infra policy: void + clean rerun)")
        INFRA["dead"] = True
        return "requeue"
    POOL = ResourcePool(NCPUS, MEM_GB)
    threading.Thread(target=rss_sampler, name="rss", daemon=True).start()
    log("RA %s episode %s paper %s wall=%ds model=%s ncpus=%d mem_gb=%s gpus=%s plan_only=%s report_min=%d"
        % (ORCH_VERSION, RUN_ID, PAPER, WALL, MODEL, NCPUS, MEM_GB, gpu_facts() or "none", PLAN_ONLY, REPORT_MIN))
    log("node: %d cores (%s), %s GB memory (%s)" % (NCPUS, NODE_SRC.get("ncpus", "?"), MEM_GB, NODE_SRC.get("mem_gb", "?")))
    # "epic_shim" (true = dsh used the local proxy): key name kept for compatibility with the published ledgers
    ev = {"kind": "episode_start", "wall": WALL, "model": MODEL, "harness": "ra3", "version": ORCH_VERSION, "orch": ORCH_VERSION, "orch_patch": ORCH_VERSION,
          "backend": BACKEND, "ncpus": NCPUS, "mem_gb": MEM_GB, "mask": len(_affinity()), "exec_slice_s": EXEC_SLICE_S, "cp_s": CP_S,
          "vision_ok": VISION_OK, "gpus": [list(x) for x in gpu_facts()], "dsh_model": _dsh_model(), "epic_shim": _shim_on(), "dsh_version": _dsh_version(),
          "smoke_force_c": SMOKE_FORCE_C, "min_attempt_s": MIN_ATTEMPT_S, "last_act_s": LAST_ACT_S, "scout_t": SCOUT_T, "scout_select_t": SCOUT_SELECT_T,
          "scout_need_s": SCOUT_NEED_S, "report_min": REPORT_MIN}
    ledger(ev)
    items = task_book_items()
    ledger({"kind": "task_book", "items": len(items), "sections": sorted(set(sec for _, sec, _ in items))[:12], "report_cost_min": report_cost_min()})
    load_notes()
    plan = get_plan()
    if INFRA["dead"]:
        return "infra"
    with STATE_LOCK:
        RUN["plan"] = plan or []
    how = "normal"
    if RUN["plan"]:
        checkpoint()
        ov = threading.Thread(target=override_thread, name="override", daemon=True)
        ov.start()
        how = schedule_loop()
        if how == "override":
            flush_wait()
    if INFRA["dead"]:
        return "infra"
    run_report(how)
    return how

def main():
    """Every exit path ends in a report (an episode without report.md scores 0) unless the backend is dead or the job was requeued - those
    are infra failures the rerun policy re-issues clean."""
    how = None
    try:
        how = _main_body()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        log("MAIN THREAD FAILED: %s\n%s" % (e, traceback.format_exc()[-2000:]))
        have = os.path.exists(os.path.join(WS, "report.md"))
        ledger({"kind": "main_exception", "error": str(e)[:300], "report_existed": have})
        if not have and not INFRA["dead"]:
            try:
                fire_override("main_exception")
                flush_wait()
            except Exception as e1:
                log("last-resort flush failed: %s" % e1)
            try:
                run_report("override")
            except Exception as e2:
                log("last-resort report failed: %s" % e2)
    STOP.set()
    if how != "requeue":
        try:
            epilogue()
        except Exception as e3:
            log("epilogue failed: %s" % e3)
    if os.path.exists(os.path.join(WS, ".infra_failure")):   # exit 3 = an infra failure the rerun policy re-issues (a delivered report is never one)
        sys.exit(3)

if __name__ == "__main__":
    main()
