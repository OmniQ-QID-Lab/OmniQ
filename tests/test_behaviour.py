"""Regression tests of specific behaviours, by group (run one or more groups by name; default: all).

dsh_endpoint: with RA_DSH_SHIM=1 (the paper's route) dsh talks to the local proxy with a dummy key and the bare model id,
whatever the caller's DEEPSEEK_* variables say; otherwise dsh gets the caller's DEEPSEEK_BASE_URL (default https://api.deepseek.com)
and DEEPSEEK_API_KEY unchanged, and the full model id. RA_DSH_NODE puts its folder first on the agents' PATH only when it is set.
End to end: direct_mode_no_key, direct_mode_passthrough, proxy_mode_endpoint (tests/test_e2e.py).
stopping: SIGTERM, SIGINT or SIGHUP kills every process that carries the run's markers (PECE_CALL_TAG=<RUN_ID>: or
PECE_UNIT=<RUN_ID>:, with the trailing colon), records orchestrator_stopped and exits with 128 + the signal number; a signal the
caller ignores stays ignored (nohup); no new session starts once the stop has begun. End to end: stop_signal (this file).
notes_missing: a memory folder that does not exist stops the run with exit 2 before anything is written; missing files are
recorded (ledger notes_missing) and warned about, and the run goes on (the ablation switch). End to end: notes_dir_missing.
run_id: without RUN_ID (unset or empty) every process gets its own id ra_<pid>_<epoch>; a set id is unchanged.
node_facts: without PECE_MEM_GB the memory is the smallest limit in the process's own cgroup chain (v2 memory.max,
v1 memory.limit_in_bytes, the root files), else physical RAM; without PECE_NCPUS the affinity count is capped by the cgroup v2 CPU
quota (cpu.max); set values win; the source of each value is logged. Fake cgroup trees (CGROUP_ROOT, CGROUP_SELF); optionally a
real transient scope (systemd-run --user, skipped where unavailable). End to end: node_facts_logged.
session_ids: a dry reap forgets the ids of sessions that no process holds any more, in place; run_call forgets its own id when
its session is empty at the end of the call; a session with a live process keeps its id.
episode_record: the digest skips ledger lines that are not JSON objects, and meta.json and the critic copies are written even
when the digest fails.
npy_check: 14 kinds of array file give the same record as a full np.load; (slow, skipped with RA_TEST_SKIP_SLOW=1) the check of
a 512 MiB array reads its header only.
subagent_usage: a call's usage counts its dsh sub-agent sessions (sessions/<cwd>/<uuid>/, no "session-" prefix; .tmp files
skipped); the session id and the transcript tail stay the main session's.

Usage: python3 tests/test_behaviour.py [group ...]   (RA_TEST_ORCH: another orchestrator module)
No key value is ever printed.
"""
import contextlib, importlib.util, json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import e2e_lib
from e2e_lib import ORCH, NOTES, NET_SEEN, strip_env, check, finish, run_scen, cleanup, kinds, plan_obj, deliver, A

strip_env(os.environ)
os.environ.update({"PECE_NCPUS": "16", "PECE_MEM_GB": "64", "RA3SR_GPU_FACTS": "none", "RUN_ID": "tf_%d" % os.getpid(), "RA3SR_NOTES_DIR": NOTES,
                   "MODEL": "deepseek/deepseek-v4.1-flash"})
spec = importlib.util.spec_from_file_location("ra_orchestrator_behaviour", ORCH)
sr = importlib.util.module_from_spec(spec)
sys.modules["ra_orchestrator_behaviour"] = sr
spec.loader.exec_module(sr)
ROOT = tempfile.mkdtemp(prefix="ra_behaviour_")
sr.WS, sr.PD, sr.STORE = os.path.join(ROOT, "ws"), os.path.join(ROOT, "ws", "_pece"), os.path.join(ROOT, "store")
sr.STATE_F = os.path.join(sr.PD, "state.json")
sr.DSH_HOME_RA3 = os.path.join(sr.WS, ".dsh_ra3")
sr.DSH_PATCH = sr.DSH_HOME_RA3 + "/gw_patch.yml"
sr.DSH_ZSTD_JS = sr.DSH_HOME_RA3 + "/zstd_cat.js"
for d in (sr.PD, sr.STORE):
    os.makedirs(d)

CALLER_KEY = "caller-key-for-tests"          # dummy values: nothing here contacts an endpoint
CALLER_URL = "https://direct-test.example.invalid/v1"
SHOWN_HOSTS = ("http://127.0.0.1:", "https://api.deepseek.com", CALLER_URL)
PLAN = [{"tag": r"^plan$", "do": [{"plan": plan_obj()}]}]
REPORT = [{"tag": r"^report_render$", "do": [{"report": True}]}, {"tag": r"^critic_render$", "do": [A("render")]}]
MAIN_SID, SUB_SID = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"   # subagent_usage: session folder names

def show_url(u):
    """A URL for a failure message: only the hosts this test uses are shown."""
    return u if isinstance(u, str) and u.startswith(SHOWN_HOSTS) else "<another endpoint>"

def show_key(k):
    return "<caller's key>" if k == CALLER_KEY else ("<empty>" if not k else ("shim-local" if k == "shim-local" else "<another value>"))

@contextlib.contextmanager
def env(**kv):
    """Set (value) or remove (None) environment variables for the block."""
    old = dict((k, os.environ.get(k)) for k in kv)
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

def safe(name, fn):
    """Run fn() -> (cond, detail); an exception is a failed check."""
    try:
        cond, detail = fn()
    except Exception:
        cond, detail = False, traceback.format_exc().strip().splitlines()[-1]
    return check(name, cond, detail)

def in_fresh_python(code, extra_env=None, head="", timeout=120):
    """Run `code` in a new interpreter that has the orchestrator under test loaded as `m` (after `head`); no bytecode cache is
    written. extra_env: a value None removes the variable. -> (exit status, output)."""
    load = ("import importlib.util, sys\nspec = importlib.util.spec_from_file_location('ra_orchestrator_child', %r)\n"
            "m = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n" % ORCH)
    e = dict(os.environ)
    for k, v in (extra_env or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    p = subprocess.run([sys.executable, "-B", "-c", head + load + code], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode(errors="replace")

def sleeper(extra_env, secs=60):
    """A process in its own session that only sleeps, with the given variables added to (or, for None, removed from) the environment."""
    e = dict(os.environ)
    for k in ("PECE_UNIT", "PECE_CALL_TAG"):
        e.pop(k, None)
    for k, v in extra_env.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%d)" % secs], env=e, start_new_session=True)

def stop(*procs):
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=10)
        except Exception:
            pass

FAKE_DSH_SH = """#!/bin/sh
# a stand-in for dsh in the in-process run_call checks: it records its start (its pid, the session leader's, and the call
# marker) and ends its turn at once; with RA_TEST_LEAVE_CHILD set it leaves a sleeping child in its own session; with
# RA_TEST_SESSIONS set it writes a main session and a sub-agent session (copies of the logs in that folder) to its DSH_HOME
echo "$$ $PECE_CALL_TAG" >> "$RA_TEST_STARTED"
if [ -n "$RA_TEST_LEAVE_CHILD" ]; then sleep 60 & echo $! > "$RA_TEST_LEAVE_CHILD"; fi
if [ -n "$RA_TEST_SESSIONS" ]; then
  s="$DSH_HOME/sessions/--workspace--"
  mkdir -p "$s/session-$RA_TEST_MAIN_ID" "$s/$RA_TEST_SUB_ID"
  cp "$RA_TEST_SESSIONS/main.jsonl" "$s/session-$RA_TEST_MAIN_ID/session.jsonl"
  cp "$RA_TEST_SESSIONS/sub.jsonl" "$s/$RA_TEST_SUB_ID/session.jsonl"
fi
exit 0
"""

@contextlib.contextmanager
def fake_dsh(started_log, leave_child=None, sessions=None):
    """The block's run_call finds FAKE_DSH_SH as dsh on PATH; every start is appended to started_log. sessions: the folder with
    main.jsonl and sub.jsonl (subagent_usage)."""
    b = tempfile.mkdtemp(prefix="bin_", dir=ROOT)
    with open(os.path.join(b, "dsh"), "w") as f:
        f.write(FAKE_DSH_SH)
    os.chmod(os.path.join(b, "dsh"), 0o755)
    with env(PATH=b + ":" + os.environ.get("PATH", ""), RA_TEST_STARTED=started_log, RA_TEST_LEAVE_CHILD=leave_child, RA_TEST_SESSIONS=sessions,
             RA_TEST_MAIN_ID=MAIN_SID, RA_TEST_SUB_ID=SUB_SID):
        yield

def marked_pids(run_id):
    """Pids of the live processes whose environment carries one of the run's markers (PECE_CALL_TAG=<run_id>: or PECE_UNIT=<run_id>:)."""
    rx = re.compile(rb"(?:^|\0)PECE_(?:CALL_TAG|UNIT)=" + re.escape(run_id.encode()) + rb":")
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open("/proc/%s/environ" % d, "rb") as f:
                if rx.search(f.read()):
                    out.append(int(d))
        except OSError:
            pass
    return out

# ================================================================ dsh's endpoint
def group_dsh_endpoint():
    def _switch():
        got = {}
        for v in ("1", " 1 ", "0", "", "true", "yes", None):
            with env(RA_DSH_SHIM=v):
                got[repr(v)] = sr._shim_on()
        want = {"'1'": True, "' 1 '": True, "'0'": False, "''": False, "'true'": False, "'yes'": False, "None": False}
        return got == want, got
    safe("dsh_endpoint_switch_is_RA_DSH_SHIM_1", _switch)

    # proxy mode (the paper's route)
    def _proxy(port, want_url):
        def fn():
            with env(RA_DSH_SHIM="1", DSH_SHIM_PORT=port, DEEPSEEK_API_KEY=CALLER_KEY, DEEPSEEK_BASE_URL=CALLER_URL):
                e = sr._dsh_env(home="/x/home")
            ok = (e["DEEPSEEK_BASE_URL"] == want_url and e["DEEPSEEK_API_KEY"] == "shim-local" and e["DSH_HOME"] == "/x/home"
                  and e["DSH_PERMISSION_MODE"] == "danger-full-access" and e["DSH_TELEMETRY_DISABLED"] == "1")
            return ok, (show_url(e["DEEPSEEK_BASE_URL"]), show_key(e["DEEPSEEK_API_KEY"]), e.get("DSH_HOME"))
        return fn
    safe("dsh_endpoint_proxy_default_port", _proxy(None, "http://127.0.0.1:8500/v1"))
    safe("dsh_endpoint_proxy_port_and_dummy_key", _proxy("8765", "http://127.0.0.1:8765/v1"))

    def _proxy_model():
        with env(RA_DSH_SHIM="1"):
            return sr._dsh_model() == "deepseek-v4.1-flash", sr._dsh_model()
    safe("dsh_endpoint_proxy_bare_model_id", _proxy_model)

    def _proxy_probe():
        with env(RA_DSH_SHIM="1", DSH_SHIM_PORT="8765", DEEPSEEK_API_KEY=CALLER_KEY):
            url, key, model = sr._gw_probe()
        return (url, key, model) == ("http://127.0.0.1:8765/v1/chat/completions", "shim-local", "deepseek-v4.1-flash"), (show_url(url), show_key(key), model)
    safe("dsh_endpoint_proxy_probe_follows_route", _proxy_probe)

    # direct mode passes the caller's endpoint and key
    def _direct(url, want_url, key):
        def fn():
            with env(RA_DSH_SHIM=None, DSH_SHIM_PORT=None, DEEPSEEK_BASE_URL=url, DEEPSEEK_API_KEY=key):
                e = sr._dsh_env()
            return (e["DEEPSEEK_BASE_URL"] == want_url and e["DEEPSEEK_API_KEY"] == (key or "")), (show_url(e["DEEPSEEK_BASE_URL"]), show_key(e["DEEPSEEK_API_KEY"]))
        return fn
    safe("dsh_endpoint_direct_default_url", _direct(None, "https://api.deepseek.com", CALLER_KEY))
    safe("dsh_endpoint_direct_caller_url_and_key", _direct(CALLER_URL, CALLER_URL, CALLER_KEY))
    safe("dsh_endpoint_direct_no_key_stays_empty", _direct(CALLER_URL, CALLER_URL, None))

    def _direct_model():
        with env(RA_DSH_SHIM=None):
            return sr._dsh_model() == "deepseek/deepseek-v4.1-flash", sr._dsh_model()
    safe("dsh_endpoint_direct_full_model_id", _direct_model)

    def _direct_probe():
        with env(RA_DSH_SHIM=None, DEEPSEEK_BASE_URL=CALLER_URL + "/", DEEPSEEK_API_KEY=CALLER_KEY):
            url, key, model = sr._gw_probe()
        return (url, key, model) == (CALLER_URL + "/chat/completions", CALLER_KEY, "deepseek/deepseek-v4.1-flash"), (show_url(url), show_key(key), model)
    safe("dsh_endpoint_direct_probe_follows_route", _direct_probe)

    # the Node folder on the agents' PATH only when RA_DSH_NODE is set
    def _node_path():
        saved = sr.DSH_NODE
        try:
            with env(RA_DSH_SHIM="1", PATH="/usr/bin:/bin"):
                sr.DSH_NODE = ""
                plain = sr._dsh_env()["PATH"]
                sr.DSH_NODE = "/usr/local/node-test/bin/node"
                pre = sr._dsh_env()["PATH"]
        finally:
            sr.DSH_NODE = saved
        shown = [p if p in ("/usr/bin:/bin", "/usr/local/node-test/bin:/usr/bin:/bin") else "<another PATH>" for p in (plain, pre)]
        return plain == "/usr/bin:/bin" and pre == "/usr/local/node-test/bin:/usr/bin:/bin", shown
    safe("dsh_endpoint_node_folder_only_when_set", _node_path)

    def _node_default():
        with env(RA_DSH_NODE=None):
            spec2 = importlib.util.spec_from_file_location("ra_orchestrator_behaviour2", ORCH)
            m2 = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(m2)
        with env(RA_DSH_NODE="/usr/local/node-test/bin/node"):
            spec3 = importlib.util.spec_from_file_location("ra_orchestrator_behaviour3", ORCH)
            m3 = importlib.util.module_from_spec(spec3)
            spec3.loader.exec_module(m3)
        shown = [p if p in ("", "/usr/local/node-test/bin/node") else "<another path>" for p in (m2.DSH_NODE, m3.DSH_NODE)]
        return m2.DSH_NODE == "" and m3.DSH_NODE == "/usr/local/node-test/bin/node", shown
    safe("dsh_endpoint_node_from_RA_DSH_NODE", _node_default)

# ================================================================ stopping a run
def group_stopping():
    def _sweep_prefix_exact():
        """The sweep kills the run's marked processes only: a longer run id that starts with this one, a marker text inside
        another variable's value and unmarked processes survive."""
        rid = "tfsweep%d_p10" % os.getpid()
        procs = {"call_marker": sleeper({"PECE_CALL_TAG": rid + ":exec_u1"}), "unit_marker": sleeper({"PECE_UNIT": rid + ":u1"}),
                 "longer_run_id": sleeper({"PECE_CALL_TAG": rid + "1:exec_u1", "PECE_UNIT": rid + "1:u1"}),
                 "marker_text_in_a_value": sleeper({"RA_TEST_NOTE": "PECE_UNIT=" + rid + ":u1"}), "unmarked": sleeper({})}
        saved = sr.RUN_ID
        try:
            sr.RUN_ID = rid
            n = sr._sweep_run()
        finally:
            sr.RUN_ID = saved
            time.sleep(0.3)
            alive = dict((k, p.poll() is None) for k, p in procs.items())
            stop(*procs.values())
        want = {"call_marker": False, "unit_marker": False, "longer_run_id": True, "marker_text_in_a_value": True, "unmarked": True}
        return alive == want and n == 2, (alive, n)
    safe("stopping_sweep_prefix_exact", _sweep_prefix_exact)

    def _sig_ign_kept():
        """A signal the caller ignores (nohup: SIGHUP) stays ignored; SIGTERM and SIGINT get the stop handler."""
        head = ("import signal\nsignal.signal(signal.SIGHUP, signal.SIG_IGN)\nsignal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
                "signal.signal(signal.SIGINT, signal.default_int_handler)\n")
        code = ("import json, signal\ninst = m._install_stop_handlers()\n"
                "print('RESULT ' + json.dumps({'installed': inst, 'hup_ignored': signal.getsignal(signal.SIGHUP) is signal.SIG_IGN,"
                " 'term': signal.getsignal(signal.SIGTERM) is m._on_stop, 'int': signal.getsignal(signal.SIGINT) is m._on_stop}))\n")
        rc, out = in_fresh_python(code, head=head)
        got = json.loads(out.split("RESULT ", 1)[1]) if "RESULT " in out else out[-600:]
        return got == {"installed": ["SIGTERM", "SIGINT"], "hup_ignored": True, "term": True, "int": True}, got
    safe("stopping_sig_ign_kept", _sig_ign_kept)

    def _refuses_after_stop():
        """Once the stop has begun, run_call starts no dsh session (the fake dsh on PATH records every start)."""
        started = os.path.join(ROOT, "stopping_started.log")
        sr.ABORT.set()
        try:
            with fake_dsh(started):
                r = sr.run_call("x", 60, "critic_u1")
        finally:
            sr.ABORT.clear()
        return (r["rc"] == -5 and r["interrupted"] == "stop" and r["tokens"] == 0 and not os.path.exists(started),
                (dict((k, r.get(k)) for k in ("rc", "interrupted", "tokens")), os.path.exists(started)))
    safe("stopping_run_call_refuses_after_stop", _refuses_after_stop)

def scen_stop_signal():
    """stopping, end to end: SIGTERM while an executor session runs and the detached worker it started computes -> no process carrying
    the run's markers is left 10 s after the signal, the ledger records orchestrator_stopped, and the exit status is 143."""
    hs = tempfile.mkdtemp(prefix="ra_stop_handshake_")
    pre = ("import os, signal\nsignal.signal(signal.SIGTERM, signal.SIG_DFL)\n"   # a caller that does not ignore SIGTERM
           "open(os.path.join(os.environ['RA_TEST_HANDSHAKE'], 'orch'), 'w').write('%d %s\\n' % (os.getpid(), os.environ['FAKE_STATE']))\n")
    sent = {}

    def stopper():   # SIGTERM from outside, 1 s after the executor's worker started
        t0 = time.time()
        while time.time() - t0 < 120:
            try:
                pid, fstate = open(os.path.join(hs, "orch")).read().split()
                if "spawned:u1:" in open(os.path.join(fstate, "calls.log")).read():
                    time.sleep(1.0)
                    sent["t"] = time.time()
                    os.kill(int(pid), signal.SIGTERM)
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
    th = threading.Thread(target=stopper, daemon=True)
    th.start()
    rules = PLAN + [{"tag": r"^exec_u1$", "do": [{"write": "/workspace/u1/src/u1_solver.py", "text": "x = 1\n"},
                                                {"spawn": {"secs": 120, "period": 5, "marker": False}}, {"sleep": 60}]},
                    {"tag": r"^critic", "do": [A()]}] + REPORT
    R = run_scen("stop_signal", rules, env={"RA_TEST_HANDSHAKE": hs}, pre=pre, timeout=150)
    th.join(5)
    run_id = "t_stop_signal_%d" % os.getpid()
    left = marked_pids(run_id)
    while left and "t" in sent and time.time() < sent["t"] + 10:
        time.sleep(0.5)
        left = marked_pids(run_id)
    check("stop_signal:signal_sent", "t" in sent, R["out"][-1500:])
    check("stop_signal:exit_143", R["rc"] == 143, "rc=%s\n%s" % (R["rc"], R["out"][-1500:]))
    check("stop_signal:no_marked_process_after_10s", "t" in sent and not left, left)
    st = kinds(R, "orchestrator_stopped")
    check("stop_signal:ledger_event", st and st[0].get("signal") == "SIGTERM" and sum(st[0].get("swept") or [0]) >= 2, st)
    shutil.rmtree(hs, ignore_errors=True)

# ================================================================ missing memories
def ledger_events(kind=None):
    out = []
    for line in sr.read(os.path.join(sr.PD, "ledger.jsonl")).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and (kind is None or e.get("kind") == kind):
            out.append(e)
    return out

def group_notes_missing():
    import io

    def _load(files):
        d = tempfile.mkdtemp(prefix="memories_", dir=ROOT)
        for f in files:
            shutil.copy(os.path.join(NOTES, f + ".md"), d)
        saved, n0, buf = sr.NOTES_DIR, len(ledger_events("notes_missing")), io.StringIO()
        try:
            sr.NOTES_DIR = d
            with contextlib.redirect_stdout(buf):
                sr.load_notes()
        finally:
            sr.NOTES_DIR = saved
        return ledger_events("notes_missing")[n0:], buf.getvalue(), dict((k, bool(v)) for k, v in sr.NOTES.items())

    def _some_missing():
        """A folder without two of the four files (the ablation switch): the run goes on with the other two, and the ledger and
        the log say which are missing."""
        ev, out, loaded = _load(["planner_notes", "residual_bank"])
        ok = (len(ev) == 1 and ev[0].get("missing") == ["critic_notes", "libraries"] and "WARNING" in out and "critic_notes.md" in out
              and loaded == {"planner_notes": True, "critic_notes": False, "residual_bank": True, "libraries": False})
        return ok, (ev, out[-400:], loaded)
    safe("notes_missing_notes_missing_warning", _some_missing)

    def _all_present():
        """With the four files (the paper's setting) nothing new is recorded."""
        ev, out, loaded = _load(["planner_notes", "critic_notes", "residual_bank", "libraries"])
        return not ev and "WARNING" not in out and all(loaded.values()), (ev, out[-400:], loaded)
    safe("notes_missing_all_present_no_event", _all_present)

def scen_notes_dir_missing():
    """notes_missing, end to end: RA3SR_NOTES_DIR names a folder that does not exist -> exit 2 with a message that names the folder and the
    variable, before anything is written: no ledger, no store record, no dsh call."""
    def setup(ws):   # run_scen copies the memories to <root>/notes and points RA3SR_NOTES_DIR there: remove the copy
        shutil.rmtree(os.path.join(os.path.dirname(ws), "notes"))
    R = run_scen("notes_dir_missing", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 setup=setup, timeout=120)
    check("notes_dir_missing:rc2", R["rc"] == 2, "rc=%s\n%s" % (R["rc"], R["out"][-1500:]))
    check("notes_dir_missing:message", "memory folder" in R["out"] and "RA3SR_NOTES_DIR" in R["out"], R["out"][-800:])
    check("notes_dir_missing:nothing_written", not R["ev"] and os.listdir(os.path.join(R["ws"], "_pece")) == [] and os.listdir(R["store"]) == []
          and sorted(os.listdir(R["ws"])) == ["CLAUDE.md", "_pece", "hang_stack.txt"],
          (sorted(os.listdir(R["ws"])), os.listdir(os.path.join(R["ws"], "_pece")), os.listdir(R["store"])))
    check("notes_dir_missing:no_dsh_call", not R["calls"], [c for c in R["calls"] if c.startswith("call:")][:5])   # call tags only

# ================================================================ the default run id
def group_run_id():
    def _ids():
        """Each process without RUN_ID (unset or empty) gets its own id ra_<pid>_<epoch>; a set id is kept as it is."""
        got = {}
        for label, value in (("unset_1", None), ("unset_2", None), ("empty_1", ""), ("empty_2", ""), ("set", "myrun_p7")):
            rc, out = in_fresh_python("print('RUN_ID=' + m.RUN_ID)\n", extra_env={"RUN_ID": value})
            hit = re.search(r"^RUN_ID=(.*)$", out, re.M)
            got[label] = hit.group(1) if hit else out[-300:]
        auto = [got[k] for k in ("unset_1", "unset_2", "empty_1", "empty_2")]
        return (all(re.match(r"^ra_\d+_\d+$", x) for x in auto) and len(set(auto)) == 4 and got["set"] == "myrun_p7"), got
    safe("run_id_default_run_id_unique", _ids)

# ================================================================ cores and memory when PECE_NCPUS / PECE_MEM_GB are unset
GIB = 1 << 30

@contextlib.contextmanager
def fake_cgroups(proc_lines, files, cores=32):
    """The module reads a fake cgroup mount (files: path below the mount -> content) and a fake /proc/self/cgroup (proc_lines);
    the affinity mask shows `cores` cores; PECE_NCPUS and PECE_MEM_GB are unset."""
    root = tempfile.mkdtemp(prefix="cgroup_", dir=ROOT)
    mount = os.path.join(root, "mount")
    os.makedirs(mount)
    for rel, txt in files.items():
        p = os.path.join(mount, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(txt + "\n")
    with open(os.path.join(root, "self_cgroup"), "w") as f:
        f.write("".join(x + "\n" for x in proc_lines))
    saved = dict((k, sr.__dict__[k]) for k in ("CGROUP_ROOT", "CGROUP_SELF", "_affinity") if k in sr.__dict__)
    sr.CGROUP_ROOT, sr.CGROUP_SELF, sr._affinity = mount, os.path.join(root, "self_cgroup"), (lambda: list(range(cores)))
    try:
        with env(PECE_NCPUS=None, PECE_MEM_GB=None):
            yield mount
    finally:
        for k in ("CGROUP_ROOT", "CGROUP_SELF", "_affinity"):
            if k in saved:
                setattr(sr, k, saved[k])
            else:
                delattr(sr, k)

def physical_ram_gb():
    return max(1, int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // GIB))

def node_src():
    """Where the module says its core count and memory came from ({} in a build that does not say)."""
    return getattr(sr, "NODE_SRC", {})

def group_node_facts():
    def _mem(proc_lines, files, want, src_has):
        def fn():
            with fake_cgroups(proc_lines, files):
                got = sr._mem_gb()
                src = node_src().get("mem_gb", "")
            return got == want and src_has in src, (got, src.replace(ROOT, "<tmp>"))
        return fn
    safe("node_facts_mem_smallest_limit_in_v2_chain", _mem(["0::/job/step/task"], {"job/memory.max": str(4 * GIB), "job/step/memory.max": "max",
                                                                               "job/step/task/memory.max": str(8 * GIB)}, 4, "job/memory.max"))
    safe("node_facts_mem_v1_chain", _mem(["4:memory:/job/step", "2:cpu,cpuacct:/job"], {"memory/job/memory.limit_in_bytes": str(3 * GIB),
                                                                                    "memory/job/step/memory.limit_in_bytes": "9223372036854771712"},
                                    3, "memory.limit_in_bytes"))
    safe("node_facts_mem_root_file_counts", _mem(["0::/"], {"memory.max": str(2 * GIB)}, 2, "memory.max"))
    safe("node_facts_mem_no_limit_is_physical_ram", _mem(["0::/job"], {"job/memory.max": "max"}, physical_ram_gb(), "physical RAM"))

    def _cpus(files, want, src_has):
        def fn():
            with fake_cgroups(["0::/job/step"], files, cores=32):
                got = sr._ncpus()
                src = node_src().get("ncpus", "")
            return got == want and src_has in src, (got, src.replace(ROOT, "<tmp>"))
        return fn
    safe("node_facts_cpus_capped_by_quota", _cpus({"job/cpu.max": "200000 100000", "job/step/cpu.max": "max 100000"}, 2, "job/cpu.max"))
    safe("node_facts_cpus_quota_rounded_up", _cpus({"job/step/cpu.max": "150000 100000"}, 2, "quota"))
    safe("node_facts_cpus_mask_below_quota", _cpus({"job/cpu.max": "6400000 100000"}, 32, "affinity"))
    safe("node_facts_cpus_no_quota", _cpus({"job/cpu.max": "max 100000"}, 32, "affinity"))

    def _env_wins():
        with fake_cgroups(["0::/job"], {"job/memory.max": str(4 * GIB), "job/cpu.max": "200000 100000"}):
            with env(PECE_MEM_GB="7", PECE_NCPUS="3"):
                got = (sr._mem_gb(), sr._ncpus(), node_src().get("mem_gb"), node_src().get("ncpus"))
        return got == (7, 3, "PECE_MEM_GB", "PECE_NCPUS"), got
    safe("node_facts_environment_wins", _env_wins)

    def _live_scope():
        """Optional: a real transient scope with a 2 GiB memory limit (systemd-run --user); skipped where it is unavailable."""
        if not shutil.which("systemd-run"):
            return None, "systemd-run not found"
        code = "print('MEM_GB=%d' % m._mem_gb())\n"
        load = ("import importlib.util\nspec = importlib.util.spec_from_file_location('ra_orchestrator_child', %r)\n"
                "m = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n" % ORCH)
        e = dict(os.environ)
        e.pop("PECE_MEM_GB", None)
        try:
            p = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=2G", sys.executable, "-B", "-c", load + code],
                               env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        except Exception as ex:
            return None, "systemd-run failed: %s" % ex
        out = p.stdout.decode(errors="replace")
        hit = re.search(r"MEM_GB=(\d+)", out)
        if p.returncode != 0 or not hit:
            return None, "no transient scope here (rc %s)" % p.returncode
        return int(hit.group(1)) == 2, out[-300:]
    try:
        res, detail = _live_scope()
    except Exception:
        res, detail = False, traceback.format_exc().strip().splitlines()[-1]
    if res is None:
        e2e_lib.skip("node_facts_live_memory_scope", detail)
    else:
        check("node_facts_live_memory_scope", res, detail)

def scen_node_facts_logged():
    """node_facts, end to end: without PECE_NCPUS and PECE_MEM_GB the episode logs where its core count and memory came from, and the
    numbers are the ones episode_start records."""
    R = run_scen("node_facts", PLAN + [{"tag": r"^exec_u1$", "do": deliver("u1")}, {"tag": r"^critic_u1$", "do": [A()]}] + REPORT,
                 env={"PECE_NCPUS": None, "PECE_MEM_GB": None})
    check("node_facts:rc", R["rc"] == 0, "rc=%s\n%s" % (R["rc"], R["out"][-1500:]))
    hit = re.search(r"node: (\d+) cores \(([^()]+)\), (\d+) GB memory \(([^()]+)\)", R["out"])
    es = kinds(R, "episode_start")
    check("node_facts:sources_logged", hit and es and int(hit.group(1)) == es[0].get("ncpus") and int(hit.group(3)) == es[0].get("mem_gb")
          and not hit.group(2).startswith("PECE_") and not hit.group(4).startswith("PECE_"),
          (hit.groups() if hit else [l for l in R["out"].splitlines() if "ncpus" in l][:2], es and (es[0].get("ncpus"), es[0].get("mem_gb"))))

# ================================================================ recorded session ids and recycled process ids
def gone(pid, secs=5.0):
    """Wait until no process (not even a zombie) has this pid; -> True when it is gone."""
    t_end = time.time() + secs
    while os.path.exists("/proc/%d" % pid) and time.time() < t_end:
        time.sleep(0.05)
    return not os.path.exists("/proc/%d" % pid)

def group_session_ids():
    if not os.path.exists(os.path.join(sr.WS, "CLAUDE.md")):
        with open(os.path.join(sr.WS, "CLAUDE.md"), "w") as f:
            f.write(e2e_lib.CLAUDE_MD)

    def _dry_reap():
        """A dry scan forgets a recorded session id once its session has no process left (an exited session leader), keeps a live
        one, and edits the list in place (run_call appends to the same list object)."""
        live = sleeper({})
        ended = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        ended.wait()
        gone(ended.pid)
        lst = [ended.pid, live.pid]
        sr.UNIT_PIDS["uprune"] = lst
        try:
            found = sr.reap_leftovers("uprune", dry=True)
            first = (list(lst), sr.UNIT_PIDS.get("uprune") is lst, any(f[0] == live.pid for f in found))
            stop(live)
            gone(live.pid)
            sr.reap_leftovers("uprune", dry=True)
            second = list(lst)
        finally:
            stop(live)
            sr.UNIT_PIDS.pop("uprune", None)
        want = ([live.pid], True, True)
        return first == want and second == [], {"after_first_scan": first, "after_the_live_one_ended": second,
                                                 "pids": {"ended": ended.pid, "live": live.pid}}
    safe("session_ids_dry_reap_forgets_ended_sessions", _dry_reap)

    def _call(tag, leave_child):
        started = os.path.join(ROOT, "session_ids_%s.log" % tag)
        child_f = os.path.join(ROOT, "session_ids_%s.child" % tag) if leave_child else None
        uid = sr._tag_uid(tag)
        sr.UNIT_PIDS.pop(uid, None)
        with fake_dsh(started, leave_child=child_f):
            r = sr.run_call("check the unit", 60, tag)
        leader = int(open(started).read().split()[0])
        child = int(open(child_f).read().split()[0]) if child_f else None
        return r, uid, leader, child

    def _call_ended_empty():
        """A call whose session is empty when it ends leaves no session id behind."""
        r, uid, leader, _ = _call("critic_uq", False)
        rec = list(sr.UNIT_PIDS.get(uid, []))
        sr.UNIT_PIDS.pop(uid, None)
        return r["rc"] == 0 and leader not in rec, {"rc": r["rc"], "recorded": rec, "leader": leader}
    safe("session_ids_run_call_forgets_empty_session", _call_ended_empty)

    def _call_left_child():
        """A call that leaves a process in its session keeps the session id (the unit's reaps still find that process); once the
        process has ended, the next dry scan forgets the id."""
        r, uid, leader, child = _call("critic_ur", True)
        kept = list(sr.UNIT_PIDS.get(uid, []))
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
        gone(child)
        sr.reap_leftovers(uid, dry=True)
        after = list(sr.UNIT_PIDS.get(uid, []))
        sr.UNIT_PIDS.pop(uid, None)
        return r["rc"] == 0 and kept == [leader] and after == [], {"rc": r["rc"], "kept": kept, "after_the_child_ended": after, "leader": leader}
    safe("session_ids_run_call_keeps_live_session_until_it_ends", _call_left_child)

# ================================================================ the digest and the episode record
def group_episode_record():
    import io

    def _epilogue(lines, run_id):
        """epilogue() on a fresh _pece folder whose ledger holds `lines`, with one graded unit that has a critic report.
        -> (meta.json in the store, digest.md written, the critic copy in the store)."""
        saved = (sr.PD, sr.RUN_ID, dict(sr.RUN["grades"]), list(sr.RUN["failed"]))
        pd = tempfile.mkdtemp(prefix="pece_", dir=ROOT)
        ud = os.path.join(sr.WS, "u7")
        os.makedirs(ud, exist_ok=True)
        with open(os.path.join(ud, "critic_report.md"), "w") as f:
            f.write("GRADE: A\n")
        with open(os.path.join(pd, "ledger.jsonl"), "w") as f:
            f.write("".join(x + "\n" for x in lines))
        try:
            sr.PD, sr.RUN_ID = pd, run_id
            sr.RUN["grades"].clear()
            sr.RUN["grades"]["u7"] = "A"
            sr.RUN["failed"][:] = []
            with contextlib.redirect_stdout(io.StringIO()):
                sr.epilogue()
        finally:
            sr.PD, sr.RUN_ID = saved[0], saved[1]
            sr.RUN["grades"].clear()
            sr.RUN["grades"].update(saved[2])
            sr.RUN["failed"][:] = saved[3]
        ed = os.path.join(sr.STORE, "episodes", run_id)
        return {"meta": os.path.exists(os.path.join(ed, "meta.json")), "digest": os.path.exists(os.path.join(pd, "digest.md")),
                "critic_copy": os.path.exists(os.path.join(ed, "critic", "u7_critic_report.md"))}

    good = json.dumps({"kind": "call", "tag": "exec_u7", "dur": 60, "tokens": 10, "rc": 0})

    def _odd_lines():
        """Ledger lines an agent appended - a JSON string, a number, a call event with odd types: the store record (meta.json and
        the critic copies) is written all the same."""
        got = _epilogue([good, '"agent note"', "42", '{"kind": "call", "tag": null, "dur": "5"}'], "tf07a_%d" % os.getpid())
        return got["meta"] and got["critic_copy"], got
    safe("episode_record_record_written_despite_odd_ledger_lines", _odd_lines)

    def _non_objects():
        """Lines that are valid JSON but not objects are skipped by the digest: digest.md is written too."""
        got = _epilogue([good, '"agent note"', "42", "[1, 2]", "null"], "tf07b_%d" % os.getpid())
        return got == {"meta": True, "digest": True, "critic_copy": True}, got
    safe("episode_record_digest_skips_non_object_lines", _non_objects)

# ================================================================ the .npy shape check
def npy_cases(d):
    """The 14 array deliverables of the npy_check group: name -> path."""
    import io, pickle
    import numpy as np
    out = {}

    def save(name, arr, ext=".npy", **kw):
        p = os.path.join(d, name + ext)
        with open(p, "wb") as f:
            np.save(f, arr, **kw)
        out[name] = p
        return p

    def raw(name, data):
        p = os.path.join(d, name + ".npy")
        with open(p, "wb") as f:
            f.write(data)
        out[name] = p

    valid = save("valid", np.arange(10, dtype=np.int64))
    save("fortran_order", np.asfortranarray(np.arange(12.0).reshape(3, 4)))
    save("zero_d", np.array(3.5))
    save("empty", np.zeros((0,)))
    save("structured", np.zeros(3, dtype=[("a", "<i4"), ("b", "<f8")]))
    save("complex", np.arange(4) * (1 + 1j))
    raw("trailing_bytes", open(valid, "rb").read() + b"bytes after the array")
    z = io.BytesIO()
    np.savez(z, x=np.arange(3))
    raw("zip_named_npy", z.getvalue())
    raw("pickle", pickle.dumps({"a": 1}))
    raw("garbage", b"this is not an array\n" * 8)
    raw("truncated_header", open(valid, "rb").read()[:20])
    big = save("truncated_data_full", np.arange(1000, dtype=np.float64))
    data = open(big, "rb").read()
    os.remove(big)
    del out["truncated_data_full"]
    raw("truncated_data", data[:len(data) - 7000])
    save("object_dtype", np.array([{"a": 1}, None], dtype=object), allow_pickle=True)
    out["npz"] = os.path.join(d, "arrays.npz")
    np.savez(out["npz"], x=np.arange(3), y=np.ones(2))
    return out

def group_npy_check():
    try:
        import numpy as np
    except ImportError:
        e2e_lib.skip("npy_check_npy_records_unchanged", "numpy is not installed")
        e2e_lib.skip("npy_check_npy_check_reads_only_the_header", "numpy is not installed")
        return

    def _records():
        """Each of the 14 cases gives the record a full load gives (np.load without a memory map: here the same
        function with numpy.load made to ignore mmap_mode)."""
        d = tempfile.mkdtemp(prefix="npy_", dir=ROOT)
        cases = npy_cases(d)
        real_load = np.load

        def load_without_map(*a, **k):
            k.pop("mmap_mode", None)
            return real_load(*a, **k)
        diff = {}
        for name, p in sorted(cases.items()):
            got = sr._check_file(p)
            np.load = load_without_map
            try:
                ref = sr._check_file(p)
            finally:
                np.load = real_load
            if got != ref:
                diff[name] = (got, ref)
        return len(cases) == 14 and not diff, diff or sorted(cases)
    safe("npy_check_npy_records_unchanged", _records)

    if os.environ.get("RA_TEST_SKIP_SLOW") == "1":
        e2e_lib.skip("npy_check_npy_check_reads_only_the_header", "slow check skipped (RA_TEST_SKIP_SLOW=1)")
        return

    def _rss():
        """(slow) A 512 MiB .npy file (sparse on disk): the check reads its header only - the process's peak memory grows by less
        than 64 MiB (a full load adds 512 MiB)."""
        p = os.path.join(tempfile.mkdtemp(prefix="npy_big_", dir=ROOT), "big.npy")
        n = (512 << 20) // 8
        with open(p, "wb") as f:
            np.lib.format.write_array_header_1_0(f, {"descr": "<f8", "fortran_order": False, "shape": (n,)})
            f.truncate(f.tell() + n * 8)
        code = ("import numpy, resource\nbefore = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\nr = m._check_file(%r)\n"
                "after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\nprint('RESULT %%d %%s %%s' %% ((after - before) // 1024, r['ok'], r['check']))\n" % p)
        rc, out = in_fresh_python(code)
        os.remove(p)
        hit = re.search(r"RESULT (\d+) (\S+) (.*)", out)
        if not hit:
            return False, out[-500:]
        grew = int(hit.group(1))
        return grew < 64 and hit.group(2) == "True" and hit.group(3) == "npy shape=(%d,)" % n, {"peak_grew_mib": grew, "record": hit.group(3)}
    safe("npy_check_npy_check_reads_only_the_header", _rss)

# ================================================================ dsh sub-agent sessions in the usage count
MAIN_USAGE = {"inputTokens": 100000, "outputTokens": 20000, "cacheReadTokens": 10000, "reasoningTokens": 5000}
SUB_USAGE = {"inputTokens": 30000, "outputTokens": 4000, "cacheReadTokens": 1000, "reasoningTokens": 500}

def session_logs(d):
    """main.jsonl and sub.jsonl in d: one assistant message each, with text and usage (dsh's V0 lines, plain jsonl)."""
    def msg(text, usage):
        return json.dumps({"type": "assistant/message", "seq": 1, "time": 0, "data": {
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, "usage": usage}}) + "\n"
    with open(os.path.join(d, "main.jsonl"), "w") as f:
        f.write(json.dumps({"type": "session", "version": 0, "id": "session-" + MAIN_SID, "createdAt": 0, "cwd": "/workspace"}) + "\n")
        f.write(msg("the main session reports", MAIN_USAGE))
    with open(os.path.join(d, "sub.jsonl"), "w") as f:
        f.write(msg("the sub-agent reports", SUB_USAGE))
    return d

def group_subagent_usage():
    logs = session_logs(tempfile.mkdtemp(prefix="sessions_", dir=ROOT))

    def _home():
        """A call home as dsh 0.1.5 leaves it: sessions/<cwd>/session-<uuid>/ (the main session), sessions/<cwd>/<uuid>/ (a
        sub-agent it started) and an in-progress write (.tmp) that must not count."""
        home = tempfile.mkdtemp(prefix="callhome_", dir=ROOT)
        s = os.path.join(home, "sessions", "--workspace--")
        for sub, src in (("session-" + MAIN_SID, "main.jsonl"), (SUB_SID, "sub.jsonl")):
            os.makedirs(os.path.join(s, sub))
            shutil.copy(os.path.join(logs, src), os.path.join(s, sub, "session.jsonl"))
        shutil.copy(os.path.join(logs, "sub.jsonl"), os.path.join(s, SUB_SID, "session.v3.jsonl.zstd.4f3a.tmp"))
        u = sr._dsh_usage_detail(home)
        got = dict((k, u.get(k)) for k in ("inputTokens", "outputTokens", "cacheReadTokens", "reasoningTokens", "usage_calls"))
        want = dict((k, MAIN_USAGE[k] + SUB_USAGE[k]) for k in MAIN_USAGE)
        want["usage_calls"] = 2
        tail, sid = sr._dsh_transcript_tail(home), sr._dsh_session_of(home)
        ok = (got == want and sid == MAIN_SID and "the main session reports" in tail and "the sub-agent reports" not in tail)
        return ok, {"usage": got, "want": want, "session_id_is_main": sid == MAIN_SID, "tail_main": "the main session reports" in tail,
                    "tail_sub": "the sub-agent reports" in tail}
    safe("subagent_usage_usage_counts_subagent_sessions", _home)

    def _call():
        """run_call's ledger event: tokens, usage and cost include the sub-agent session; the call's session id is the main one."""
        if not os.path.exists(os.path.join(sr.WS, "CLAUDE.md")):
            with open(os.path.join(sr.WS, "CLAUDE.md"), "w") as f:
                f.write(e2e_lib.CLAUDE_MD)
        n0 = len(ledger_events("call"))
        with fake_dsh(os.path.join(ROOT, "subagent_usage_started.log"), sessions=logs):
            r = sr.run_call("check the unit", 60, "critic_us")
        ev = ledger_events("call")[n0:]
        e = ev[-1] if ev else {}
        tok = sum(MAIN_USAGE[k] + SUB_USAGE[k] for k in ("inputTokens", "cacheReadTokens", "outputTokens"))
        both = dict((k, MAIN_USAGE[k] + SUB_USAGE[k]) for k in MAIN_USAGE)
        usage = {"input": both["inputTokens"], "cached": both["cacheReadTokens"], "output": both["outputTokens"], "reasoning": both["reasoningTokens"]}
        cost = round((usage["input"] * sr.RATES[0] + usage["cached"] * sr.RATES[1] + usage["output"] * sr.RATES[2]) / 1e6, 4)
        ok = r["sid"] == MAIN_SID and r["tokens"] == tok and e.get("tokens") == tok and e.get("usage") == usage and e.get("cost_usd") == cost
        return ok, {"sid_is_main": r["sid"] == MAIN_SID, "tokens": (r["tokens"], e.get("tokens"), tok), "usage": e.get("usage"),
                    "cost": (e.get("cost_usd"), cost)}
    safe("subagent_usage_call_ledger_counts_subagent_sessions", _call)

# the end-to-end scenarios of this file: name -> (group, function)
SCENS = {"stop_signal": ("stopping", scen_stop_signal), "notes_dir_missing": ("notes_missing", scen_notes_dir_missing),
         "node_facts_logged": ("node_facts", scen_node_facts_logged)}
GROUPS = {"dsh_endpoint": group_dsh_endpoint, "stopping": group_stopping, "notes_missing": group_notes_missing, "run_id": group_run_id,
          "node_facts": group_node_facts, "session_ids": group_session_ids, "episode_record": group_episode_record,
          "npy_check": group_npy_check, "subagent_usage": group_subagent_usage}

def run_scenario(name):
    t0 = time.time()
    n_fail = len(e2e_lib.FAILS)
    try:
        SCENS[name][1]()
    except Exception:
        check(name + ":exception", False, traceback.format_exc()[-1500:])
    check(name + ":no_network", not NET_SEEN, [x.split(" ", 1)[0] for x in NET_SEEN[:5]])   # event names only
    cleanup(failed=len(e2e_lib.FAILS) > n_fail)
    print("---- %s %.1fs" % (name, time.time() - t0), flush=True)

if __name__ == "__main__":
    want = sys.argv[1:] or list(GROUPS)
    bad = [w for w in want if w not in GROUPS]
    if bad:
        print("unknown group(s): %s; known: %s" % (" ".join(bad), " ".join(GROUPS)))
        sys.exit(2)
    for fx in want:
        GROUPS[fx]()
        for name, (f, _) in SCENS.items():
            if f == fx:
                run_scenario(name)
    if os.environ.get("RA_TEST_KEEP") == "1":
        print("kept: %s" % ROOT)
    else:
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(finish())
