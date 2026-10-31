
# ---------------------------------------------------------------- mechanical checks: unit directory, deliverables, handoff.json

def _ws_path(p):
    """Deliverable path -> real path (tests run with WS != /workspace)."""
    p = str(p).strip()
    if p.startswith("/workspace/") and WS != "/workspace":
        return os.path.join(WS, p[len("/workspace/"):])
    if not p.startswith("/"):
        return os.path.join(WS, p)
    return p


def _ws_name(real):
    """Real path -> the /workspace/... name used in prompts and the ledger."""
    return "/workspace/" + os.path.relpath(real, WS) if str(real).startswith(WS + "/") else str(real)


def unit_dir(uid):   # the unit's directory = its working directory = where its products, logs and handoff.json live
    return os.path.join(WS, str(uid))


def _py_ok(p):
    """Syntax check without writing a .pyc."""
    try:
        with open(p, "rb") as f:
            src = f.read()
        compile(src, p, "exec")
        return True, ""
    except SyntaxError as e:
        return False, "SyntaxError line %s: %s" % (e.lineno, (e.msg or "")[:160])
    except Exception as e:
        return False, str(e)[:200]


def _check_file(p):
    """One deliverable: exists / non-empty / parses (a directory counts as present when it holds a non-empty file)."""
    if os.path.isdir(p):
        n = 0
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in files:
                try:
                    if os.path.getsize(os.path.join(root, fn)) > 0:
                        n += 1
                except OSError:
                    pass
        return {"path": p, "exists": True, "size": n, "check": "dir(%d files)" % n, "ok": n > 0,
                "err": "" if n > 0 else "directory has no non-empty files"}
    try:   # a file can vanish between two calls (a job rotating its outputs)
        sz = os.path.getsize(p)
    except OSError:
        sz = None
    r = {"path": p, "exists": sz is not None, "size": sz or 0, "check": "", "ok": False, "err": ""}
    if not r["exists"] or r["size"] == 0:
        r.update(check="missing" if not r["exists"] else "empty", err="file does not exist" if not r["exists"] else "zero bytes")
        return r
    ext = os.path.splitext(p)[1].lower()
    try:
        if ext == ".py":
            ok, err = _py_ok(p)
            r.update(check="py_compile", ok=ok, err=err)
        elif ext in (".npy", ".npz"):
            try:
                import numpy as np
            except ImportError:
                r.update(check="unchecked(numpy unavailable)", ok=True)
                return r
            if ext == ".npy":
                try:   # a memory map reads the header only: a large array never enters the orchestrator's memory
                    z = np.load(p, mmap_mode="r", allow_pickle=False)
                except Exception:   # whatever the map cannot open is loaded as before: the same result and the same message
                    z = np.load(p, allow_pickle=False)
            else:
                z = np.load(p, allow_pickle=False)
            if ext == ".npz":
                keys = list(z.keys())
                r.update(check="npz keys=%s" % keys[:12], ok=bool(keys), err="" if keys else "no arrays")
            else:
                r.update(check="npy shape=%s" % (getattr(z, "shape", None),), ok=True)
        elif ext in (".csv", ".tsv"):
            import csv
            rows = []
            with open(p, newline="", encoding="utf-8", errors="replace") as f:
                for i, row in enumerate(csv.reader(f, delimiter="\t" if ext == ".tsv" else ",")):
                    rows.append(row)
                    if i >= 4:
                        break
            r.update(check="csv rows>=%d cols=%d" % (len(rows), len(rows[0]) if rows else 0),
                     ok=bool(rows and any(rows[0])))
        elif ext == ".json":
            json.load(open(p))
            r.update(check="json", ok=True)
        elif ext in (".h5", ".hdf5"):
            try:
                import h5py
            except ImportError:
                r.update(check="unchecked(h5py unavailable)", ok=True)
                return r
            with h5py.File(p, "r") as h:
                ks = list(h.keys())
            r.update(check="hdf5 keys=%s" % ks[:12], ok=True)
        elif ext == ".png":
            r.update(check="png header", ok=open(p, "rb").read(8) == b"\x89PNG\r\n\x1a\n")
        elif ext == ".pdf":
            r.update(check="pdf header", ok=open(p, "rb").read(5) == b"%PDF-")
        elif ext in (".pkl", ".pickle"):
            r.update(check="unchecked(pickle not loaded)", ok=True)
        elif ext in (".txt", ".dat", ".md", ".log", ".yaml", ".yml", ".toml", ".jl", ".sh", ".cfg", ".ini"):
            r.update(check="text", ok=bool(open(p, encoding="utf-8", errors="replace").read(4096).strip()))
        else:
            r.update(check="non-empty", ok=True)
        if not r["ok"] and not r["err"]:
            r["err"] = r["check"] + " failed"
    except Exception as e:
        r.update(ok=False, err=("%s: %s" % (type(e).__name__, e))[:200], check=r["check"] or "parse")
    return r


def _has_glob(p):
    return any(ch in p for ch in "*?[")

_RANGE_RX = re.compile(r"[A-Za-z0-9_]+\.\.[A-Za-z0-9_]+")

_BRACE_RX = re.compile(r"\{([^{}]*)\}")


def _brace_expand(p, cap=400):
    """Shell-style brace lists in a listed path - data/x_{3,4,5}.npz, y_{3..9}.npz - expand to the alternatives (glob has none)."""
    m = _BRACE_RX.search(p)
    if not m:
        return [p]
    body = m.group(1).strip()
    rm = re.match(r"^(-?\d+)\.\.(-?\d+)$", body)
    if rm:
        a, b = int(rm.group(1)), int(rm.group(2))
        step = 1 if b >= a else -1
        alts = [str(i) for i in range(a, b + step, step)[:cap]]   # the range is cut before it is built
    elif "," in body:
        alts = [x.strip() for x in body.split(",")]
    else:
        return [p]
    out = []
    for a in alts:
        out += _brace_expand(p[:m.start()] + a + p[m.end():], cap)
        if len(out) >= cap:
            break
    return out[:cap]


def _pattern_hits(real):   # a listed path with glob characters or an a..b range is a pattern; present when >= 1 file matches
    if not (_has_glob(real) or _RANGE_RX.search(real) or "{" in real):
        return []
    out = []
    for alt in _brace_expand(real):
        out += glob.glob(_RANGE_RX.sub("*", alt))
    return out


def allowed_paths(unit):
    """[(kind, real_path)] of a unit's declared deliverables (dir = the subtree, glob = the pattern, file)."""
    out = []
    for d in ((unit or {}).get("deliverables") or []):
        d = str(d).strip()
        real = _ws_path(d)
        if d.endswith("/") or os.path.isdir(real.rstrip("/")):
            out.append(("dir", real.rstrip("/")))
        elif _has_glob(real):
            out.append(("glob", real))
        else:
            out.append(("file", real))
    return out


def _allowed(real, allow):
    for kind, p in allow:
        if kind == "dir" and (real == p or real.startswith(p + "/")):
            return True
        if kind == "glob" and fnmatch.fnmatch(real, p):
            return True
        if kind == "file" and real == p:
            return True
    return False


def own_scope(u):
    """What a unit writes by contract: its directory, its declared deliverables and (render unit) the final files at the root."""
    out = allowed_paths(u) + [("dir", unit_dir((u or {}).get("id")))]
    if (u or {}).get("type") == "render":
        out += [("file", os.path.join(WS, "report.md")), ("glob", os.path.join(WS, "reproduced_figure*"))]
    return out


def handoff_check(uid, unit, warns=None):
    """(handoff dict or None, [errors]) of /workspace/<uid>/handoff.json, plus tolerated defects appended to `warns`.
    Only these are HARD failures: the file is missing or unparseable, a listed deliverable path does not exist, a "waiting"
    block is present without a usable pid / marker, and a criteria row with pass=false in a complete handoff (stopped=null).
    Everything else (uid, how_to_use, known_limits, the type of "stopped", a malformed criteria table, the shape of
    deliverables, a path outside the unit's scope, a declared deliverable the listing forgot) is normalised where possible
    and collected as a WARNING: the executor reads it in the continuation prompt it gets anyway, it is never sent back for it."""
    warns = warns if warns is not None else []
    p = os.path.join(unit_dir(uid), "handoff.json")
    if not os.path.exists(p):
        return None, ["handoff.json missing (/workspace/%s/handoff.json)" % uid]
    try:
        h = json.load(open(p))
    except Exception as e:
        return None, ["handoff.json is not valid JSON: %s" % str(e)[:160]]
    if not isinstance(h, dict):
        return None, ["handoff.json must be a JSON object"]
    errs = []
    if h.get("uid") != uid:
        warns.append('"uid" should be "%s" (got %r)' % (uid, h.get("uid")))
    stopped = h.get("stopped")
    if stopped is not None and not (isinstance(stopped, str) and stopped.strip()):
        warns.append('"stopped" should be null or a non-empty reason string (got %r)' % (stopped,))   # its truthiness still decides "partial", as before
    wt, werrs = waiting_spec(h)   # a registered background job makes this a partial handoff (like a stop); an unusable block stays hard
    if wt is not None and _stale_wait(uid, wt):   # a registration already waited on whose job is over is not a registration
        warns.append('"waiting" names the job already waited on (pid %s, finished) - remove the "waiting" field' % wt.get("pid"))
        wt = None
    errs, partial = errs + werrs, bool(stopped) or wt is not None
    for k in ("how_to_use", "known_limits"):
        if not isinstance(h.get(k), (str, list, dict)):
            warns.append('"%s" missing' % k)
    dl = h.get("deliverables")
    if not isinstance(dl, list):
        warns.append('"deliverables" should be a list of {"path", "what"}')
        dl = []
    scope, listed = own_scope(unit), []
    for x in dl:
        if isinstance(x, str):   # a plain path string is accepted and normalised
            x = {"path": x, "what": ""}
        if not isinstance(x, dict) or not isinstance(x.get("path"), str):
            warns.append("deliverables entry without a path: %s" % str(x)[:80])
            continue
        real = _ws_path(x["path"])
        listed.append(real.rstrip("/"))
        if not (_allowed(real.rstrip("/"), scope) or _allowed(real, scope)):
            warns.append("lists a path outside the unit's directory / declared deliverables: %s" % x["path"])
        if not partial and not os.path.exists(real) and not _pattern_hits(real):
            errs.append("listed path does not exist: %s" % x["path"])
    if not partial:
        for kind, d in allowed_paths(unit):
            if kind != "glob" and os.path.basename(d) != "handoff.json" and not any(l == d or l.startswith(d + "/") for l in listed):
                warns.append("declared deliverable not listed: %s" % _ws_name(d))
    # the criteria table: a malformed row is a note for the executor and for the audit; pass=false in a complete handoff is an error
    cr = h.get("criteria")
    if not partial and (unit or {}).get("type", "produce") == "produce":
        if not isinstance(cr, list):
            warns.append('"criteria" missing: a list of {"item": "Fn", "value", "target", "pass": true|false} for every quantitative item this unit serves ([] if none)')
        else:
            for c in cr:
                if not isinstance(c, dict) or "pass" not in c or not isinstance(c.get("item"), str):
                    warns.append("criteria entry without item / pass: %s" % str(c)[:80])
                elif c.get("pass") is False:
                    errs.append("criteria %s pass=false (value %s, target %s) with stopped=null: fix it before the handoff, or stop with the numbers"
                                 % (c.get("item"), str(c.get("value"))[:40], str(c.get("target"))[:40]))
    return h, errs


def waiting_spec(h):   # (validated handoff.json "waiting" or None, [errors]) - the ONE detached job the executor registered
    w = (h or {}).get("waiting")
    if w is None:
        return None, []
    def _num(x):
        try:
            v = float(str(x).strip())
            return v if math.isfinite(v) else None   # NaN / Infinity are no numbers
        except Exception:
            return None
    if isinstance(w, list):   # several jobs registered as a list - the protocol takes ONE registration: say how to merge
        return None, ['"waiting" is a list of %d jobs - register exactly ONE detached leader that starts every job and touches ONE done marker after all of them '
                      'finish, e.g. setsid nohup bash -c \'echo $$ > bg.pid; (job1) & (job2) & wait; touch bg.done\' > bg.log 2>&1 & (in your unit directory), '
                      'then "waiting": {"pid": <that leader\'s pid from bg.pid>, "done_marker": "<unit dir>/bg.done", "expect_min": <the longest job>, '
                      '"resume_note": "..."} (residual R7)' % len(w)]
    if not isinstance(w, dict):
        return None, ['"waiting" must be {"pid": <integer PID of the detached job>, "done_marker": "<file the job touches when '
                      'finished>", "expect_min": <minutes > 0>, "resume_note": "<what to do when it is done>"} (got %s)' % str(w)[:80]]
    pid = w.get("pid")   # the obvious variants are accepted - a digit string, the first of "pids", expect_min as a string or in hours / seconds
    if (pid is None or _num(pid) is None) and isinstance(w.get("pids"), list) and w["pids"]:
        pid = w["pids"][0]
    pid = int(_num(pid)) if _num(pid) is not None else None
    if pid is not None and pid >= 2 ** 31:   # beyond any kernel pid
        pid = None
    em = _num(w.get("expect_min"))
    if em is None:
        for k, f in (("expect_h", 60.0), ("expect_hours", 60.0), ("expected_min", 1.0), ("eta_min", 1.0), ("expect_s", 1 / 60.0)):
            if _num(w.get(k)) is not None:
                em = _num(w.get(k)) * f
                break
    dm = str(w.get("done_marker") or "").strip()
    if em:
        em = min(em, 1e5)   # a declared duration beyond ~70 days is read as that (the clock arithmetic stays finite)
    if not (pid and pid > 1 and em and em > 0 and dm):
        missing = [k for k, ok in (("pid", bool(pid and pid > 1)), ("expect_min", bool(em and em > 0)), ("done_marker", bool(dm))) if not ok]
        got = ", ".join("%s=%s" % (k, str(v)[:28]) for k, v in list(w.items())[:8])
        return None, ['"waiting" must be {"pid": <integer PID of the detached job>, "done_marker": "<file the job touches when '
                      'finished>", "expect_min": <minutes > 0>, "resume_note": "<what to do when it is done>"}; missing or invalid: %s; got {%s}'
                      % (", ".join(missing), got)]
    spec = {"pid": pid, "done_marker": dm, "expect_min": em, "resume_note": str(w.get("resume_note") or "")[:600]}
    lg = w.get("log")   # optional - the job's log (default /workspace/<id>/bg.log) and the files whose growth proves progress
    if isinstance(lg, str) and lg.strip():
        spec["log"] = lg.strip()[:300]
    pr = w.get("progress")
    if isinstance(pr, str):
        pr = [pr]
    if isinstance(pr, list):
        spec["progress"] = [str(x).strip()[:300] for x in pr if isinstance(x, str) and str(x).strip()][:20]
    return spec, []


def _vendored(path, root):
    """A file inside a vendored tree (pip --target / venv / node_modules) that the executor installed but did not write: never compiled,
    never copied back."""
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if any(p in VENDORED_DIRS for p in parts[:-1]):
        return True
    d = root
    for p in parts[:-1]:
        d = os.path.join(d, p)
        try:
            if any(x.endswith((".dist-info", ".egg-info")) for x in os.listdir(d)):
                return True
        except OSError:
            return False
    return False

def _changed_py(ud, since, limit=6000):
    """.py files under the unit written after `since`, vendored trees skipped (bounded walk)."""
    out, seen = [], 0
    for root, dirs, files in os.walk(ud):
        dirs[:] = [d for d in dirs if d not in VENDORED_DIRS and not d.startswith("audit_r") and d != "probes"
                   and not any(x.endswith((".dist-info", ".egg-info")) for x in dirs)]
        for fn in files:
            seen += 1
            if seen > limit:
                return out
            if fn.endswith(".py"):
                p = os.path.join(root, fn)
                try:
                    if os.stat(p).st_mtime > since and not _vendored(p, ud):
                        out.append(p)
                except OSError:
                    pass
    return out

def _root_writes(uid, since):
    """Final deliverables at /workspace root (report.md, reproduced_figure*) written after `since` by a unit that is not the render unit -
    recorded (a flush wrap-up can write report.md just before the report step)."""
    hits = []
    for fn in os.listdir(WS):
        if fn == "report.md" or fn.startswith("reproduced_figure"):
            p = os.path.join(WS, fn)
            try:
                if os.stat(p).st_mtime > since:
                    hits.append(fn)
            except OSError:
                pass
    if hits and uid != "render":
        ledger({"kind": "writes_outside_own_dir", "unit": uid, "by": "exec", "n": len(hits), "sample": hits[:10]})
        log("%s wrote final deliverable(s) at /workspace root: %s (recorded; only the report step writes them)" % (uid, ", ".join(hits)))
    return hits

def mech_check(uid, unit, t_call=None):
    """After a HANDOFF - protocol completeness only: declared deliverables present / parse / compile, handoff.json valid (+ a legal
    waiting block), the .py files written in the call compile -> /workspace/<uid>/mech.json + ledger 'mech'."""
    ud = unit_dir(uid)
    os.makedirs(ud, exist_ok=True)
    items = []
    for d in [str(x) for x in (unit.get("deliverables") or [])]:
        real = _ws_path(d)
        alts = _brace_expand(real)
        if len(alts) == 1 and not _has_glob(real):
            hits = [real.rstrip("/") or real]
        else:
            hits = sorted(set(h for a in alts for h in (glob.glob(a) if _has_glob(a) else ([a] if os.path.exists(a) else []))))
        if not hits:
            items.append({"path": d, "exists": False, "size": 0, "check": "missing(glob matched nothing)", "ok": False, "err": "no file matches the pattern"})
        for h in hits[:50]:
            items.append(_check_file(h))
    pyfiles = set(h["path"] for h in items if h["exists"] and h["path"].endswith(".py"))
    if t_call:
        pyfiles.update(_changed_py(ud, t_call))
    pyc = [dict(zip(("path", "ok", "err"), (p,) + _py_ok(p))) for p in sorted(pyfiles)]
    hwarns = []
    handoff, herrs = handoff_check(uid, unit, hwarns)
    try:   # a declared deliverable the handoff names MISSING under known_limits is declared, not missing (the critic judges the miss)
        kl = json.dumps((handoff or {}).get("known_limits") or "") + json.dumps((handoff or {}).get("missing") or "")
        for h in items:
            if not h["ok"] and h.get("err") in ("file does not exist", "no file matches the pattern"):
                base = os.path.basename(h["path"].rstrip("/"))
                if base and base in kl and re.search(r"MISSING|dropped|ruled", kl, re.I):
                    h["ok"], h["check"], h["declared_missing"] = True, "declared MISSING in known_limits", True
                    hwarns.append("deliverable %s declared MISSING in known_limits (the audit judges the miss)" % _ws_name(h["path"]))
    except Exception:
        pass
    stopped = bool((handoff or {}).get("stopped"))
    waiting = None
    if not herrs:
        waiting = waiting_spec(handoff)[0]
        if waiting is not None and _stale_wait(uid, waiting):
            waiting = None
    outside = _root_writes(uid, t_call) if t_call else []
    res = {"uid": uid, "ts": int(time.time()), "deliverables": items, "py_compile": pyc,
           "handoff_ok": not herrs, "handoff_errors": herrs, "warnings": hwarns, "stopped": ((handoff or {}).get("stopped") if stopped else None),
           "waiting": waiting, "outside": len(outside), "outside_sample": outside[:20],
           "n_deliv_ok": sum(1 for h in items if h["ok"]), "n_deliv": len(items),
           "n_py_ok": sum(1 for x in pyc if x["ok"]), "n_py": len(pyc)}
    deliv_ok = res["n_deliv_ok"] == res["n_deliv"] or stopped or waiting is not None
    res["all_ok"] = bool(deliv_ok and res["n_py_ok"] == res["n_py"] and not herrs)
    fails = []
    if not deliv_ok:
        fails += ["missing/unparseable deliverable %s (%s)" % (_ws_name(h["path"]), h["err"]) for h in items if not h["ok"]]
    fails += ["%s does not compile (%s)" % (_ws_name(x["path"]), x["err"]) for x in pyc if not x["ok"]]
    fails += ["handoff.json: %s" % e for e in herrs]
    res["fails"] = fails
    try:
        _atomic_json_dump(res, os.path.join(ud, "mech.json"))
    except Exception as e:
        log("mech.json write failed: %s" % e)
    mev = {"kind": "mech", "unit": uid, "ok": res["all_ok"], "deliv": "%d/%d" % (res["n_deliv_ok"], res["n_deliv"]),
           "py": "%d/%d" % (res["n_py_ok"], res["n_py"]), "handoff_ok": not herrs, "stopped": stopped, "waiting": waiting is not None,
           "outside": len(outside), "fail": fails[:12]}
    if hwarns:
        mev["warnings"] = hwarns[:12]
    ledger(mev)
    log("mech check %s: deliverables %d/%d, py_compile %d/%d, handoff %s%s%s%s"
        % (uid, res["n_deliv_ok"], res["n_deliv"], res["n_py_ok"], res["n_py"], "ok" if not herrs else "INVALID", " (waiting)" if waiting else "",
           (" - %d handoff note(s)" % len(hwarns)) if hwarns else "", "" if res["all_ok"] else " - FAILURES: " + "; ".join(fails[:4])[:400]))
    return res

def mech_block(uid, max_lines=30):
    """Critic prompt block: the machine result the critic cites instead of re-checking files. mech.json lies in the unit's directory, where
    an agent can rewrite it: a file that is not in the orchestrator's schema reads as 'not available', never as an error."""
    try:
        return _mech_block_text(uid, json.load(open(os.path.join(unit_dir(uid), "mech.json"))), max_lines)
    except Exception:
        return "MECH CHECK: not available for %s - verify deliverable existence yourself, briefly." % uid

def _mech_block_text(uid, m, max_lines):
    L = ["MECH CHECK (machine-run by the orchestrator after the executor's turn; authoritative, do NOT re-stat files): "
         "deliverables %d/%d OK, py_compile %d/%d OK, handoff.json %s, details in /workspace/%s/mech.json.%s"
         % (m["n_deliv_ok"], m["n_deliv"], m["n_py_ok"], m["n_py"], "valid" if m.get("handoff_ok") else "INVALID", uid,
            (" The unit STOPPED ITSELF: %s" % str(m.get("stopped"))[:300]) if m.get("stopped") else "")]
    rows = [h for h in m["deliverables"] if not h["ok"]]
    rows += [{"path": x["path"], "ok": False, "check": "py_compile", "err": x["err"]} for x in m["py_compile"] if not x["ok"]]
    rows += [h for h in m["deliverables"] if h["ok"]]
    for h in rows[:max_lines]:
        L.append("  %s %s - %s%s" % ("OK  " if h["ok"] else "FAIL", _ws_name(h["path"]), h.get("check", ""),
                                     (" (%s)" % h["err"]) if h.get("err") else ""))
    if len(rows) > max_lines:
        L.append("  ... %d more in mech.json" % (len(rows) - max_lines))
    return "\n".join(L)


def mech_notes(mech, sep=""):
    """The tolerated handoff defects as a short list, for the continuation prompt that already exists (the machine
    critic's FIX LIST on a mechanical retry, the RESUME_* prompts after a wait). Empty when there is nothing to say."""
    w = [str(x)[:200] for x in ((mech or {}).get("warnings") or [])][:8]
    if not w:
        return ""
    return sep + "HANDOFF NOTES (not blocking - put them right in the next handoff.json):\n%s\n" % "\n".join("- " + x for x in w)
