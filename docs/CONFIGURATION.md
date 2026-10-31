# Configuration

The orchestrator reads no command-line arguments. Everything is set through environment variables. Most names use
the fixed prefixes `PECE_`, `RA3_` and `RA3SR_` (see [GLOSSARY.md](GLOSSARY.md)); the launcher, route and proxy
settings start with `RA_`.

The code defaults are not the paper's settings. For example, the code default wall is 12 hours, images are off, and
the cost rates differ. Use the paper profile to get the paper's settings.

## Profiles

`ra run --profile NAME` sets every variable of the profile that is still unset, then prints the effective
configuration before it starts the orchestrator. `NAME` is `paper` (the default), `smoke`, `none`, or the path of your
own file of `KEY=VALUE` lines. A variable you set yourself always wins. `ra preflight --profile NAME` applies a profile
the same way before its checks (default: none).

| file | content |
|---|---|
| `residual_agents/profiles/paper.env` | the paper's settings: `MODEL`, `WALLTIME_SEC=85800`, `RA3SR_VISION=1`, `RA3SR_RATES=0.15,0.003,0.60` and `RA_DSH_SHIM=1` (the "paper" column of the tables below) |
| `residual_agents/profiles/smoke.env` | a short test run with the paper's model, image input, rates and proxy mode, and shorter clocks: `WALLTIME_SEC=21000`, `RA3_CP_S=1800`, `RA3_SCOUT_T=900`, `RA3_SCOUT_SELECT_T=600`, `RA3_MIN_ATTEMPT_S=3600`; optionally `RA3_SMOKE_FORCE_C=1` (commented out) |
| `residual_agents/profiles/task_resources.tsv` | one row per task: paper number, registry id, cores, memory in GB, number of GPUs and GPU model, taken from each published run's `episode_start` event. Give a task's cores and memory to the launcher, which passes them as `PECE_NCPUS` and `PECE_MEM_GB` |

`--profile none` applies nothing, so the code defaults hold for every unset variable.

## Run identity, budget and model

| variable | code default | paper | meaning |
|---|---|---|---|
| `PAPER_NUM` | `??` | the task's registry id | written into every ledger event (`paper`) |
| `RUN_ID` | unique `ra_<pid>_<epoch>` | `<label>_p<registry id>` | the run id: part of every process marker and of the store path. Must be unique among runs that share a machine |
| `MODEL` | `deepseek/deepseek-v4.1-flash` | same | the model id; see "Model endpoint" below |
| `WALLTIME_SEC` | 43200 | 85800 | the wall the clock plans against, in seconds (the paper: a 24-hour job minus 600 s) |
| `RA3SR_VISION` | off | `1` | `1` = the model accepts images: DSH gets a catalog entry with text and image input, and the text-only hint is left out of the prompts. The endpoint must accept images |
| `RA3SR_RATES` | `0.10,0.02,0.40` | `0.15,0.003,0.60` | US$ per million tokens for uncached input, cached input and output; output includes reasoning, so reasoning is not billed twice |
| `PECE_NCPUS` | the affinity mask, capped by the cgroup CPU quota | per task (`task_resources.tsv`, passed by the launcher) | cores the plan is validated against and the pool is sized to |
| `PECE_MEM_GB` | the smallest cgroup memory limit of the process, else the machine's RAM | per task | memory in GB, used the same way |
| `RA3SR_NOTES_DIR` | `<module folder>/memories` | default | the memory folder (see "Memories" below) |
| `RA3SR_REPORT_MIN` | 75 | default | report reserve in minutes, clamped to 30 to 90 |
| `RA3SR_TASK_BOOK_MAX` | 64000 | default | character cap of the TASK BOOK block |
| `RA3SR_DS_CONTEXT` | 1000000 | default | context window written into the image catalog entry |
| `RA3SR_GPU_FACTS` | unset: ask `nvidia-smi` | unset | GPU list for the prompt header: `none`, or `name, 40960 MiB; ...` |
| `RA3SR_DSH_ARGV_FILE` | `1` | default | `0` puts the prompt on the DSH command line instead of passing it through the prompt-file launcher |
| `PECE_PLAN_ONLY` | unset | unset | `1` = stop after the first valid plan and write `_pece/plan_summary.txt` (a diagnostic) |
| `RA3_SMOKE_FORCE_C` | `0` | `0` | smoke tests only: the run's first A or B audit is handled as a C, to exercise the probe agents. Never set it in a scored run |
| `HOST_WORK_DIR` | empty | set by the launcher | host path of the run folder; written only into the store's `meta.json` |

Reasoning effort has no variable: the DSH patch file always sets it to `max`, as in the paper's runs.

When unset, `PECE_NCPUS` and `PECE_MEM_GB` are measured inside the container; the source of each value is logged.
Set them to the allocation you intend, because the plan and the pool are sized to these numbers.

## Model endpoint

DSH talks to an OpenAI-compatible chat-completions endpoint. There are two modes.

| variable | default | paper | meaning |
|---|---|---|---|
| `RA_DSH_SHIM` | unset (direct mode) | `1` | `1` = proxy mode: DSH talks to the local proxy at `http://127.0.0.1:$DSH_SHIM_PORT/v1` with a dummy key |
| `DSH_SHIM_PORT` | 8500 | set by the launcher | port of the local proxy |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | not used | direct mode: the endpoint, passed to DSH unchanged |
| `DEEPSEEK_API_KEY` | none (required) | not used | direct mode: the key, passed to DSH unchanged |
| `RA_DSH_NODE` | unset: `node` on `PATH` | unset | the Node binary for DSH and for the zstd fallback. When set, its folder is put first on the agents' `PATH` |

**Proxy mode** (`RA_DSH_SHIM=1`, the paper's setting). DSH sends the model id without its provider prefix (for
`deepseek/deepseek-v4.1-flash` it sends `deepseek-v4.1-flash`), and the proxy replaces it with the id the upstream
expects (`RA_PROXY_MODEL_FORCE`). The key stays in the proxy process; agents see only the dummy key. See
[RUNNING.md](RUNNING.md).

**Direct mode** (`RA_DSH_SHIM` unset). DSH sends `MODEL` exactly as set, so `MODEL` must be the id your endpoint
expects. `DEEPSEEK_API_KEY` must be set; otherwise the run stops with exit code 2 before it writes anything. Every
agent session inherits the orchestrator's environment, so **in direct mode the agents can read the key**. Use proxy
mode when that matters.

## Clocks

Integer overrides of the time constants, in seconds. The defaults are the paper's values; the paper's runs changed
none of them. Shorter values are meant for the offline tests and smoke runs.

| variable | default | meaning |
|---|---|---|
| `RA3_PLAN_T` | 3600 | one planner call |
| `RA3_PLAN_FIX_T` | 1200 | a planner call that fixes a rejected `plan.json` in place |
| `RA3_PLAN_TOTAL_S` | 6300 | all planner attempts together |
| `RA3_CRITIC_T` | 2700 | a full audit |
| `RA3_CRITIC_DELTA_T` | 1800 | a delta re-audit after a B |
| `RA3_CHECK_T` | 1500 | a checkpoint audit |
| `RA3_WRAPUP_T` | 1800 | the pens-down session after a critic, report or planner timeout |
| `RA3_EXEC_SLICE_S` | 7200 | one executor session (a slice) ... |
| `RA3_SLICE_EXT_S` | 3600 | ... extended by this while the unit is productive (at most 3 times) |
| `RA3_BFIX_MIN_S` | 1200 | lower bound of a repair slice: clamp(2 x estimate, this, `RA3_EXEC_SLICE_S`) |
| `RA3_CP_S` | 10800 | a checkpoint is due when the unit's last look (launch, restart, audit or checkpoint) is this old |
| `RA3_LAST_ACT_S` | 2100 | the "too late" threshold: below it, no checkpoint and no repair round |
| `RA3_MIN_ATTEMPT_S` | 10800 | usable wall a fresh attempt needs after the probe agents |
| `RA3_SPIN_HOLD_S` | 300 | hold after 5 executor calls in a row that end within a minute without a handoff |
| `RA3_REOPEN_MIN_S` | 7200 | a finished unit is reopened for a changed input only with this much usable wall |
| `RA3_POOL_WAIT_S` | 900 | a ready unit waits at most this long for its full declaration, then takes what is free |
| `RA3_FILE_GATE_S` | 1800 | a dependent may start on an upstream file unchanged this long behind a valid handoff |
| `RA3_INFRA_HOLDS` | `120,300,600` | holds before the retries of a dead call |
| `RA3_INFRA_PROBE_S` | 3600 | a call still dead after its retries: the endpoint is probed this long before the run is declared dead |
| `RA3_SCOUT_T` | 2700 | one probe agent |
| `RA3_SCOUT_SELECT_T` | 1800 | one panel critic |
| `PECE_WAIT_TICK` | 60 | polling interval while waiting on a registered job |
| `PECE_WAIT_STALL_S` | 900 | no visible progress of a waited job for this long, with no live process: the executor looks |
| `PECE_WAIT_PROG_STALL_S` | 3600 | declared progress files silent this long while processes run: the executor looks |
| `PECE_WAIT_DEAD_GRACE_S` | 120 | the registered process is gone and nothing runs: after this long without new files the wait ends as stalled |
| `PECE_WAIT_ADOPT_MAX_S` | 3600 | the registered process is gone but other processes of the unit still run: after this long the wait ends as an orphan |
| `PECE_OVERRIDE_TICK` | 60 | tick of the override thread (it also rewrites `_pece/clock.json`) |
| `PECE_POLL_S` | 30 | polling interval of the call watchdog |
| `PECE_SCHED_TICK` | 15 | tick of the scheduling loop |

The usable wall that starts probe agents is derived, not set: `RA3_SCOUT_T + RA3_SCOUT_SELECT_T + 2 x 300 +
RA3_MIN_ATTEMPT_S + 600` = 16,500 s with the defaults.

Fixed constants (no variable): at most 8 units per plan (soft), 3 slice extensions, 6 mechanical repair rounds per
unit, 3 probe agents and 3 panel critics, 2 reopenings per unit, a 15-minute flush window, 600 s per critic shell
command, 3 keyword-matched residual entries at a unit's first launch, 4,000 characters per residual block, and 3,500
characters of transcript tail in a continuation.

## Memories (the ablation switch)

`RA3SR_NOTES_DIR` points at the memory folder; by default it is `residual_agents/memories/`, which holds the four
files of the paper's memories. To run without part of the experience layer, point it at a folder that lacks
some of the files:

| file left out | effect |
|---|---|
| `planner_notes.md` | no planner memory in the planner and replanner prompts |
| `critic_notes.md` | no critic memory in the audits |
| `residual_bank.md` | no residual entries anywhere, and an empty RESIDUAL INDEX |
| `libraries.md` | no `/workspace/LIBRARIES.md` |

A missing folder stops the run with exit code 2 before anything is written; missing files only give a
warning and a `notes_missing` ledger event. `ra preflight` warns when the memory hashes differ from the shipped files. The
paper's runs always had all four files; such ablations were not part of the paper.

## Settings outside the orchestrator

| variable | used by | meaning |
|---|---|---|
| `RA_ORCH_NO_RENAME` | `ra run` | `1` = run the orchestrator with the interpreter itself, not with a renamed copy (for virtual environments or conda) |
| `RA_PROXY_UPSTREAM` | proxy | base URL of the upstream OpenAI-compatible endpoint, for example `https://openrouter.ai/api/v1` (required; no default) |
| `RA_PROXY_KEY_FILE` | proxy | file that holds the upstream key (required); only the proxy process reads it |
| `RA_PROXY_MODEL_FORCE` | proxy | model id sent upstream, whatever DSH sent (the paper: `deepseek/deepseek-v4.1-flash`) |
| `RA_PROXY_PROVIDER` | proxy | pin one provider of a routing service (OpenRouter request format; no fallback) |
| `RA_PROXY_EXPECT_MODEL` | proxy | the model id the answer must report; a mismatch is logged, the answer is still passed on |
| `RA_PROXY_INJECT_IDS` | proxy | `1` = give every message a stable id, as in the paper's runs (default 0) |
| `RA_PROXY_REWRITE` | proxy | optional field rewrites for an endpoint that rejects a value DSH sends (for example `reasoning_effort?=high`); this changes the experiment, and the paper's runs used none |
| `RA_PROXY_USAGE_LOG` | proxy | JSONL file with one line per request (status, attempts, latency, the model requested, sent and served, provider, token counts); keep it outside the run folder |
| `RA_PROXY_TIMEOUT` | proxy | seconds per upstream request (default 14400) |
| `RA_PROXY_BIND`, `RA_PROXY_PORT`, `RA_PROXY_PORT_RANGE` | proxy | listening address (default `127.0.0.1`) and port; `start_proxy.sh` picks a free port in the range (default 8900-9199) |
| `GW_RETRY_MAX`, `GW_RETRY_BASE`, `GW_RETRY_CAP`, `GW_RETRY_BUDGET`, `GW_RETRY_INSPECT` | proxy | the retry policy: at most 8 attempts, back-off 2 x 2^k s capped at 90 s, a 900 s budget, 3 retries of a content-inspection 400; `GW_RETRY=0` turns retries off |
| `RA_TEST_KEEP` | `tests/run_all.sh` | `1` = keep the temporary test workspaces |

## What the orchestrator sets for each agent session

These are written by the orchestrator; do not set them yourself. `DSH_HOME` (the session's own home under
`/workspace/.dsh_ra3/calls/`), `DEEPSEEK_BASE_URL` and `DEEPSEEK_API_KEY` (see "Model endpoint"),
`DSH_PERMISSION_MODE=danger-full-access`, `DSH_TELEMETRY_DISABLED=1`, the markers `PECE_CALL_TAG` and `PECE_UNIT`,
and with a resource grant `PECE_GRANT_CORES`, `PECE_GRANT_MEM_GB` and the thread caps `OMP_NUM_THREADS`,
`MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS` and `JULIA_NUM_THREADS`.
