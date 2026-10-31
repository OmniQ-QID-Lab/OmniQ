"""Integrity checks anyone can run:
  A. the built orchestrator equals a fresh build of fragments/ (tools/build.py --check);
  B. the sha256 of every prompt constant equals tools/provenance/prompt_sha256.json;
  C. the four memory files: raw sha256, size and the hash the ledger records, against tools/provenance/memory_sha256.json,
     residual_agents/__init__.py and the orchestrator's own load_notes(); `ra verify` passes.
Usage: python3 tests/test_provenance.py
"""
import hashlib, importlib.util, json, os, shutil, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
from e2e_lib import strip_env, check, finish
import residual_agents

PROV = os.path.join(REPO, "tools", "provenance")
ORCH = os.path.join(REPO, "residual_agents", "orchestrator.py")
MEM = os.path.join(REPO, "residual_agents", "memories")
MEMORY_FILES = ("planner_notes.md", "critic_notes.md", "residual_bank.md", "libraries.md")
ROOT = tempfile.mkdtemp(prefix="ra_provenance_")

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def run(cmd, env=None, timeout=600):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=REPO, env=env, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")

def clean_env(**extra):
    e = strip_env(dict(os.environ))
    e["PYTHONPATH"] = REPO + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e.update(extra)
    return e

strip_env(os.environ)
os.environ.update({"PECE_NCPUS": "16", "PECE_MEM_GB": "64", "RA3SR_GPU_FACTS": "none", "RUN_ID": "tp_%d" % os.getpid(), "RA_DSH_SHIM": "1"})
sr = load(ORCH, "ra_orchestrator_provenance")
build = load(os.path.join(REPO, "tools", "build.py"), "ra_tool_build")

pj = json.load(open(os.path.join(PROV, "prompt_sha256.json"), encoding="utf-8"))
mj = json.load(open(os.path.join(PROV, "memory_sha256.json"), encoding="utf-8"))

# ---------------------------------------------------------------- A. build check
rc, out = run([sys.executable, os.path.join("tools", "build.py"), "--check"])
check("A_build_check_cli", rc == 0 and "OK" in out, out[-500:])
built = build.build_bytes()
check("A_build_bytes_equal_committed", built == open(ORCH, "rb").read() and built.startswith(build.HEADER.encode("utf-8")), len(built))
frags = build.manifest()
check("A_manifest_20_fragments", len(frags) == 20 and frags == sorted(frags) and frags[0] == "00_config.py" and frags[-1] == "19_report_main.py", frags)

# ---------------------------------------------------------------- B. prompt fingerprints
consts = pj["constants"]
check("B_prompt_set", len(consts) == 42 and all(hasattr(sr, n) for n in consts), sorted(n for n in consts if not hasattr(sr, n)))
def vsha(v):
    b = v.encode("utf-8") if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b).hexdigest()
diff = [n for n, d in sorted(consts.items()) if not hasattr(sr, n) or vsha(getattr(sr, n)) != d["sha256"]]
check("B_prompt_sha256_42_of_42", not diff, diff)
check("B_prompt_types_and_sizes", all(type(getattr(sr, n)).__name__ == d["type"] for n, d in consts.items() if hasattr(sr, n))
      and all(len(getattr(sr, n)) == d["chars"] for n, d in consts.items() if d["type"] == "str" and hasattr(sr, n)),
      [n for n, d in consts.items() if hasattr(sr, n) and type(getattr(sr, n)).__name__ != d["type"]])

# ---------------------------------------------------------------- C. memories
files = sorted(f for f in os.listdir(MEM) if not f.startswith("."))
check("C_memory_folder_holds_four_files", files == sorted(MEMORY_FILES), files)
rows = {}
for f in MEMORY_FILES:
    raw = open(os.path.join(MEM, f), "rb").read()
    rows[f] = (len(raw), hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw.decode("utf-8").strip().encode("utf-8")).hexdigest()[:8])
want_json = dict((f, (d["bytes"], d["sha256"], d["ledger_hash"])) for f, d in mj["files"].items())
check("C_memory_raw_sha256_and_size", all(rows[f][:2] == want_json[f][:2] for f in MEMORY_FILES),
      dict((f, rows[f][0]) for f in MEMORY_FILES if rows[f][:2] != want_json[f][:2]))
check("C_memory_init_constants", all(rows[f][1] == residual_agents.MEMORY_SHA256[f] for f in MEMORY_FILES)
      and all(rows[f][2] == residual_agents.MEMORY_LEDGER_HASH[f[:-3]] for f in MEMORY_FILES), "")
check("C_memory_ledger_hash", all(rows[f][2] == want_json[f][2] for f in MEMORY_FILES),
      dict((f, rows[f][2]) for f in MEMORY_FILES))
# what the orchestrator itself records (ledger event notes_loaded) for the shipped folder
ws = os.path.join(ROOT, "ws")
os.makedirs(os.path.join(ws, "_pece"))
sr.WS, sr.PD, sr.STORE = ws, os.path.join(ws, "_pece"), os.path.join(ROOT, "store")
os.makedirs(sr.STORE)
check("C_default_memory_folder", os.path.realpath(sr.NOTES_DIR) == os.path.realpath(MEM), sr.NOTES_DIR)
sr.load_notes()
ev = [json.loads(l) for l in open(os.path.join(ws, "_pece", "ledger.jsonl"))]
nl = [e for e in ev if e.get("kind") == "notes_loaded"]
got = dict((k, (v or {}).get("sha")) for k, v in (nl[0].get("notes") or {}).items()) if nl else {}
check("C_load_notes_records_hashes", got == residual_agents.MEMORY_LEDGER_HASH, got)
rc, out = run([sys.executable, "-m", "residual_agents", "verify"], env=clean_env())
check("C_ra_verify", rc == 0 and "verify: PASS" in out and "42 of 42 constants match" in out and "4 of 4 match" in out, out[-1500:])

if os.environ.get("RA_TEST_KEEP") == "1":
    print("kept: %s" % ROOT)
else:
    shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(finish())
