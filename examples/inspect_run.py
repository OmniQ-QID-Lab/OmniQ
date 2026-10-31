#!/usr/bin/env python3
"""Print the plan, the unit grades, a timeline and the cost of one Residual Agents run.

Usage:
    python examples/inspect_run.py PATH [--events] [--no-timeline] [--rates U,C,O]

PATH is either a task folder of the trajectory dataset (the folder that holds run/)
or a run folder itself (the folder that was mounted at /workspace during the run;
it holds _pece/).

The script only reads files; it never writes. It uses:
    _pece/ledger.jsonl    the orchestrator's event log (required)
    _pece/state.json      final unit status, last critic grade, settle reason
    _pece/plan_full.json  the accepted plan: requirement rows, readings, units
    _pece/logs/           file names only, to map each agent session to its log
    token_usage.json      RA's own token and cost totals, for comparison
    run_meta.json         in a dataset task folder: checkpoint and verdict

Options:
    --events        print every ledger event in the timeline, not only sessions and decisions
    --no-timeline   skip the timeline
    --rates U,C,O   US$ per million tokens for uncached input, cached input and output
                    (default: the rate card used in the paper, 0.15,0.003,0.60)

Python 3.8 or newer, standard library only.
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys

PAPER_RATES = (0.15, 0.003, 0.60)   # US$ per million tokens: uncached input, cached input, output (output includes reasoning)

NOTE_ORDER = ("planner_notes", "critic_notes", "residual_bank", "libraries")
MEMORY_SETS = {   # ledger hashes of the four memory files (sha256 of the stripped text, first 8 hex digits)
    ("28d0e828", "c8521f77", "24ca922e", "a3a681e8"): "the memories shipped in residual_agents/memories",
}

# Session tag prefix -> role. The order matters: longer prefixes first.
ROLES = (("criticsel", "panel"), ("criticcp", "critic-checkpoint"), ("criticd", "critic-delta"),
         ("criticfmt", "critic-format"), ("critic", "critic"), ("scout", "probe agent"),
         ("replan", "planner"), ("plan", "planner"), ("report", "report agent"),
         ("exec", "executor"), ("restart", "executor"), ("cont", "executor"),
         ("resume", "executor"), ("fix", "executor"))
TAG_SUFFIX = re.compile(r"(_wrapup|_gwretry\d*|_only)+$")

SETTLE_WORDS = {
    "A": "passed review",
    "reaudit_A": "passed again after a re-check",
    "gap_no_time": "time ran out; kept with a known gap",
    "b_all_pass_no_time": "time ran out with all gates passing",
    "g1_no_time": "time ran out with a provenance (G1) hit",
    "gap_unaudited": "time ran out; handoff never reviewed",
    "gap_unaudited_checkpoint": "time ran out; last look was a checkpoint C",
    "no_time": "time ran out",
    "D": "retired on task-book grounds",
    "reopen_D_noted": "reopened; a D was noted",
    "reopen_unfinished": "reopened; not finished",
    "reopen_override": "reopened; stopped by the override",
    "override": "stopped by the end-of-run override",
    "failed": "failed",
    "thread_error": "orchestrator fault in the unit thread",
    "thread_error_kept": "orchestrator fault; result kept",
    "reopen_thread_error": "orchestrator fault after a reopen",
}


def die(msg):
    sys.stderr.write("inspect_run: %s\n" % msg)
    sys.exit(2)


def find_run(path):
    """The run folder: PATH itself or PATH/run, whichever holds _pece/ledger.jsonl."""
    base = os.path.abspath(path)
    for cand in (base, os.path.join(base, "run")):
        if os.path.isfile(os.path.join(cand, "_pece", "ledger.jsonl")):
            return cand
    die("no _pece/ledger.jsonl in %s or in %s" % (base, os.path.join(base, "run")))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)        # Python's json also accepts the NaN/Infinity some agent files hold
    except (OSError, ValueError):
        return None


def read_ledger(path):
    """[(line number, event)] for every line that parses as a JSON object; the count of other lines."""
    events, skipped = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(e, dict):
                events.append((n, e))
            else:
                skipped += 1
    return events, skipped


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def role_of(tag):
    base = TAG_SUFFIX.sub("", tag or "")
    for prefix, role in ROLES:
        if base.startswith(prefix):
            return role
    return "other"


def unit_of(tag):
    base = TAG_SUFFIX.sub("", tag or "")
    if re.match(r"(plan|replan)$", base):
        return None
    m = re.match(r"[a-z]+\d*_(.+)$", base)
    return m.group(1) if m else None


def hm(seconds):
    s = int(round(max(0.0, seconds)))
    return "%d:%02d" % (s // 3600, (s % 3600) // 60)


def utc(ts):
    if not ts:
        return "?"
    return datetime.datetime.fromtimestamp(num(ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tok(n):
    n = num(n)
    if n >= 1e9:
        return "%.2f G" % (n / 1e9)
    if n >= 1e6:
        return "%.2f M" % (n / 1e6)
    if n >= 1e3:
        return "%.1f k" % (n / 1e3)
    return "%d" % n


def first_sentence(text, width):
    t = " ".join(str(text or "").split())
    m = re.match(r"(.{20,}?[.;])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= width else t[:width - 3] + "..."


def gates_text(g):
    if not isinstance(g, dict):
        return ""
    return " ".join("%s %s" % (k, g.get(k)) for k in ("G1", "G2", "G3") if k in g)


def log_map(run):
    """tag -> log file names in launch order. The k-th log of a tag belongs to the k-th 'call' event of that tag."""
    d = os.path.join(run, "_pece", "logs")
    try:
        names = [n for n in os.listdir(d) if re.match(r"\d+_.+\.log$", n)]
    except OSError:
        return {}
    names.sort(key=lambda n: int(n.split("_", 1)[0]))   # numeric order: as text, 100_x sorts before 10_x
    by_tag = collections.defaultdict(list)
    for n in names:
        by_tag[n.split("_", 1)[1][:-4]].append(n)
    return by_tag


class Run(object):
    def __init__(self, path):
        self.dir = find_run(path)
        self.task = os.path.dirname(self.dir) if os.path.basename(self.dir) == "run" else None
        pece = os.path.join(self.dir, "_pece")
        self.events, self.skipped = read_ledger(os.path.join(pece, "ledger.jsonl"))
        self.state = read_json(os.path.join(pece, "state.json")) or {}
        self.plan = read_json(os.path.join(pece, "plan_full.json")) or {}
        self.usage_file = read_json(os.path.join(self.dir, "token_usage.json"))
        self.meta = read_json(os.path.join(self.task, "run_meta.json")) if self.task else None
        self.start = self.first("episode_start")
        self.end = self.last("episode_end")
        self.t0 = num(self.start.get("ts")) if self.start else num(self.events[0][1].get("ts")) if self.events else 0.0
        self.calls = self._calls()

    def first(self, kind):
        return next((e for _, e in self.events if e.get("kind") == kind), None)

    def last(self, kind):
        return next((e for _, e in reversed(self.events) if e.get("kind") == kind), None)

    def kinds(self, kind):
        return [e for _, e in self.events if e.get("kind") == kind]

    def _calls(self):
        logs, seen, out = log_map(self.dir), collections.Counter(), []
        for line, e in self.events:
            if e.get("kind") != "call":
                continue
            tag = str(e.get("tag") or "")
            names = logs.get(tag, [])
            log = names[seen[tag]] if seen[tag] < len(names) else None
            seen[tag] += 1
            u = e.get("usage") if isinstance(e.get("usage"), dict) else {}
            out.append({"line": line, "tag": tag, "role": role_of(tag), "unit": unit_of(tag),
                        "log": log, "nn": log.split("_", 1)[0] if log else "--",
                        "end": num(e.get("ts")), "dur": num(e.get("dur")), "rc": e.get("rc"),
                        "tokens": num(e.get("tokens")), "cost": e.get("cost_usd"),
                        "input": num(u.get("input")), "cached": num(u.get("cached")),
                        "output": num(u.get("output")), "reasoning": num(u.get("reasoning"))})
        return out


def section(title):
    print()
    print(title)
    print("-" * len(title))


def print_header(r):
    s, e = r.start or {}, r.end or {}
    section("Run")
    print("run id      %s (registry id %s)" % (s.get("episode", "?"), s.get("paper", "?")))
    if r.meta:
        m = r.meta
        if m.get("paper_number") is not None:
            print("paper       #%s %s" % (m.get("paper_number"), first_sentence(m.get("title"), 90)))
        bits = [
                ("model checkpoint", m.get("model_checkpoint")), ("split", m.get("split"))]
        print("provenance  " + "; ".join("%s %s" % (k, v) for k, v in bits if v not in (None, "")))
        if m.get("judge_score") is not None:
            print("verdict     strict score %s (%s)" % (m.get("judge_score"), m.get("verdict_source", "source not given")))
    gpus = s.get("gpus") or []
    print("model       %s through dsh %s; vision %s" % (s.get("model", "?"), s.get("dsh_version", "?"),
                                                       "on" if s.get("vision_ok") else "off"))
    print("resources   %s CPU cores, %s GB memory, %s" % (s.get("ncpus", "?"), s.get("mem_gb", "?"),
                                                         ", ".join(str(g) for g in gpus) if gpus else "no GPU"))
    used = num(e.get("ts")) - r.t0 if e else None
    print("time        budget %s h:mm; used %s; start %s; end %s" % (hm(num(s.get("wall"))), hm(used) if used is not None else "?",
                                                               utc(r.t0), utc(e.get("ts")) if e else "?"))
    notes = r.first("notes_loaded")
    if notes:
        rec = notes.get("notes") or {}
        key = tuple((rec.get(n) or {}).get("sha") for n in NOTE_ORDER)
        missing = [n for n in NOTE_ORDER if not rec.get(n)]
        label = MEMORY_SETS.get(key, "missing: %s" % ", ".join(missing) if missing else "")
        print("memories    %shashes %s" % (label + "; " if label else "", " ".join(h or "-" for h in key)))
    if e:
        print("end state   %s sessions; report mode %s; override %s; deliverables %s; infra failure %s"
              % (e.get("calls", len(r.calls)), e.get("report_mode", "?"), "yes" if e.get("override") else "no",
                 "yes" if e.get("deliverables") else "no", "yes" if e.get("infra_dead") else "no"))
    else:
        print("end state   no episode_end event (the run did not finish normally)")
    if r.skipped:
        print("note        %d ledger line(s) skipped: not JSON objects" % r.skipped)


def print_plan(r):
    units = r.plan.get("units") if isinstance(r.plan, dict) else None
    section("Plan")
    if not isinstance(units, list):
        print("no _pece/plan_full.json")
        return
    accepted = r.kinds("plan")
    attempts = len(r.kinds("plan_invalid")) + len(accepted)
    print("%d units, %d requirement rows, %d readings; %d planner attempt(s); %s replan(s)"
          % (len(units), len(r.plan.get("requirements") or []), len(r.plan.get("readings") or []),
             attempts, r.state.get("replans", 0)))
    if accepted and accepted[0].get("critical_path_min") is not None:
        print("critical path %s min" % accepted[0].get("critical_path_min"))
    print()
    print("%-14s %-8s %-18s %5s %6s  %s" % ("unit", "type", "depends on", "cores", "mem GB", "goal (first sentence)"))
    for u in units:
        if not isinstance(u, dict):
            continue
        deps = ",".join(str(d) for d in (u.get("depends_on") or [])) or "-"
        print("%-14s %-8s %-18s %5s %6s  %s" % (u.get("id"), u.get("type", ""), deps[:18], u.get("cores", ""),
                                               u.get("mem_gb", ""), first_sentence(u.get("goal"), 70)))


def print_units(r):
    section("Units: final state")
    plan = r.state.get("plan") if isinstance(r.state, dict) else None
    if not isinstance(plan, list):
        print("no _pece/state.json")
        return
    grades = collections.defaultdict(list)
    for e in r.kinds("critic"):
        g = str(e.get("grade") or "?")
        grades[e.get("unit")].append(g + ("m" if e.get("by") == "mech" else ""))
    report = r.last("report_grade")   # the report unit never gets a settle reason; its last render review says how it ended
    sessions = collections.Counter(c["unit"] for c in r.calls)
    scouting = collections.Counter(e.get("unit") for e in r.kinds("scout_select"))
    reopens = collections.Counter(e.get("unit") for e in r.kinds("reopen"))
    last = r.state.get("grades") or {}
    print("%-14s %-8s %-5s %-38s %8s %6s %6s  %s" % ("unit", "status", "grade", "how it ended", "sessions", "scout", "reopen",
                                                     "grades in order"))
    for u in plan:
        if not isinstance(u, dict):
            continue
        uid = u.get("id")
        reason = u.get("settle_reason")
        if reason:
            words = SETTLE_WORDS.get(reason, reason)
        elif u.get("type") == "render" or uid == "render":
            words = ("report: review %s, then %s" % (report.get("grade"), report.get("action"))
                     if report else "report unit")
        else:
            words = "never settled"
        seq = " ".join(grades.get(uid, [])) or "-"
        print("%-14s %-8s %-5s %-38s %8d %6d %6d  %s" % (uid, u.get("status") or "-", last.get(uid) or "-", words[:38],
                                                         sessions.get(uid, 0), scouting.get(uid, 0), reopens.get(uid, 0),
                                                         seq if len(seq) <= 60 else seq[:57] + "..."))
    print()
    print("Grades: A pass, B local repair, C new approach, D retire, W checkpoint on track; 'Bm' is a repair order written")
    print("by the orchestrator's mechanical check, not a model review. 'scout' counts new-approach rounds (probe agents")
    print("and a critic panel). Unit grades are RA's internal reviews; the benchmark verdict is in judge_result.md.")


def describe(kind, e, r):
    """One line for an event in the condensed timeline, or None to skip it."""
    u = e.get("unit")
    if kind == "plan":
        return "plan accepted: %s units, %s requirement rows, %s readings" % (len(e.get("units") or []), e.get("requirements"),
                                                                          e.get("readings"))
    if kind == "plan_invalid":
        errs = e.get("errors") or ""
        n = len(errs) if isinstance(errs, list) else len([x for x in str(errs).splitlines() if x.strip().startswith("-")])
        return "plan rejected by the validator (%d error line(s))" % n
    if kind == "launch":
        return "launch %s (%s cores, %s GB%s%s)" % (u, e.get("cores"), e.get("mem_gb"),
                                                   ", provisional" if e.get("provisional") else "",
                                                   ", re-audit first" if e.get("start") == "reaudit" else "")
    if kind == "critic":
        by = {"llm": "critic", "delta": "delta re-audit", "checkpoint": "checkpoint", "mech": "mechanical check"}.get(e.get("by"), e.get("by"))
        cited = ",".join("R%s" % x for x in (e.get("residual_ids") or []))
        return "review %s: %s by %s  %s%s" % (u, e.get("grade"), by, gates_text(e.get("gates")),
                                              ("  residuals " + cited) if cited else "")
    if kind == "fix_plan":
        return "decision %s: %s" % (u, e.get("action"))
    if kind == "settle":
        reason = e.get("reason")
        return "settle %s: %s (%s)" % (u, e.get("status"), SETTLE_WORDS.get(reason, reason))
    if kind == "scout_select":
        return "panel %s: winner %s, adopted %s" % (u, e.get("winner"), e.get("adopt") or "-")
    if kind == "reading_change":
        return "reading %s changed to v%s (by %s)" % (e.get("id"), e.get("version"), e.get("by") or e.get("source"))
    if kind == "reopen":
        return "reopen %s (an input changed)" % u
    if kind == "restart":
        return "fresh executor for %s" % u
    if kind == "override":
        return "end-of-run override: open work is told to wrap up"
    if kind == "report":
        return "report %s" % ("written" if e.get("ok") else "missing")
    if kind == "report_grade":
        return "report review: %s (%s)" % (e.get("grade"), e.get("action"))
    if kind in ("infra_call_dead", "infra_failure", "infra_unit_hold"):
        return "backend problem: %s" % kind
    return None


def compact(e):
    skip = ("kind", "ts", "paper", "episode")
    parts = []
    for k, v in e.items():
        if k in skip:
            continue
        s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        parts.append("%s=%s" % (k, s if len(s) <= 60 else s[:57] + "..."))
    return " ".join(parts)


def print_timeline(r, every):
    section("Timeline (h:mm since the start, in ledger order)")
    by_line = {c["line"]: c for c in r.calls}
    for line, e in r.events:
        kind = e.get("kind")
        at = "+" + hm(num(e.get("ts")) - r.t0)
        if kind == "call":
            c = by_line[line]
            print("%7s  session %-4s %-24s %-17s %6.1f min %10s tok rc %-3s %s"
                  % (at, c["nn"], c["tag"][:24], c["role"], c["dur"] / 60.0, tok(c["tokens"]), c["rc"],
                     ("_pece/logs/" + c["log"]) if c["log"] else ""))
        elif every:
            print("%7s  L%-5d %s %s" % (at, line, kind, compact(e)))
        else:
            text = describe(kind, e, r)
            if text:
                print("%7s  %s" % (at, text))
    if r.end:
        print("%7s  end: %s sessions, US$%s in RA's ledger" % ("+" + hm(num(r.end.get("ts")) - r.t0), r.end.get("calls"),
                                                            r.end.get("cost_usd")))


def print_cost(r, rates):
    section("Tokens and cost (from the ledger's call events)")
    rows = collections.OrderedDict()
    for c in r.calls:
        x = rows.setdefault(c["role"], collections.Counter())
        x["n"] += 1
        for k in ("dur", "input", "cached", "output", "reasoning"):
            x[k] += c[k]
    total = collections.Counter()
    print("%-18s %8s %8s %12s %12s %12s %12s %9s" % ("role", "sessions", "hours", "uncached in", "cached in", "output",
                                                    "(reasoning)", "US$"))
    for role, x in sorted(rows.items(), key=lambda kv: -kv[1]["n"]):
        cost = (x["input"] * rates[0] + x["cached"] * rates[1] + x["output"] * rates[2]) / 1e6
        total.update(x)
        total["cost"] += cost
        print("%-18s %8d %8.2f %12s %12s %12s %12s %9.2f" % (role, x["n"], x["dur"] / 3600.0, tok(x["input"]), tok(x["cached"]),
                                                             tok(x["output"]), tok(x["reasoning"]), cost))
    print("%-18s %8d %8.2f %12s %12s %12s %12s %9.2f" % ("total", total["n"], total["dur"] / 3600.0, tok(total["input"]),
                                                         tok(total["cached"]), tok(total["output"]), tok(total["reasoning"]),
                                                         total["cost"]))
    print()
    print("Rates: US$%s, %s and %s per million uncached-input, cached-input and output tokens; output includes"
          % rates)
    print("reasoning, so reasoning is not billed twice. 'hours' adds up session durations; parallel sessions overlap.")
    led = sum(num(c["cost"]) for c in r.calls)
    print("Sum of the ledger's per-session cost_usd: US$%.4f." % led)
    if isinstance(r.usage_file, dict):
        print("token_usage.json (RA's own record): US$%s, %s tokens." % (r.usage_file.get("cost_usd"),
                                                                          tok(r.usage_file.get("total_tokens"))))
    print("The paper's cost basis re-settles every DSH session file, sub-agent sessions included; in five runs it is")
    print("slightly higher than RA's own record. The dataset manifest gives both (cost_usd and cost_usd_ledger).")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Print the plan, unit grades, timeline and cost of one Residual Agents run.")
    ap.add_argument("path", help="dataset task folder (holds run/) or run folder (holds _pece/)")
    ap.add_argument("--events", action="store_true", help="print every ledger event in the timeline")
    ap.add_argument("--no-timeline", action="store_true", help="skip the timeline")
    ap.add_argument("--rates", default=",".join(str(x) for x in PAPER_RATES),
                    help="US$ per million tokens: uncached input, cached input, output (default %(default)s)")
    a = ap.parse_args(argv)
    try:
        rates = tuple(float(x) for x in a.rates.split(","))
    except ValueError:
        rates = ()
    if len(rates) != 3:
        die("--rates needs three numbers, e.g. 0.15,0.003,0.60")
    r = Run(a.path)
    print("Residual Agents run: %s" % r.dir)
    print_header(r)
    print_plan(r)
    print_units(r)
    if not a.no_timeline:
        print_timeline(r, a.events)
    print_cost(r, rates)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:          # e.g. piped into head
        sys.exit(0)
