"""The run profiles (residual_agents/profiles/): paper.env holds the settings of the paper's runs, smoke.env the authors' shortened
clocks, task_resources.tsv one row per task. Every profile variable is one the orchestrator reads, and the orchestrator built under
each profile has the expected settings. With RA_TEST_TRAJECTORIES=DIR (published runs, unpacked), every run's episode_start and
token_usage.json are compared with the paper profile and its task_resources.tsv row.
Usage: python3 tests/test_profiles.py
"""
import contextlib, glob, importlib.util, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
from e2e_lib import strip_env, check, skip, finish
from residual_agents import cli

ORCH = os.path.join(REPO, "residual_agents", "orchestrator.py")
PAPER = {"MODEL": "deepseek/deepseek-v4.1-flash", "WALLTIME_SEC": "85800", "RA3SR_VISION": "1", "RA3SR_RATES": "0.15,0.003,0.60", "RA_DSH_SHIM": "1"}
SMOKE = dict(PAPER, WALLTIME_SEC="21000", RA3_CP_S="1800", RA3_SCOUT_T="900", RA3_SCOUT_SELECT_T="600", RA3_MIN_ATTEMPT_S="3600")
# the module settings of the paper's runs (their episode_start events) and of the smoke profile
PAPER_SETTINGS = {"WALL": 85800, "MODEL": "deepseek/deepseek-v4.1-flash", "VISION_OK": True, "RATES": (0.15, 0.003, 0.6), "REPORT_MIN": 75,
                  "CP_S": 10800, "MIN_ATTEMPT_S": 10800, "SCOUT_T": 2700, "SCOUT_SELECT_T": 1800, "LAST_ACT_S": 2100, "EXEC_SLICE_S": 7200,
                  "SMOKE_FORCE_C": False}
SMOKE_SETTINGS = dict(PAPER_SETTINGS, WALL=21000, CP_S=1800, MIN_ATTEMPT_S=3600, SCOUT_T=900, SCOUT_SELECT_T=600)

strip_env(os.environ)
os.environ.update({"RA3SR_GPU_FACTS": "none", "RUN_ID": "tprof_%d" % os.getpid()})

@contextlib.contextmanager
def profile_env(name):
    saved = dict(os.environ)
    try:
        for k in [k for k in os.environ if k in PAPER or k in SMOKE]:
            os.environ.pop(k)
        cli.apply_profile(name)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)

def load_orch(tag):
    spec = importlib.util.spec_from_file_location("ra_orchestrator_profile_%s" % tag, ORCH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def settings(m):
    return dict((k, getattr(m, k, None)) for k in PAPER_SETTINGS)

# ---------------------------------------------------------------- the files
paper = dict(cli.read_profile("paper"))
smoke = dict(cli.read_profile("smoke"))
check("paper_env_exact", paper == PAPER and len(cli.read_profile("paper")) == len(PAPER), paper)
check("smoke_env_exact", smoke == SMOKE and len(cli.read_profile("smoke")) == len(SMOKE), smoke)
smoke_txt = open(os.path.join(cli.PROFILES, "smoke.env"), encoding="utf-8").read()
check("smoke_force_c_documented_not_set", re.search(r"(?m)^#\s*RA3_SMOKE_FORCE_C=1\s*$", smoke_txt) is not None and "RA3_SMOKE_FORCE_C" not in smoke, "")
src = open(ORCH, encoding="utf-8").read()
read_by_orch = set(re.findall(r'(?:os\.environ\.get|_envi)\(\s*"([A-Z0-9_]+)"', src))
unread = sorted(k for k in set(PAPER) | set(SMOKE) if k not in read_by_orch)
check("profile_variables_are_read_by_the_orchestrator", not unread, unread)
check("profile_variables_shown_by_ra_run", all(k in cli.MAIN_VARS or k.startswith("RA3_") for k in set(PAPER) | set(SMOKE)), "")

# ---------------------------------------------------------------- the orchestrator under each profile
with profile_env("paper"):
    m = load_orch("paper")
    got = settings(m)
    check("paper_profile_module_settings", got == PAPER_SETTINGS, dict((k, v) for k, v in got.items() if v != PAPER_SETTINGS[k]))
    check("paper_profile_route", m._shim_on() is True and m._dsh_model() == "deepseek-v4.1-flash" and m._dsh_env()["DEEPSEEK_API_KEY"] == "shim-local",
          (m._shim_on(), m._dsh_model()))
with profile_env("smoke"):
    m = load_orch("smoke")
    got = settings(m)
    check("smoke_profile_module_settings", got == SMOKE_SETTINGS, dict((k, v) for k, v in got.items() if v != SMOKE_SETTINGS[k]))
with profile_env("none"):
    m = load_orch("none")
    check("no_profile_code_defaults_differ", m.WALL == 43200 and m.VISION_OK is False and m.RATES == (0.10, 0.02, 0.40) and m._shim_on() is False,
          (m.WALL, m.VISION_OK, m.RATES))

# ---------------------------------------------------------------- task_resources.tsv
tsv = os.path.join(cli.PROFILES, "task_resources.tsv")
lines = open(tsv, encoding="utf-8").read().splitlines()
head = lines[0].split("\t")
rows = [dict(zip(head, l.split("\t"))) for l in lines[1:] if l.strip()]
check("resources_header", head == ["paper_number", "registry_id", "ncpus", "mem_gb", "n_gpu", "gpu_model"], head)
check("resources_59_tasks", len(rows) == 59 and sorted(int(r["paper_number"]) for r in rows) == list(range(1, 60)), len(rows))
check("resources_registry_ids_unique", len(set(r["registry_id"] for r in rows)) == 59 and all(r["registry_id"].isdigit() for r in rows), "")
def _row_ok(r):
    try:
        return (int(r["ncpus"]) > 0 and int(r["mem_gb"]) > 0 and r["n_gpu"] in ("0", "1")
                and bool(r.get("gpu_model", "").strip()) == (r["n_gpu"] == "1"))
    except (KeyError, ValueError):
        return False
check("resources_rows_well_formed", all(_row_ok(r) for r in rows), [r for r in rows if not _row_ok(r)][:3])
check("resources_match_cli_lookup", cli.task_resources() == dict((r["registry_id"], r) for r in rows), "")

# ---------------------------------------------------------------- the published runs (optional)
traj = os.environ.get("RA_TEST_TRAJECTORIES")
if traj and os.path.isdir(traj):
    by_reg = dict((r["registry_id"], r) for r in rows)
    seen, bad_settings, bad_res, bad_rates = {}, [], [], []
    for led in sorted(glob.glob(os.path.join(traj, "**", "_pece", "ledger.jsonl"), recursive=True)):
        run_dir = os.path.dirname(os.path.dirname(led))
        es = None
        for ln in open(led, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("kind") == "episode_start":
                es = e
                break
        name = os.path.basename(run_dir)
        if es is None:
            bad_settings.append((name, "no episode_start"))
            continue
        want = {"wall": 85800, "model": PAPER["MODEL"], "vision_ok": True, "dsh_model": "deepseek-v4.1-flash", "epic_shim": True,
                "dsh_version": cli.PAPER_DSH_VERSION, "report_min": 75, "cp_s": 10800, "min_attempt_s": 10800, "scout_t": 2700,
                "scout_select_t": 1800, "last_act_s": 2100, "exec_slice_s": 7200, "smoke_force_c": False}
        diff = dict((k, es.get(k)) for k, v in want.items() if es.get(k) != v)
        if diff:
            bad_settings.append((name, diff))
        reg = str(es.get("paper"))
        seen[reg] = seen.get(reg, 0) + 1
        r = by_reg.get(reg)
        gpus = es.get("gpus") or []
        if (r is None or int(r["ncpus"]) != es.get("ncpus") or int(r["mem_gb"]) != es.get("mem_gb") or int(r["n_gpu"]) != len(gpus)
                or (gpus and gpus[0][0] != r["gpu_model"])):
            bad_res.append((name, reg))
        tu = os.path.join(run_dir, "token_usage.json")
        if os.path.isfile(tu):
            rt = json.load(open(tu, encoding="utf-8")).get("rates_per_mtok") or {}
            if (rt.get("uncached_input"), rt.get("cached_input"), rt.get("output")) != PAPER_SETTINGS["RATES"]:
                bad_rates.append(name)
    n = sum(seen.values())
    print("     (%d published runs read)" % n)
    check("published_runs_have_paper_settings", n > 0 and not bad_settings, bad_settings[:5])
    check("published_runs_match_task_resources", n > 0 and not bad_res, bad_res[:5])
    check("published_runs_have_paper_rates", n > 0 and not bad_rates, bad_rates[:5])
    if n == 59:
        check("published_runs_cover_every_task_once", sorted(seen) == sorted(by_reg) and set(seen.values()) == {1},
              sorted(set(seen) ^ set(by_reg)))
else:
    skip("published_runs_have_paper_settings", "set RA_TEST_TRAJECTORIES to a folder of published runs")
sys.exit(finish())
