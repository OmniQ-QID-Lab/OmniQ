
# ---------------------------------------------------------------- propagation: ONE rule - an input a unit consumed changed (upstream files, READINGS.md)
def apply_reading_change(kid, reading, evidence, by_uid, source):
    """A reading changes (a critic's FIX_PLAN reading_change with its evidence file, or an adopted scout candidate): READINGS.md gets the new version;
    every unit that consumed READINGS.md then sees the change through the input rule. A flip back to an earlier reading needs new evidence."""
    kid = str(kid).strip().upper()
    norm = lambda x: re.sub(r"\W+", " ", str(x or "")).strip().lower()
    with STATE_LOCK:
        rd = RUN.setdefault("readings", [])
        ent = next((x for x in rd if str(x.get("id")).upper() == kid), None)
        if ent is not None and norm(ent.get("reading")) == norm(reading):
            return False
        if ent is not None and ent.get("history"):   # one change per reading per episode: later conflicting evidence is a note for the report
            RUN.setdefault("reading_conflicts", []).append({"id": kid, "reading": reading[:300], "evidence": evidence[:200], "by": by_uid})
            ledger({"kind": "reading_change_rejected", "unit": by_uid, "id": kid, "why": "already changed once this episode", "evidence": evidence[:200]})
            return False
        if ent is None:
            ent = {"id": kid, "F": "-", "reading": reading, "version": 0}
            rd.append(ent)
        ent.setdefault("history", []).append({"version": int(ent.get("version") or 1), "reading": ent.get("reading"), "evidence": evidence})
        old = ent.get("reading")
        ent["version"] = int(ent.get("version") or 1) + 1
        ent["reading"] = reading
        ent["changed"] = "%s (was: %s; evidence: %s; by %s, %s)" % (reading, str(old)[:200], evidence or "-", by_uid, source)
        SCHED_CV.notify_all()
    write_readings(RUN["readings"])
    try:
        _atomic_json_dump(RUN["readings"], os.path.join(PD, "readings.json"))
    except Exception:
        pass
    ledger({"kind": "reading_change", "id": kid, "version": ent["version"], "reading": reading[:300], "evidence": (evidence or "")[:200], "by": by_uid, "source": source})
    log("READING CHANGE %s -> v%d by %s (%s): %s" % (kid, ent["version"], by_uid, source, reading[:160]))
    propagate_inputs(exclude=by_uid)
    return True

def _quick_sig(rp):
    """(size, mtime) of a file / (count, total size, newest mtime) of a directory (bounded) / None when absent."""
    try:
        if os.path.isdir(rp):
            n = tot = newest = 0
            for root, dirs, files in os.walk(rp):
                dirs[:] = [d for d in dirs if d not in REUSE_SKIP_DIRS]
                for fn in files:
                    try:
                        st = os.stat(os.path.join(root, fn))
                    except OSError:
                        continue
                    n += 1; tot += st.st_size; newest = max(newest, int(st.st_mtime))
                    if n > 5000:
                        return [n, tot, newest]
            return [n, tot, newest]
        st = os.stat(rp)
        return [st.st_size, int(st.st_mtime)]
    except OSError:
        return None

def _file_hash(p, chunk=1 << 20):
    h = hashlib.sha1()
    try:
        sz = os.path.getsize(p)
        with open(p, "rb") as f:
            h.update(f.read(chunk))
            if sz > 2 * chunk:
                f.seek(-chunk, 2)
                h.update(f.read(chunk))
        h.update(str(sz).encode())
    except OSError:
        return None
    return h.hexdigest()

def _content_sig(rp):
    """Content signature (heads and tails of the files, sizes, names) - a rewrite with identical content is no change."""
    if os.path.isdir(rp):
        h, n = hashlib.sha1(), 0
        for root, dirs, files in os.walk(rp):
            dirs[:] = sorted(d for d in dirs if d not in REUSE_SKIP_DIRS)
            for fn in sorted(files):
                n += 1
                if n > 2000:
                    break
                fp = os.path.join(root, fn)
                h.update(os.path.relpath(fp, rp).encode("utf-8", "surrogateescape"))   # any file name
                h.update(str(_file_hash(fp, 65536)).encode())
        return h.hexdigest()
    return _file_hash(rp) if os.path.exists(rp) else None

def record_inputs(u):
    """The unit's consumed inputs, fingerprinted: its needs files, its upstreams' declared deliverables, and READINGS.md (+ the reading versions)."""
    fp = {}
    paths = []
    for d in (u.get("depends_on") or []):
        du_ = unit_by_id(d) or {}
        paths += need_paths((u.get("needs") or {}).get(d), d) + [x for x in (du_.get("deliverables") or []) if isinstance(x, str)]
    if u.get("type") == "produce":
        paths.append("/workspace/READINGS.md")
    for p in paths[:40]:
        if any(ch in p for ch in "*?[{<") or p in fp:
            continue
        rp = _ws_path(p)
        q = _quick_sig(rp)
        if q is not None:
            fp[p] = {"q": q, "h": _content_sig(rp)}
    u["inputs"] = fp
    u["readings_v"] = dict((str(x.get("id")), int(x.get("version") or 1)) for x in (RUN.get("readings") or []))

def upstream_changes(u):
    """Inputs this unit consumed whose CONTENT changed since they were recorded -> [path] (a rewrite with the same content is no change)."""
    out = []
    for p, sig in list((u.get("inputs") or {}).items()):
        rp = _ws_path(p)
        q = _quick_sig(rp)
        if q == sig.get("q"):
            continue
        if q is None:
            out.append(p + " (gone)")
            continue
        h = _content_sig(rp)
        if h == sig.get("h"):
            sig["q"] = q
            continue
        out.append(p)
    return out

def change_notice(u):
    """The facts of input changes the unit has not been audited against yet (orchestrator truth), for its prompts and audits."""
    ch = upstream_changes(u)
    if not ch:
        return ""
    L = ["INPUT CHANGED since this unit consumed it (orchestrator facts): %s - check whether this unit used the old content and what must be recomputed."
         % ", ".join(ch[:12])]
    if "/workspace/READINGS.md" in ch:
        seen = u.get("readings_v") or {}
        for x in (RUN.get("readings") or []):
            if int(x.get("version") or 1) > int(seen.get(str(x.get("id")), 1)):
                L.append("READINGS CHANGED: %s v%d: %s" % (x.get("id"), int(x.get("version") or 1), str(x.get("changed") or x.get("reading"))[:400]))
    return "\n".join(L)

def propagate_inputs(exclude=None):
    """Every DONE produce unit whose consumed inputs changed is flagged for a reopen (a delta audit of the change); running units see the change in
    their next prompt and audit (change_notice)."""
    with STATE_LOCK:
        cands = [v for v in RUN["plan"] if isinstance(v, dict) and v.get("type") == "produce" and v.get("status") == "done"
                 and v.get("id") != exclude and not v.get("reopen")]
    found = [(v, change_notice(v)) for v in cands]   # content hashing outside the lock (the time trigger never waits on it)
    with STATE_LOCK:
        for v, n in found:
            if n and v.get("status") == "done" and not v.get("reopen"):
                v["reopen"] = {"why": n, "t": time.time()}
                ledger({"kind": "input_changed", "unit": v["id"], "notice": n[:400]})
        SCHED_CV.notify_all()
