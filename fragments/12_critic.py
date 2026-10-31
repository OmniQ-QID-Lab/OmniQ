
# ---------------------------------------------------------------- critic: full / incremental audits, the checkpoint, verdict parsing
REUSE_MAX_FILES, REUSE_MAX_BYTES = 400, 8 * 2 ** 30
REUSE_SKIP_NAMES = ("handoff.json", "critic_report.md", "residual.md", "mech.json", "spec.md", "profile.json")
REUSE_SKIP_DIRS = ("probes", "__pycache__", ".git", "node_modules", ".dsh_ra3", "site-packages", "dist-packages", ".venv", "venv")

def archive_audit(uid):
    """Move the previous round's critic_report.md (+ probes, residual.md) aside (audit_rN/ in the unit directory) before a re-audit."""
    ud = unit_dir(uid)
    if not os.path.isdir(ud) or not os.path.exists(os.path.join(ud, "critic_report.md")):
        return
    nums = [int(re.sub(r"\D", "", x) or 0) for x in os.listdir(ud) if x.startswith("audit_r")]
    n = 1 + (max(nums) if nums else 0)   # by number, not by count - a foreign audit_rN (copied from an archive by the executor) never collides
    ad = os.path.join(ud, "audit_r%d" % n)
    while os.path.exists(ad):
        n += 1
        ad = os.path.join(ud, "audit_r%d" % n)
    os.makedirs(ad, exist_ok=True)
    for f in ("critic_report.md", "residual.md") + tuple(x for x in os.listdir(ud) if x.startswith("probe_") and x.endswith(".txt")):
        try:
            shutil.move(os.path.join(ud, f), os.path.join(ad, f))
        except OSError:
            pass
    log("archived the previous audit of %s -> audit_r%d" % (uid, n))


def previous_audits_block(uid, limit=6):
    """The earlier audits of this unit (audit_rN/critic_report.md, newest last): grade, FIX LIST head, residual head - so a re-audit
    checks what was asked before instead of rediscovering or silently contradicting it."""
    ud = unit_dir(uid)
    try:
        dirs = sorted([x for x in os.listdir(ud) if x.startswith("audit_r")], key=lambda x: int(re.sub(r"\D", "", x) or 0))
    except Exception:
        dirs = []
    if not dirs:
        return "PREVIOUS AUDITS of this unit: none (first audit)."
    L = ["PREVIOUS AUDITS of this unit (newest last; the files are under /workspace/%s/audit_rN/). First check whether the previous FIX LIST was "
         "carried out; an item you re-raise after it was fixed needs re-verification; if you overturn an earlier ruling, say so explicitly. "
         "A probe from an earlier audit whose inputs are unchanged since (see 'changed since') may be CITED by its audit_rN/ path instead of "
         "re-run - re-run only what changed or what was not covered." % uid]
    for d in dirs[-limit:]:
        rep_ = read(os.path.join(ud, d, "critic_report.md")).strip()
        g = re.match(r"\s*GRADE:\s*(\w)", rep_)
        m = re.search(r"FIX LIST(.*?)(\nFIX_PLAN:|\n#|\Z)", rep_, re.S | re.I)
        fixes = re.sub(r"\s+", " ", m.group(1)).strip()[:600] if m else ""
        res = re.sub(r"\s+", " ", read(os.path.join(ud, d, "residual.md")).strip())[:400]
        try:   # the probes of that audit and whether the unit's files moved since - a cited probe needs unchanged inputs
            probes = sorted(x for x in os.listdir(os.path.join(ud, d)) if x.startswith("probe_") and x.endswith(".txt"))
        except Exception:
            probes = []
        at = _mtime(os.path.join(ud, d, "critic_report.md")) or 0
        ch = _changed_since(ud, at) if at else []
        L.append("- %s: GRADE %s%s%s; probes on disk: %s; unit files changed since: %s" % (
            d, g.group(1) if g else "?", ("; fix list: " + fixes) if fixes else "", ("; residual: " + res) if res else "",
            ", ".join(probes) if probes else "none", ("%d (%s%s)" % (len(ch), ", ".join(ch[:6]), " ..." if len(ch) > 6 else "")) if ch else "none - its probes still apply"))
    return "\n".join(L)


def _changed_since(root, since, limit=4000):
    """Files under root written after `since` (bounded scan; bookkeeping, probes and earlier audits excluded) - tells a re-audit
    whether an earlier probe's inputs moved."""
    out, seen, stack = [], 0, [root]
    while stack and seen < limit and len(out) < 50:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    nm = e.name
                    if nm.startswith("audit_r") or nm.startswith("probe_") or nm.startswith(".critic_wait") or nm in REUSE_SKIP_NAMES:
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except Exception:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if nm not in REUSE_SKIP_DIRS:
                            stack.append(e.path)
                        continue
                    if st.st_mtime > since:
                        out.append(os.path.relpath(e.path, root))
        except Exception:
            pass
    return sorted(out)


def _pool_facts(uid):
    """What this unit's grant costs the others - for the registration / feasibility rulings (a long job can hold cores that
    ready dependents need)."""
    try:
        g = (POOL.grants.get(uid) if POOL else None) or {}
        uc, um = POOL.used() if POOL else (0, 0)
        plan = RUN.get("plan") or []
        deps = [x.get("id") for x in plan if isinstance(x, dict) and uid in (x.get("depends_on") or []) and x.get("status") not in ("running",) + TERMINAL]
        blocked = [x.get("id") for x in plan if isinstance(x, dict) and x.get("ready_since") and x.get("status") not in ("running",) + TERMINAL]
        return ("; this unit holds %sc/%sGB of the node's %dc (%dc/%dGB granted in total); units waiting for it: %s; ready units blocked by the pool: %s"
                % (g.get("cores", "?"), g.get("mem_gb", "?"), NCPUS, uc, um, ", ".join(deps) or "none", ", ".join(blocked) or "none"))
    except Exception:
        return ""


def unit_activity_text(uid):
    try:
        lv = reap_leftovers(uid, dry=True)
    except Exception:
        lv = []
    try:
        age = time.time() - _dir_activity(unit_dir(uid), 0)
        age_s = "%d min ago" % (age // 60) if age < 1e9 else "unknown"
    except Exception:
        age_s = "unknown"
    return "%d live process(es) of this unit%s; newest file under /workspace/%s/ written %s" % (
        len(lv), (": " + "; ".join(c for _, _, c in lv[:4])[:300]) if lv else "", uid, age_s) + _pool_facts(uid)


def machine_critic(uid, mech, grade="B"):
    """The orchestrator's own report is a FALLBACK (override, or no critic answer twice) and it never grades C: the mechanical findings
    become a B FIX LIST for the executor's own session."""
    archive_audit(uid)
    steps = mech.get("fails") or ["mechanical check failed"]
    hint = ""
    txt = ("GRADE: %s\nGATES: G1=n/a G2=n/a G3=n/a\nMECHANICAL FINDINGS (written by the orchestrator from its "
           "machine check); nothing here is a judgement on the science.\nFIX LIST\n%s\n%s%s"
           "FIX_PLAN: %s\n" % (grade, "\n".join("- " + s for s in steps), hint, mech_notes(mech),
                                json.dumps({"est_min": 10, "steps": steps[:8]}, ensure_ascii=False)))
    try:
        open(os.path.join(unit_dir(uid), "critic_report.md"), "w").write(txt)
    except Exception as e:
        log("machine report write failed for %s: %s" % (uid, e))
    ledger({"kind": "critic", "unit": uid, "grade": grade, "by": "mech", "tokens": 0, "dur": 0,
            "fails": steps[:8], "gates": None, "fix_plan": {"est_min": 10}})
    log("%s: mechanical findings written by the orchestrator as a %s fix list" % (uid, grade))
    return grade


def parse_fix_plan(uid):
    """Last `FIX_PLAN: {...}` JSON line of critic_report.md -> dict (est_min float|None, steps) or None."""
    best = None
    for m in re.finditer(r"FIX_PLAN\s*:\s*(\{.*?\})\s*$", read(os.path.join(unit_dir(uid), "critic_report.md")), re.M):
        try:
            best = json.loads(m.group(1)) if isinstance(json.loads(m.group(1)), dict) else best
        except Exception:
            continue
    if best is None:
        return None
    try:
        v = float(best.get("est_min"))
        best["est_min"] = max(0.0, v) if math.isfinite(v) else None   # inf / NaN are no estimate
    except Exception:
        best["est_min"] = None
    return best


def parse_gates(uid):
    """{G1, G2, G3: pass|hit|None} from the mandatory line-2 `GATES:` of critic_report.md (information for the ledger)."""
    txt = read(os.path.join(unit_dir(uid), "critic_report.md"))
    res = {"G1": None, "G2": None, "G3": None}
    m = re.search(r"^GATES?\s*:\s*(.+)$", txt[:1500], re.M | re.I)
    if m:
        for g, v in re.findall(r"(G[123])\s*[=:]\s*([A-Za-z/]+)", m.group(1)):   # synonyms read, n/a / pending stay None
            v = v.lower()
            res[g.upper()] = "pass" if v in ("pass", "passed", "ok", "met") else ("hit" if v in ("hit", "fail", "failed", "miss", "missed", "partial", "violated") else None)
    return res


def parse_grade(uid):
    head = read(os.path.join(unit_dir(uid), "critic_report.md"))[:200]
    m = re.match(r"\s*GRADE:\s*([ABCDW])\s*(?:\r?\n|$)", head)   # W = a checkpoint's "on track"; a literal echo of "GRADE: A|B|C|D" does not parse
    return m.group(1) if m else None

def parse_reading_change(uid):
    """A report's reading change -> [(id, reading, evidence)] from its FIX_PLAN "reading_change". A change without an existing evidence file (the
    check that decides it) is not a change."""
    txt = read(os.path.join(unit_dir(uid), "critic_report.md"))
    out = []
    fp = _fix_plan_of(txt)
    rc = (fp or {}).get("reading_change")
    if isinstance(rc, dict):
        out.append((str(rc.get("id") or "").strip().upper(), str(rc.get("reading") or "").strip()[:600], str(rc.get("evidence") or "").strip()[:300]))
    good = []
    for k, rd, evd in out:
        if not re.match(r"^K\d+$", k) or not rd:
            continue
        if not _evidence_exists(uid, evd):
            ledger({"kind": "reading_change_rejected", "unit": uid, "id": k, "why": "no evidence file", "evidence": evd[:200]})
            continue
        good.append((k, rd, evd))
    return good

def _evidence_exists(uid, ev, base=None):
    """The evidence names a file that exists (absolute /workspace path, or relative to `base` / the unit directory / its probes)."""
    toks = re.findall(r"/workspace/[^\s,;'\")\]]+", ev or "") + re.findall(r"(?<![\w/])[\w][\w./-]*\.(?:txt|json|npz|npy|csv|log|md|dat|py|png)\b", ev or "")
    for t in toks:
        t = t.rstrip(".,;:")
        for cand in ([_ws_path(t)] if t.startswith("/") else [os.path.join(b, t) for b in ([base] if base else []) + [unit_dir(uid), os.path.join(unit_dir(uid), "probes"), WS]]):
            if os.path.exists(cand):
                return True
    return False

def _anchor_missed(txt):
    """Rows of the report's ANCHORS table whose verdict says missed (quoted in a gap note)."""
    m = re.search(r"\n\s*(?:#+\s*)?ANCHORS\b(.*?)(?=\n\s*(?:#+\s*)?(?:GRADES|GATE CHECK|CRITERIA|FIX LIST|EVIDENCE|WHAT IS WRONG|RESIDUAL)\b|\nFIX_PLAN:|\Z)", txt, re.S | re.I)
    if not m:
        return []
    out = []
    for l in m.group(1).splitlines():
        if l.count("|") < 3 or re.match(r"^\s*\|?\s*:?-{2,}", l) or re.search(r"\bquantity\b.*\bpaper\b", l, re.I):
            continue
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        verdict = " ".join(cells[4:]) if len(cells) >= 5 else cells[-1]
        if re.search(r"conflict", verdict, re.I):
            continue
        if re.search(r"\b(miss(ed)?|fail(ed|s)?|off|outside|beyond|violat\w*|revers\w*|absent|not met)\b", verdict, re.I):
            out.append(l.strip()[:200])
    return out

def _critic_format_retry(uid, csid, tag, what="report"):
    """A critic session that ended without a graded report gets one short pens-down call."""
    return run_call("You have not delivered a valid graded %s. Write /workspace/%s/critic_report.md NOW: line 1 exactly 'GRADE: A|B|C|D' (a checkpoint "
                    "may also write 'GRADE: W') on what you actually verified - B listing the unverified checks when time ran out, C only for a "
                    "verified defect (with residual.md); line 2 the GATES line; last line FIX_PLAN: {\"est_min\": N, \"steps\": [...], \"residual_ids\": [...], \"reading_change\": null}. Then stop." % (what, uid),
                    slot(900), tag, resume_sid=csid, cwd=unit_dir(uid))

def run_critic(uid, delta=False, notice="", why_delta="", force=False):
    """A full audit, or (delta) an incremental re-audit of a B fix / of a change notice. Returns the grade (A/B/C/D, W read as B) or None under
    the override. Every LLM verdict resets the unit's checkpoint clock."""
    ud = unit_dir(uid)
    if OVERRIDE.is_set() and not force:
        ledger({"kind": "critic_skipped", "unit": uid, "reason": "override"})
        return None
    u = unit_by_id(uid) or {"id": uid}
    fixes_prev = extract_fixes(uid)[:3000] if delta else ""
    at_prev = (_mtime(os.path.join(ud, "critic_report.md")) or 0) if delta else 0
    archive_audit(uid)
    changed, prev_dir = [], ""
    if delta:
        try:
            ds = sorted([x for x in os.listdir(ud) if x.startswith("audit_r")], key=lambda x: int(re.sub(r"\D", "", x) or 0))
            prev_dir = ds[-1] if ds else ""
        except Exception:
            prev_dir = ""
        changed = _changed_since(ud, at_prev) if at_prev else []
    t_c0 = time.time()
    notice = notice or change_notice(u)
    if delta:
        items = (notice or "(no change notice)") if why_delta else ((fixes_prev or "(the previous report had no FIX LIST)") + (("\n" + notice) if notice else ""))
        prompt = (CRITIC_DELTA_PROMPT.format(uid=uid, why=why_delta or "your previous audit graded it B and the executor applied the FIX LIST below.",
                                             prev_dir=prev_dir or "audit_r?", fixes=items,
                                             changed=((", ".join(changed[:40]) + (" ..." if len(changed) > 40 else "")) if changed else "none"),
                                             handoff=handoff_block([uid], title="HANDOFF OF THE AUDITED UNIT"), mech=mech_block(uid), tool_min=CRITIC_TOOL_S // 60)
                  + (CRITIC_RULES + CRITIC_CONTRACT).format(uid=uid) + notes_block("critic_notes", CRITIC_NOTES_TITLE, index=True))
        t_lim, tag = CRITIC_DELTA_T, "criticd_%s" % uid
        ledger({"kind": "critic_delta", "unit": uid, "prev": prev_dir, "changed": len(changed), "notice": bool(notice)})
    else:
        prompt = (CRITIC_PROMPT.format(uid=uid, handoff=handoff_block([uid], title="HANDOFF OF THE AUDITED UNIT"), mech=mech_block(uid),
                                       changes=notice, previous=previous_audits_block(uid), tool_min=CRITIC_TOOL_S // 60)
                  + notes_block("critic_notes", CRITIC_NOTES_TITLE, index=True))
        t_lim, tag = CRITIC_T, "critic_%s" % uid
    r = run_call(prompt, t_lim, tag, wrapup=WRAP_CRITIC.format(uid=uid), cwd=ud)
    csid, tokens = r["sid"], r["tokens"]
    grade = parse_grade(uid)
    if grade is None and (force or not OVERRIDE.is_set()) and not INFRA["dead"] and (csid or os.path.exists(os.path.join(ud, "critic_report.md"))):
        f = _critic_format_retry(uid, csid, "criticfmt_%s" % uid)
        tokens += f["tokens"]
        grade = parse_grade(uid)
    reap_critic(uid, tag, "criticfmt_%s" % uid)
    gates = parse_gates(uid) if grade else None
    if grade == "W":
        grade = "B"   # the audit follows a handoff; "still running" is a fix list ("finish and hand off")
    if grade == "B" and gates and gates.get("G1") == "hit":
        grade = "C"   # a fabricated or untraceable number is never a same-session patch
    rep_ = read(os.path.join(ud, "critic_report.md"))[:12000]
    if grade == "D" and not (re.search(r"\bF\d+\b", rep_) and re.search(r"\((a|b)\)", rep_)):
        ledger({"kind": "d_rejected", "unit": uid, "why": "no (a)/(b) ground with task-book item numbers"})
        grade = "C"
    if grade in ("C", "D"):
        ensure_residual(uid, "grade %s" % grade)
    fp = parse_fix_plan(uid) if grade else None
    ledger({"kind": "critic", "unit": uid, "grade": grade, "by": "delta" if delta else "llm", "tokens": tokens, "dur": int(time.time() - t_c0),
            "gates": gates, "delta": bool(delta), "residual": os.path.isfile(os.path.join(ud, "residual.md")),
            "fix_plan": ({"est_min": fp.get("est_min")} if fp else None), "residual_ids": bank_cited_ids(os.path.join(ud, "critic_report.md")),
            "override": OVERRIDE.is_set()})
    if grade and u.get("type") == "produce":
        LAST_LOOK[uid] = time.time()
        if u.get("inputs") is not None:
            record_inputs(u)   # the audit saw the change notice: the inputs as they are now are the new baseline
        for k, rd, evd in parse_reading_change(uid):
            apply_reading_change(k, rd, evd, uid, "critic")
    return grade

def run_checkpoint(uid, u, reason, job=None):
    """The one time check: STATE, RESULTS SO FAR, PACE, SCALE -> W (on track) / B (named changes, the executor resumes) / C / D. Nothing is
    killed. No verdict -> W while a job runs, else a machine B. Resets the unit's checkpoint clock."""
    ud = unit_dir(uid)
    if OVERRIDE.is_set():
        return "W"
    archive_audit(uid)
    t_c0 = time.time()
    since = time.time() - LAST_LOOK.get(uid, T0)
    ul = usable_left()
    if reason == "after_job":
        sit = ("The unit's job ended and the session that verifies it was cut before it handed off: its results are on disk - look at what is "
               "complete now.")
    elif reason == "past_end":
        t_end = float(job.get("t0") or time.time()) + float(job.get("expect_min") or 0) * 60
        sit = ("The unit registered a job (pid %s) declared at %.0f min; it would end at %s, after the usable end %s: rule whether it can deliver "
               "the required rows in time and what must change." % (job.get("pid"), float(job.get("expect_min") or 0), time.strftime("%H:%M", time.localtime(t_end)),
                                                                   time.strftime("%H:%M", time.localtime(time.time() + ul))))
    else:
        sit = ("The unit has worked %.1f h since its last review (launch, restart or audit)%s." % (since / 3600.0,
               ("; its job (pid %s) is running" % job.get("pid")) if job else "; its executor session was cut at a slice boundary without a handoff"))
    sit += " Usable wall left: %d min." % int(ul // 60)
    notice = change_notice(u)
    prompt = (CHECKPOINT_PROMPT.format(uid=uid, situation=sit + (("\n" + notice) if notice else ""), activity=unit_activity_text(uid),
                                       mech=mech_block(uid) if os.path.exists(os.path.join(ud, "mech.json")) else "MECH CHECK: none yet (no handoff).",
                                       previous=previous_audits_block(uid), usable_end=time.strftime("%H:%M", time.localtime(time.time() + ul)))
              + notes_block("critic_notes", CRITIC_NOTES_TITLE, index=True))
    ledger({"kind": "checkpoint", "unit": uid, "reason": reason, "since_s": int(since), "usable_s": int(ul),
            "job_pid": (job or {}).get("pid"), "implicit": bool((job or {}).get("implicit"))})
    log("%s: CHECKPOINT (%s) - %.1f h since the last look, %d min usable -> the critic reads state, results so far and pace" % (uid, reason, since / 3600.0, ul // 60))
    tag = "criticcp_%s" % uid
    r = run_call(prompt, CHECK_T, tag, wrapup=WRAP_CHECK.format(uid=uid), cwd=ud, wrap_t=(300 if usable_left() < 2400 else 900))
    csid, tokens = r["sid"], r["tokens"]
    grade = parse_grade(uid)
    if grade is None and not OVERRIDE.is_set() and not INFRA["dead"] and (csid or os.path.exists(os.path.join(ud, "critic_report.md"))):
        f = _critic_format_retry(uid, csid, "criticfmt_%s" % uid, "checkpoint")
        tokens += f["tokens"]
        grade = parse_grade(uid)
    reap_critic(uid, tag, "criticfmt_%s" % uid)
    if grade == "A":
        grade = "W"
    if grade is None:
        ledger({"kind": "checkpoint_no_verdict", "unit": uid, "tokens": tokens})
        grade = "W" if job else machine_critic(uid, {"fails": ["the checkpoint gave no verdict: finish the work, then write handoff.json"]}, "B")
    if grade in ("C", "D"):
        ensure_residual(uid, "checkpoint: grade %s" % grade)
    fp = parse_fix_plan(uid)
    ledger({"kind": "critic", "unit": uid, "grade": grade, "by": "checkpoint", "reason": reason, "tokens": tokens, "dur": int(time.time() - t_c0),
            "gates": None, "fix_plan": ({"est_min": fp.get("est_min")} if fp else None), "residual_ids": bank_cited_ids(os.path.join(ud, "critic_report.md")),
            "override": OVERRIDE.is_set()})
    log("%s: checkpoint ruling %s (est %s min)" % (uid, grade, (fp or {}).get("est_min")))
    LAST_LOOK[uid] = time.time()
    CP_DONE.setdefault(uid, set()).add((_job_key(job), reason))
    if u.get("inputs") is not None:
        record_inputs(u)
    return grade

def _job_key(job):
    return (int((job or {}).get("pid") or 0), str((job or {}).get("done_marker") or "")) if job else None

def checkpoint_due(uid, job=None, t_now=None):
    """The one time check -> the reason it is due now, or None: 'past_end' (a registered job declared past the usable end, once per job) or
    'interval' (CP_S since the unit's last look, in any state, implicit jobs included). Never under the override or in the last LAST_ACT_S.
    (A session cut right after its job ended is looked at too - the unit worker asks for that 'after_job' checkpoint itself.)"""
    if not uid or OVERRIDE.is_set() or usable_left() < LAST_ACT_S:
        return None
    t_now = t_now or time.time()
    done = CP_DONE.get(uid) or set()
    if job and not job.get("implicit"):
        key = _job_key(job)
        if float(job.get("t0") or t_now) + float(job.get("expect_min") or 0) * 60 > t_now + usable_left() and (key, "past_end") not in done:
            return "past_end"
    if uid in LAST_LOOK and t_now - LAST_LOOK[uid] >= CP_S:
        return "interval"
    return None
