"""The `ra` command line (residual_agents/cli.py): profiles, every preflight error and warning, `ra run` (a profile sets only unset
variables, the renamed interpreter, -X utf8, the orchestrator's exit status, masked keys), `ra preflight`, `ra cost`, `ra trace`,
`python -m residual_agents`.
The preflight runs against a temporary run folder and store, a fake dsh package, a fake node and a small HTTP server on 127.0.0.1
that stands in for the local proxy, so nothing reaches the network. `ra run` is tested in-process with os.execv replaced, and once
for real in a child process whose "orchestrator" is a stub that reports how it was started.
The good setup decodes the preflight's zstd frame with zstd(1) from /usr/bin or /bin or with python-zstandard (the container
image has zstd); on a host with neither, the preflight rightly reports an error and the checks of the good setup fail.
Usage: python3 tests/test_cli.py
"""
import atexit, contextlib, hashlib, http.server, json, os, shutil, socket, stat, subprocess, sys, tempfile, threading
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
from e2e_lib import strip_env, check, skip, finish
from residual_agents import cli, MEMORY_LEDGER_HASH, __version__

strip_env(os.environ)
for k in ("RUN_ID", "PAPER_NUM", "MODEL", "WALLTIME_SEC", "LC_ALL", "LC_CTYPE"):
    os.environ.pop(k, None)
os.environ.update({"RA3SR_GPU_FACTS": "none", "LANG": "C.UTF-8", "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"})
ROOT = tempfile.mkdtemp(prefix="ra_cli_")
TEST_KEY = "test-key-not-a-secret"
COPIES = []   # renamed interpreter copies made by the tests (removed at exit, also after a crash)

def _remove_copies():
    for c in COPIES:
        try:
            os.remove(c)
        except OSError:
            pass
atexit.register(_remove_copies)

@contextlib.contextmanager
def env(**kv):
    """Set (value) or remove (None) environment variables for the block; everything else the block changes is undone too."""
    saved = dict(os.environ)
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)

class Captured(object):
    text = ""
    def getvalue(self):
        return self.text

@contextlib.contextmanager
def captured():
    """Everything written to file descriptor 1 inside the block (the preflight report writes to the sys.stdout object it was
    defined with, so replacing sys.stdout would miss it)."""
    sys.stdout.flush()
    saved, tmp, cap = os.dup(1), tempfile.TemporaryFile(), Captured()
    os.dup2(tmp.fileno(), 1)
    try:
        yield cap
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
        tmp.seek(0)
        cap.text = tmp.read().decode("utf-8", "replace")
        tmp.close()

def mkfile(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)
    return path

# ---------------------------------------------------------------- fakes: dsh package, node, the local proxy
def fake_tools(name, dsh_version="0.1.5-rc.1", dsh_version_rc=0, node_reply="24.11.1 function"):
    """A bin folder with dsh (a symlink into a fake @deepseek-ai/dsh package, as npm installs it) and node."""
    d = os.path.join(ROOT, "tools_" + name)
    pkg = os.path.join(d, "lib", "node_modules", "@deepseek-ai", "dsh")
    mkfile(os.path.join(pkg, "package.json"), json.dumps({"name": "@deepseek-ai/dsh", "version": dsh_version}))
    script = mkfile(os.path.join(pkg, "bin", "dsh.js"),
                    '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "%s"; exit %d; fi\nexit 0\n' % (dsh_version, dsh_version_rc), 0o755)
    bindir = os.path.join(d, "bin")
    os.makedirs(bindir)
    os.symlink(script, os.path.join(bindir, "dsh"))
    mkfile(os.path.join(bindir, "node"), '#!/bin/sh\nif [ "$1" = "-e" ]; then printf "%%s" "%s"; exit 0; fi\nexit 1\n' % node_reply, 0o755)
    return bindir

REQS = []
class Proxy(http.server.BaseHTTPRequestHandler):
    status = {"GET /health": 200, "POST /v1/chat/completions": 200}
    def _answer(self, method, body=None):
        REQS.append((method, self.path, self.headers.get("Authorization"), body))
        code = self.status.get("%s %s" % (method, self.path), 404)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"choices": [{"message": {"content": "pong"}}]}')
    def do_GET(self):
        self._answer("GET")
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._answer("POST", self.rfile.read(n))
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = str(srv.server_address[1])
def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return str(p)

# ---------------------------------------------------------------- the orchestrator module the preflight looks at
m = cli.load_orchestrator("ra_orchestrator_cli_test")
WS, STORE = os.path.join(ROOT, "workspace"), os.path.join(ROOT, "pece_store")
SHIPPED_NOTES = m.NOTES_DIR
def fresh_run_folder():
    for d in (WS, STORE):
        if os.path.isdir(d):
            os.chmod(d, 0o755)
            shutil.rmtree(d)
    mkfile(os.path.join(WS, "CLAUDE.md"), "# a task book\n")
    os.makedirs(STORE)
    m.WS, m.STORE, m.PD, m.NOTES_DIR, m.DSH_NODE = WS, STORE, os.path.join(WS, "_pece"), SHIPPED_NOTES, ""
GOOD_BIN = fake_tools("good")
GOOD = {"PATH": GOOD_BIN + ":/usr/bin:/bin", "RA_DSH_SHIM": "1", "DSH_SHIM_PORT": PORT, "RUN_ID": "ratest_%d" % os.getpid(),
        "PECE_NCPUS": "16", "PECE_MEM_GB": "64", "PAPER_NUM": "47", "LANG": "C.UTF-8", "RA_ORCH_NO_RENAME": None,
        "DEEPSEEK_API_KEY": None, "DEEPSEEK_BASE_URL": None, "MODEL": None}

def pre(probe=False, **kv):
    """Run the preflight on the temporary run folder with GOOD overridden by kv -> (errors, warnings, notes)."""
    e = dict(GOOD)
    e.update(kv)
    with env(**e):
        rep = cli.preflight(probe=probe, orch=m)
    return rep.errors, rep.warnings, rep.notes

def has(msgs, *words):
    return any(all(w in x for w in words) for x in msgs)

# ---------------------------------------------------------------- profiles
pf = mkfile(os.path.join(ROOT, "p.env"), '# comment\n\nA_ONE=1\nA_TWO="two words"\nA_THREE=\'x=y\'\n  A_FOUR = spaced  \n')
check("profile_parse", cli.read_profile(pf) == [("A_ONE", "1"), ("A_TWO", "two words"), ("A_THREE", "x=y"), ("A_FOUR", "spaced")], cli.read_profile(pf))
bad = mkfile(os.path.join(ROOT, "bad.env"), "A_ONE=1\nNOVALUE\n")
try:
    cli.read_profile(bad)
    check("profile_bad_line_refused", False, "no error")
except SystemExit as e:
    check("profile_bad_line_refused", "line 2" in str(e), str(e))
try:
    cli.read_profile("no_such_profile")
    check("profile_unknown_refused", False, "no error")
except SystemExit as e:
    check("profile_unknown_refused", "no profile" in str(e), str(e))
check("profile_none_is_empty", cli.read_profile("none") == [] and cli.read_profile("") == [] and cli.read_profile(None) == [], "")
e0 = {"WALLTIME_SEC": "1234", "RA3SR_VISION": ""}
done = cli.apply_profile("paper", e0)
check("profile_sets_only_unset_variables", e0["WALLTIME_SEC"] == "1234" and e0["RA3SR_VISION"] == "" and e0["MODEL"] == "deepseek/deepseek-v4.1-flash"
      and "WALLTIME_SEC" not in done and "RA3SR_VISION" not in done and done.get("RA_DSH_SHIM") == "1", (e0, done))

# ---------------------------------------------------------------- masking
check("mask_secret_names", cli._mask("DEEPSEEK_API_KEY", TEST_KEY) == TEST_KEY[:4] + "***" and cli._mask("X_TOKEN", "abcdef") == "abcd***"
      and cli._mask("A_SECRET", "abcdef") == "abcd***" and cli._mask("MODEL", "deepseek/x") == "deepseek/x" and cli._mask("DEEPSEEK_API_KEY", "") == "", "")
with env(DEEPSEEK_API_KEY=TEST_KEY, MODEL="m1", WALLTIME_SEC=None):
    txt = cli.effective_config({"MODEL": "m1"})
check("effective_config_masks_and_marks", "DEEPSEEK_API_KEY=" + TEST_KEY[:4] + "***" in txt and TEST_KEY not in txt and "MODEL=m1   (profile)" in txt
      and "WALLTIME_SEC (unset: code default)" in txt, txt[:600])

# ---------------------------------------------------------------- preflight: the good setup
fresh_run_folder()
before = (sorted(os.listdir(WS)), sorted(os.listdir(STORE)))
errs, warns, notes = pre()
check("preflight_good_setup_clean", not errs and not warns, (errs, warns))
check("preflight_good_setup_notes", has(notes, "exists and is writable") and has(notes, "CLAUDE.md exists") and has(notes, "holds the four shipped memory files")
      and has(notes, "dsh 0.1.5-rc.1") and has(notes, "has zlib.zstdDecompressSync") and has(notes, "zstd session logs decode")
      and has(notes, "local proxy", "answers 200") and has(notes, "RUN_ID="), notes)
check("preflight_writes_nothing", (sorted(os.listdir(WS)), sorted(os.listdir(STORE))) == before, (os.listdir(WS), os.listdir(STORE)))

# ---------------------------------------------------------------- preflight: errors
shutil.rmtree(WS)
errs, _, _ = pre()
check("preflight_error_no_workspace", has(errs, WS, "does not exist") and has(errs, "CLAUDE.md is missing"), errs)
fresh_run_folder()
os.remove(os.path.join(WS, "CLAUDE.md"))
errs, _, _ = pre()
check("preflight_error_no_task_book", has(errs, "CLAUDE.md is missing") and len(errs) == 1, errs)
fresh_run_folder()
mkfile(os.path.join(WS, "JUDGE.md"), "criteria\n")
errs, _, _ = pre()
check("preflight_error_judge_md_visible", has(errs, "JUDGE.md exists") and len(errs) == 1, errs)
fresh_run_folder()
shutil.rmtree(STORE)
errs, _, _ = pre()
check("preflight_error_no_store", has(errs, STORE, "does not exist") and len(errs) == 1, errs)
fresh_run_folder()
os.chmod(STORE, 0o500)
if os.access(STORE, os.W_OK):
    skip("preflight_error_store_not_writable", "this user can write a read-only folder (running as root?)")
else:
    errs, _, _ = pre()
    check("preflight_error_store_not_writable", has(errs, STORE, "is not writable") and len(errs) == 1, errs)
os.chmod(STORE, 0o755)
fresh_run_folder()
m.NOTES_DIR = os.path.join(ROOT, "no_memories")
errs, _, _ = pre()
check("preflight_error_memory_folder_missing", has(errs, "memory folder", "missing") and len(errs) == 1, errs)
part = os.path.join(ROOT, "partial_memories")
shutil.copytree(SHIPPED_NOTES, part)
os.remove(os.path.join(part, "libraries.md"))
m.NOTES_DIR = part
errs, _, _ = pre()
check("preflight_error_memory_file_missing", has(errs, "lacks libraries.md") and len(errs) == 1, errs)
fresh_run_folder()
errs, _, _ = pre(PATH=os.path.dirname(GOOD_BIN) + "/nothing:/usr/bin:/bin")
check("preflight_error_no_dsh", has(errs, "dsh is not on PATH"), errs)
errs, _, _ = pre(PATH=fake_tools("dsh_fails", dsh_version_rc=1) + ":/usr/bin:/bin")
check("preflight_error_dsh_fails", has(errs, "--version` failed") and len(errs) == 1, errs)
errs, _, _ = pre(PATH=fake_tools("old_node", node_reply="22.14.0 undefined") + ":/usr/bin:/bin")
check("preflight_error_node_without_zstd", has(errs, "lacks zlib.zstdDecompressSync") and len(errs) == 1, errs)
saved_zstd = sys.modules.get("zstandard", "absent")
sys.modules["zstandard"] = None   # python-zstandard unavailable; no zstd(1) on PATH; the node on PATH cannot decode
try:
    errs, _, _ = pre(PATH=GOOD_BIN)
finally:
    if saved_zstd == "absent":
        sys.modules.pop("zstandard", None)
    else:
        sys.modules["zstandard"] = saved_zstd
check("preflight_error_zstd_frame_undecodable", has(errs, "zstd test frame does not decode") and len(errs) == 1, errs)
errs, _, _ = pre(DSH_SHIM_PORT=closed_port())
check("preflight_error_proxy_not_answering", has(errs, "RA_DSH_SHIM=1 but the local proxy does not answer") and len(errs) == 1, errs)
errs, _, _ = pre(RA_DSH_SHIM=None)
check("preflight_error_direct_mode_without_key", has(errs, "direct mode", "needs DEEPSEEK_API_KEY") and len(errs) == 1, errs)
errs, _, _ = pre(RUN_ID=None)
check("preflight_error_no_run_id", has(errs, "RUN_ID is not set") and len(errs) == 1, errs)

# ---------------------------------------------------------------- preflight: the endpoint and --probe
errs, warns, notes = pre(RA_DSH_SHIM=None, DEEPSEEK_API_KEY=TEST_KEY, MODEL="deepseek/deepseek-v4.1-flash")
check("preflight_direct_mode_with_key", not errs and has(notes, "direct mode: https://api.deepseek.com") and has(warns, "keeps its provider prefix")
      and not has(notes + warns, TEST_KEY), (errs, warns))
del REQS[:]
errs, warns, notes = pre(probe=True)
posts = [r for r in REQS if r[0] == "POST"]
body = json.loads(posts[0][3].decode()) if posts else {}
check("preflight_probe_through_proxy", not errs and has(notes, "1-token request to http://127.0.0.1:%s/v1/chat/completions" % PORT)
      and posts and posts[0][1] == "/v1/chat/completions" and posts[0][2] == "Bearer " + "shim-local"
      and body.get("model") == "deepseek-v4.1-flash" and body.get("max_tokens") == 1, (errs, posts[:1]))
Proxy.status["POST /v1/chat/completions"] = 503
errs, _, _ = pre(probe=True)
Proxy.status["POST /v1/chat/completions"] = 200
check("preflight_probe_failure_is_error", has(errs, "1-token request") and len(errs) == 1, errs)

# ---------------------------------------------------------------- preflight: warnings
_, warns, _ = pre(PATH=fake_tools("dsh016", dsh_version="0.1.6") + ":/usr/bin:/bin")
check("preflight_warn_dsh_version", has(warns, "dsh version 0.1.6", "0.1.5-rc.1") and len(warns) == 1, warns)
_, warns, _ = pre(PECE_NCPUS=None, PECE_MEM_GB=None)
check("preflight_warn_resources_unset_with_task_hint", has(warns, "PECE_NCPUS is not set", "had 16 cores") and has(warns, "PECE_MEM_GB is not set", "had 96 GB")
      and len(warns) == 2, warns)
edited = os.path.join(ROOT, "edited_memories")
shutil.copytree(SHIPPED_NOTES, edited)
with open(os.path.join(edited, "critic_notes.md"), "a") as f:
    f.write("\nan extra line\n")
m.NOTES_DIR = edited
_, warns, _ = pre()
m.NOTES_DIR = SHIPPED_NOTES
check("preflight_warn_memories_differ", has(warns, "differ from the shipped files", "critic_notes.md") and len(warns) == 1, warns)
if "python" in sys.executable:
    _, warns, _ = pre(RA_ORCH_NO_RENAME="1")
    check("preflight_warn_interpreter_named_python", has(warns, "interpreter path", "contains 'python'") and len(warns) == 1, warns)
else:
    skip("preflight_warn_interpreter_named_python", "this interpreter's path has no 'python' in it")
_, warns, _ = pre(RUN_ID="python_run")
check("preflight_warn_run_id_named_python", has(warns, "/tmp/.ra_orch_python_run", "contains 'python'"), warns)
_, warns, _ = pre(LANG="C")
check("preflight_warn_locale", has(warns, "locale is not UTF-8") and len(warns) == 1, warns)

# ---------------------------------------------------------------- ra preflight / ra run, in-process
class Exec(Exception):
    pass
def fake_execv(exe, argv):
    raise Exec(exe, argv)
real_load, real_execv = cli.load_orchestrator, os.execv
cli.load_orchestrator = lambda name="x": m
os.execv = fake_execv
try:
    fresh_run_folder()
    with env(**GOOD):
        with captured() as out:
            rc = cli.main(["preflight"])
    check("ra_preflight_exit_0", rc == 0 and "preflight: 0 error(s), 0 warning(s)" in out.getvalue(), out.getvalue()[-400:])
    with env(**dict(GOOD, RUN_ID=None)):
        with captured() as out:
            rc = cli.main(["preflight"])
    check("ra_preflight_exit_2_on_error", rc == 2 and "ERROR RUN_ID is not set" in out.getvalue(), out.getvalue()[-400:])
    # ra run: the paper profile fills only what is unset; preflight errors stop it before the exec
    run_env = dict(GOOD, RA_DSH_SHIM=None, WALLTIME_SEC="1234", DEEPSEEK_API_KEY=TEST_KEY, RA_ORCH_NO_RENAME="1")
    with env(**run_env):
        with captured() as out:
            try:
                cli.main(["run"])
                got = None
            except Exec as x:
                got = x.args
            seen = dict((k, os.environ.get(k)) for k in ("WALLTIME_SEC", "MODEL", "RA_DSH_SHIM", "RA3SR_VISION", "RA3SR_RATES"))
    txt = out.getvalue()
    check("ra_run_profile_fills_unset_only", seen == {"WALLTIME_SEC": "1234", "MODEL": "deepseek/deepseek-v4.1-flash", "RA_DSH_SHIM": "1",
                                                     "RA3SR_VISION": "1", "RA3SR_RATES": "0.15,0.003,0.60"}, seen)
    check("ra_run_prints_effective_configuration", "WALLTIME_SEC=1234\n" in txt and "MODEL=deepseek/deepseek-v4.1-flash   (profile)" in txt
          and "RA_DSH_SHIM=1   (profile)" in txt, txt[-1500:])
    check("ra_run_masks_keys", "DEEPSEEK_API_KEY=" + TEST_KEY[:4] + "***" in txt and TEST_KEY not in txt, "")
    check("ra_run_no_rename_execs_interpreter", got == (sys.executable, [sys.executable, "-X", "utf8", cli.ORCH]), got)
    copy = "/tmp/.ra_orch_" + GOOD["RUN_ID"]
    with env(**dict(GOOD, RA_ORCH_NO_RENAME=None)):
        with captured() as out:
            try:
                cli.main(["run", "--profile", "none"])
                got = None
            except Exec as x:
                got = x.args
    COPIES.append(copy)
    same = os.path.isfile(copy) and hashlib.sha256(open(copy, "rb").read()).hexdigest() == hashlib.sha256(open(sys.executable, "rb").read()).hexdigest()
    check("ra_run_renamed_interpreter", got == (copy, [copy, "-X", "utf8", cli.ORCH]) and same and stat.S_IMODE(os.stat(copy).st_mode) == 0o755, got)
    check("ra_run_orchestrator_path_has_no_python", "python" not in cli.ORCH, cli.ORCH)
    with env(**dict(GOOD, RUN_ID="a/b c")):
        with captured() as out:
            try:
                cli.main(["run", "--profile", "none"])
                got = None
            except Exec as x:
                got = x.args
    COPIES.append("/tmp/.ra_orch_a_b_c")
    check("ra_run_copy_name_sanitized", got is not None and got[0] == "/tmp/.ra_orch_a_b_c", got)
    with env(**dict(GOOD, RUN_ID=None)):
        with captured() as out:
            try:
                rc = cli.main(["run", "--profile", "none"])
            except Exec as x:
                rc = "exec %s" % (x.args,)
    check("ra_run_preflight_error_exit_2_no_exec", rc == 2 and "ERROR RUN_ID is not set" in out.getvalue(), rc)
finally:
    cli.load_orchestrator, os.execv = real_load, real_execv

# ---------------------------------------------------------------- ra run for real: exec, -X utf8, exit status
stub = mkfile(os.path.join(ROOT, "stub_orchestrator.py"),
              "import json, os, sys\njson.dump({'exe': sys.executable, 'utf8': sys.flags.utf8_mode, 'argv0': sys.argv[0], 'wall': os.environ.get('WALLTIME_SEC')},"
              " open(os.environ['RA_TEST_EXEC_OUT'], 'w'))\nsys.exit(7)\n")
child = ("import sys\nfrom residual_agents import cli\ncli.preflight = lambda probe=False, orch=None: cli.Report()\ncli.ORCH = sys.argv[1]\n"
         "sys.exit(cli.main(['run', '--profile', sys.argv[2]]))\n")
for rename in (True, False):
    rid = "ratest_exec_%d_%d" % (os.getpid(), rename)
    res = os.path.join(ROOT, "exec_%d.json" % rename)
    e = strip_env(dict(os.environ))
    e.update({"PYTHONPATH": REPO, "RUN_ID": rid, "RA_TEST_EXEC_OUT": res, "LANG": "C"})
    if not rename:
        e["RA_ORCH_NO_RENAME"] = "1"
    p = subprocess.run([sys.executable, "-c", child, stub, "paper"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    if rename:
        COPIES.append("/tmp/.ra_orch_" + rid)
    try:
        r = json.load(open(res))
    except Exception:
        r = {}
    want_exe = ("/tmp/.ra_orch_" + rid) if rename else sys.executable
    name = "ra_run_exec_%s" % ("renamed" if rename else "no_rename")
    check(name + "_exit_status_passed_on", p.returncode == 7, (p.returncode, p.stdout.decode(errors="replace")[-800:]))
    check(name + "_interpreter_and_utf8", r.get("exe") == want_exe and r.get("utf8") == 1 and r.get("argv0") == stub and r.get("wall") == "85800", r)

# ---------------------------------------------------------------- ra cost, ra trace
run = os.path.join(ROOT, "run1")
calls = [("plan", {"input": 1000000, "cached": 2000000, "output": 300000, "reasoning": 100000}, 0.5),
         ("exec_u1", {"input": 500000, "cached": 1000000, "output": 200000, "reasoning": 50000}, 0.25),
         ("critic_u1", {"input": 100000, "cached": 0, "output": 50000, "reasoning": 0}, 0.1),
         ("criticsel2_u1_wrapup", {"input": 10, "cached": 20, "output": 30, "reasoning": 5}, 0.0)]
lines = [json.dumps({"kind": "episode_start", "ts": 100, "episode": "x_p1", "paper": "1", "wall": 3600, "version": "v61", "harness": "ra3"})]
lines += [json.dumps({"kind": "call", "ts": 110 + i, "tag": t, "usage": u, "tokens": u["input"] + u["cached"] + u["output"], "cost_usd": c, "dur": 5})
          for i, (t, u, c) in enumerate(calls)]
lines += [json.dumps({"kind": "call", "ts": 120, "tag": "exec_u1", "tokens": 0}), '"an agent note"', "42", "{not json",
          json.dumps({"kind": "call", "tag": None, "usage": {"input": "7", "output": None}, "dur": "5"}),
          json.dumps({"kind": "episode_end", "ts": 200, "grades": {}})]
mkfile(os.path.join(run, "_pece", "ledger.jsonl"), "\n".join(lines) + "\n")
def cost_json(*extra):
    with captured() as out:
        rc = cli.main(["cost"] + list(extra) + ["--json"])
    return rc, json.loads(out.getvalue())
rc, cj = cost_json(run)
def price(u, r):
    return (float(u.get("input") or 0) * r[0] + float(u.get("cached") or 0) * r[1] + float(u.get("output") or 0) * r[2]) / 1e6
want = sum(price(u, (0.15, 0.003, 0.60)) for _, u, _ in calls) + price({"input": 7}, (0.15, 0.003, 0.60))
check("ra_cost_paper_rates", rc == 0 and abs(cj["cost_usd"] - round(want, 4)) < 1e-4 and cj["rates_usd_per_mtok"] == [0.15, 0.003, 0.60] and cj["calls"] == 5,
      (cj.get("cost_usd"), round(want, 4), cj.get("calls")))
check("ra_cost_tokens_and_roles", cj["tokens"] == {"input": 1600017, "cached": 3000020, "output": 550030, "reasoning": 150005}
      and set(cj["cost_usd_by_role"]) == {"plan", "exec", "critic", "criticsel", "?"} and abs(cj["cost_usd_in_run"] - 0.85) < 1e-9, cj)
rc, cj2 = cost_json(os.path.join(run, "_pece"), "--rates", "1,2,3")
check("ra_cost_rates_option_and_pece_path", abs(cj2["cost_usd"] - round(sum(price(u, (1, 2, 3)) for _, u, _ in calls) + price({"input": 7}, (1, 2, 3)), 4)) < 1e-4, cj2.get("cost_usd"))
for args, name in ((["cost", run, "--rates", "1,2"], "ra_cost_bad_rates_refused"), (["cost", os.path.join(ROOT, "nowhere")], "ra_cost_no_ledger_refused")):
    try:
        with captured():
            cli.main(args)
        check(name, False, "no error")
    except SystemExit as x:
        check(name, "ra:" in str(x), str(x))
run2 = os.path.join(ROOT, "run2")   # trace.py expects well-formed call events (a string "dur" makes it raise: not tested here)
mkfile(os.path.join(run2, "_pece", "ledger.jsonl"), "\n".join(l for l in lines if '"tag": null' not in l) + "\n")
with captured() as out:
    rc = cli.main(["trace", run2, "--out", os.path.join(ROOT, "trace_out")])
check("ra_trace_writes_three_files", rc == 0 and sorted(os.listdir(os.path.join(ROOT, "trace_out"))) == ["timeline.html", "timeline.txt", "trace.json"],
      out.getvalue()[-500:])


# ---------------------------------------------------------------- python -m residual_agents
e = strip_env(dict(os.environ))
e["PYTHONPATH"] = REPO
p = subprocess.run([sys.executable, "-m", "residual_agents", "--version"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
check("module_version", p.returncode == 0 and p.stdout.decode().strip() == "residual-agents %s" % __version__, p.stdout[-200:])
p = subprocess.run([sys.executable, "-m", "residual_agents"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
check("module_no_command_usage_exit_2", p.returncode == 2 and b"usage: ra" in p.stdout, p.stdout[-300:])

# ---------------------------------------------------------------- cleanup
srv.shutdown()
_remove_copies()
check("renamed_copies_removed", not [c for c in COPIES if os.path.exists(c)], COPIES)
if os.environ.get("RA_TEST_KEEP") == "1":
    print("kept: %s" % ROOT)
else:
    for d in (WS, STORE):
        if os.path.isdir(d):
            os.chmod(d, 0o755)
    shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(finish())
