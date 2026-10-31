#!/usr/bin/env python3
"""Settle the token usage and cost of finished RA runs from their dsh session logs (the cost method of the paper).

Usage:
    settle_sessions.py [--rates FILE.json | --rates a,b,c] [--by-role] [--format csv|json] [--ledger-fallback]
                       [--out FILE] PATH [PATH ...]
PATH is one of
    a run folder (the folder that was /workspace during the run: it holds .dsh_ra3/ and _pece/);
    a task folder in the dataset layout (run/, plus an optional transcripts.zip of decoded session logs);
    a .dsh_ra3 folder;
    a transcripts.zip file (a zip of decoded session logs).

Method:
  - every session log .dsh_ra3/calls/<NN_tag>/sessions/*/*/session*.jsonl* is read, including dsh sub-agent sessions
    (their folder name lacks the "session-" prefix); files ending in .tmp (writes in progress) are skipped;
  - a log is zstd-compressed JSONL (dsh 0.1.5 writes session.v3.jsonl.zstd, several frames per file) or plain JSONL
    (decoded logs); zstd is decoded with the zstd program or the python zstandard package; a file that
    cannot be decoded is counted in undecodable_files and makes the exit status 1, it is never read as zero tokens;
  - an "assistant/message" record counts its data.usage, or else the last usage chunk of data.stream (dsh 0.1.5 keeps
    both and they repeat each other, so only one is taken); an "assistant/attempt" record (a request that dsh retried)
    counts the last usage chunk of its stream;
  - cost = (inputTokens x uncached_input + cacheReadTokens x cached_input + outputTokens x output) / 1e6 USD, rates
    in USD per million tokens. outputTokens already include reasoningTokens, which are not billed again;
    cacheWriteTokens are reported but not billed. tokens = inputTokens + cacheReadTokens + outputTokens.
Ledger cross-check: when the run folder has _pece/ledger.jsonl, the sums of its "call" events (the orchestrator's own
accounting during the run) are reported beside the settlement, re-priced at the same rates, and so is the cost in
token_usage.json. The in-run figures of the published runs leave dsh sub-agent sessions out, so such a run
settles a little higher than its ledger. With --ledger-fallback, a run without session files is reported from
its ledger (basis "ledger"); the published trajectories hold no session files, so they need this option.
Roles (--by-role) come from the tag in the call folder name, classified as the orchestrator does: plan (plan, replan),
exec (exec, fix, cont, resume, restart, bfix), critic, scout, report, other.
Exit status: 0 when every path was settled; 1 when a path had no usable data or a session file could not be decoded;
2 on a usage error. Python 3.6 or newer, standard library only (plus zstd or zstandard for compressed logs).
"""
import argparse
import csv
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RATES = os.path.join(HERE, "rates", "deepseek-v4.1-flash.json")
FIELDS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "totalTokens")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
ZIP_SESSION = re.compile(r"(?:^|/)\.dsh_ra3/calls/([^/]+)/sessions/[^/]+/([^/]+)/(session[^/]*\.jsonl[^/]*)$")
ROLES = ("plan", "exec", "critic", "scout", "report", "other")
COLUMNS = ("run", "role", "basis", "inputTokens", "cacheReadTokens", "outputTokens", "reasoningTokens",
           "cacheWriteTokens", "tokens", "usage_calls", "retried_attempts", "sessions", "subagent_sessions",
           "session_files", "undecodable_files", "cost_usd", "ledger_calls", "ledger_tokens", "ledger_cost_usd",
           "token_usage_cost_usd", "done")


def role_of(tag):
    """Call family of a call tag, as the orchestrator classifies it."""
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


def tag_of_call_folder(name):
    m = re.match(r"^\d+_(.+)$", name)
    return m.group(1) if m else name


# ---------------------------------------------------------------- decoding

def decode(data):
    """Text of a session log (bytes); None when it is zstd and cannot be decoded here."""
    if data[:4] != ZSTD_MAGIC:
        return data.decode("utf-8", "ignore")
    exe = shutil.which("zstd")
    if exe:
        try:
            r = subprocess.run([exe, "-dcq"], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "ignore")
        except Exception:
            pass
    try:
        import zstandard
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True) as zr:
            return zr.read().decode("utf-8", "ignore")
    except Exception:
        return None


def _last_stream_usage(stream):
    for rec in reversed(stream if isinstance(stream, list) else []):
        if isinstance(rec, dict) and rec.get("type") == "chunk" and isinstance(rec.get("chunk"), dict) \
                and rec["chunk"].get("type") == "usage":
            return rec["chunk"].get("usage")
    return None


def new_tot():
    t = dict((k, 0) for k in FIELDS)
    t.update({"usage_calls": 0, "retried_attempts": 0})
    return t


def add_text(tot, text):
    """Add the usage records of one decoded session log to tot."""
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        t, d = ev.get("type"), ev.get("data")
        if not isinstance(d, dict):
            continue
        if t == "assistant/message":
            u = d.get("usage") or _last_stream_usage(d.get("stream"))
            tot["usage_calls"] += 1
        elif t == "assistant/attempt":
            u = _last_stream_usage(d.get("stream"))
            tot["retried_attempts"] += 1
        else:
            continue
        if not isinstance(u, dict):
            continue
        for k in FIELDS:
            v = u.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                tot[k] += v


# ---------------------------------------------------------------- inputs

def resolve(path):
    """-> dict(label, run, dsh, zip): the run folder (for the ledger), a .dsh_ra3 folder and/or a transcripts.zip."""
    p = os.path.abspath(path)
    if os.path.isfile(p):
        if not zipfile.is_zipfile(p):
            raise ValueError("%s is not a folder or a zip file" % path)
        task = os.path.dirname(p)
        run = os.path.join(task, "run")
        return {"label": os.path.basename(task), "run": run if os.path.isdir(run) else None, "dsh": None, "zip": p}
    if not os.path.isdir(p):
        raise ValueError("%s does not exist" % path)
    if os.path.basename(p) == ".dsh_ra3":
        parent = os.path.dirname(p)
        return {"label": os.path.basename(parent), "run": parent, "dsh": p, "zip": None}
    if os.path.isdir(os.path.join(p, "run")) and not os.path.isdir(os.path.join(p, "_pece")):   # dataset task folder
        run = os.path.join(p, "run")
        z = os.path.join(p, "transcripts.zip")
        d = os.path.join(run, ".dsh_ra3")
        return {"label": os.path.basename(p), "run": run, "dsh": d if os.path.isdir(d) else None,
                "zip": z if os.path.isfile(z) else None}
    d = os.path.join(p, ".dsh_ra3")
    return {"label": os.path.basename(p), "run": p, "dsh": d if os.path.isdir(d) else None, "zip": None}


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def session_files(src):
    """[(call folder, session folder, reader)] of one resolved path; reader() returns the file's bytes."""
    out = []
    if src["dsh"]:
        for f in sorted(glob.glob(os.path.join(src["dsh"], "calls", "*", "sessions", "*", "*", "session*.jsonl*"))):
            if f.endswith(".tmp") or not os.path.isfile(f):
                continue
            sess = os.path.dirname(f)                                   # calls/<call>/sessions/<cwd>/<session>/<file>
            call = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(sess))))
            out.append((call, sess, (lambda f=f: _read_bytes(f))))
    if not out and src["zip"]:
        zf = zipfile.ZipFile(src["zip"])
        for name in sorted(zf.namelist()):
            m = ZIP_SESSION.search(name)
            if not m or name.endswith(".tmp"):
                continue
            out.append((m.group(1), name.rsplit("/", 1)[0], (lambda n=name, z=zf: z.read(n))))
    return out


def read_ledger(run, rates):
    """Sums of the ledger's call events, total and per role; None without a ledger."""
    lf = os.path.join(run or "", "_pece", "ledger.jsonl")
    if not run or not os.path.isfile(lf):
        return None
    led = {"all": {"calls": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "tokens": 0, "retried": 0}}
    with open(lf, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("kind") != "call":
                continue
            u = e.get("usage") if isinstance(e.get("usage"), dict) else {}
            for key in ("all", role_of(e.get("tag"))):
                r = led.setdefault(key, {"calls": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0,
                                         "tokens": 0, "retried": 0})
                r["calls"] += 1
                for k in ("input", "cached", "output", "reasoning"):
                    v = u.get(k)
                    r[k] += v if isinstance(v, int) and not isinstance(v, bool) else 0
                r["tokens"] += int(e.get("tokens") or 0)
                r["retried"] += int(e.get("retried_attempts") or 0)
    for r in led.values():
        r["cost_usd"] = price(r["input"], r["cached"], r["output"], rates)
    return led


def price(inp, cached, out, rates):
    return (inp * rates[0] + cached * rates[1] + out * rates[2]) / 1e6


def load_rates(spec):
    spec = spec or DEFAULT_RATES
    if re.match(r"^\s*[0-9.eE+-]+\s*,\s*[0-9.eE+-]+\s*,\s*[0-9.eE+-]+\s*$", spec):
        v = tuple(float(x) for x in spec.split(","))
        return v, {"uncached_input": v[0], "cached_input": v[1], "output": v[2], "source": "command line"}
    with open(spec) as f:
        j = json.load(f)
    v = (float(j["uncached_input"]), float(j["cached_input"]), float(j["output"]))
    return v, {"uncached_input": v[0], "cached_input": v[1], "output": v[2], "source": os.path.basename(spec)}


# ---------------------------------------------------------------- settlement

def settle(path, rates, ledger_fallback=False):
    src = resolve(path)
    files = session_files(src)
    per_role = dict((r, new_tot()) for r in ROLES)
    sessions, subagents, sess_role = set(), set(), {}
    undecodable = 0
    for call, sess, reader in files:
        text = decode(reader())
        if text is None:
            undecodable += 1
            sys.stderr.write("settle_sessions: cannot decode %s/%s (install zstd or the python zstandard package)\n"
                             % (call, os.path.basename(sess)))
            continue
        role = role_of(tag_of_call_folder(call))
        add_text(per_role[role], text)
        sessions.add(sess)
        sess_role[sess] = role
        if not os.path.basename(sess.rstrip("/")).startswith("session-"):
            subagents.add(sess)
    ledger = read_ledger(src["run"], rates)
    tu_cost = None
    tu = os.path.join(src["run"] or "", "token_usage.json")
    if src["run"] and os.path.isfile(tu):
        try:
            with open(tu) as f:
                tu_cost = json.load(f).get("cost_usd")
        except (ValueError, OSError):
            tu_cost = None
    done = os.path.isfile(os.path.join(src["run"], "report.md")) if src["run"] else None
    basis = "sessions" if files else ("ledger" if (ledger_fallback and ledger and ledger["all"]["calls"]) else "none")
    rows = []
    for role in ("all",) + ROLES:
        if role == "all":
            tot = new_tot()
            for r in ROLES:
                for k in tot:
                    tot[k] += per_role[r][k]
            n_sess, n_sub = len(sessions), len(subagents)
            n_files, n_undec = len(files), undecodable
        else:
            tot = per_role[role]
            n_sess = sum(1 for s in sessions if sess_role[s] == role)
            n_sub = sum(1 for s in subagents if sess_role[s] == role)
            n_files = n_undec = None
        led = (ledger or {}).get(role)
        row = {"run": src["label"], "role": role, "basis": basis, "done": done if role == "all" else None,
               "ledger_calls": led["calls"] if led else None, "ledger_tokens": led["tokens"] if led else None,
               "ledger_cost_usd": led["cost_usd"] if led else None,
               "token_usage_cost_usd": tu_cost if role == "all" else None}
        if basis == "sessions":
            row.update(dict((k, tot[k]) for k in FIELDS))
            row.update({"tokens": tot["inputTokens"] + tot["cacheReadTokens"] + tot["outputTokens"],
                        "usage_calls": tot["usage_calls"], "retried_attempts": tot["retried_attempts"],
                        "sessions": n_sess, "subagent_sessions": n_sub, "session_files": n_files,
                        "undecodable_files": n_undec,
                        "cost_usd": price(tot["inputTokens"], tot["cacheReadTokens"], tot["outputTokens"], rates)})
        elif basis == "ledger":
            led = led or {"calls": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "tokens": 0, "retried": 0,
                          "cost_usd": 0.0}
            row.update({"inputTokens": led["input"], "cacheReadTokens": led["cached"], "outputTokens": led["output"],
                        "reasoningTokens": led["reasoning"], "cacheWriteTokens": None, "totalTokens": None,
                        "tokens": led["input"] + led["cached"] + led["output"], "usage_calls": None,
                        "retried_attempts": led["retried"], "sessions": None, "subagent_sessions": None,
                        "session_files": 0 if role == "all" else None, "undecodable_files": None,
                        "cost_usd": led["cost_usd"]})
        else:
            row.update(dict((k, None) for k in FIELDS))
            row.update({"tokens": None, "usage_calls": None, "retried_attempts": None, "sessions": None,
                        "subagent_sessions": None, "session_files": 0 if role == "all" else None,
                        "undecodable_files": None, "cost_usd": None})
        rows.append(row)
    return rows, undecodable


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return "%.4f" % v
    return str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Settle RA runs from their dsh session logs (the paper's cost method).")
    ap.add_argument("paths", nargs="+", metavar="PATH")
    ap.add_argument("--rates", help="rates JSON file (default rates/deepseek-v4.1-flash.json) or 'a,b,c' in USD per "
                                    "million tokens: uncached input, cached input, output")
    ap.add_argument("--by-role", action="store_true", help="also one row per role")
    ap.add_argument("--format", choices=("csv", "json"), default="csv")
    ap.add_argument("--ledger-fallback", action="store_true",
                    help="report a run without session files from its ledger's call events")
    ap.add_argument("--out", help="write here instead of stdout")
    a = ap.parse_args(argv)
    try:
        rates, rates_info = load_rates(a.rates)
    except (OSError, ValueError, KeyError) as e:
        sys.stderr.write("settle_sessions: bad --rates: %s\n" % e)
        return 2
    status, runs = 0, []
    for p in a.paths:
        try:
            rows, undec = settle(p, rates, a.ledger_fallback)
        except ValueError as e:
            sys.stderr.write("settle_sessions: %s\n" % e)
            status = 1
            continue
        main_row = rows[0]
        if main_row["basis"] == "none":
            sys.stderr.write("settle_sessions: %s: no session files%s\n" % (
                main_row["run"], "" if a.ledger_fallback else " (use --ledger-fallback to report the ledger)"))
            status = 1
        if undec:
            status = 1
        if main_row["cost_usd"] is not None:
            sys.stderr.write("settle_sessions: %s: basis %s, %s session files, cost US$%.4f\n"
                             % (main_row["run"], main_row["basis"], main_row["session_files"], main_row["cost_usd"]))
        runs.append(rows if a.by_role else rows[:1])
    out = open(a.out, "w", newline="") if a.out else sys.stdout
    try:
        if a.format == "json":
            doc = {"rates_usd_per_mtok": rates_info, "runs": []}
            for rows in runs:
                r0 = dict(rows[0])
                if a.by_role:
                    r0["roles"] = dict((r["role"], dict((k, v) for k, v in r.items() if k not in ("run", "role")))
                                       for r in rows[1:])
                doc["runs"].append(r0)
            json.dump(doc, out, indent=2)
            out.write("\n")
        else:
            w = csv.writer(out, lineterminator="\n")
            w.writerow(COLUMNS)
            for rows in runs:
                for r in rows:
                    w.writerow([fmt(r.get(c)) for c in COLUMNS])
    finally:
        if a.out:
            out.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
