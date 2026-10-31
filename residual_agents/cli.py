"""The `ra` command line (also `python -m residual_agents`).

    ra run [--profile paper|smoke|none]      inside the container: apply the profile, preflight, start the orchestrator
    ra preflight [--probe] [--profile ...]   check the setup; --probe also sends a 1-token request to the endpoint
    ra trace RUN_DIR [--out DIR]             re-render _pece/trace.json, timeline.html and timeline.txt from the ledger
    ra cost RUN_DIR [--rates A,B,C]          re-price the ledger's call events ($/Mtok: uncached input, cached input, output)
    ra verify                              build check, prompt fingerprints, memory hashes

The orchestrator itself reads no arguments: it is configured by environment variables (docs/CONFIGURATION.md). A profile
sets a variable only when it is not in the environment. `ra run` copies the interpreter to /tmp/.ra_orch_<RUN_ID> and
starts the orchestrator under that name with -X utf8, so that an agent's `pkill -f python3` cannot hit it
(RA_ORCH_NO_RENAME=1 skips the copy, e.g. for a venv interpreter). Exit codes: the orchestrator's own (0 normal end,
2 configuration error, 3 infrastructure failure); `ra preflight` and `ra run` exit 2 on a failed preflight.
"""
import argparse
import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from residual_agents import MEMORY_LEDGER_HASH, MEMORY_SHA256, __version__

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
ORCH = os.path.join(PKG, "orchestrator.py")
TRACE = os.path.join(PKG, "trace.py")
PROFILES = os.path.join(PKG, "profiles")
MEMORY_FILES = ("planner_notes.md", "critic_notes.md", "residual_bank.md", "libraries.md")
PAPER_RATES = (0.15, 0.003, 0.60)
PAPER_DSH_VERSION = "0.1.5-rc.1"
# one small zstd frame (with checksum) and its text: the decoder check of the preflight
ZSTD_TEST_FRAME = b'(\xb5/\xfd$\x1f\xf9\x00\x00{"type":"preflight","ok":true}\nop\xfbu'
ZSTD_TEST_TEXT = '{"type":"preflight","ok":true}\n'
# the variables the orchestrator reads (besides the RA3_* and PECE_* clocks), shown by `ra run` and `ra preflight`
MAIN_VARS = ("PAPER_NUM", "RUN_ID", "MODEL", "WALLTIME_SEC", "RA3SR_VISION", "PECE_NCPUS", "PECE_MEM_GB", "RA3SR_RATES",
             "RA_DSH_SHIM", "DSH_SHIM_PORT", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY", "RA_DSH_NODE", "RA3SR_NOTES_DIR",
             "RA3SR_REPORT_MIN", "RA3SR_TASK_BOOK_MAX", "RA3SR_DS_CONTEXT", "RA3SR_GPU_FACTS", "RA3SR_DSH_ARGV_FILE",
             "PECE_PLAN_ONLY", "RA3_SMOKE_FORCE_C", "HOST_WORK_DIR")


# ---------------------------------------------------------------- helpers
def _mask(name, value):
    if value and re.search(r"KEY|TOKEN|SECRET|PASSWORD", name):
        return value[:4] + "***"
    return value


def read_profile(name):
    """[(name, value)] of profiles/<name>.env (KEY=VALUE lines; '#' starts a comment line)."""
    if name in (None, "", "none"):
        return []
    path = name if os.path.sep in name else os.path.join(PROFILES, name + ".env")
    if not os.path.isfile(path):
        raise SystemExit("ra: no profile %s" % path)
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                raise SystemExit("ra: %s line %d is not KEY=VALUE" % (path, n))
            k, v = s.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out.append((k.strip(), v))
    return out


def apply_profile(name, env=None):
    """Set the profile's variables that are not in the environment; returns {name: value} of those set."""
    env = os.environ if env is None else env
    done = {}
    for k, v in read_profile(name):
        if k not in env:
            env[k] = v
            done[k] = v
    return done


def load_orchestrator(name="ra_orchestrator_cli"):
    """Import residual_agents/orchestrator.py as a module (its top level only reads the environment and /proc)."""
    spec = importlib.util.spec_from_file_location(name, ORCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def value_sha256(v):
    """The prompt fingerprint method of tools/provenance/prompt_sha256.json."""
    if isinstance(v, str):
        b = v.encode("utf-8")
    else:
        b = json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def task_resources():
    """{registry id: row} of profiles/task_resources.tsv."""
    out = {}
    try:
        with open(os.path.join(PROFILES, "task_resources.tsv"), encoding="utf-8") as f:
            head = f.readline().rstrip("\n").split("\t")
            for line in f:
                row = dict(zip(head, line.rstrip("\n").split("\t")))
                out[row["registry_id"]] = row
    except (OSError, KeyError):
        pass
    return out


def _run(cmd, timeout=60, env=None):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=env)
        return r.returncode, r.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)


# ---------------------------------------------------------------- preflight
class Report(object):
    def __init__(self):
        self.errors, self.warnings, self.notes = [], [], []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def ok(self, msg):
        self.notes.append(msg)

    def show(self, out=sys.stdout):
        for m in self.notes:
            out.write("ok    %s\n" % m)
        for m in self.warnings:
            out.write("WARN  %s\n" % m)
        for m in self.errors:
            out.write("ERROR %s\n" % m)
        out.write("preflight: %d error(s), %d warning(s)\n" % (len(self.errors), len(self.warnings)))


def _writable_dir(path):
    if not os.path.isdir(path):
        return "does not exist"
    try:
        fd, p = tempfile.mkstemp(prefix=".ra_preflight_", dir=path)
        os.close(fd)
        os.remove(p)
        return None
    except OSError as e:
        return "is not writable (%s)" % e


def preflight(probe=False, orch=None):
    """The checks of `ra preflight` (errors are fatal for `ra run`). Writes nothing outside a temporary folder."""
    rep = Report()
    m = orch or load_orchestrator()
    ws, store = m.WS, m.STORE
    # the run folder and the store
    why = _writable_dir(ws)
    (rep.error if why else rep.ok)("%s %s" % (ws, why or "exists and is writable"))
    if os.path.isfile(os.path.join(ws, "CLAUDE.md")):
        rep.ok("%s/CLAUDE.md exists" % ws)
    else:
        rep.error("%s/CLAUDE.md is missing (the task book)" % ws)
    if os.path.exists(os.path.join(ws, "JUDGE.md")):
        rep.error("%s/JUDGE.md exists: the judging criteria must never be visible to the agents" % ws)
    why = _writable_dir(store)
    (rep.error if why else rep.ok)("%s %s" % (store, why or "exists and is writable (episode store)"))
    # memories
    missing = [f for f in MEMORY_FILES if not os.path.isfile(os.path.join(m.NOTES_DIR, f))]
    if not os.path.isdir(m.NOTES_DIR) or missing:
        rep.error("memory folder %s: %s" % (m.NOTES_DIR, "missing" if not os.path.isdir(m.NOTES_DIR) else "lacks " + ", ".join(missing)))
    else:
        diff = [f for f in MEMORY_FILES if file_sha256(os.path.join(m.NOTES_DIR, f)) != MEMORY_SHA256[f]]
        if diff:
            rep.warn("memory files differ from the shipped files: %s (an ablation or an edit)" % ", ".join(diff))
        else:
            rep.ok("memory folder %s holds the four shipped memory files" % m.NOTES_DIR)
    # dsh and Node
    dsh = shutil.which("dsh")
    if not dsh:
        rep.error("dsh is not on PATH (install @deepseek-ai/dsh@%s)" % PAPER_DSH_VERSION)
    else:
        rc, out = _run(["dsh", "--version"])
        if rc != 0:
            rep.error("dsh is on PATH but `dsh --version` failed (%s): %s" % (rc, out[-200:]))
        ver = m._dsh_version()
        if ver != PAPER_DSH_VERSION:
            rep.warn("dsh version %s (the paper runs used %s)" % (ver or "unknown", PAPER_DSH_VERSION))
        else:
            rep.ok("dsh %s at %s" % (ver, dsh))
    node = m.DSH_NODE if (m.DSH_NODE and os.path.isfile(m.DSH_NODE)) else shutil.which("node")
    if not node:
        rep.error("no Node binary (RA_DSH_NODE or node on PATH); dsh needs Node >= 23.8")
    else:
        rc, out = _run([node, "-e", "process.stdout.write(process.versions.node + ' ' + typeof require('zlib').zstdDecompressSync)"])
        if rc == 0 and out.endswith(" function"):
            rep.ok("Node %s at %s has zlib.zstdDecompressSync" % (out.split()[0], node))
        else:
            rep.error("Node at %s lacks zlib.zstdDecompressSync (need Node >= 23.8; got: %s)" % (node, out[-120:]))
    # the session-log decoder, through the orchestrator's own _zstd_cat
    tmp = tempfile.mkdtemp(prefix="ra_preflight_")
    saved = m.DSH_ZSTD_JS
    try:
        m.DSH_ZSTD_JS = os.path.join(tmp, "zstd_cat.js")
        p = os.path.join(tmp, "session.v3.jsonl.zstd")
        with open(p, "wb") as f:
            f.write(ZSTD_TEST_FRAME)
        txt = m._zstd_cat(p)
        if txt == ZSTD_TEST_TEXT:
            rep.ok("zstd session logs decode (%s)" % ", ".join(x for x, ok in (
                ("zstd", shutil.which("zstd")), ("python-zstandard", importlib.util.find_spec("zstandard")), ("node", node)) if ok))
        else:
            rep.error("a zstd test frame does not decode through _zstd_cat: token counts would read 0 (install zstd, python "
                      "zstandard, or Node >= 23.8)")
    finally:
        m.DSH_ZSTD_JS = saved
        shutil.rmtree(tmp, ignore_errors=True)
    # the endpoint
    if m._shim_on():
        port = os.environ.get("DSH_SHIM_PORT", "8500")
        url = "http://127.0.0.1:%s/health" % port
        try:
            import urllib.request
            st = urllib.request.urlopen(url, timeout=5).status
            (rep.ok if st == 200 else rep.error)("local proxy %s answers %s" % (url, st))
        except Exception as e:
            rep.error("RA_DSH_SHIM=1 but the local proxy does not answer at %s (%s)" % (url, e))
    elif not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        rep.error("direct mode (RA_DSH_SHIM is not 1) needs DEEPSEEK_API_KEY")
    else:
        rep.ok("direct mode: %s (the agents can read DEEPSEEK_API_KEY; the local proxy keeps the key out of the sandbox)"
               % (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"))
        if "/" in m.MODEL:
            rep.warn("MODEL=%s keeps its provider prefix in direct mode; it must be the id the endpoint expects" % m.MODEL)
    if probe:
        (rep.ok if m.gw_ok() else rep.error)("1-token request to %s with model %s" % (m._gw_probe()[0], m._dsh_model()))
    # identity and resources
    if os.environ.get("RUN_ID", "").strip():
        rep.ok("RUN_ID=%s" % os.environ["RUN_ID"])
    else:
        rep.error("RUN_ID is not set (it must be unique per concurrent episode: it marks every process of the run)")
    for v in ("PECE_NCPUS", "PECE_MEM_GB"):
        if not os.environ.get(v):
            hint = task_resources().get(os.environ.get("PAPER_NUM", ""))
            rep.warn("%s is not set: the orchestrator falls back to %s%s" % (
                v, "%s=%s" % ("NCPUS" if v == "PECE_NCPUS" else "MEM_GB", m.NCPUS if v == "PECE_NCPUS" else m.MEM_GB),
                ("; the paper run of this task had %s" % (hint["ncpus"] + " cores" if v == "PECE_NCPUS" else hint["mem_gb"] + " GB")) if hint else ""))
    renamed = os.environ.get("RA_ORCH_NO_RENAME") != "1"
    interp = ("/tmp/.ra_orch_" + _safe(os.environ.get("RUN_ID", ""))) if renamed else sys.executable
    for what, path in (("interpreter", interp), ("orchestrator", ORCH)):
        if "python" in path:
            rep.warn("the %s path %s contains 'python': an agent's `pkill -f python3` could stop the orchestrator" % (what, path))
    loc = " ".join(os.environ.get(k, "") for k in ("LC_ALL", "LC_CTYPE", "LANG"))
    if not re.search(r"(?i)utf-?8", loc):
        rep.warn("the locale is not UTF-8 (LANG/LC_ALL); the orchestrator runs with -X utf8, the agents' tools inherit the locale")
    return rep


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s) or "run"


def effective_config(applied):
    lines = []
    names = list(MAIN_VARS) + sorted(k for k in os.environ if (k.startswith("RA3_") or k.startswith("PECE_")) and k not in MAIN_VARS)
    for k in names:
        if k in os.environ:
            lines.append("  %s=%s%s" % (k, _mask(k, os.environ[k]), "   (profile)" if k in applied else ""))
        else:
            lines.append("  %s (unset: code default)" % k)
    return "\n".join(lines)


# ---------------------------------------------------------------- commands
def cmd_run(a):
    applied = apply_profile(a.profile)
    rep = preflight(probe=False)
    rep.show()
    if rep.errors:
        return 2
    print("effective configuration (profile %s):\n%s" % (a.profile, effective_config(applied)))
    exe = sys.executable
    if os.environ.get("RA_ORCH_NO_RENAME") != "1":
        copy = "/tmp/.ra_orch_" + _safe(os.environ.get("RUN_ID", ""))
        shutil.copy2(exe, copy)
        os.chmod(copy, 0o755)
        exe = copy
    argv = [exe, "-X", "utf8", ORCH]
    print("ra run: exec %s" % " ".join(argv))
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(exe, argv)


def cmd_preflight(a):
    apply_profile(a.profile)
    rep = preflight(probe=a.probe)
    rep.show()
    return 2 if rep.errors else 0


def _pece_dir(run_dir):
    d = run_dir.rstrip("/")
    if os.path.basename(d) != "_pece" and os.path.isdir(os.path.join(d, "_pece")):
        d = os.path.join(d, "_pece")
    if not os.path.isfile(os.path.join(d, "ledger.jsonl")):
        raise SystemExit("ra: no ledger.jsonl in %s" % d)
    return d


def ledger_events(pece):
    out = []
    with open(os.path.join(pece, "ledger.jsonl"), encoding="utf-8", errors="replace") as f:
        for ln in f:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def cmd_trace(a):
    spec = importlib.util.spec_from_file_location("ra_trace", TRACE)
    tr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tr)
    pece = _pece_dir(a.run_dir)
    files = tr.build(pece, out_dir=a.out, plan_path=os.path.join(pece, "plan.json"))
    print("ra trace: wrote %s" % ", ".join(files))
    return 0


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def cmd_cost(a):
    rates = PAPER_RATES
    if a.rates:
        try:
            rates = tuple(float(x) for x in a.rates.split(","))
            assert len(rates) == 3
        except (ValueError, AssertionError):
            raise SystemExit("ra: --rates takes three numbers: uncached input, cached input, output ($/Mtok)")
    pece = _pece_dir(a.run_dir)
    tot = {"input": 0.0, "cached": 0.0, "output": 0.0, "reasoning": 0.0}
    by_role, n, in_run = {}, 0, 0.0
    for e in ledger_events(pece):
        if e.get("kind") != "call" or not isinstance(e.get("usage"), dict):
            continue
        n += 1
        u = e["usage"]
        c = (_num(u.get("input")) * rates[0] + _num(u.get("cached")) * rates[1] + _num(u.get("output")) * rates[2]) / 1e6
        role = re.split(r"[_\d]", str(e.get("tag") or "?"), 1)[0] or "?"
        by_role[role] = by_role.get(role, 0.0) + c
        for k in tot:
            tot[k] += _num(u.get(k))
        in_run += _num(e.get("cost_usd"))
    cost = sum(by_role.values())
    res = {"run": os.path.dirname(os.path.abspath(pece)), "calls": n, "rates_usd_per_mtok": list(rates),
           "tokens": dict((k, int(v)) for k, v in tot.items()), "cost_usd": round(cost, 4),
           "cost_usd_by_role": dict((k, round(v, 4)) for k, v in sorted(by_role.items(), key=lambda x: -x[1])),
           "cost_usd_in_run": round(in_run, 4)}
    if a.json:
        print(json.dumps(res, indent=1))
        return 0
    print("run %s: %d calls with usage" % (res["run"], n))
    print("tokens: uncached input %d, cached input %d, output %d (reasoning inside: %d)"
          % (tot["input"], tot["cached"], tot["output"], tot["reasoning"]))
    print("cost at %s $/Mtok: $%.2f  (the ledger's own estimate: $%.2f)" % (",".join("%g" % r for r in rates), cost, in_run))
    for k, v in res["cost_usd_by_role"].items():
        print("  %-10s $%.2f" % (k, v))
    print("note: the ledger counts each call's main session; the paper's cost re-settles every dsh session file "
          "(infra/accounting/settle_sessions.py)")
    return 0


def _load_tool(name):
    p = os.path.join(REPO, "tools", name + ".py")
    if not os.path.isfile(p):
        return None
    spec = importlib.util.spec_from_file_location("ra_tool_" + name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_verify(a):
    bad = 0
    print("residual-agents %s" % __version__)
    build = _load_tool("build")
    if build and os.path.isdir(os.path.join(REPO, "fragments")):
        same = build.build_bytes() == open(ORCH, "rb").read()
        print("%s build check: orchestrator.py %s a fresh build of fragments/" % ("ok   " if same else "FAIL ", "equals" if same else "differs from"))
        bad += not same
    else:
        print("skip  build check (no fragments/ next to the package)")
    pj = os.path.join(REPO, "tools", "provenance", "prompt_sha256.json")
    if os.path.isfile(pj):
        want = json.load(open(pj, encoding="utf-8"))["constants"]
        m = load_orchestrator()
        diff = [k for k, v in want.items() if not hasattr(m, k) or value_sha256(getattr(m, k)) != v["sha256"]]
        print("%s prompt fingerprints: %d of %d constants match%s" % ("ok   " if not diff else "FAIL ", len(want) - len(diff), len(want),
                                                                       (" (differ: %s)" % ", ".join(diff)) if diff else ""))
        bad += bool(diff)
    else:
        print("skip  prompt fingerprints (no tools/provenance/prompt_sha256.json next to the package)")
    mem = os.path.join(PKG, "memories")
    diff = [f for f in MEMORY_FILES if not os.path.isfile(os.path.join(mem, f)) or file_sha256(os.path.join(mem, f)) != MEMORY_SHA256[f]]
    print("%s memory files: %d of 4 match their recorded hashes%s" % ("ok   " if not diff else "FAIL ", 4 - len(diff),
                                                                          (" (differ: %s)" % ", ".join(diff)) if diff else ""))
    bad += bool(diff)
    print("verify: %s" % ("PASS" if not bad else "FAIL (%d)" % bad))
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ra", description="Residual Agents (RA) command line.")
    ap.add_argument("--version", action="version", version="residual-agents %s" % __version__)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="apply a profile, run the preflight and start the orchestrator (inside the container)")
    p.add_argument("--profile", default="paper", help="paper (default), smoke, none, or a path to a KEY=VALUE file")
    p = sub.add_parser("preflight", help="check the setup")
    p.add_argument("--probe", action="store_true", help="also send a 1-token request through the configured endpoint")
    p.add_argument("--profile", default="none", help="apply this profile first (default: none)")
    p = sub.add_parser("trace", help="re-render the timeline of a run from its ledger")
    p.add_argument("run_dir")
    p.add_argument("--out", help="write here instead of RUN_DIR/_pece")
    p = sub.add_parser("cost", help="re-price the ledger's call events")
    p.add_argument("run_dir")
    p.add_argument("--rates", help="uncached input, cached input, output in $/Mtok (default: the paper's 0.15,0.003,0.60)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="build check, prompt fingerprints, memory hashes")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    return {"run": cmd_run, "preflight": cmd_preflight, "trace": cmd_trace, "cost": cmd_cost, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
