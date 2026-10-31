#!/usr/bin/env python3
"""Scenario-driven fake `dsh` for the offline tests (no model, no network).
Invocation like the real CLI (argv mode): dsh --profile headless --patch <patch.yml> "<prompt>".
It writes a dsh-style session log under $DSH_HOME/sessions/--workspace--/session-<uuid>/session.jsonl with usage records (so the
orchestrator settles tokens) and then performs the ACTIONS of the first scenario rule whose regex matches the call tag.
Scenario file ($FAKE_SCEN_FILE, JSON): {"rules": [{"tag": "<regex on the tag>", "nth": [1, 2] (optional: which invocations of this rule),
  "do": [<action>, ...]}]}. Unmatched calls: exit 0 after writing nothing (a turn that ended without output).
Calls run in parallel (scouts, panel critics). The per-rule counters in $FAKE_STATE/counts.json are therefore read, updated and
written under an exclusive flock of $FAKE_STATE/fake.lock, through a temporary file and os.replace, so that no call ever reads a
truncated file or loses another call's update. Every line of calls.log is appended under the same lock.
Actions (paths may start with /workspace - mapped to $FAKE_WS):
  {"write": path, "text": "..."} | {"npz": path} | {"handoff": {...}} (to /workspace/<uid>/handoff.json; "uid" defaults to the tag's unit)
  {"critic": {"grade": "A", "gates": "G1=pass G2=pass G3=pass", "est": 0, "residual": false, "ids": "R1", "anchors": "met", "fix": "...",
              "reading_change": "K1: new reading (evidence: probe_x.txt)"}}  -> /workspace/<uid>/critic_report.md (+ residual.md)
  {"plan": {...}} -> /workspace/_pece/plan.json   {"report": true} -> figures + report.md (render dir + root)
  {"candidate": {"hypothesis": "...", "supports": true, "reading_change": null}} -> <scout dir from the prompt>/candidate.json
  {"select": {"rank": "1 2 3", "also": "none", "disq": "none"}} -> <select file from the prompt> ("also" is the ADOPT line, "disq" the DISQUALIFIED line)
  {"spawn": {"secs": 5, "period": 1, "marker": true, "register": true, "expect_min": 1, "progress": true}} -> a detached worker in the unit dir
  {"sleep": s} | {"rc": n} (exit code) | {"tokens": 0} (no usage records) | {"stream_cut": true} (prints STREAM_CLOSED and exits 1)
Every call appends "call:<tag>:<rule index>:<n>" and observations to $FAKE_STATE/calls.log, among them the endpoint dsh would use:
"endpoint:<tag>:<DEEPSEEK_BASE_URL>:<first 12 hex of the sha256 of DEEPSEEK_API_KEY, or - when it is empty>" and
"model:<tag>:<the model id of the --patch file>"."""
import fcntl, hashlib, json, os, re, subprocess, sys, time, uuid

argv = sys.argv[1:]
PROMPT = argv[-1] if argv else ""
WS = os.environ["FAKE_WS"]
SF = os.environ.get("FAKE_STATE") or os.path.join(os.path.dirname(WS), "fake_state")
os.makedirs(SF, exist_ok=True)
LOCK_F = os.path.join(SF, "fake.lock")
TAGFULL = os.environ.get("PECE_CALL_TAG", "-")
TAG = TAGFULL.split(":", 1)[1] if ":" in TAGFULL else TAGFULL
HOME = os.environ.get("DSH_HOME") or os.path.join(WS, ".dsh_ra3")
SID = str(uuid.uuid4())
SESS = os.path.join(HOME, "sessions", "--workspace--", "session-%s" % SID)
os.makedirs(SESS, exist_ok=True)
LOGF = os.path.join(SESS, "session.jsonl")

class Locked(object):
    """An exclusive flock of $FAKE_STATE/fake.lock: one fake process at a time touches counts.json and calls.log."""
    def __enter__(self):
        self.fd = os.open(LOCK_F, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)

def real(p):
    return p.replace("/workspace", WS, 1) if str(p).startswith("/workspace") else p

def _append(line):
    with open(os.path.join(SF, "calls.log"), "a") as f:
        f.write(line + "\n")

def clog(line):
    with Locked():
        _append(line)

def ev(t, **data):
    return json.dumps({"type": t, "seq": 0, "time": int(time.time() * 1000), "data": data})

def unit_of(tag):
    t = re.sub(r"(_wrapup|_gwretry\d*|_only)+$", "", tag)
    m = re.match(r"[a-z]+\d*_(.+)$", t)
    return m.group(1) if m else None

def patch_model(args):
    """The model id dsh would send: the agent-default-model entry of the --patch file."""
    try:
        txt = open(args[args.index("--patch") + 1]).read()
        m = re.search(r"id: agent-default-model\n  config:\n(?:    .*\n)*?    model: (\S+)", txt)
        return m.group(1) if m else "?"
    except Exception:
        return "?"

UID = unit_of(TAG)
scen = json.load(open(os.environ["FAKE_SCEN_FILE"]))
cnt_f = os.path.join(SF, "counts.json")
base_tag = re.sub(r"_gwretry\d*$", "", TAG)
rule, ri = None, None
with Locked():
    try:
        with open(cnt_f) as f:
            counts = json.load(f)
    except (OSError, ValueError):
        counts = {}
    for i, r in enumerate(scen.get("rules", [])):
        if re.search(r["tag"], base_tag):
            n = counts.get(str(i), 0) + 1
            counts[str(i)] = n
            if r.get("nth") and n not in r["nth"]:
                continue
            rule, ri = r, i
            break
    tmp = "%s.%d.tmp" % (cnt_f, os.getpid())
    with open(tmp, "w") as f:
        json.dump(counts, f)
    os.replace(tmp, cnt_f)
    _append("call:%s:%s:%s" % (TAG, ri, counts.get(str(ri), 0) if ri is not None else 0))
api_key = os.environ.get("DEEPSEEK_API_KEY", "")
clog("endpoint:%s:%s:%s" % (TAG, os.environ.get("DEEPSEEK_BASE_URL", ""), hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12] if api_key else "-"))
clog("model:%s:%s" % (TAG, patch_model(argv)))
for key, needle in (("budget", "BUDGET = "), ("scouting", "SCOUTING:"), ("also", "ALSO ADOPTED"), ("change", "CHANGE NOTICE"),
                    ("readings", "/workspace/READINGS.md"), ("residual_block", "RESIDUALS FOR THIS ROUND"), ("lessons", "LESSONS FOR THIS UNIT"),
                    ("upstream_changed", "UPSTREAM CHANGED"), ("readings_changed", "READINGS CHANGED"), ("tried", "ALREADY TESTED"),
                    ("continuation", "CONTINUATION IN A NEW SESSION"), ("index", "RESIDUAL INDEX"), ("grant", "RESOURCE GRANT"),
                    ("resume", "JOB ENDED"), ("session_hint", "SESSION MODEL:"), ("cut_instructions", "THE FIX ROUND'S INSTRUCTIONS"),
                    ("fix_in_place", "fixed IN PLACE"), ("job_running", "is still running: keep it only if"), ("disq", "DISQUALIFIED by the panel"), ("plan_not_written", "ended WITHOUT writing"),
                    ("mech_na", "MECH CHECK: not available"), ("fault", "internal fault after your last session")):
    if needle in PROMPT:
        clog("saw:%s:%s" % (TAG, key))
m = re.search(r"RESOURCE GRANT: cores=(\d+) mem=(\d+) GB", PROMPT)
if m:
    clog("grant:%s:%s:%s" % (TAG, m.group(1), m.group(2)))
acts = (rule or {}).get("do", [])
tokens_on = not any(isinstance(a, dict) and a.get("tokens") == 0 for a in acts)
if tokens_on:
    with open(LOGF, "a") as f:
        f.write(json.dumps({"type": "session", "version": 0, "id": "session-%s" % SID, "createdAt": int(time.time() * 1000), "cwd": "/workspace"}) + "\n")
        f.write(ev("assistant/message", message={"role": "assistant", "content": [{"type": "text", "text": "working"}]},
                   usage={"inputTokens": 1000, "outputTokens": 200, "cacheReadTokens": 100, "reasoningTokens": 50}) + "\n")

def gates_line(g):
    return "GATES: " + g

rc = 0
for a in acts:
    if not isinstance(a, dict):
        continue
    if "sleep" in a:
        time.sleep(float(a["sleep"]))
    if "write" in a:
        p = real(a["write"]); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(a.get("text", "x\n"))
    if "npz" in a:
        p = real(a["npz"]); os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            import numpy as np
            np.savez(p, x=np.arange(3))
        except Exception:
            open(p, "wb").write(b"PK")
    if "handoff" in a:
        h = dict(a["handoff"]); uid = h.get("uid") or UID
        h["uid"] = uid
        h.setdefault("deliverables", []); h.setdefault("key_numbers", {}); h.setdefault("criteria", []); h.setdefault("how_to_use", "x")
        h.setdefault("checks", []); h.setdefault("known_limits", "none"); h.setdefault("stopped", None)
        w = h.get("waiting")
        if isinstance(w, dict) and w.get("pid") == "BGPID":
            try:
                w["pid"] = int(open(real("/workspace/%s/bg.pid" % uid)).read().split()[0])
            except Exception:
                w["pid"] = 999999
        p = real("/workspace/%s/handoff.json" % uid); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(json.dumps(h))
    if "critic" in a:
        c = a["critic"]; uid = c.get("uid") or UID
        d = real("/workspace/%s" % uid); os.makedirs(d, exist_ok=True)
        L = ["GRADE: %s" % c.get("grade", "A"), gates_line(c.get("gates", "G1=pass G2=pass G3=pass")), "EVIDENCE", "verified (fake)"]
        L += ["ANCHORS", "| quantity | paper | ours | ratio | verdict |", "|---|---|---|---|---|",
              "| claim | 1.0 | 1.0 | 1.0 | %s |" % c.get("anchors", "met")]
        L += ["FIX LIST", c.get("fix", "- nothing"), "REDUCIBLE / NOT REDUCIBLE: precision may be lowered; no required row may be dropped"]
        if c.get("reading_change"):
            L.append("READING_CHANGE: %s" % c["reading_change"])
        L.append("RESIDUAL_IDS: %s" % c.get("ids", "none"))
        L.append('FIX_PLAN: {"est_min": %s, "steps": ["fake step"]}' % c.get("est", 0))
        open(os.path.join(d, "critic_report.md"), "w").write("\n".join(L) + "\n")
        if c.get("residual"):
            open(os.path.join(d, "residual.md"), "w").write("SIGNATURE: fake defect\nPROBE: probe_fake.txt\nSUGGESTION: fix it\nREUSABLE:\n/workspace/%s/src/solver.py - code - reviewed\n" % uid)
    if "plan" in a:
        p = real("/workspace/_pece/plan.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(json.dumps(a["plan"]))
    if "report" in a:
        for d in (real("/workspace/render"), real("/workspace")):
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "report.md"), "w").write("# report\nF1 - met\n")
            open(os.path.join(d, "reproduced_figure.png"), "wb").write(b"\x89PNG\r\n\x1a\nfake")
            open(os.path.join(d, "reproduced_figure.pdf"), "wb").write(b"%PDF-fake")
        open(real("/workspace/render/handoff.json"), "w").write(json.dumps({"uid": "render", "deliverables": [{"path": "/workspace/render/report.md", "what": "r"}],
            "key_numbers": {}, "criteria": [], "how_to_use": "x", "checks": [], "known_limits": "none", "stopped": None}))
    if "candidate" in a:
        m = re.search(r"Your directory is (/workspace/\S+?)/ -", PROMPT)
        sdir = real(m.group(1)) if m else None
        if sdir:
            os.makedirs(sdir, exist_ok=True)
            c = dict(a["candidate"]); c.setdefault("poc", {"command": "python poc.py", "result_file": "poc.txt", "finding": "fake finding 1.0"})
            c.setdefault("plan", {"steps": ["do it"], "est_full_min": 60, "scale_knobs": []}); c.setdefault("angle", "fake")
            open(os.path.join(sdir, "candidate.json"), "w").write(json.dumps(c))
            open(os.path.join(sdir, "poc.py"), "w").write("print(1)\n")
            clog("scout_dir:%s:%s" % (TAG, sdir))
    if "select" in a:
        m = re.search(r"Write (/workspace/\S+?select\d+\.md) in ONE pass", PROMPT)
        if m:
            s = a["select"]
            open(real(m.group(1)), "w").write("RANK: %s\nADOPT: %s\nDISQUALIFIED: %s\nfake panel\n" % (s.get("rank", "1 2 3"), s.get("also", "none"), s.get("disq", "none")))
    if "spawn" in a:
        s = a["spawn"]; uid = s.get("uid") or UID
        ud = real("/workspace/%s" % uid); os.makedirs(os.path.join(ud, "data"), exist_ok=True)
        script = ("import os, sys, time\nud, secs, period, mk = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]\n"
                  "open(os.path.join(ud, 'bg.pid'), 'w').write(str(os.getpid()))\n"
                  "t0 = time.time(); i = 0\n"
                  "while time.time() - t0 < secs:\n"
                  "    i += 1\n"
                  "    open(os.path.join(ud, 'data', 'pt_%03d.json' % i), 'w').write('{\"i\": %d}' % i)\n"
                  "    open(os.path.join(ud, 'bg.log'), 'a').write('step %d\\n' % i)\n"
                  "    time.sleep(period)\n"
                  "if mk != '-':\n    open(mk, 'w').write('done')\n")
        mk = os.path.join(ud, "bg.done") if s.get("marker", True) else "-"
        p = subprocess.Popen([sys.executable, "-c", script, ud, str(s.get("secs", 5)), str(s.get("period", 1)), mk], preexec_fn=os.setsid,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        time.sleep(0.3)
        clog("spawned:%s:%d" % (uid, p.pid))
        if s.get("register"):
            h = {"uid": uid, "deliverables": [{"path": "/workspace/%s/data/" % uid, "what": "points"}], "key_numbers": {}, "criteria": [], "how_to_use": "x",
                 "checks": [], "known_limits": "running", "stopped": None,
                 "waiting": {"pid": p.pid, "done_marker": "/workspace/%s/bg.done" % uid, "expect_min": s.get("expect_min", 1), "resume_note": "verify",
                             "log": "/workspace/%s/bg.log" % uid, "progress": (["/workspace/%s/data/*.json" % uid] if s.get("progress") else [])}}
            open(os.path.join(ud, "handoff.json"), "w").write(json.dumps(h))
    if "rc" in a:
        rc = int(a["rc"])
    if a.get("stream_cut"):
        print("Error: STREAM_CLOSED by upstream")
        rc = 1
print("fake dsh done: %s" % TAG)
sys.exit(rc)
