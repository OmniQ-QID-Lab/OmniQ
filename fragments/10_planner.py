
# ---------------------------------------------------------------- planner: plan.json = {requirements, readings, units}
_UID_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

HISTORY = ("failed",)   # retired units stay in the plan as history; their ids are never reused


def _in_unit_dir(uid, n):
    return n == "/workspace/%s" % uid or n.startswith("/workspace/%s/" % uid)


def plan_alloc_min(u):
    """The minutes of wall a unit owns on the critical path - alloc_min for a produce unit, report_cost_min for the
    render unit, 0 for a unit that is already finished or retired (its time is spent, not planned)."""
    u = u or {}
    if u.get("status") == "done" or u.get("status") in HISTORY:
        return 0
    if u.get("type") == "render":
        try:
            return max(REPORT_COST_BOUNDS[0], min(REPORT_COST_BOUNDS[1], int(float(u.get("report_cost_min")))))
        except Exception:
            return REPORT_MIN
    try:
        return max(0, int(float(u.get("alloc_min"))))
    except Exception:
        return 0


def critical_path(plan):
    """(minutes, [ids]) of the longest depends_on chain of alloc_min, the render unit's report cost included."""
    by = dict((u.get("id"), u) for u in (plan or []) if isinstance(u, dict))
    memo = {}
    def walk(uid, seen=()):
        if uid in memo:
            return memo[uid]
        best = (0, [])
        for d in ((by.get(uid) or {}).get("depends_on") or []):
            if d in by and d not in seen and d != uid:
                c = walk(d, seen + (uid,))
                if c[0] > best[0]:
                    best = c
        memo[uid] = (best[0] + plan_alloc_min(by.get(uid)), best[1] + [uid])
        return memo[uid]
    out = (0, [])
    for u in (plan or []):
        if not isinstance(u, dict) or u.get("status") in HISTORY:
            continue
        c = walk(u.get("id"))
        if c[0] > out[0]:
            out = c
    return out


def plan_shape(plan):
    """The dependency shape of the live produce units -> {"width0": units that can start at t=0, "depth": longest produce chain,
    "gate": the one root every other produce unit hangs on (or None), "fan_in": [(uid, [independent producers it waits on])]}."""
    by = dict((u.get("id"), u) for u in (plan or []) if isinstance(u, dict))
    live = [u for u in (plan or []) if isinstance(u, dict) and u.get("type") == "produce" and u.get("status") not in HISTORY]
    def live_deps(u):
        return [d for d in (u.get("depends_on") or []) if d in by and by[d].get("type") == "produce" and by[d].get("status") not in HISTORY]
    memo = {}
    def depth(uid, seen=()):
        if uid in memo:
            return memo[uid]
        ds = [depth(d, seen + (uid,)) for d in live_deps(by[uid]) if d not in seen and d != uid]
        memo[uid] = 1 + (max(ds) if ds else 0)
        return memo[uid]
    def anc(uid, seen=()):
        out = set()
        for d in live_deps(by.get(uid) or {}):
            if d not in seen and d != uid:
                out.add(d); out |= anc(d, seen + (uid,))
        return out
    roots = [u["id"] for u in live if not live_deps(u)]
    gate = None
    if len(live) >= 3 and len(roots) == 1 and all(roots[0] in anc(u["id"]) for u in live if u["id"] != roots[0]):
        gate = roots[0]
    fan_in = []
    for u in live:
        ds = live_deps(u)
        if len(ds) >= 2 and not any(a in anc(b) or b in anc(a) for a in ds for b in ds if a != b):
            fan_in.append((u["id"], ds))
    return {"width0": len(roots), "depth": max([depth(u["id"]) for u in live] or [0]), "gate": gate, "fan_in": fan_in}


def _crit_has_number(c):
    """Does a done criterion state a number (paths, file names, unit ids and F-references removed)?"""
    s = re.sub(r"\S*/\S*", " ", c)
    s = re.sub(r"\b[\w.-]+\.(?:py|json|npz|npy|csv|tsv|txt|png|pdf|dat|h5|hdf5|md|log|jl)\b", " ", s)
    s = re.sub(r"\b[uU]\d+\b|\bF\d+\b", " ", s)
    return bool(re.search(r"\d", s))


def plan_summary(plan):
    L = ["PLAN SUMMARY (%d units; node %d cores / %s GB):" % (len(plan), NCPUS, MEM_GB or "?")]
    for u in plan:
        L.append("  %-6s %-8s cores=%-3s mem=%-4s deps=%-18s %s%s" % (
            u.get("id"), u.get("type"), u.get("cores"), u.get("mem_gb"),
            ",".join(str(x) for x in (u.get("depends_on") or [])) or "-",
            (str(u.get("goal", "")).splitlines() or [""])[0][:90],
            (" [%s]" % u.get("status")) if u.get("status") else ""))
    return "\n".join(L)

_NEG_BEFORE = re.compile(r"\b(never|not|no|none|don't|do not|must not|may not|cannot|can't|nor|without|forbid\w*|prohibit\w*|disallow\w*)\b", re.I)
_NEG_AFTER = re.compile(r"\b(forbidden|prohibited|disallowed|not allowed|not permitted|never)\b", re.I)
_CLAUSE_END = re.compile(r"[.;:!?,\n]")
_DROP_RX = re.compile(r"\b(drop(?:ped|ping|s)?|omit(?:ted|s)?|skip(?:ped|s)?|sacrific\w*|leave out|left out|mark(?:ed)?(?: it| them)? (?:as )?missing|as missing)\b", re.I)
_FALLBACK_RX = re.compile(r"\b(fall ?back|fallback|in place of|instead of|substitut\w*|stand-?in|approximat\w* (?:it|them) by)\b", re.I)

def _unnegated(rx, text):
    """Phrases of `text` matching rx that no negation in the same clause qualifies ("no required row may be dropped", "never drop", "a substitute
    is forbidden" are compliance, not authorisation)."""
    out, text = [], text or ""
    for m in rx.finditer(text):
        start = max([c.end() for c in _CLAUSE_END.finditer(text, 0, m.start())] or [0])
        e = _CLAUSE_END.search(text, m.end())
        if _NEG_BEFORE.search(text[start:m.start()]) or _NEG_AFTER.search(text[m.end():(e.start() if e else len(text))]):
            continue
        out.append(text[max(0, m.start() - 50):m.end() + 50].replace("\n", " "))
    return out

def need_paths(text, dep=None):
    """ONE path extractor for a "needs" entry, shared by the plan validator and the file gate: every /workspace/<dep>/... token
    ("A and B", "A plus B", "A (plus B)", "A + B", "A, B"), trailing punctuation stripped; placeholders (<key>, {a,b}, *) stay patterns."""
    toks = re.findall(r"/workspace/(?:[^\s,;'\"()\[\]{}]|\{[^{}]*\})+", str(text or ""))
    out = []
    for t in toks:
        t = t.rstrip(".:")
        if dep is not None and not (t == "/workspace/%s" % dep or t.startswith("/workspace/%s/" % dep)):
            continue
        if t.rstrip("/").endswith("/handoff.json"):
            continue
        if t not in out:
            out.append(t)
    return out

def split_plan(obj):
    """plan.json as written by the planner -> (units, requirements, readings, format error or None). A bare array is an old-style plan."""
    if isinstance(obj, list):
        return obj, None, None, None
    if isinstance(obj, dict) and isinstance(obj.get("units"), list):
        rq = obj.get("requirements")
        rd = obj.get("readings")
        return obj["units"], (rq if isinstance(rq, list) else None), (rd if isinstance(rd, list) else None), None
    return None, None, None, "plan.json must be a JSON object {\"requirements\": [...], \"readings\": [...], \"units\": [...]}"

def validate_plan(obj, old_plan=None, strict=True):
    """(errors, warnings, units, requirements, readings). Errors go back to the planner verbatim. HARD errors (the plan cannot run) are errors
    on every attempt; SOFT errors (the planning contract: requirements table, core first, allocation caps, drop / fallback wording) are errors
    while `strict`, warnings on the last attempt, so a plan always comes out. Uncited numbers in done_criteria never bounce a plan: they
    move to the unit's "notes". With old_plan (a replan) done / running / retired units are restored byte-identical."""
    errs, warns, soft = [], [], []
    plan, reqs, reads, ferr = split_plan(obj)
    if ferr:
        return [ferr], warns, None, None, None
    tb_n = len(task_book_items())
    if not plan or not all(isinstance(u, dict) for u in plan):
        return ["\"units\" must be a non-empty list of unit objects"], warns, None, None, None
    if not all(isinstance(u.get("id"), str) for u in plan):
        return ["every unit needs a string id"], warns, None, None, None
    for u in plan:   # the planner's free notes as a list (a string, number or null in its place is normalised)
        nt = u.get("notes")
        if not isinstance(nt, list):
            u["notes"] = [] if nt is None or (isinstance(nt, str) and not nt.strip()) else [nt if isinstance(nt, str) else json.dumps(nt)]
    if old_plan is not None:
        have = dict((u.get("id"), u) for u in plan)
        restored = []
        for ou in old_plan:
            st = ou.get("status") if isinstance(ou, dict) else None
            if st not in ("done", "running") + HISTORY:
                continue
            keep = json.loads(json.dumps(ou))
            nu = have.get(ou.get("id"))
            if nu is None:
                at = max([i + 1 for i, x in enumerate(plan) if x.get("id") in (keep.get("depends_on") or [])] or [0])
                plan.insert(at, keep)
                have[keep["id"]] = keep
                restored.append("%s (%s, missing -> re-inserted)" % (keep["id"], st))
            elif nu.get("status") != st or any(nu.get(k) != ou.get(k) for k in ("type", "goal", "deliverables", "done_criteria", "depends_on")):
                if st in HISTORY:
                    errs.append("unit %s: this id is RETIRED - a replacement needs a NEW id; keep the retired unit as it was or drop it" % keep["id"])
                nu.clear()
                nu.update(keep)
                restored.append("%s (%s, rewritten -> restored)" % (keep["id"], st))
        if restored:
            warns.append("replan: done / running / retired unit(s) restored from the live plan: %s" % ", ".join(restored))
    rends = [u for u in plan if u.get("type") == "render"]
    if len(rends) == 1:   # mechanical slips of the render unit are mended, never bounced: last in the list, depending on every produce unit
        rd0 = rends[0]
        if plan[-1] is not rd0:
            plan.remove(rd0); plan.append(rd0)
            warns.append("the render unit moved to the end of the plan")
        miss0 = [x["id"] for x in plan if x.get("type") == "produce" and x.get("status") not in HISTORY + ("done",) and x["id"] not in (rd0.get("depends_on") or [])]
        if miss0:
            rd0["depends_on"] = list(rd0.get("depends_on") or []) + miss0
            warns.append("the render unit now depends on %s too" % miss0)
    live = [u for u in plan if u.get("status") not in HISTORY]
    if len(live) < 2:
        errs.append("unit count %d below 2 (a produce unit and the render unit; retired units excluded)" % len(live))
    elif len(live) > MAX_UNITS:   # soft: on the last attempt a larger plan runs rather than the one-unit fallback
        soft.append("unit count %d above %d (retired units excluded): merge units" % (len(live), MAX_UNITS))
    ids = [u["id"] for u in plan]
    if len(set(ids)) != len(ids):
        errs.append("duplicate unit ids")
    by = dict((u["id"], u) for u in plan)
    pos = dict((u["id"], i) for i, u in enumerate(plan))
    wall_min = int(max(0.0, remaining()) // 60)
    for u in plan:
        uid = u["id"]
        tag = "unit %s" % uid
        hist = u.get("status") in HISTORY
        if not _UID_RX.match(uid):
            errs.append("%s: id must match %s" % (tag, _UID_RX.pattern))
        elif re.search(r"_(only|wrapup|gwretry\d*)$", uid):
            errs.append("%s: an id may not end in _only, _wrapup or _gwretry (the orchestrator's call suffixes)" % tag)
        errs += ["%s: missing %s" % (tag, k) for k in ("goal", "deliverables", "done_criteria") if k not in u]
        if not isinstance(u.get("deliverables"), list) or not u.get("deliverables") or not all(isinstance(x, str) and x.strip() for x in u["deliverables"]):
            errs.append("%s: deliverables must be a non-empty list of path strings" % tag)
        elif not hist:
            hj = [d for d in u["deliverables"] if d.strip().rstrip("/").endswith("/handoff.json")]
            if hj:
                u["deliverables"] = [d for d in u["deliverables"] if d not in hj]
                warns.append("%s: handoff.json is not a deliverable (dropped from the list)" % tag)
                if not u["deliverables"]:
                    errs.append("%s: deliverables must name at least one product besides handoff.json" % tag)
            for d in u["deliverables"]:
                n = d.strip().rstrip("/")
                if not _in_unit_dir(uid, n) and not (u.get("type") == "render" and re.match(r"^/workspace/[^/]+$", n)):
                    errs.append("%s: deliverable %r must be inside the unit's own directory /workspace/%s/%s"
                                % (tag, d, uid, " (the render unit may also name the final files at /workspace/ root)" if u.get("type") == "render" else ""))
        if not isinstance(u.get("done_criteria"), list) or not u.get("done_criteria"):
            errs.append("%s: done_criteria must be a non-empty list" % tag)
        elif not hist and tb_n:   # a number of the planner's own is not a criterion: it moves to "notes" (no bounce)
            keep_c, moved = [], []
            for c in u.get("done_criteria"):
                if isinstance(c, str) and _crit_has_number(c) and not any(1 <= int(k) <= tb_n for k in re.findall(r"\bF(\d+)\b", c)):
                    moved.append(c)
                else:
                    keep_c.append(c)
            if moved:
                u["done_criteria"] = keep_c or ["every deliverable exists and follows the task book"]
                u.setdefault("notes", [])
                u["notes"] = list(u["notes"]) + ["(planner number, not a criterion) " + c for c in moved]
                warns.append("%s: %d done criteria state a number without a task-book item - moved to notes (not criteria): %s"
                             % (tag, len(moved), "; ".join(c[:80] for c in moved[:3])))
        t = u.get("type")
        if t not in UNIT_TYPES:
            errs.append("%s: type must be one of %s (got %r)" % (tag, "/".join(UNIT_TYPES), t))
        elif t == "render" and uid != "render":
            errs.append('%s: the render unit\'s id must be exactly "render"' % tag)
        c = u.get("cores")
        if not (isinstance(c, int) and not isinstance(c, bool) and c >= 1):
            errs.append("%s: cores must be a positive integer (got %r)" % (tag, c))
        elif c > NCPUS:
            errs.append("%s: cores=%d exceeds the node's %d cores" % (tag, c, NCPUS))
        m = u.get("mem_gb")
        if not (isinstance(m, (int, float)) and not isinstance(m, bool) and m >= 1):
            errs.append("%s: mem_gb must be a positive number (got %r)" % (tag, m))
        elif MEM_GB > 0 and m > MEM_GB:
            errs.append("%s: mem_gb=%s exceeds the node's %d GB" % (tag, m, MEM_GB))
        deps = u.setdefault("depends_on", [])
        if not isinstance(deps, list):
            errs.append("%s: depends_on must be a list of unit ids" % tag)
            deps = []
        if t == "render" and not hist:   # the report accounts for every unit; a retired id in its list schedules nothing
            gone = [d for d in deps if d in by and by[d].get("status") in HISTORY]
            if gone:
                deps[:] = [d for d in deps if d not in gone]
                warns.append("unit render: retired dependency %s dropped (the report accounts for retired units)" % ", ".join(gone))
        for d in deps:
            if d not in by:
                errs.append("%s: depends_on unknown unit %r" % (tag, d))
            elif d == uid:
                errs.append("%s: depends on itself" % tag)
            elif by[d].get("status") in HISTORY and not hist and u.get("status") not in ("done", "running"):
                errs.append("%s: depends_on %s which is RETIRED" % (tag, d))
            elif pos[d] > pos[uid]:
                errs.append("%s: depends_on %s which is listed AFTER it - list dependencies first" % (tag, d))
        if t == "produce" and deps and not hist:
            needs = u.get("needs")
            if not isinstance(needs, dict):
                errs.append('%s: depends_on %s but no "needs" object naming, per dependency, the upstream FILE this unit cannot start without - '
                            'or drop the dependency (comparisons and conventions are not dependencies)' % (tag, deps))
            else:
                for d in deps:
                    if not need_paths(needs.get(d), d):
                        errs.append('%s: needs[%s] must name a file under /workspace/%s/ other than handoff.json (got %r)' % (tag, d, d, str(needs.get(d))[:80]))
        if t == "render" and u.get("report_cost_min") is not None:
            try:
                float(u.get("report_cost_min"))
            except Exception:
                warns.append("%s: report_cost_min is not a number - the default %d min is used" % (tag, REPORT_MIN))
                u.pop("report_cost_min", None)
        a = u.get("alloc_min")
        if t == "produce" and not hist:
            if not (isinstance(a, (int, float)) and not isinstance(a, bool) and a > 0):
                errs.append("%s: alloc_min must be a positive number of minutes (got %r)" % (tag, a))
            elif a > wall_min / 3.0 + 1:
                soft.append("%s: alloc_min %d exceeds a third of the %d min of wall left - split the unit along its independent inputs or lower its "
                            "precision plan" % (tag, int(a), wall_min))
        if t == "produce" and u.get("scale_knobs") is not None and not isinstance(u.get("scale_knobs"), list):
            warns.append("%s: scale_knobs must be a list of strings - ignored" % tag)
            u.pop("scale_knobs", None)
        if not hist and t == "produce":
            txt = " ".join([str(u.get("goal", ""))] + [str(x) for x in (u.get("scale_knobs") or [])] + [str(x) for x in (u.get("done_criteria") or [])])
            for ph in _unnegated(_DROP_RX, txt)[:2]:
                soft.append("%s: a goal / knob / criterion may drop a required row (\"...%s...\"): required rows are never optional - lower precision "
                            "instead, and let the review decide what does not fit" % (tag, ph.strip()))
            for ph in _unnegated(_FALLBACK_RX, str(u.get("goal", "")))[:2]:
                soft.append("%s: the goal names a fallback / substitute (\"...%s...\"): a method component the task book names is implemented as "
                            "named, with a library's implementation of it where one exists - no fallback clause" % (tag, ph.strip()))
    renders = [u for u in plan if u.get("type") == "render"]
    prods = [u for u in plan if u.get("type") == "produce" and u.get("status") not in HISTORY]
    if len(renders) != 1:
        errs.append("exactly ONE render unit is required (got %d)" % len(renders))
    else:
        rd = renders[0]
        if plan[-1] is not rd:
            errs.append("the render unit must be the LAST unit")
        missing = [p["id"] for p in prods if p["id"] not in (rd.get("depends_on") or []) and p.get("status") != "done"]
        if missing:
            errs.append("the render unit must depends_on every produce unit; missing %s" % missing)
    if not prods:
        errs.append("at least one produce unit is required")
    seen_scripts = {}
    for p in prods:
        for d in (p.get("deliverables") or []):
            nm = str(d).strip()
            if nm.endswith("/") or not re.search(r"\.(py|sh|jl|c|cpp|R|m)$", nm):
                continue
            b = os.path.basename(nm)
            if b in seen_scripts and seen_scripts[b] != p.get("id"):
                soft.append("units %s and %s both deliver a script named %r - give every unit's scripts distinct names (shared process space)"
                            % (seen_scripts[b], p.get("id"), b))
            seen_scripts.setdefault(b, p.get("id"))
    # ---- the requirements table (one owner per row, never optional) and the core-first rule
    prod_ids = set(p["id"] for p in prods) | set(u["id"] for u in plan if u.get("type") == "produce" and u.get("status") == "done")
    if reqs is None:
        soft.append("plan.json has no \"requirements\" list: write one row per required item of the task book (series x required value x panel, "
                    "and every named method component) with its owner, \"core\" and \"precision_target\"")
        reqs = []
    good = []
    for i, r in enumerate(reqs):
        if not isinstance(r, dict) or not str(r.get("what") or "").strip():
            soft.append("requirements[%d] must be an object with at least \"what\" and \"owner\"" % i)
            continue
        r.setdefault("id", "Q%d" % (i + 1))
        r["core"] = bool(r.get("core") is True or str(r.get("core")).lower() == "true")
        if isinstance(r.get("owner"), list) and len(r["owner"]) == 1:   # ["u1"] is u1
            r["owner"] = r["owner"][0]
        if not isinstance(r.get("owner"), str):
            soft.append("requirements %s (%s): owner must be ONE unit id (got %s) - split the row by producer or name the unit that produces it"
                        % (r.get("id"), str(r.get("what"))[:60], json.dumps(r.get("owner"))[:80]))
            continue
        if r.get("owner") == "render":   # a row the render unit delivers (figure files, report, a schematic panel) - never a computed core row
            r["core"] = False
            good.append(r)
            continue
        if r.get("owner") not in prod_ids:
            soft.append("requirements %s (%s): owner %r is not a unit of this plan" % (r.get("id"), str(r.get("what"))[:60], r.get("owner")))
            continue
        good.append(r)
    if good and not any(r["core"] for r in good):
        soft.append("no requirement row is marked \"core\": mark the rows that carry the organizing claim and the largest required size / value / time")
    live_by = dict((u["id"], u) for u in live)
    def start_min(uid, seen=()):
        best = 0
        for d in (live_by.get(uid) or {}).get("depends_on") or []:
            if d in live_by and d not in seen and live_by[d].get("status") not in ("done",):
                best = max(best, start_min(d, seen + (uid,)) + plan_alloc_min(live_by[d]))
        return best
    for r in good:
        if not r["core"]:
            continue
        ow = r["owner"]
        if (live_by.get(ow) or {}).get("status") in ("done", "running"):
            continue
        sm = start_min(ow)
        preds = [d for d in ((live_by.get(ow) or {}).get("depends_on") or []) if d in live_by and live_by[d].get("status") not in ("done",)]
        big = [d for d in preds if int(live_by[d].get("cores") or 0) > max(1, NCPUS // 2)]
        if sm > 180 or big:
            soft.append("core row %s (%s) is owned by %s, which starts only after %d min behind %s%s: owners of core rows start at t=0 or after "
                        "upstream units of at most 180 min and at most half the cores; run the smallest series beside the core, not before it"
                        % (r.get("id"), str(r.get("what"))[:60], ow, sm, ", ".join(preds) or "-", (" (%s use more than half the cores)" % ", ".join(big)) if big else ""))
    if reads is None:
        warns.append("plan.json has no \"readings\" list - READINGS.md will say so")
        reads = []
    reads = [x for x in reads if isinstance(x, dict) and str(x.get("reading") or "").strip()]
    for i, x in enumerate(reads):
        x.setdefault("id", "K%d" % (i + 1))
        if isinstance(x.get("check_by"), list) and len(x["check_by"]) <= 1:   # ["u1"] is u1, [] is none
            x["check_by"] = x["check_by"][0] if x["check_by"] else None
        if x.get("check_by") is not None and not isinstance(x.get("check_by"), str):
            warns.append("readings %s: check_by must be ONE unit id (got %s) - ignored" % (x.get("id"), json.dumps(x.get("check_by"))[:80]))
            x["check_by"] = None
        if x.get("check_by") and x.get("check_by") not in prod_ids:
            warns.append("readings %s: check_by %r is not a produce unit - ignored" % (x.get("id"), x.get("check_by")))
            x["check_by"] = None
    cp_min, cp_ids = critical_path(plan)
    if cp_min > wall_min:   # soft: the usable wall bounds every unit anyway
        soft.append("critical path %d min does not fit the %d min of wall left: %s" % (cp_min, wall_min, " + ".join("%s %d" % (x, plan_alloc_min(by.get(x))) for x in cp_ids)))
    elif cp_min > 0.6 * wall_min + 1:
        soft.append("critical path %d min exceeds 60%% of the %d min of wall left (%s): shorten the chain or its allocations"
                    % (cp_min, wall_min, " + ".join("%s %d" % (x, plan_alloc_min(by.get(x))) for x in cp_ids)))
    if strict:
        errs += soft
    else:
        warns += ["(accepted on the last attempt) " + s for s in soft]
    return errs, warns, plan, good, reads

READINGS_LOCK = threading.Lock()

def write_readings(reads):
    """/workspace/READINGS.md from the plan's readings (and later versions). Its content depends on the readings only (it is a consumed input);
    rendering and writing happen under one lock, so the newest readings are written last."""
    with READINGS_LOCK:
        _write_readings(reads)

def _write_readings(reads):
    L = ["# READINGS - the task-book interpretations every unit shares (from the plan)",
         "Use them as written. Evidence that one is wrong goes into handoff.json \"reading_issue\" (executors) or the \"reading_change\" field of the FIX_PLAN line (critics);",
         "never switch silently. A changed reading is marked with its version.", ""]
    if not reads:
        L.append("(the plan names no shared readings - take every setting from the task book's own words)")
    for x in reads:
        L.append("%s%s (%s): %s" % (x.get("id"), (" v%d" % x["version"]) if x.get("version", 1) > 1 else "", x.get("F") or "-", x.get("reading")))
        if x.get("why"):
            L.append("    why: %s" % str(x.get("why"))[:400])
        if x.get("options"):
            L.append("    options: %s" % json.dumps(x.get("options"), ensure_ascii=False)[:400])
        if x.get("check") and str(x.get("check")).lower() not in ("none", "-", "null"):
            L.append("    check: %s%s" % (str(x.get("check"))[:400], (" - run FIRST by %s; until its verdict the reading above stands" % x["check_by"]) if x.get("check_by") else ""))
        if x.get("changed"):
            L.append("    CHANGED (version %d): %s" % (x.get("version", 2), str(x.get("changed"))[:500]))
    tmp = os.path.join(WS, "READINGS.md.tmp.%d.%d" % (os.getpid(), threading.get_ident()))
    try:
        with open(tmp, "w") as f:
            f.write("\n".join(L) + "\n")
        os.replace(tmp, os.path.join(WS, "READINGS.md"))
    except Exception as e:
        log("READINGS.md write failed: %s" % e)

# ---------------------------------------------------------------- run state (unit threads + scheduler share it under STATE_LOCK)
STATE_F = os.path.join(PD, "state.json")
RUN = {"plan": [], "grades": {}, "failed": [], "replans": 0, "retired": [], "requirements": [], "readings": [], "scouts": 0}

def checkpoint():
    """plan.json (the units array, disk is truth) + state.json, atomically. The live dicts are snapshotted under STATE_LOCK with the C encoder
    (the pure-Python indent encoder raised on concurrent mutation) and written from the snapshot."""
    with STATE_LOCK:
        try:
            snap_plan = json.loads(json.dumps(RUN["plan"], default=str))
            snap_state = json.loads(json.dumps({"plan": RUN["plan"], "grades": RUN["grades"], "failed": RUN["failed"], "replans": RUN["replans"],
                                                "retired": RUN["retired"], "exec_sids": EXEC_SIDS, "report": REPORT, "override": OVERRIDE.is_set(),
                                                "last_look": LAST_LOOK, "ts": int(time.time())}, default=str))
        except Exception as e:
            log("state snapshot failed: %s" % e)
            return
        replanning = REPLANNING.is_set()
    try:
        if not replanning:
            _atomic_json_dump(snap_plan, os.path.join(PD, "plan.json"))
    except Exception as e:
        log("plan.json rewrite failed: %s" % e)
    try:
        _atomic_json_dump(snap_state, STATE_F)
    except Exception as e:
        log("state save failed: %s" % e)

def _handoff_summary(uid, limit=500):
    """One line about a unit's handoff.json for the planner / report prompts (the file itself is what agents read)."""
    try:
        h = json.load(open(os.path.join(unit_dir(uid), "handoff.json")))
        return "%d deliverable(s); key_numbers %s; known_limits %s" % (len(h.get("deliverables") or []), json.dumps(h.get("key_numbers"), ensure_ascii=False)[:limit],
                                                                       str(h.get("known_limits"))[:200])
    except Exception:
        return "(no readable handoff.json)"

def _minimal_plan():
    """No valid plan inside the planning budget: one produce unit for the whole figure + the render unit (generic; better than no attempt)."""
    wall_min = int(max(60, remaining() // 60))
    return [{"id": "u1", "type": "produce", "goal": "Reproduce every required series of the task book's figure with the method the task book names: "
             "validate the production code path on the smallest exactly solvable case, time one production step at the largest required value, "
             "then produce the full required set at the precision the budget allows, one file per point.",
             "deliverables": ["/workspace/u1/data/", "/workspace/u1/run.py"], "done_criteria": ["every required series of the task book has data files under /workspace/u1/data/"],
             "depends_on": [], "cores": max(1, NCPUS - 2), "mem_gb": max(1, int((MEM_GB or 16) * 0.8)), "alloc_min": max(30, int(wall_min * 0.55)),
             "scale_knobs": ["sample counts, tolerances and grid density the task book leaves free"]},
            {"id": "render", "type": "render", "goal": "Render the figure and write report.md from the data on disk.", "deliverables": ["/workspace/render/report.md"],
             "done_criteria": ["report.md accounts for every task-book item"], "depends_on": ["u1"], "cores": 2, "mem_gb": 8, "alloc_min": REPORT_MIN}]

def _repair_plan(obj):
    """The last parseable plan with its structural errors mended (ids, deliverables outside the unit, unknown or late dependencies, missing
    needs / alloc / criteria, resource overshoots, the render unit) - used when every planner attempt was rejected."""
    units = split_plan(obj)[0] or []
    units = [dict(u) for u in units if isinstance(u, dict) and isinstance(u.get("id"), str) and _UID_RX.match(u["id"]) and u.get("type") != "render"]
    seen, out = set(), []
    for u in units:
        if u["id"] in seen or u["id"] == "render":
            continue
        seen.add(u["id"])
        u["type"] = "produce"
        dl = [d for d in (u.get("deliverables") or []) if isinstance(d, str) and _in_unit_dir(u["id"], d.strip().rstrip("/")) and not d.strip().endswith("handoff.json")]
        u["deliverables"] = dl or ["/workspace/%s/data/" % u["id"]]
        if not isinstance(u.get("done_criteria"), list) or not u["done_criteria"]:
            u["done_criteria"] = ["every deliverable exists and follows the task book"]
        u["goal"] = str(u.get("goal") or "produce the required rows this unit owns")
        try:
            u["cores"] = max(1, min(NCPUS, int(u.get("cores") or 4)))
        except Exception:
            u["cores"] = 4
        try:
            u["mem_gb"] = max(1, min(MEM_GB or 10 ** 6, int(float(u.get("mem_gb") or 8))))
        except Exception:
            u["mem_gb"] = 8
        try:
            u["alloc_min"] = max(10, int(float(u.get("alloc_min"))))
        except Exception:
            u["alloc_min"] = 60
        out.append(u)
    ids = set(u["id"] for u in out)
    for u in out:
        u["depends_on"] = [d for d in (u.get("depends_on") if isinstance(u.get("depends_on"), list) else []) if isinstance(d, str) and d in ids and d != u["id"]]
        nd = u.get("needs") if isinstance(u.get("needs"), dict) else {}
        u["needs"] = dict((d, nd.get(d) if need_paths(nd.get(d), d) else "/workspace/%s/ - the upstream deliverables" % d) for d in u["depends_on"])
    ordered, placed = [], set()
    while len(ordered) < len(out):   # dependencies first (a cycle is broken by dropping the back edges)
        progress = False
        for u in out:
            if u["id"] not in placed and all(d in placed for d in u["depends_on"]):
                ordered.append(u); placed.add(u["id"]); progress = True
        if not progress:
            for u in out:
                if u["id"] not in placed:
                    u["depends_on"] = [d for d in u["depends_on"] if d in placed]
                    u["needs"] = dict((d, v) for d, v in u["needs"].items() if d in placed)
                    ordered.append(u); placed.add(u["id"])
                    break
    if not ordered:
        return None
    ordered.append({"id": "render", "type": "render", "goal": "Render the figure and write report.md from the data on disk.", "deliverables": ["/workspace/render/report.md"],
                    "done_criteria": ["report.md accounts for every task-book item"], "depends_on": [u["id"] for u in ordered], "cores": 2, "mem_gb": 8})
    rq = split_plan(obj)[1] or []
    rq = [r for r in rq if isinstance(r, dict) and isinstance(r.get("owner"), str) and (r["owner"] in ids or r["owner"] == "render")]
    rd = split_plan(obj)[2] or []
    return {"requirements": rq, "readings": rd, "units": ordered}

def _accept_plan(units, reqs, reads, obj, r, attempt, warns, replan):
    """Publish a validated plan: plan_full.json (the planner's object), plan.json (units array), requirements.json, READINGS.md."""
    with STATE_LOCK:
        RUN["requirements"] = list(reqs or [])
        n_read = len(RUN.get("readings") or [])
        if not replan:
            RUN["readings"] = [dict(x, version=(int(x.get("version")) if str(x.get("version") or "").isdigit() else 1)) for x in (reads or [])]
        else:   # the live readings stand (in-run changes included); a replan may only add new ids
            have = set(str(x.get("id")).upper() for x in (RUN.get("readings") or []))
            RUN["readings"] = list(RUN.get("readings") or []) + [dict(x, version=1) for x in (reads or []) if str(x.get("id")).upper() not in have]
    try:
        _atomic_json_dump({"requirements": RUN["requirements"], "readings": RUN["readings"], "units": units}, os.path.join(PD, "plan_full.json"))
        _atomic_json_dump(units, os.path.join(PD, "plan.json"))
        _atomic_json_dump(RUN["requirements"], os.path.join(PD, "requirements.json"))
    except Exception as e:
        log("plan files write failed: %s" % e)
    if not replan or len(RUN["readings"]) != n_read:
        write_readings(RUN["readings"])
    if not replan and not RUN.get("report_cost_min"):
        RUN["plan"] = units
        RUN["report_cost_min"] = report_cost_min()   # frozen: a later replan never moves the override instant
    ledger({"kind": "plan", "ok": True, "units": [u["id"] for u in units], "replan_of": replan["uid"] if replan else None,
            "requirements": len(RUN["requirements"]), "core_rows": sum(1 for x in RUN["requirements"] if x.get("core")),
            "readings": len(RUN["readings"]),
            "alloc_min": dict((u.get("id"), plan_alloc_min(u)) for u in units), "critical_path_min": critical_path(units)[0],
            "shape": dict((k, v) for k, v in plan_shape(units).items() if k != "fan_in"), "tokens": (r or {}).get("tokens", 0),
            "attempt": attempt, "warnings": [w[:300] for w in warns[:8]], "types": [u.get("type") for u in units]})
    log(plan_summary(units))

class _PlanNotWritten(ValueError):
    pass


def get_plan(replan=None, old_plan=None):
    """Planner call(s) + validation; an invalid plan goes back with the error list (PLAN_REWRITES times, PLAN_TOTAL_S in total). The last
    attempt accepts the planning contract's soft errors as warnings. A dead backend -> None (infra). No valid first plan -> a minimal plan."""
    t_begin = time.time()
    if replan:
        old = [u for u in (old_plan or []) if isinstance(u, dict)]
        x = replan["uid"]
        txt = read(os.path.join(unit_dir(x), "residual.md"), "").strip() or read(os.path.join(unit_dir(x), "critic_report.md"), "(no residual.md / critic_report.md)").strip()
        done = [u["id"] for u in old if u.get("status") == "done"]
        base = REPLAN_PROMPT.format(uid=x, reason=replan.get("reason", "failed"), blocked=", ".join(replan.get("blocked") or []) or "(none)",
                                    status_table=unit_status_table(), residual=txt[:6000] + ("\n[...truncated - read the file]" if len(txt) > 6000 else ""),
                                    done_block="\n".join("  %s -> /workspace/%s/ : %s" % (d, d, _handoff_summary(d)) for d in done) or "  (none)",
                                    retired=", ".join(u["id"] for u in old if u.get("status") in HISTORY) or "(none)") + notes_block("planner_notes", PLANNER_NOTES_TITLE)
        total = min(PLAN_TOTAL_S, 1800)
    else:
        base = (PLAN_PROMPT % {"ncpus": NCPUS, "mem": (MEM_GB if MEM_GB > 0 else "node memory"), "rmin": REPORT_MIN, "maxu": MAX_UNITS}) + notes_block("planner_notes", PLANNER_NOTES_TITLE)
        total = PLAN_TOTAL_S
    prompt, attempt, max_attempts, last_obj, budget, wrap = base, 0, PLAN_REWRITES + 1, None, PLAN_T, (300 if replan else PLAN_WRAP_FIRST_T)
    while attempt < max_attempts:
        attempt += 1
        left = total - (time.time() - t_begin)
        if attempt > 1 and left < 600:
            break
        if replan:
            REPLANNING.set()
        before = _mtime(os.path.join(PD, "plan.json"))
        try:
            with PLANNER_LOCK:
                r = run_call(prompt, int(min(budget, max(600, left))), "replan" if replan else "plan", wrapup=WRAP_PLAN, wrap_t=wrap)
            raw = read(os.path.join(PD, "plan.json"), "null")
        finally:
            REPLANNING.clear()
        if r.get("infra") or INFRA["dead"]:
            ledger({"kind": "plan", "ok": False, "infra": True, "replan_of": replan["uid"] if replan else None})
            return None
        try:
            if _mtime(os.path.join(PD, "plan.json")) in (None, before):   # not (re)written: the file on disk is not its answer
                raise _PlanNotWritten("- /workspace/_pece/plan.json was not written in this attempt")
            obj = json.loads(raw)
            if split_plan(obj)[0]:
                last_obj = obj
            strict = attempt < max_attempts and (total - (time.time() - t_begin)) >= 900
            errs, warns, units, reqs, reads = validate_plan(obj, old_plan=old_plan if replan else None, strict=strict)
            for w in warns:
                log("plan warning: %s" % w)
            if errs:
                raise ValueError("\n".join("- " + e for e in errs))
            _accept_plan(units, reqs, reads, obj, r, attempt, warns, replan)
            if PLAN_ONLY:
                open(os.path.join(PD, "plan_summary.txt"), "w").write(plan_summary(units) + "\nvalidation: OK (attempt %d)%s\n" % (
                    attempt, ("; warnings:\n  " + "\n  ".join(warns)) if warns else ""))
                log("PLAN-ONLY diagnostic: plan written and validated, exiting before execution")
                sys.exit(0)
            return units
        except SystemExit:
            raise
        except Exception as e:
            log("plan.json invalid (attempt %d):\n%s" % (attempt, e))
            ledger({"kind": "plan_invalid", "attempt": attempt, "replan_of": replan["uid"] if replan else None, "errors": str(e)[:1500]})
            if OVERRIDE.is_set():
                break
            if isinstance(e, _PlanNotWritten):   # nothing to edit: a fresh session writes the plan first and refines it after
                prompt = (base + "\n\nThe previous planner session ended WITHOUT writing /workspace/_pece/plan.json. Write it now, before anything "
                          "else: the TASK BOOK items in the header are what will be judged - write a complete plan from them first, read the paper "
                          "only for a method question they leave open, then refine the written file if time remains (attempt %d of %d)."
                          % (attempt + 1, max_attempts))
                budget, wrap = (PLAN_T // 2, 600) if not replan else (PLAN_FIX_T, 300)
            else:
                why = str(e)
                if not (why.startswith("- ") or isinstance(e, json.JSONDecodeError)):   # a Python error inside the check: name the type rules
                    why = ("- the orchestrator could not read a field of plan.json (%s: %s) - a field has the wrong type: a requirement's owner is ONE "
                           "unit id string; a unit's depends_on, deliverables, done_criteria and notes are lists of strings; cores and mem_gb are "
                           "numbers; a reading's check_by is one unit id and its version an integer" % (type(e).__name__, why[:200]))
                prompt = (base + "\n\nYour previous /workspace/_pece/plan.json was REJECTED. The orchestrator's check reported:\n%s\nFix exactly these "
                          "points by EDITING /workspace/_pece/plan.json in place - keep every other unit, requirement and reading as it is, do not plan "
                          "again from scratch (attempt %d of %d)." % (why[:3000], attempt + 1, max_attempts))
                budget, wrap = PLAN_FIX_T, 300   # a fix attempt edits the file: short call, short pens-down
    ledger({"kind": "plan", "ok": False, "replan_of": replan["uid"] if replan else None})
    if replan:
        return None
    if PLAN_ONLY:
        log("PLAN-ONLY diagnostic: no valid plan")
        sys.exit(2)
    if last_obj is not None:
        try:   # best effort: a plan the check cannot even read falls through to the minimal plan, never ends the episode
            fixed = _repair_plan(last_obj)
            if fixed:
                errs, warns, units, reqs, reads = validate_plan(fixed, strict=False)
                if not errs:
                    ledger({"kind": "plan_repaired", "units": [u["id"] for u in units]})
                    log("no plan passed the check -> the last parseable plan, mended, is used")
                    _accept_plan(units, reqs, reads, fixed, None, attempt, warns + ["repaired"], None)
                    return units
        except Exception as e:
            log("mending the last parseable plan failed: %s" % e)
            ledger({"kind": "plan_repair_error", "error": str(e)[:300]})
    units = _minimal_plan()
    ledger({"kind": "plan_minimal", "units": [u["id"] for u in units]})
    log("no parseable plan inside the planning budget -> the minimal plan (one produce unit + render)")
    _accept_plan(units, [], RUN.get("readings") or [], None, None, attempt, ["minimal plan"], None)
    return units

def owned_rows(uid):
    return [r for r in (RUN.get("requirements") or []) if r.get("owner") == uid]

def _rows_text(rows):
    return "\n".join("- %s%s (%s): %s%s" % (r.get("id"), " [CORE]" if r.get("core") else "", r.get("F") or "-", str(r.get("what"))[:300],
                                            (" - precision target: %s" % str(r.get("precision_target"))[:200]) if r.get("precision_target") else "")
                     for r in rows)

def write_spec(u):
    """/workspace/<id>/spec.md - the unit's own brief (rewritten at launch and on every restart)."""
    ud = unit_dir(u["id"])
    os.makedirs(ud, exist_ok=True)
    deps = [d for d in (u.get("depends_on") or [])]
    rows = (RUN.get("requirements") or []) if u.get("type") == "render" else owned_rows(u["id"])
    lines = ["# Unit %s\n" % u["id"], "## Goal\n%s\n" % u["goal"],
             "## Type / resources\ntype: %s; cores: %s; mem_gb: %s; depends_on: %s%s\n"
             % (u.get("type"), u.get("cores"), u.get("mem_gb"), ", ".join(str(x) for x in deps) or "(none)",
                ("; scale_knobs: %s" % json.dumps(u.get("scale_knobs"), ensure_ascii=False)) if u.get("scale_knobs") else ""),
             "## Directory\n/workspace/%s/ - your working directory; code, data, logs, profile.json and handoff.json go here.\n" % u["id"],
             "## Clock\nalloc_min: %s (minutes of wall planned for this unit); t_launch: %d (epoch seconds; the live figures are in "
             "/workspace/_pece/clock.json).\n" % (u.get("alloc_min") if u.get("alloc_min") is not None else "(not set)", int(u.get("t_launch") or time.time())),
             ("## Required rows %s (never optional; the precision target is the error per plotted point the claim needs)\n%s\n"
              % ("of the whole figure - account for every one in report.md" if u.get("type") == "render" else "this unit owns", _rows_text(rows))) if rows else "",
             "## Readings\nUse /workspace/READINGS.md as written (shared by every unit); report evidence against a reading in handoff.json \"reading_issue\".\n",
             ("## Readings this unit settles FIRST (before any production run: run each check on the smallest exact case and write the verdict into "
              "handoff.json \"readings_verdict\": [{\"id\", \"verdict\", \"evidence\": \"<check file>\"}])\n%s\n"
              % "\n".join("- %s: options %s; check: %s" % (x.get("id"), json.dumps(x.get("options") or [], ensure_ascii=False), x.get("check"))
                           for x in (RUN.get("readings") or []) if x.get("check_by") == u["id"])) if any(x.get("check_by") == u["id"] for x in (RUN.get("readings") or [])) else "",
             "## Deliverables\n" + "\n".join("- %s" % d for d in u["deliverables"]),
             "\n## Done criteria\n" + "\n".join("- %s" % c for c in u["done_criteria"]),
             ("\n## Planner notes (numbers without a task-book item - information, not criteria)\n" + "\n".join("- %s" % c for c in (u["notes"] if isinstance(u["notes"], list) else [u["notes"]]))) if u.get("notes") else "",
             "\n## Judged against\nThe TASK BOOK block in every prompt header (CLAUDE.md's judged sections, numbered F1..Fn); the critic audits provenance "
             "(G1), implementation (G2) and the task-book items and required rows this unit serves (G3).",
             "\n## Inputs\n" + ("\n".join("- %s -> /workspace/%s/ (%s): read /workspace/%s/handoff.json first%s%s"
                                           % (d, d, ("DONE WITH GAP" if (unit_by_id(d) or {}).get("gap") else "final" if (unit_by_id(d) or {}).get("status") == "done" else "provisional"), d,
                                              ("; needs: %s" % (u.get("needs") or {}).get(d)) if isinstance(u.get("needs"), dict) and (u.get("needs") or {}).get(d) else "",
                                              (" [GAP: this upstream ended with an unmet item (%s) - its data are real; never fill, rescale or calibrate it]" % (unit_by_id(d) or {}).get("gap_note", "")[:300]) if (unit_by_id(d) or {}).get("gap") else "")
                                           for d in deps) if deps else "(no upstream units)")]
    if u.get("restarts"):
        lines.append("\n## Earlier rounds\n" + "\n".join("- %s/ (residual.md inside)" % a for a in (u.get("archives") or [])))
    with open(os.path.join(ud, "spec.md"), "w") as f:
        f.write("\n".join(x for x in lines if x is not None) + "\n")
    tp = os.path.join(ud, "handoff_template.json")
    if not os.path.exists(tp):
        json.dump({"uid": u["id"], "deliverables": [{"path": d, "what": "<one phrase>"} for d in (u.get("deliverables") or [])],
                   "key_numbers": {"<name>": "<value or 'value (where saved)'>"},
                   "criteria": [{"item": "F<n>", "value": "<measured>", "target": "<the item's number>", "pass": True}],
                   "how_to_use": "<imports, entry points, file formats>", "checks": [{"cmd": "<command run>", "observed": "<actual output>"}],
                   "known_limits": "<anything reduced, approximated or uncertain - or 'none'>", "stopped": None,
                   "_note": "delete this key; add \"waiting\": {\"pid\": <int>, \"done_marker\": \"/workspace/%s/bg.done\", \"expect_min\": <minutes>, \"resume_note\": \"...\"} only while a detached job runs; \"reading_issue\" only with evidence" % u["id"]},
                  open(tp, "w"), indent=1)

def handoff_block(uids, title="UPSTREAM HANDOFFS", limit=5000):
    """The handoff.json of the given units, verbatim (truncated), as one prompt block (the critic's copy of the audited claim)."""
    L = ["%s (%d unit%s; each block is /workspace/<id>/handoff.json verbatim - the FILES it lists are the truth, "
         "the prose is a claim):" % (title, len(uids), "" if len(uids) == 1 else "s")]
    for x in uids:
        txt = read(os.path.join(unit_dir(x), "handoff.json"), "").strip()
        if not txt:
            txt = "(no handoff.json on disk)"
        if len(txt) > limit:
            txt = txt[:limit] + "\n[...truncated - read the file]"
        L.append("--- %s ---\n%s" % (x, txt))
    return "\n".join(L)
