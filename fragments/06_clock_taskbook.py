
# ---------------------------------------------------------------- the clock: ONE usable-time figure for every budget
def remaining(): return WALL - (time.time() - T0)

def _report_min():   # the report reserve: RA3SR_REPORT_MIN (default 75 min = a report + its audit + one fix, clamped 30..90); the plan's render unit may override
    try:
        return max(REPORT_COST_BOUNDS[0], min(REPORT_COST_BOUNDS[1], int(float(os.environ.get("RA3SR_REPORT_MIN", "75")))))
    except Exception:
        return 75
REPORT_MIN = _report_min()

def report_reserve_s():
    """The report cost, held until the report agent has started (then 0); 0 in PLAN_ONLY."""
    return 0 if (PLAN_ONLY or REPORT["started"]) else report_cost_min() * 60

def override_due():
    """The single time trigger: report not started AND remaining <= report cost + FLUSH_WINDOW."""
    return (not REPORT["started"]) and remaining() <= report_cost_min() * 60 + FLUSH_WINDOW

def usable_left():
    """Seconds until the override instant: the wall a unit can still use. Every budget (exec BUDGET, clock.json spare_s, fix slices, the
    checkpoint floor, the close rule, scouting) reads this one figure. Once the report has started it is 0 (no unit decides any more)."""
    if REPORT["started"]:
        return 0
    return remaining() - report_reserve_s() - FLUSH_WINDOW

# ---------------------------------------------------------------- task book = CLAUDE.md verbatim, numbered F1..Fn
_TASK_BOOK = {"mtime": None, "items": [], "context": [], "text": "", "warned": False}

TASK_BOOK_MAX = int(os.environ.get("RA3SR_TASK_BOOK_MAX", "64000"))   # header cap of the TASK BOOK block (a truncation is logged once)

_TB_SECTIONS = ("objective", "general guidelines", "figure fidelity standard", "paper-specific preliminaries", "deliverables")   # the Objective is judged, not context

_TB_CONTEXT = ()

_TB_SKIP = ("paper",)   # every other non-judged top-level section reaches every role as unnumbered context


def task_book_items():
    """[(id, section, text)] - every non-empty, non-heading line of CLAUDE.md's judged sections, in file order, numbered F1..Fn by
    the orchestrator; bullet markers stripped, HTML comments skipped. Cached per CLAUDE.md mtime together with the header text
    and the other (unnumbered) top-level sections."""
    p = os.path.join(WS, "CLAUDE.md")
    try:
        mt = os.stat(p).st_mtime
    except OSError:
        return []
    if _TASK_BOOK["mtime"] == mt and _TASK_BOOK["items"] is not None and _TASK_BOOK["text"]:
        return _TASK_BOOK["items"]
    items, ctx, out, extra = [], [], [], []
    sec, sub, in_comment, n, sec_title = None, None, False, 0, None
    for raw in read(p).splitlines():
        line = raw.rstrip()
        st = line.strip()
        if in_comment:
            if "-->" in st:
                in_comment = False
            continue
        if st.startswith("<!--"):
            if "-->" not in st:
                in_comment = True
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", st)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            if level <= 2:
                sec, sub, sec_title = title.lower(), None, title
                if sec in _TB_SECTIONS:
                    out.append("## " + title)
            else:
                sub = title
                if sec in _TB_SECTIONS:
                    out.append("### " + title)
            continue
        if not st:
            continue
        if sec in _TB_CONTEXT:
            ctx.append(re.sub(r"^[-*+]\s+", "", st))
            continue
        if sec not in _TB_SECTIONS:
            if sec and sec not in _TB_SKIP:
                extra.append((sec_title, re.sub(r"^[-*+]\s+", "", st)))
            continue
        n += 1
        text = re.sub(r"^[-*+]\s+", "", st)
        items.append(("F%d" % n, (sub or sec), text))
        out.append("F%d: %s" % (n, text))
    _TASK_BOOK.update({"mtime": mt, "items": items, "context": ctx, "text": "\n".join(out), "extra": extra})
    return items


def task_book(max_chars=None):
    """Prompt-header block: CLAUDE.md's judged sections verbatim, numbered, the Objective as context, the report reserve.
    The cap is TASK_BOOK_MAX (64000; RA3SR_TASK_BOOK_MAX); a truncation is logged once (task_book_truncated)."""
    max_chars = TASK_BOOK_MAX if max_chars is None else max_chars
    items = task_book_items()
    if not items:
        return ""
    L = ["TASK BOOK (CLAUDE.md verbatim, %d items numbered F1..F%d by the orchestrator - cite items as Fn and quote the "
         "line). Judged: the figure's organizing claim (Objective), the method (no substituted algorithm), every numeric checkpoint, "
         "every required series and panel, the deliverables; documentation does not excuse a violated item. A window or tolerance the text states "
         "is a gate - a quantity's plotted window (its axis range) included; display settings, 'approximately' values and layout wording are targets to "
         "plot against; a series the text explicitly permits at reduced scale is judged at that scale, and no required value is removed by it:" % (len(items), len(items))]
    if _TASK_BOOK["context"]:
        L.append("CONTEXT (Objective): " + " | ".join(_TASK_BOOK["context"])[:1500])
    L.append(_TASK_BOOK["text"])
    if _TASK_BOOK.get("extra"):
        L.append("OTHER TASK-FILE SECTIONS (CLAUDE.md verbatim, not numbered - they are still part of the task):")
        last = None
        for t, line in _TASK_BOOK["extra"]:
            if t != last:
                L.append("## " + t)
                last = t
            L.append(line)
    L.append("REPORT RESERVE: %d min to render + write report.md (the override keeps it)." % report_cost_min())
    t = "\n".join(L)
    if len(t) <= max_chars:
        return t
    if not _TASK_BOOK.get("warned"):   # never silent - the tail (end of the preliminaries, deliverables) is what gets lost
        _TASK_BOOK["warned"] = True
        try:
            ledger({"kind": "task_book_truncated", "chars": len(t), "cap": max_chars, "items": len(items)})
            log("TASK BOOK header is %d chars, cap %d -> truncated (raise RA3SR_TASK_BOOK_MAX)" % (len(t), max_chars))
        except Exception:
            pass
    return t[:max_chars - 20] + "\n  [...truncated]"


def report_cost_min():
    """The report reserve in minutes: REPORT_MIN, raised (never lowered) by the first plan's render unit (report_cost_min, clamped 30..90) and
    frozen then (a replan never moves the override instant)."""
    if RUN.get("report_cost_min"):
        return RUN["report_cost_min"]
    try:
        for u in (RUN.get("plan") or []):
            if isinstance(u, dict) and u.get("type") == "render" and u.get("report_cost_min") is not None:
                v = int(float(u.get("report_cost_min")))
                return max(REPORT_MIN, min(REPORT_COST_BOUNDS[1], v))
    except Exception:
        pass
    return REPORT_MIN
