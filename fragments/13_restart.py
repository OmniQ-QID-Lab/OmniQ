
# ---------------------------------------------------------------- archives, residuals, restarts and what a fresh attempt may reuse

def archive_dir(uid, suffix, reason):
    """Move /workspace/<uid>/ aside as /workspace/<uid>.<suffix>/ - a readable reference that is never touched again."""
    src = unit_dir(uid)
    if not os.path.isdir(src):
        return None
    dst, n = src + "." + suffix, 0
    while os.path.exists(dst):   # never overwrite an earlier archive
        n += 1
        dst = "%s.%s_%d" % (src, suffix, n)
    shutil.move(src, dst)
    ledger({"kind": "archive", "unit": uid, "dir": _ws_name(dst), "reason": reason})
    log("%s: directory archived -> %s (%s)" % (uid, _ws_name(dst), reason))
    return dst


def ensure_residual(uid, reason):
    """residual.md in the unit directory: the critic writes it on C / D; when it is missing the orchestrator writes one from
    critic_report.md and mech.json, so the next attempt always has one. Returns who wrote it ("critic" or "orchestrator")."""
    ud = unit_dir(uid)
    p = os.path.join(ud, "residual.md")
    if os.path.isfile(p) and read(p).strip():
        return "critic"
    crit, fails = read(os.path.join(ud, "critic_report.md")).strip(), []
    try:
        fails = json.load(open(os.path.join(ud, "mech.json"))).get("fails") or []
        fails = [str(f) for f in fails] if isinstance(fails, list) else []   # mech.json can be an agent's rewrite
    except Exception:
        pass
    os.makedirs(ud, exist_ok=True)
    open(p, "w").write("# residual of unit %s (written by the ORCHESTRATOR: the critic left no residual.md)\nREASON: %s\n%s%s"
                       % (uid, reason, ("MECHANICAL CHECK FAILURES:\n%s\n" % "\n".join("- " + f for f in fails)) if fails else "",
                          ("\nCRITIC REPORT (verbatim):\n%s\n" % crit[:8000]) if crit else "(no critic report on disk)\n"))
    return "orchestrator"


def extract_fixes(uid):
    txt = read(os.path.join(unit_dir(uid), "critic_report.md"))
    starts = [m.start() for m in re.finditer(r"FIX LIST", txt, re.I)
              if not re.search(r"previous\W*$", txt[max(0, m.start() - 12):m.start()], re.I)]   # skip "PREVIOUS FIX LIST ..." echoes of a re-audit
    m = re.search(r"FIX LIST(.*?)(\n#|\nFIX_PLAN:|\Z)", txt[starts[0]:], re.S | re.I) if starts else None
    return (m.group(1).strip() if m else txt[-3000:])


def reusable_paths(residual_txt):
    """The paths named in residual.md's REUSABLE section (header line remainder + following lines, up to the next SIGNATURE / PROBE /
    SUGGESTION / NOT REUSABLE / FIX_PLAN header). Absolute /workspace/<id>/... tokens and the first path-like token of a bullet line count."""
    m = re.search(r"^[ \t#*>-]*REUSABLE\b[ \t]*:?(.*)\n?((?:(?![ \t#*>-]*(?:NOT REUSABLE|SIGNATURE|PROBE|SUGGESTION|FIX_PLAN)\b).*\n?)*)", residual_txt, re.M | re.I)
    if not m:
        return []
    head = re.split(r"NOT REUSABLE", m.group(1), flags=re.I)[0]
    out = []
    for line in [head] + m.group(2).splitlines():
        toks = re.findall(r"/workspace/[\w.-]+/[^\s`'\",;:)\]]+", line)
        if not toks:
            mm = re.match(r"^[ \t*>-]*`?([\w][\w./\-]*)", line)
            if mm and ("/" in mm.group(1) or "." in mm.group(1)) and not mm.group(1).lower().startswith(("none", "n/a", "nothing")):
                toks = [mm.group(1)]
        for t in toks:
            t = t.rstrip(".,;:")
            if t and t not in out:
                out.append(t)
    return out


def copy_reusable(uid, arch):
    """Copy the files the critic listed under REUSABLE in the archived residual.md into the fresh unit directory - regular files under
    the unit only, REAL copies (never links: the archive stays untouched), bounded in count and bytes; bookkeeping files and probes never.
    Returns the relative paths copied (ledger: reusable_copied)."""
    if not arch or not os.path.isdir(arch):
        return []
    txt = read(os.path.join(arch, "residual.md"), "")
    if not txt.strip():
        return []
    return _copy_from_archive(uid, arch, reusable_paths(txt), "residual REUSABLE")


def _copy_from_archive(uid, arch, toks, source):
    """The copy machinery of copy_reusable for any list of unit paths (`source` names who vouched for them: the critic's REUSABLE list,
    or the declared deliverables of a B round whose gates all passed - an anchor restart)."""
    if not arch or not os.path.isdir(arch):
        return []
    ud, prefix = unit_dir(uid), "/workspace/%s/" % uid
    arch_real, copied, skipped, total = os.path.realpath(arch), [], [], 0
    for tok in toks:
        rel = tok[len(prefix):] if tok.startswith(prefix) else (tok if not tok.startswith("/") else None)
        if rel is None:
            skipped.append(tok + " (outside the unit)"); continue
        rel = os.path.normpath(rel)
        if rel.startswith("..") or rel in (".", ""):
            skipped.append(tok + " (outside the unit)"); continue
        src = os.path.join(arch, rel)
        if not os.path.exists(src):
            skipped.append(rel + " (not in the archive)"); continue
        files = []
        if os.path.isdir(src) and not os.path.islink(src):
            for dp, dns, fns in os.walk(src):
                dns[:] = [d for d in dns if d not in REUSE_SKIP_DIRS and not d.startswith("audit_r")]
                for fn in fns:
                    files.append(os.path.join(dp, fn))
        else:
            files = [src]
        for f in files:
            r = os.path.relpath(f, arch)
            nm = os.path.basename(f)
            if nm in REUSE_SKIP_NAMES or (nm.startswith("probe_") and nm.endswith(".txt")) or r.split(os.sep)[0] in REUSE_SKIP_DIRS or r.startswith("audit_r"):
                skipped.append(r + " (bookkeeping)"); continue
            if os.path.islink(f) or not os.path.isfile(f) or not os.path.realpath(f).startswith(arch_real + os.sep):
                skipped.append(r + " (not a regular file)"); continue
            try:
                sz = os.path.getsize(f)
            except OSError:
                continue
            if len(copied) >= REUSE_MAX_FILES or total + sz > REUSE_MAX_BYTES:
                skipped.append(r + " (over the copy bound)"); continue
            dst = os.path.join(ud, r)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(f, dst)
                copied.append(r); total += sz
            except Exception as e:
                skipped.append("%s (%s)" % (r, e))
    ledger({"kind": "reusable_copied", "unit": uid, "archive": _ws_name(arch), "n": len(copied), "bytes": total, "files": copied[:60],
            "skipped": skipped[:30], "source": source})
    if copied or skipped:
        log("%s: %s from %s -> %d file(s) (%.1f MB) copied into the fresh directory%s" % (uid, source, _ws_name(arch), len(copied), total / 1e6,
            ("; skipped: " + "; ".join(skipped[:5])) if skipped else ""))
    return copied

def _copy_code_from_archive(uid, arch, cap_files=400, cap_bytes=50 * 1024 * 1024):
    """The archived round's CODE files (source, scripts, configs, requirements; never data, logs, bookkeeping, audits, probes or vendored
    packages) copied into the fresh directory where nothing exists yet (the REUSABLE copies come first)."""
    if not arch or not os.path.isdir(arch):
        return []
    ud, copied, total = unit_dir(uid), [], 0
    skip_names = {"handoff.json", "critic_report.md", "residual.md", "mech.json", "spec.md", "STATUS.md", "bg.pid", "profile.json"}
    for dp, dns, fns in os.walk(arch):
        dns[:] = sorted(d for d in dns if d not in REUSE_SKIP_DIRS and not d.startswith("audit_r") and d not in ("logs", "from_scout", "from_scout_also")
                        and not any(x.endswith((".dist-info", ".egg-info")) for x in dns))
        for fn in sorted(fns):
            if fn in skip_names or fn.startswith("probe_") or fn.endswith(".log"):
                continue
            low = fn.lower()
            if not (low.endswith(CODE_EXT) and (not low.endswith(".txt") or "requirement" in low or low == "cmakelists.txt")):
                continue
            src = os.path.join(dp, fn)
            rel = os.path.relpath(src, arch)
            dst = os.path.join(ud, rel)
            try:
                if os.path.islink(src) or not os.path.isfile(src) or os.path.exists(dst) or _vendored(src, arch):
                    continue
                sz = os.path.getsize(src)
                if sz > 2 * 1024 * 1024 or total + sz > cap_bytes or len(copied) >= cap_files:
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(rel); total += sz
            except Exception as e:
                log("code copy %s failed: %s" % (rel, e))
    if copied:
        ledger({"kind": "code_copied", "unit": uid, "archive": _ws_name(arch), "n": len(copied), "bytes": total, "files": copied[:60]})
        log("%s: %d code file(s) of the archived round copied back as the starting point (%d bytes)" % (uid, len(copied), total))
    return copied


def _produce_downstream(uid):
    return [d for d in downstream_ids(uid, RUN["plan"]) if (unit_by_id(d) or {}).get("type") != "render"]

def _prov_restart(u, rounds, g):
    """A unit whose files dependents may already use is restarted -> its deliverables come back into the fresh directory."""
    if not u.get("released_deps"):
        return False, ""
    ledger({"kind": "provisional_restart", "unit": u["id"], "round": rounds, "grade": g, "dependents": _produce_downstream(u["id"])})
    return True, PROV_RESTART_NOTE


def restart_unit(u, rounds, grade, note="", keep_deliverables=False):
    """A fresh attempt: the unit's processes stopped, residual.md guaranteed, the directory archived as /workspace/<id>.r<n>/, a fresh
    directory with spec.md, the critic-verified files (REUSABLE) and the previous code copied back; keep_deliverables (dependents already use
    the files) copies the archived deliverables back too. Returns the restart record for the new session."""
    uid = u["id"]
    try:
        w_old = _waiting_of(uid)
        kp = (w_old or {}).get("pid")
        killed = reap_leftovers(uid, kill_pid=(kp if kp and _pid_is_units(kp, uid) else None))
        if killed:
            ledger({"kind": "restart_reaped", "unit": uid, "n": len(killed)})
    except Exception as e:
        log("%s: reaping the archived round's processes failed: %s" % (uid, e))
    by = ensure_residual(uid, "grade %s (round %d)" % (grade, rounds))
    n = int(u.get("restarts") or 0) + 1
    arch = archive_dir(uid, "r%d" % n, "restart after grade %s" % grade)
    b = u.get("_best") or {}
    if arch and b.get("round") == n - 1:   # the best validated round is the one just archived
        b["dir"] = _ws_name(arch)
    u["restarts"] = n
    u.setdefault("archives", []).append(_ws_name(arch) if arch else "(no directory)")
    write_spec(u)
    u.pop("handoff_valid_at", None)   # the fresh attempt has no valid handoff yet (the file gate must not open on archived copies)
    copied = copy_reusable(uid, arch) if by == "critic" else []
    kept = []
    if arch and keep_deliverables:
        kept = [x for x in _copy_from_archive(uid, arch, [d for d in (u.get("deliverables") or []) if isinstance(d, str)], "deliverables of the archived round") if x not in copied]
    code = _copy_code_from_archive(uid, arch) if (RESTART_KEEP_CODE and arch) else []
    ledger({"kind": "restart", "unit": uid, "round": rounds, "n": n, "grade": grade, "archive": _ws_name(arch) if arch else None, "residual_by": by,
            "reusable": len(copied), "kept": len(kept), "code": len(code)})
    log("%s RESTART %d: archive %s, residual.md by the %s, fresh directory%s" % (uid, n, _ws_name(arch) if arch else "-", by,
        (", %d verified file(s) copied back" % len(copied)) if copied else ""))
    LAST_LOOK[uid] = time.time()
    return {"grade": grade, "archive": _ws_name(arch) if arch else "/workspace/%s (no archive)" % uid, "round": n, "note": note, "copied": copied,
            "kept": kept, "code": code}

def _scale_hint(u):
    """Knobs a bounded redo must use: the unit's own measured scale_knobs (profile.json, current directory or the newest archive) and the planner's."""
    pj = None
    for p in [os.path.join(unit_dir(u["id"]), "profile.json")] + [os.path.join(_ws_path(a), "profile.json") for a in reversed(u.get("archives") or [])]:
        try:
            pj = json.load(open(p))
            if isinstance(pj, dict):   # an agent's profile.json in another shape is skipped
                break
            pj = None
        except Exception:
            pass
    return ("SCALE KNOBS (lower PRECISION first - never drop a required size, value or series while a precision knob can still be lowered; re-time ONE "
            "unit of work, then produce the full required set; keep every task-book item decidable and "
            "document the reduction under known_limits): measured scale_knobs=%s; planner-declared scale_knobs=%s."
            % (json.dumps(((pj or {}).get("scale_knobs") or []), ensure_ascii=False)[:400],
               json.dumps(u.get("scale_knobs") or [], ensure_ascii=False)[:400]))
