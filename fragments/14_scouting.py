
# ---------------------------------------------------------------- C-grade SCOUTING: parallel root-cause candidates with proofs of concept, a panel picks
SCOUT_K = 3                                           # scouts per round
SCOUT_T = _envi("RA3_SCOUT_T", 2700)                  # one scout's session
SCOUT_POC_MIN = 30                                    # its proof-of-concept computation (minutes)
SCOUT_SELECT_T = _envi("RA3_SCOUT_SELECT_T", 1800)    # one selection critic
SCOUT_PANEL = 3                                       # selection critics (Borda)
SCOUT_WRAP_T = 300                                    # the pens-down of a scout / a selection critic (no 30-min wrap-ups inside the scouting)
SCOUT_NEED_S = SCOUT_T + SCOUT_SELECT_T + 2 * SCOUT_WRAP_T + MIN_ATTEMPT_S + 600   # usable wall a scouted fresh attempt needs
SCOUT_COPY_MAX_B = 50 * 1024 * 1024


def scouting_due(u):
    """A C is scouted whenever a scouted fresh attempt still fits the usable wall (no count cap)."""
    return not OVERRIDE.is_set() and not INFRA["dead"] and usable_left() >= SCOUT_NEED_S

def _read_candidate(sdir):
    """candidate.json of a scout directory -> dict (hypothesis + poc required) or None."""
    try:
        c = json.loads(read(os.path.join(sdir, "candidate.json"), "null"))
    except Exception:
        return None
    if not isinstance(c, dict) or not str(c.get("hypothesis") or "").strip() or not isinstance(c.get("poc"), dict):
        return None
    s = c.get("supports")
    c["supports"] = s is True or str(s).strip().lower() in ("true", "yes", "1")
    rc = c.get("reading_change")
    c["reading_change"] = rc if (isinstance(rc, dict) and re.match(r"^K\d+$", str(rc.get("id") or "").strip()) and str(rc.get("reading") or "").strip()) else None
    return c

def _parse_rank(txt, valid):
    """'RANK: 2 3 1' -> [2, 3, 1]; 'RANK: NONE' -> 'NONE'; no RANK line -> None. Also returns the ADOPT and DISQUALIFIED lists (valid scout numbers)."""
    head = (txt or "")[:800]
    m = re.search(r"^\s*RANK\s*:\s*(.+)$", head, re.M | re.I)
    def nums(rx):
        mm = re.search(rx, head, re.M | re.I)
        got = [int(x) for x in re.findall(r"\d+", mm.group(1))] if mm and not re.match(r"\s*none\b", mm.group(1), re.I) else []
        return [k for i, k in enumerate(got) if k in valid and k not in got[:i]]
    adopt, disq = nums(r"^\s*ADOPT\s*:\s*(.+)$"), nums(r"^\s*DISQUALIFIED\s*:\s*(.+)$")
    if not m:
        return None, adopt, disq
    if re.match(r"\s*none\b", m.group(1), re.I):
        return "NONE", [], disq
    out = []
    for x in re.findall(r"\d+", m.group(1)):
        k = int(x)
        if k in valid and k not in out:
            out.append(k)
    return (out or None), adopt, disq

def _copy_small_tree(src_dir, dst_dir):
    """Copy a scout directory into the unit (files up to SCOUT_COPY_MAX_B each; caches and vendored trees skipped) -> [relative paths]."""
    got = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in REUSE_SKIP_DIRS and not any(x.endswith((".dist-info", ".egg-info")) for x in dirs)]
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.getsize(p) > SCOUT_COPY_MAX_B:
                    continue
                rel = os.path.relpath(p, src_dir)
                os.makedirs(os.path.dirname(os.path.join(dst_dir, rel)), exist_ok=True)
                shutil.copy2(p, os.path.join(dst_dir, rel))
                got.append(rel)
            except OSError:
                pass
    return got


def _cand_line(k, c):
    return "scout %d (%s): %s | PoC: %s | supports=%s" % (k, str(c.get("angle") or SCOUT_ANGLES[(k - 1) % len(SCOUT_ANGLES)])[:60],
        str(c.get("hypothesis"))[:240], str((c.get("poc") or {}).get("finding") or "-")[:200], c.get("supports"))

def scout_round(u, grant, job=None):
    """After a C, BEFORE any archive: SCOUT_K scouts on SCOUT_K angles read the unit's directory (read-only), propose root causes and test them with
    a PoC in parallel on the unit's grant; a panel of SCOUT_PANEL critics ranks them (Borda) and may adopt compatible others. A running job (a
    checkpoint C) keeps running. Returns {"winner": k|None, "adopt": [k], "res": {k: dir info}, "note": text} - the caller archives and restarts
    on a winner, and fixes in place when there is none."""
    uid = u["id"]
    u["scouted"] = int(u.get("scouted") or 0) + 1
    with STATE_LOCK:
        RUN["scouts"] = RUN.get("scouts", 0) + 1
    n = u["scouted"]
    base = os.path.join(PD, "scouts", "%s.s%d" % (uid, n))
    os.makedirs(base, exist_ok=True)
    arch = "/workspace/%s" % uid
    cores = max(1, int((grant or {}).get("cores") or 1) // SCOUT_K)
    mem = max(2, int((grant or {}).get("mem_gb") or 6) // SCOUT_K)
    spec = "/workspace/%s/spec.md" % uid
    hist = u.get("scout_hist") or []
    tried = ("\nALREADY TESTED in earlier scout rounds of this unit (build on them; do not repeat a refuted hypothesis): " + " || ".join(hist[-6:])) if hist else ""
    bank = bank_scout_block(uid, arch)
    running = ("; its registered job pid %s is STILL RUNNING - its files may grow, never stop it" % job.get("pid")) if job else ""
    ledger({"kind": "scout_start", "unit": uid, "k": SCOUT_K, "cores_each": cores, "mem_each": mem, "n_episode": RUN["scouts"], "n_unit": n, "job_running": bool(job)})
    log("%s: SCOUTING after grade C - %d scouts (%d core(s), %d GB each, %d min), then a panel of %d critics" % (uid, SCOUT_K, cores, mem, SCOUT_T // 60, SCOUT_PANEL))
    res = {}
    def one(k):
        sdir = os.path.join(base, "s%d" % k)
        os.makedirs(sdir, exist_ok=True)
        wsd = _ws_name(sdir)
        try:
            r = run_call(SCOUT_PROMPT.format(k=k, K=SCOUT_K, uid=uid, archive=arch, running=running, sdir=wsd, spec=spec,
                                             angle=SCOUT_ANGLES[(k - 1) % len(SCOUT_ANGLES)], tried=tried, poc_min=SCOUT_POC_MIN, cores=cores,
                                             mins=SCOUT_T // 60, bank=bank),
                         slot(SCOUT_T), "scout%d_%s" % (k, uid), wrapup=SCOUT_WRAP.format(sdir=wsd), grant={"cores": cores, "mem_gb": mem}, cwd=sdir,
                         wrap_t=SCOUT_WRAP_T)
        except Exception as e:
            log("%s: scout %d failed: %s" % (uid, k, e))
            r = {"rc": -2, "tokens": 0}
        c = _read_candidate(sdir)
        res[k] = {"dir": wsd, "real": sdir, "cand": c}
        ledger({"kind": "scout_done", "unit": uid, "k": k, "valid": bool(c), "supports": (c or {}).get("supports"), "tokens": r.get("tokens"), "rc": r.get("rc")})
    ths = [threading.Thread(target=one, args=(k,), name="scout%d_%s" % (k, uid), daemon=True) for k in range(1, SCOUT_K + 1)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    for k in range(1, SCOUT_K + 1):   # no scout computation outlives the scouting (the unit's own job, if any, is not touched)
        try:
            _kill_marked("scout%d_%s" % (k, uid), 9)
        except Exception:
            pass
    valid = dict((k, v) for k, v in res.items() if v["cand"])
    u["scout_hist"] = hist + ["%s (supports=%s: %s)" % (str(v["cand"].get("hypothesis"))[:160], v["cand"].get("supports"),
                                                         str((v["cand"].get("poc") or {}).get("finding") or "-")[:120]) for k, v in sorted(valid.items())]
    out = {"winner": None, "adopt": [], "disq": [], "res": res, "note": ""}
    def fallback(why, disq=()):
        out["disq"] = sorted(disq)
        ledger({"kind": "scout_fallback", "unit": uid, "why": why, "valid": sorted(valid), "disqualified": sorted(disq)})
        log("%s: scouting gave no winner (%s) -> the unit is fixed in place" % (uid, why))
        if valid:
            out["note"] = ("SCOUTING found no admissible route - the candidates checked (an unsupported hypothesis stays refuted unless you find new "
                           "evidence): " + " || ".join(_cand_line(k, v["cand"]) + (DISQ_MARK if k in disq else "") for k, v in sorted(valid.items())))
        return out
    if not valid:
        return fallback("no valid candidate")
    if OVERRIDE.is_set():
        return fallback("override")
    sel = scout_select(uid, base, valid, arch, spec)
    if sel.get("fallback"):
        return fallback(sel["fallback"], sel.get("disq") or ())
    out.update(winner=sel["winner"], adopt=sel["adopt"], disq=sel.get("disq") or [])
    return out

def scout_select(uid, base, valid, arch, spec):
    """The selection panel: SCOUT_PANEL critics rank the valid candidates side by side (Borda); the winner is the best-ranked SUPPORTED one,
    adopted = the other supported candidates a majority names. -> {"winner", "adopt"} or {"fallback": why}."""
    spare_min = max(0, int(usable_left() / 60))
    dirs = ", ".join("scout %d: %s/" % (k, v["dir"]) for k, v in sorted(valid.items()))
    sel = {}
    def judge(j):
        outf = os.path.join(base, "select%d.md" % j)
        try:
            run_call(SELECT_PROMPT.format(uid=uid, n=len(valid), dirs=dirs, archive=arch, spec=spec, spare_min=spare_min, out=_ws_name(outf)),
                     slot(SCOUT_SELECT_T), "criticsel%d_%s" % (j, uid), wrapup=SELECT_WRAP.format(out=_ws_name(outf)), cwd=base, wrap_t=SCOUT_WRAP_T)
        except Exception as e:
            log("%s: selection critic %d failed: %s" % (uid, j, e))
        sel[j] = _parse_rank(read(outf), set(valid))
    ths = [threading.Thread(target=judge, args=(j,), name="criticsel%d_%s" % (j, uid), daemon=True) for j in range(1, SCOUT_PANEL + 1)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    ranks = [v[0] for v in sel.values() if isinstance(v[0], list)]
    nones = sum(1 for v in sel.values() if v[0] == "NONE")
    pts = dict((k, 0) for k in valid)
    for rk in ranks:
        for i, k in enumerate(rk):
            pts[k] += len(valid) - 1 - i
    if nones * 2 > SCOUT_PANEL or not ranks:
        ledger({"kind": "scout_select", "unit": uid, "ranks": dict((str(j), v[0]) for j, v in sel.items()), "nones": nones})
        return {"fallback": "the panel found no supported candidate" if ranks or nones else "no ranking", "disq": []}
    dq_votes = {}
    for v in sel.values():
        for k in (v[2] or []):
            dq_votes[k] = dq_votes.get(k, 0) + 1
    disq = sorted(k for k, n_ in dq_votes.items() if n_ * 2 > SCOUT_PANEL)   # a majority's disqualification (criterion 2) is binding
    ranked = [k for k in sorted(pts, key=lambda k: (-pts[k], k)) if valid[k]["cand"].get("supports") and k not in disq]
    if not ranked:   # the winner, like every adopted candidate, is supported by its PoC and not disqualified
        ledger({"kind": "scout_select", "unit": uid, "ranks": dict((str(j), v[0]) for j, v in sel.items()), "points": dict((str(k), p) for k, p in pts.items()),
                "nones": nones, "disq_votes": dict((str(k), n_) for k, n_ in dq_votes.items()), "disqualified": disq})
        sup = [k for k in pts if valid[k]["cand"].get("supports")]
        return {"fallback": "the panel disqualified every supported candidate" if sup else "no ranked candidate is supported by its PoC", "disq": disq}
    win = ranked[0]
    votes = {}
    for v in sel.values():
        for k in (v[1] or []):
            if k != win:
                votes[k] = votes.get(k, 0) + 1
    adopt = sorted(k for k, nv in votes.items() if nv * 2 > SCOUT_PANEL and valid[k]["cand"].get("supports") and k not in disq)
    ledger({"kind": "scout_select", "unit": uid, "ranks": dict((str(j), v[0]) for j, v in sel.items()), "adopt_votes": dict((str(k), n_) for k, n_ in votes.items()),
            "points": dict((str(k), p) for k, p in pts.items()), "nones": nones, "disq_votes": dict((str(k), n_) for k, n_ in dq_votes.items()),
            "disqualified": disq, "winner": win, "adopt": adopt})
    return {"winner": win, "adopt": adopt, "disq": disq}

def scout_note(uid, sc):
    """The restart preamble's account of the selected route (the winner's files are in /workspace/<uid>/from_scout/, adopted ones beside)."""
    valid = dict((k, v) for k, v in sc["res"].items() if v["cand"])
    win, c = sc["winner"], valid[sc["winner"]]["cand"]
    ad = " ".join("ALSO ADOPTED (apply this change as well; its files are in /workspace/%s/from_scout_%d/): %s Its PoC: %s. Its plan: %s."
                  % (uid, k, str(valid[k]["cand"].get("hypothesis"))[:300], str((valid[k]["cand"].get("poc") or {}).get("finding") or "-")[:200],
                     json.dumps(valid[k]["cand"].get("plan"), ensure_ascii=False)[:400]) for k in sc["adopt"])
    others = [_cand_line(k, v["cand"]) + (DISQ_MARK if k in (sc.get("disq") or ()) else "") for k, v in sorted(valid.items()) if k != win and k not in sc["adopt"]]
    return ("SCOUTING: after the failed round %d scouts each tested a root-cause hypothesis with a small proof-of-concept; a panel of %d critics picked "
            "scout %d. Its hypothesis: %s Its evidence: %s Its PoC: %s (command: %s). Its plan: %s. Its code and PoC outputs are in /workspace/%s/from_scout/ - "
            "re-run its PoC once, then carry the plan to full scale (the executor rules still apply). %s The other candidates, your alternatives if "
            "this route fails: %s"
            % (len(valid), SCOUT_PANEL, win, str(c.get("hypothesis"))[:400], str(c.get("evidence") or "-")[:300],
               str((c.get("poc") or {}).get("finding") or "-")[:300], str((c.get("poc") or {}).get("command") or "-")[:160],
               json.dumps(c.get("plan"), ensure_ascii=False)[:700], uid, ad, " || ".join(others) or "(none)"))

def scout_seed(uid, sc):
    """Copy the winner's (and every adopted candidate's) files into the unit directory (the fresh one after an archive)."""
    valid = dict((k, v) for k, v in sc["res"].items() if v["cand"])
    got = _copy_small_tree(valid[sc["winner"]]["real"], os.path.join(unit_dir(uid), "from_scout"))
    for k in sc["adopt"]:
        _copy_small_tree(valid[k]["real"], os.path.join(unit_dir(uid), "from_scout_%d" % k))
    ledger({"kind": "scout_winner", "unit": uid, "winner": sc["winner"], "adopt": sc["adopt"], "files": len(got)})
    log("%s: scout %d wins%s - its files copied to /workspace/%s/from_scout/" % (uid, sc["winner"], (", adopted %s" % sc["adopt"]) if sc["adopt"] else "", uid))

def scout_readings(uid, sc):
    """The winner's and the adopted candidates' reading changes, checked against their evidence files as the scouting left them (before any
    archive moves the unit's files) and propagated."""
    if not sc.get("winner"):
        return
    valid = dict((k, v) for k, v in sc["res"].items() if v["cand"])
    for k in [sc["winner"]] + list(sc["adopt"]):
        rc = valid[k]["cand"].get("reading_change")
        if rc and not _evidence_exists(uid, str(rc.get("evidence") or ""), base=valid[k]["real"]):   # the same rule as a critic's change
            ledger({"kind": "reading_change_rejected", "unit": uid, "id": rc.get("id"), "why": "no evidence file", "evidence": str(rc.get("evidence") or "")[:200],
                    "source": "scout %d" % k})
        elif rc:
            apply_reading_change(str(rc["id"]).strip(), str(rc["reading"])[:600], str(rc.get("evidence") or "")[:300], uid, "scout")
