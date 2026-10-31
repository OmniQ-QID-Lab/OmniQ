"""Shared machinery of the offline tests: a temporary workspace, the fake dsh on PATH, short clocks, one orchestrator run per scenario,
and the check / skip / summary helpers every test file uses.

Knobs (read by the test process; none of them reaches the orchestrator):
  RA_TEST_ORCH    the orchestrator module under test (default: residual_agents/orchestrator.py of this repository)
  RA_TEST_NOTES   the memory folder (default: residual_agents/memories, the shipped memories)
  RA_TEST_KEEP    1 = keep every temporary workspace; failed = keep only those of failed scenarios (default: remove them)
Temporary folders are made by tempfile, so TMPDIR decides where they go (tests/run_all.sh: build/test-output/tmp).
Each orchestrator process records every socket or urllib call it attempts in <fake state>/net_attempts.log (a Python audit
hook); the end-to-end checks require that log to stay empty.
No test process writes bytecode caches, so a module loaded from outside the repository (RA_TEST_ORCH, a reference renderer)
leaves its folder unchanged.
"""
import json, os, shutil, signal, subprocess, sys, tempfile, time
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ORCH = os.environ.get("RA_TEST_ORCH") or os.path.join(REPO, "residual_agents", "orchestrator.py")
NOTES = os.environ.get("RA_TEST_NOTES") or os.path.join(REPO, "residual_agents", "memories")
FAKE_DSH = os.path.join(HERE, "fake_dsh.py")
# variables of the caller's environment that would change the orchestrator's or dsh's behaviour: never passed to a test episode
STRIP_PREFIXES = ("RA3_", "RA3SR_", "PECE_", "RA_", "DEEPSEEK_", "DSH_")

def strip_env(env):
    """Remove every variable of STRIP_PREFIXES from env (a dict or os.environ) in place; the tests' own RA_TEST_* knobs stay."""
    for k in list(env):
        if k.startswith(STRIP_PREFIXES) and not k.startswith("RA_TEST_"):
            env.pop(k)
    return env

CLAUDE_MD = """# Task: Reproduce Figure 2

## Paper
- **Title**: a synthetic test paper (not judged)

## Objective
Reproduce Figure 2: the observable O(t) for p = 1, 2, 3 with method A, against the exact reference.

## General Guidelines
- Strictly follow the methodology of the paper.
- All results MUST come from your own computation.

## Figure Fidelity Standard
- **Scientific accuracy**: computed correctly by the agent's own method.
- **Required series**: every series p = 1, 2, 3 present, with a legend.

## Paper-Specific Preliminaries
- Method A with component A1 MUST be used.
- The coupling is J = 1.
"""

def plan_obj(units=None, reqs=None, reads=None):
    units = units or [unit("u1")]
    ids = [u["id"] for u in units]
    units = units + [{"id": "render", "type": "render", "goal": "render the figure and write report.md", "deliverables": ["/workspace/render/report.md"],
                      "done_criteria": ["report.md exists"], "depends_on": ids, "cores": 2, "mem_gb": 4}]
    return {"requirements": reqs if reqs is not None else [{"id": "Q%d" % (i + 1), "F": "F5", "what": "series of %s" % u, "owner": u, "core": i == 0,
                                                            "precision_target": None} for i, u in enumerate(ids)],
            "readings": reads if reads is not None else [{"id": "K1", "F": "F7", "reading": "J = 1 as written", "why": "F7", "check": "none"}],
            "units": units}

def unit(uid, deps=(), cores=4, mem=8, alloc=30, gpu=False, needs=None, goal=None):
    u = {"id": uid, "type": "produce", "goal": goal or "compute the series of %s with method A" % uid,
         "deliverables": ["/workspace/%s/data/" % uid, "/workspace/%s/src/%s_solver.py" % (uid, uid)], "done_criteria": ["the data files exist (F5)"],
         "depends_on": list(deps), "cores": cores, "mem_gb": mem, "alloc_min": alloc, "scale_knobs": ["samples: 100..1000"]}
    if gpu:
        u["gpu"] = True
    if deps:
        u["needs"] = needs or dict((d, "/workspace/%s/data/ - the upstream data" % d) for d in deps)
    return u

def deliver(uid, extra=None):
    """Actions that write a unit's deliverables and a valid handoff."""
    acts = [{"npz": "/workspace/%s/data/a.npz" % uid}, {"write": "/workspace/%s/src/%s_solver.py" % (uid, uid), "text": "def solve(x):\n    return 2 * x\n"},
            {"handoff": {"uid": uid, "deliverables": [{"path": "/workspace/%s/data/" % uid, "what": "data"},
                                                      {"path": "/workspace/%s/src/%s_solver.py" % (uid, uid), "what": "code"}]}}]
    return acts + (extra or [])

def A(uid=None, **kw):
    c = {"grade": "A"}
    if uid:
        c["uid"] = uid
    c.update(kw)
    return {"critic": c}

def G(grade, uid=None, **kw):
    c = {"grade": grade, "gates": kw.pop("gates", "G1=pass G2=pass G3=pass" if grade in ("A", "B") else "G1=pass G2=hit G3=hit")}
    if grade in ("C", "D"):
        c["residual"] = True
    if uid:
        c["uid"] = uid
    c.update(kw)
    return {"critic": c}

# short clocks; RA_DSH_SHIM=1 = the local-proxy route of the paper's runs (the fake dsh never connects anywhere)
BASE_ENV = {"PECE_NCPUS": "16", "PECE_MEM_GB": "64", "WALLTIME_SEC": str(8 * 3600), "RA3SR_REPORT_MIN": "45", "PECE_POLL_S": "1", "PECE_SCHED_TICK": "1",
            "PECE_OVERRIDE_TICK": "1", "PECE_WAIT_TICK": "1", "PECE_WAIT_STALL_S": "6", "PECE_WAIT_PROG_STALL_S": "8", "PECE_WAIT_DEAD_GRACE_S": "2",
            "PECE_WAIT_ADOPT_MAX_S": "20", "RA3_INFRA_HOLDS": "1,1,1", "RA3_POOL_WAIT_S": "3", "RA3_FILE_GATE_S": "1", "RA3SR_GPU_FACTS": "none",
            "RA3_SPIN_HOLD_S": "1", "MODEL": "deepseek/deepseek-v4.1-flash", "RA3SR_VISION": "1", "RA3SR_RATES": "0.1,0.02,0.4",
            "RA_DSH_SHIM": "1"}

DRIVER = r'''
import os, sys, importlib.util
sys.dont_write_bytecode = True
def _net_audit(event, args, _log=os.path.join(os.environ["FAKE_STATE"], "net_attempts.log")):
    if event not in ("socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg", "urllib.Request"):
        return
    where = args[1] if event in ("socket.connect", "socket.sendto", "socket.sendmsg") and len(args) > 1 else (args[0] if args else None)
    if event != "socket.getaddrinfo" and event != "urllib.Request" and isinstance(where, (str, bytes)):
        return   # a Unix socket
    with open(_log, "a") as f:
        f.write("%s %s\n" % (event, str(where)[:200]))
if hasattr(sys, "addaudithook"):
    sys.addaudithook(_net_audit)
spec = importlib.util.spec_from_file_location("ra_orchestrator_e2e", os.environ["RA_TEST_ORCH_PATH"])
sr = importlib.util.module_from_spec(spec)
sys.modules["ra_orchestrator_e2e"] = sr
spec.loader.exec_module(sr)
ws = os.environ["FAKE_WS"]
sr.WS, sr.PD, sr.STORE = ws, os.path.join(ws, "_pece"), os.environ["FAKE_STORE"]
sr.STATE_F = os.path.join(sr.PD, "state.json")
sr.DSH_HOME_RA3 = os.path.join(ws, ".dsh_ra3")
sr.DSH_PATCH = sr.DSH_HOME_RA3 + "/gw_patch.yml"
sr.DSH_ZSTD_JS = sr.DSH_HOME_RA3 + "/zstd_cat.js"
import faulthandler
faulthandler.dump_traceback_later(int(os.environ.get("FAKE_HANG_S", "170")), repeat=False, exit=True, file=open(os.path.join(ws, "hang_stack.txt"), "w"))
exec(os.environ.get("FAKE_PRE", ""))
sr.main()
'''

ROOTS = []        # (root, ws) of every scenario run since the last cleanup()
NET_SEEN = []     # network attempts recorded by the orchestrator processes since the last cleanup()

def run_scen(name, rules, env=None, timeout=200, pre="", setup=None):
    """-> dict(rc, out, ws, root, ev (ledger events), calls (fake calls.log lines), net (network attempts), secs).
    env: extra variables for the episode; a value None removes the variable."""
    root = tempfile.mkdtemp(prefix="ra_e2e_%s_" % name)
    ws = os.path.join(root, "ws")
    ROOTS.append((root, ws))
    os.makedirs(os.path.join(ws, "_pece"))
    store = os.path.join(root, "store")
    os.makedirs(store)
    fstate = os.path.join(root, "fake_state")
    os.makedirs(fstate)
    open(os.path.join(ws, "CLAUDE.md"), "w").write(CLAUDE_MD)
    notes = os.path.join(root, "notes")
    shutil.copytree(NOTES, notes)
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "dsh"), "w") as f:
        f.write("#!/bin/bash\nexec %s %s \"$@\"\n" % (sys.executable, FAKE_DSH))
    os.chmod(os.path.join(bindir, "dsh"), 0o755)
    scen_f = os.path.join(root, "scen.json")
    json.dump({"rules": rules}, open(scen_f, "w"))
    if setup:
        setup(ws)
    e = strip_env(dict(os.environ))
    e.update(BASE_ENV)
    for k, v in (env or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    e.update({"FAKE_WS": ws, "FAKE_STORE": store, "FAKE_STATE": fstate, "FAKE_SCEN_FILE": scen_f, "RA_TEST_ORCH_PATH": ORCH,
              "RA3SR_NOTES_DIR": notes, "PATH": bindir + ":" + e.get("PATH", ""), "RUN_ID": "t_%s_%d" % (name, os.getpid()), "PAPER_NUM": "99",
              "FAKE_PRE": pre})
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, "-c", DRIVER], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, cwd=root)
        rc, out = p.returncode, p.stdout.decode(errors="replace")
    except subprocess.TimeoutExpired as te:
        rc, out = "TIMEOUT", (te.stdout or b"").decode(errors="replace")
    ev = []
    lp = os.path.join(ws, "_pece", "ledger.jsonl")
    if os.path.exists(lp):
        for l in open(lp):
            try:
                ev.append(json.loads(l))
            except Exception:
                pass
    cl = os.path.join(fstate, "calls.log")
    calls = [l.strip() for l in open(cl)] if os.path.exists(cl) else []
    nl = os.path.join(fstate, "net_attempts.log")
    net = [l.strip() for l in open(nl)] if os.path.exists(nl) else []
    NET_SEEN.extend(net)
    return {"rc": rc, "out": out, "ws": ws, "root": root, "store": store, "ev": ev, "calls": calls, "net": net, "secs": time.time() - t0}

def kinds(R, k):
    return [e for e in R["ev"] if e.get("kind") == k]

def tags(R):
    return [c.split(":")[1] for c in R["calls"] if c.startswith("call:")]

def _environ_has(pid, needle):
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            return needle in f.read()
    except OSError:
        return False

def kill_leftovers(ws):
    """SIGKILL every process that still carries this workspace in FAKE_WS (fake dsh calls and the workers they started)."""
    needle = b"FAKE_WS=" + ws.encode() + b"\0"
    n = 0
    for d in os.listdir("/proc"):
        if d.isdigit() and int(d) != os.getpid() and _environ_has(int(d), needle):
            try:
                os.kill(int(d), signal.SIGKILL)
                n += 1
            except OSError:
                pass
    return n

def cleanup(failed=False):
    """Stop the leftovers of the scenarios run since the last call and remove their temporary folders (RA_TEST_KEEP keeps them)."""
    keep = os.environ.get("RA_TEST_KEEP", "")
    while ROOTS:
        root, ws = ROOTS.pop()
        n = kill_leftovers(ws)
        if n:
            print("cleanup: %d leftover process(es) of %s stopped" % (n, os.path.basename(root)))
        if keep == "1" or (keep == "failed" and failed):
            print("kept: %s" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)
    del NET_SEEN[:]

FAILS = []
SKIPS = []
def check(name, cond, detail=""):
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else ": " + str(detail)[:2500]), flush=True)
    if not cond:
        FAILS.append(name)
    return cond

def skip(name, why):
    print("SKIP %s: %s" % (name, why), flush=True)
    SKIPS.append(name)

def finish():
    """The summary line tests/run_all.sh reads; returns the exit status (1 when a check failed)."""
    print("%d check(s) failed - %s%s" % (len(FAILS), "GREEN" if not FAILS else "RED " + str(FAILS),
                                         ("; %d skipped" % len(SKIPS)) if SKIPS else ""), flush=True)
    return 1 if FAILS else 0
