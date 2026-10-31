
# ---------------------------------------------------------------- dsh session logs: usage accounting, transcript tails, continuations
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _zstd_cat(path):
    """Decoded text of a dsh session log: plain jsonl as is; .zstd via zstd(1), python-zstandard, then node + zstd_cat.js."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except Exception:
        return ""
    if head != _ZSTD_MAGIC:
        return read(path)
    try:
        rr = subprocess.run(["zstd", "-dc", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if rr.returncode == 0 and rr.stdout:
            return rr.stdout.decode(errors="ignore")
    except Exception:
        pass
    try:
        import zstandard
        with open(path, "rb") as f:
            with zstandard.ZstdDecompressor().stream_reader(f, read_across_frames=True) as zr:
                return zr.read().decode(errors="ignore")
    except Exception:
        pass
    if not os.path.exists(DSH_ZSTD_JS):
        try:
            os.makedirs(os.path.dirname(DSH_ZSTD_JS), exist_ok=True)
            open(DSH_ZSTD_JS, "w").write(ZSTD_CAT_JS)
        except Exception:
            return ""
    for node in (DSH_NODE, "node"):
        try:
            rr = subprocess.run([node, DSH_ZSTD_JS, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if rr.returncode == 0 and rr.stdout:
                return rr.stdout.decode(errors="ignore")
        except Exception:
            continue
    return ""


def _dsh_session_logs(home):
    """dsh 0.1.5 (session format V3) names the log session.v3.jsonl.zstd (V0: session.jsonl.zstd); an in-progress write is
    <name>.<hex>.tmp and a migration session.migration.<token>.jsonl.zstd.tmp - both skipped."""
    return sorted(p for p in glob.glob(os.path.join(home, "sessions", "*", "session-*", "session*.jsonl*")) if not p.endswith(".tmp"))


def _dsh_usage_logs(home):
    """Every session log a call's usage comes from: the main session (sessions/<cwd>/session-<uuid>/) and the dsh sub-agent sessions
    it started (sessions/<cwd>/<uuid>/, no "session-" prefix); in-progress writes (.tmp) are skipped. The session id and the
    transcript tail read the main session only (_dsh_session_of, _dsh_session_logs)."""
    return sorted(p for p in glob.glob(os.path.join(home, "sessions", "*", "*", "session*.jsonl*")) if not p.endswith(".tmp"))


def _dsh_session_of(home):
    """uuid of the (newest) session dsh created under a call's DSH_HOME, None if none."""
    dirs = glob.glob(os.path.join(home, "sessions", "*", "session-*"))
    if not dirs:
        return None
    try:
        d = max(dirs, key=lambda x: os.stat(x).st_mtime)
    except OSError:
        d = sorted(dirs)[-1]
    return os.path.basename(d)[len("session-"):]


def _dsh_find_home(sid):
    """DSH_HOME of the call that created session `sid` (registry first, then the disk)."""
    rec = DSH_SESSIONS.get(sid)
    if rec and rec.get("home"):
        return rec["home"]
    hits = glob.glob(os.path.join(DSH_HOME_RA3, "calls", "*", "sessions", "*", "session-%s" % sid))
    return os.path.dirname(os.path.dirname(os.path.dirname(hits[0]))) if hits else None

DSH_USAGE_FIELDS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "totalTokens")   # dsh-llm TokenUsage (0.1.5: + cacheWrite, total)


def _dsh_last_stream_usage(stream):
    """V3: the last usage chunk of the raw stream a record embeds (dsh-llm's lastAssistantStreamChunk); None for V0 records (no stream)."""
    for rec in reversed(stream or []):
        if isinstance(rec, dict) and rec.get("type") == "chunk" and isinstance(rec.get("chunk"), dict) and rec["chunk"].get("type") == "usage":
            return rec["chunk"].get("usage")
    return None


def _dsh_usage_detail(home):
    """Per-field token sums (dsh 0.1.5, session format V3) over the event records of THIS call's logs, the sub-agent sessions included
    (_dsh_usage_logs), plus usage_calls (assistant/message records) and retried_attempts (assistant/attempt records). A V3 assistant/message
    carries its usage TWICE (data.usage and a usage chunk inside data.stream): data.usage is taken, else the last stream chunk - never both.
    An assistant/attempt (an attempt llm-retry gave up on) has stream usage only and is billed by the endpoint all the same, so it counts.
    V0 lines (no stream field) read exactly as before."""
    tot = {k: 0 for k in DSH_USAGE_FIELDS}
    calls = attempts = 0
    for p in _dsh_usage_logs(home):
        for ln in _zstd_cat(p).splitlines():
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            t, d = e.get("type"), (e.get("data") or {})
            if not isinstance(d, dict):
                continue
            if t == "assistant/message":
                u = d.get("usage") or _dsh_last_stream_usage(d.get("stream"))
                calls += 1
            elif t == "assistant/attempt":
                u = _dsh_last_stream_usage(d.get("stream"))
                attempts += 1
            else:
                continue
            if not isinstance(u, dict):
                continue
            for k in DSH_USAGE_FIELDS:
                v = u.get(k)
                if isinstance(v, int) and not isinstance(v, bool):
                    tot[k] += v
    tot["usage_calls"], tot["retried_attempts"] = calls, attempts
    return tot


def _dsh_events(home):
    out = []
    for p in _dsh_session_logs(home):
        for ln in _zstd_cat(p).splitlines():
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def _dsh_transcript_tail(home, max_chars=DSH_CTX_CHARS):
    """Last assistant texts / tool calls / tool results of a session, newest last, cut to max_chars from the END."""
    L = []
    for e in _dsh_events(home):
        t, d = e.get("type"), (e.get("data") or {})
        try:
            if t == "assistant/message":
                blocks = ((d.get("message") or {}).get("content") or [])
                txt = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
                if txt:
                    L.append("ASSISTANT: " + txt[:700])
            elif t == "tool/call":
                L.append("TOOL %s %s" % (d.get("name", "?"), str(d.get("arguments", ""))[:300]))
            elif t == "tool/result":
                parts = []
                for c in ((d.get("message") or {}).get("content") or []):
                    for cc in (c.get("content") or []) if isinstance(c, dict) else []:
                        if isinstance(cc, dict) and cc.get("type") == "text":
                            parts.append(cc.get("text", ""))
                txt = "\n".join(parts).strip()
                if txt:
                    L.append("RESULT: " + txt[:400])
            elif t == "turn/end":
                L.append("TURN END: %s" % json.dumps(d.get("reason"), ensure_ascii=False)[:160])
        except Exception:
            continue
    if not L:
        return ""
    out, size = [], 0
    for ln in reversed(L):
        if size + len(ln) + 1 > max_chars:
            break
        out.append(ln)
        size += len(ln) + 1
    if len(out) < len(L):
        out.append("[... %d earlier entries omitted ...]" % (len(L) - len(out)))
    return "\n".join(reversed(out))


def _dsh_files_written(t0, t1, limit=40):
    """Workspace files (outside the pipeline's own dirs) modified inside [t0, t1]."""
    hits = []
    for root, dirs, files in os.walk(WS):
        if "_pece" in root or ".codex-rw" in root or ".dsh_ra3" in root:
            dirs[:] = []
            continue
        for fn in files:
            if fn == "dsh_shim.log":
                continue
            p = os.path.join(root, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if t0 - 2 <= st.st_mtime <= t1 + 2:
                hits.append((st.st_mtime, p, st.st_size))
    hits.sort(reverse=True)
    rel = ["%s (%d B)" % (os.path.relpath(p, WS), sz) for _, p, sz in hits[:limit]]
    return rel, len(hits)


def _dsh_ended(rc, intr, killed=False):
    if intr:
        return "was interrupted by the orchestrator (%s)" % intr[0]
    return {0: "completed its turn", -1: "was stopped at its time limit", -2: "failed inside the orchestrator",
            -9: "was killed by a SIGKILL from outside the orchestrator (a kill or pkill run by some unit) - what it wrote before is on disk"}.get(rc, "exited with code %s" % rc)


def _dsh_continuation(sid, tag):
    """dsh headless cannot resume a session: the follow-up call carries what the orchestrator knows about the previous one."""
    rec = dict(DSH_SESSIONS.get(sid) or {})
    home = rec.get("home") or _dsh_find_home(sid)
    uid = rec.get("uid") or _tag_uid(tag)
    t0, t1 = rec.get("t0"), rec.get("t1")
    L = ["CONTINUATION IN A NEW SESSION: the previous session of this role (unit %s, call %s) %s. This is a FRESH "
         "session - it has NONE of that session's memory; the files on disk are the truth: re-inspect them before "
         "acting, never restart finished work, never claim to remember what is not written down. What the "
         "orchestrator knows about the earlier session follows."
         % (uid or "?", rec.get("tag") or "?", rec.get("ended") or "ended (details unknown: the orchestrator was restarted since)")]
    if t0 and t1:
        L[0] += " It ran %.0f minutes." % ((t1 - t0) / 60.0)
    if home:
        try:
            tail = _dsh_transcript_tail(home)
        except Exception as e:
            tail = "(transcript unavailable: %s)" % e
        if tail:
            L.append("PREVIOUS SESSION TRANSCRIPT TAIL (newest last):\n" + tail)
    if t0 and t1:
        try:
            rel, n = _dsh_files_written(t0, t1)
        except Exception:
            rel, n = [], 0
        if rel:
            L.append("FILES WRITTEN DURING THAT SESSION (%d; newest first%s): %s"
                     % (n, "" if n <= len(rel) else ", first %d listed" % len(rel), "; ".join(rel)))
    if uid:
        logp = os.path.join(unit_dir(uid), "run.log")
        if not os.path.exists(logp):
            cands = []
            for p in glob.glob(os.path.join(WS, "logs", "*.log")) + glob.glob(os.path.join(unit_dir(uid), "*.log")):
                m = _mtime(p)   # a job may rotate its logs between the listing and the stat
                if m is not None and t0 and t1 and t0 - 2 <= m <= t1 + 2:
                    cands.append((m, p))
            logp = max(cands)[1] if cands else None
        if logp and os.path.exists(logp):
            tl = "\n".join(read(logp).splitlines()[-25:])[-1500:]
            if tl.strip():
                L.append("TAIL OF %s:\n%s" % (os.path.relpath(logp, WS), tl))
        hf = os.path.join(unit_dir(uid), "handoff.json")
        if os.path.exists(hf):
            L.append("HANDOFF.JSON CURRENTLY ON DISK (/workspace/%s/handoff.json, first lines):\n%s"
                     % (uid, read(hf)[:1200]))
    return "\n\n".join(L)
