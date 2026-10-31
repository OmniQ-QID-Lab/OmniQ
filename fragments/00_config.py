"""Residual Agents (RA) orchestrator: one episode of the harness on one task (DeepSeek V4.1 Flash through dsh).

Four roles over a file bus, one model for all of them:
  PLANNER  - the task book (CLAUDE.md) -> a DAG of units, the REQUIRED ROWS (one owner each, never optional) and the shared READINGS
  EXECUTOR - one unit per directory /workspace/<id>/, fresh sessions; long computations run as ONE registered detached job
  CRITIC   - A pass / B fix in place / C a fresh attempt after SCOUTING / D task-book grounds only; static checks first
  REPORT   - the render unit: figures + report.md from the data on disk
The policy lives in few places:
  call_outcome()    HANDOFF / CUT / INFRA / INTERRUPTED of an executor call - a session that wrote no handoff is continued, never audited;
                    five sub-minute calls in a row without a handoff -> one hold (a runaway loop); INFRA holds until the backend answers
  checkpoint_due()  the one time check: 3 h since the unit's last look, a job whose declared end lies past the usable end (+ a look when a
                    job's verification is cut)
  decide()          the one close rule: only time closes a unit (fix in place while LAST_ACT_S is usable, a scouted fresh attempt while its
                    wall is usable, else settle in place with the unit's best validated round pointed out to the report)
  scout_round()     every C with time left is scouted: 3 scouts (plan + proof of concept), a panel of 3 critics picks and may adopt more
  propagate_inputs() a changed reading or a changed upstream file reaches the units that depend on it
  ResourcePool      one admission rule (declarations first; cores may be oversubscribed after POOL_WAIT_S or for a repair round, memory only
                    by measured use)
  settle()          every terminal transition stops the unit's processes
Knobs are constants; RA3_* environment overrides exist for the offline tests and the smoke run (shorter clocks; RA3_SMOKE_FORCE_C=1
handles the first A/B audit as a C to exercise the scouting - never set in a scored run).
The module reads no command-line arguments; `ra run` (residual_agents/cli.py) starts it. It is built from fragments/ by
tools/build.py.
"""
import fnmatch, glob, hashlib, json, math, os, re, shutil, subprocess, sys, threading, time
import signal   # the stop handlers (_install_stop_handlers)

ORCH_VERSION = "v61"
WS = "/workspace"
PD = os.path.join(WS, "_pece")
STORE = "/pece_store"
PAPER = os.environ.get("PAPER_NUM", "??")
RUN_ID = os.environ.get("RUN_ID") or "ra_%d_%d" % (os.getpid(), int(time.time()))   # unset or empty: unique per process (the run's markers carry it)
MODEL = os.environ.get("MODEL", "deepseek/deepseek-v4.1-flash")
VISION_OK = os.environ.get("RA3SR_VISION", "").strip() == "1"   # images on for the model (the paper runs set RA3SR_VISION=1); off -> TEXT_ONLY_HINT
BACKEND = "dsh"
WALL = int(os.environ.get("WALLTIME_SEC", "43200"))
T0 = time.time()

def _envi(name, default):
    """Integer knob: the default is the value the paper's runs used; tests may shorten it through the environment."""
    try:
        return int(float(os.environ.get(name, default)))
    except Exception:
        return int(default)

# ---- sizes and clocks (seconds unless stated)
MAX_UNITS = 8
UNIT_TYPES = ("produce", "render")
PLAN_FIX_T = _envi("RA3_PLAN_FIX_T", 1200)    # a planner FIX attempt (edit the rejected plan.json in place)
PLAN_T = _envi("RA3_PLAN_T", 3600)              # one planner call
PLAN_TOTAL_S = _envi("RA3_PLAN_TOTAL_S", 6300)  # all planner attempts together (a first call + its pens-down leave room for one retry)
PLAN_WRAP_FIRST_T = 900                        # the pens-down of the first planner call (a resumed session only writes the file)
PLAN_REWRITES = 2                               # an invalid plan goes back to the planner at most this many times
CRITIC_T = _envi("RA3_CRITIC_T", 2700)          # a full audit
CRITIC_DELTA_T = _envi("RA3_CRITIC_DELTA_T", 1800)   # an incremental re-audit after a B
CHECK_T = _envi("RA3_CHECK_T", 1500)            # a checkpoint audit
CRITIC_TOOL_S = 600                             # one critic tool call (probe) is cut after this
FIN_T = 5400                                    # the report call
WRAPUP_T = _envi("RA3_WRAPUP_T", 1800)          # the pens-down session after a critic / report / planner timeout
EXEC_SLICE_S = _envi("RA3_EXEC_SLICE_S", 7200)  # one executor call is at most one slice ...
SLICE_EXT_S = _envi("RA3_SLICE_EXT_S", 3600)    # ... extended by this while the unit is productive ...
SLICE_EXT_MAX = 3                               # ... at most this many times
SLICE_PROG_S = 900                              # productive = a file under the unit written in the last 15 min (or CPU, first extension only)
SLICE_CPU_TICK_S = 300
STALL_CPU_S = 60
BFIX_MIN_S = _envi("RA3_BFIX_MIN_S", 1200)      # a fix slice is clamp(2 x est, BFIX_MIN_S, EXEC_SLICE_S)
CP_S = _envi("RA3_CP_S", 10800)                 # CHECKPOINT: a unit's last look (launch, restart, audit, checkpoint) is at most this old
LAST_ACT_S = _envi("RA3_LAST_ACT_S", 2100)      # the one "too late" threshold (a checkpoint + 10 min): below it no checkpoint, no fix round
MIN_ATTEMPT_S = _envi("RA3_MIN_ATTEMPT_S", 10800)   # a fresh attempt (archive + restart) needs this much usable wall after the scouting
B_SAME_HIT_MAX = 3                              # the one loop guard: the same gate hit standing after this many B rounds in a row -> a C
MECH_MAX = 6                                    # mechanical fix rounds per unit before the audit decides anyway
NOHAND_MAX = 3                                  # sessions in a row that ended their turn without writing handoff.json -> the audit decides
SPIN_N, SPIN_HOLD_S = 5, _envi("RA3_SPIN_HOLD_S", 300)   # executor calls in a row that end in under a minute with no handoff -> a hold (a broken backend spins)
REOPEN_MIN_S = _envi("RA3_REOPEN_MIN_S", 7200)  # a done unit is reopened for a changed input only with this much usable wall
REOPEN_MAX = 2                                  # ... at most this many times per unit
POOL_WAIT_S = _envi("RA3_POOL_WAIT_S", 900)     # a ready unit waits at most this long for its full declaration, then takes what is free
FILE_GATE_S = _envi("RA3_FILE_GATE_S", 1800)    # a dependent may start on an upstream file unchanged this long behind a valid handoff
FLUSH_WINDOW = 900                              # the override fires at remaining <= report reserve + this
REPORT_COST_BOUNDS = (30, 90)                   # report reserve clamp (minutes)
WRAP_MIN_S = 300
INFRA_HOLDS = tuple(int(x) for x in os.environ.get("RA3_INFRA_HOLDS", "120,300,600").split(","))   # holds before the retries of a dead call
INFRA_PROBE_S = _envi("RA3_INFRA_PROBE_S", 3600)   # a call still dead after its retries: the backend is probed this long before the episode is dead
WAIT_TICK = _envi("PECE_WAIT_TICK", 60)
WAIT_STALL_S = _envi("PECE_WAIT_STALL_S", 900)  # no visible progress of a waited job for this long (no live process) -> the executor looks
WAIT_PROG_STALL_S = _envi("PECE_WAIT_PROG_STALL_S", 3600)   # declared progress files silent this long while processes live -> the executor looks
WAIT_DEAD_GRACE_S = _envi("PECE_WAIT_DEAD_GRACE_S", 120)
WAIT_ADOPT_MAX_S = _envi("PECE_WAIT_ADOPT_MAX_S", 3600)
OVERRIDE_TICK = _envi("PECE_OVERRIDE_TICK", 60)
POLL_S = _envi("PECE_POLL_S", 30)
SCHED_TICK = _envi("PECE_SCHED_TICK", 15)
CLAMP_MARGIN_S = 300
RESTART_KEEP_CODE = True
CODE_EXT = (".py", ".sh", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".jl", ".c", ".cpp", ".h", ".hpp", ".f90", ".f", ".mk", ".txt")
VENDORED_DIRS = ("site-packages", "dist-packages", "node_modules", ".venv", "venv", "__pycache__", ".git")
PLAN_ONLY = os.environ.get("PECE_PLAN_ONLY") == "1"   # diagnostic: exit right after a valid plan
SMOKE_FORCE_C = os.environ.get("RA3_SMOKE_FORCE_C", "0") == "1"   # SMOKE ONLY: the episode's first A/B audit is handled as a C (exercises scouting)

def _rates():   # $ per million tokens (uncached input, cached input, output incl. reasoning) for the cost estimate
    try:
        v = tuple(float(x) for x in os.environ.get("RA3SR_RATES", "0.10,0.02,0.40").split(","))
        return v if len(v) == 3 else (0.10, 0.02, 0.40)
    except Exception:
        return (0.10, 0.02, 0.40)
RATES = _rates()

# ---- run-wide state and locks
EXEC_SIDS = {}          # uid -> the last executor session id
CALLS = {"n": 0}
INFRA = {"dead": False}
UNIT_PIDS = {}          # uid -> session ids of the unit's calls (their leftovers)
LAST_LOOK = {}          # uid -> time of the unit's launch / restart / last LLM verdict (the checkpoint clock)
CP_DONE = {}            # uid -> set of (job key, reason) the checkpoint already looked at
REPLANNING = threading.Event()
OVERRIDE = threading.Event()
STOP = threading.Event()
ABORT = threading.Event()   # a stop signal arrived: no new dsh session starts, the run's processes are killed, the process exits
REPORT = {"started": False, "reason": None, "t": None}
LEDGER_LOCK = threading.RLock()
CALL_LOCK = threading.RLock()
STATE_LOCK = threading.RLock()
PLANNER_LOCK = threading.RLock()
ACTIVE_CALLS = {}       # tag -> {uid, role, t0, p}
INTERRUPT_REQ = {}      # tag -> (reason, mode)
