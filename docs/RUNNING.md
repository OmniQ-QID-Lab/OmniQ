# Running RA

This page explains how to run RA on one task: build the container image, start the model proxy, launch the run, and
read the result. The settings are in [CONFIGURATION.md](CONFIGURATION.md); judging the result is in
[JUDGING.md](JUDGING.md).

**Run RA only inside a container.** Every agent session runs shell commands without asking
(`DSH_PERMISSION_MODE=danger-full-access`), and the agents install packages, start background jobs and stop
processes. The container is the only boundary.

## What you need

- A Linux host with Docker, or Apptainer (Singularity). RA reads `/proc`, uses process groups and sessions, and must
  share one PID namespace with its agents.
- An OpenAI-compatible chat-completions endpoint that serves DeepSeek V4.1 Flash with image input. The paper's runs
  had images on.
- The resources of the task: 8 to 64 cores and 64 to 432 GB of memory in the paper's runs, and for eight tasks one
  NVIDIA A100 40 GB GPU. `residual_agents/profiles/task_resources.tsv` lists each task.
- Time: RA is given 24 hours per task (a 24-hour job; RA plans against 23 hours 50 minutes).
- Inside the container (the Dockerfile provides all of it): Python 3.11 with the scientific stack listed in
  `infra/container/ENV.md.template`, Node.js 23.8 or newer with built-in zstd (the paper's runs used Node 24), DSH
  0.1.5-rc.1 (`@deepseek-ai/dsh`), the `zstd` program, `pdftotext` (poppler-utils), `procps`, and a UTF-8 locale.

## 1. Build the image

```bash
docker build -f infra/container/Dockerfile --target runner -t residual-agents:1.0.0 .
# or, for Apptainer:
apptainer build residual-agents.sif infra/container/apptainer.def
```

The image is Debian 13 with Python 3.11.15, Node 24 and DSH pinned at 0.1.5-rc.1, installed from npm at build time;
DSH's sub-packages are resolved on the day of the build and may differ from the tree of the paper's runs
(`infra/container/dsh/README.md`; `infra/container/VERSIONS.md` gives the evidence for every version). The repository is installed at `/opt/ra` in editable mode, so no
path of the orchestrator contains the string "python". The container user has an empty home folder. No prebuilt
image is published; build it yourself.

## 2. Check the setup

Inside the container, `ra preflight` checks the environment and stops on any error:

| check | kind |
|---|---|
| `/workspace` exists and is writable; `/workspace/CLAUDE.md` exists; `/workspace/JUDGE.md` does not exist | error |
| `/pece_store` is writable | error |
| the memory folder exists and holds the four files | error |
| `dsh` is on `PATH` and runs; Node has `zlib.zstdDecompressSync` (Node 23.8 or newer) | error |
| an embedded zstd test frame decodes through the orchestrator's own decoder | error |
| proxy mode: `http://127.0.0.1:$DSH_SHIM_PORT/health` answers; direct mode: `DEEPSEEK_API_KEY` is set | error |
| `RUN_ID` is set | error |
| DSH is not 0.1.5-rc.1; `PECE_NCPUS` or `PECE_MEM_GB` is unset; the memory hashes differ from the paper set; in direct mode `MODEL` keeps a provider prefix; the interpreter or orchestrator path contains "python"; the locale is not UTF-8 | warning |

`ra preflight --probe` also sends a 1-token completion through the configured endpoint. `ra run` runs the same
checks before it starts the orchestrator; both exit with code 2 when a check fails.

## 3. The model proxy

In the paper's runs DSH did not talk to the model provider directly. It talked to a small proxy on `127.0.0.1`, and
the proxy forwarded the requests through OpenRouter to DeepSeek V4.1 Flash. The release ships that proxy as
`infra/proxy/ra_proxy.py`. It:

- accepts `POST .../chat/completions` only;
- replaces the model id with `RA_PROXY_MODEL_FORCE` and, if set, pins one provider (`RA_PROXY_PROVIDER`);
- optionally gives every message a stable id (`RA_PROXY_INJECT_IDS=1`, as in the paper's runs), and keeps only the
  first non-empty tool-call id per slot of a streamed answer;
- retries (`infra/proxy/gwretry.py`) before the first byte of the answer reaches DSH: on HTTP 408, 425, 429, 500,
  502 to 504 and 520 to 524, on connection errors and timeouts, and up to 3 times on a content-inspection 400 that
  some gateways return; at most 8 tries, back-off 2 x 2^k seconds plus or minus 30%, capped at 90 s, `Retry-After`
  honoured, within a 15-minute budget (`GW_RETRY_*` variables change the policy);
- writes one JSONL line per request to `RA_PROXY_USAGE_LOG` (model served, provider, system fingerprint, prompt,
  cached, completion and reasoning tokens, status, retries, latency). Keep this log outside the run folder. It is a
  cross-check; the paper's costs come from DSH's own session logs (section 8).

**The key stays in the proxy.** `infra/proxy/start_proxy.sh --state-dir DIR` checks that the key file exists but
does not read it: only the proxy process reads the key, so it never enters a shell, an environment variable or a
command line. The script starts the proxy under a renamed copy of the interpreter with a clean environment, picks a
random free port (8900 to 9199 by default) and accepts the proxy only when its health answer carries this launch's
nonce (up to 6 tries). It prints the port and writes `DIR/port`, `DIR/pid` and the proxy's log `DIR/proxy.log`; keep
`DIR` outside the run folder. On failure it touches the flag file given with `--infra-flag` (for a run, its
`.infra_failure`) and exits 66. The agents see only the dummy key that proxy mode gives DSH.

Example settings for an OpenRouter upstream, as in the paper:

```bash
export RA_PROXY_UPSTREAM=https://openrouter.ai/api/v1
export RA_PROXY_KEY_FILE=$HOME/.config/ra/upstream_key        # a file you create, mode 600
export RA_PROXY_MODEL_FORCE=deepseek/deepseek-v4.1-flash
export RA_PROXY_INJECT_IDS=1
export RA_PROXY_USAGE_LOG=$PWD/runs/proxy_usage.jsonl
```

**Model checkpoint.** DeepSeek replaced DeepSeek V4.1 Flash with a newer checkpoint (1017) in place in October 2026,
after almost all of the paper's runs; the runs used checkpoint 0910 (two runs, #10 and #33, crossed the switch;
the dataset records the checkpoint of every session). Checkpoint 0910 is now offered only by third-party hosts, which you can select with
`RA_PROXY_PROVIDER`. A third-party host may serve the model differently from the vendor (for example with quantization
or speculative decoding), so a run there is not the paper's model setting.

**Direct mode.** Without the proxy (`RA_DSH_SHIM` unset), DSH uses `DEEPSEEK_BASE_URL` and `DEEPSEEK_API_KEY` as you
set them, and `MODEL` must be the id the endpoint expects. The agents inherit that environment and can read the key.

## 4. Run one task

`infra/run/run_task.sh` prepares the run folder, starts the proxy and runs the container:

```bash
infra/run/run_task.sh \
    --task path/to/task --paper path/to/paper_N.pdf [--sm path/to/paper_N_SM.pdf] \
    --paper-num N --label mylabel --out runs \
    --ncpus 16 --mem-gb 128 [--gpu] [--wall-hours 24] \
    --profile paper [--engine docker|apptainer]
```

- `--task` is a folder with the task specification `CLAUDE.md` and, for some tasks, `skills/`. It must not contain
  `JUDGE.md`: the launcher refuses such a folder, because the criteria must stay out of the run.
- `--paper` (and `--sm` for supplementary material) is your own copy of the source paper. It is staged as
  `paper_N.pdf` (`paper_N_SM.pdf`), the name the task specification uses; `N` is the task's registry id
  (`--paper-num`). Leave `--paper` out only for a task without a source paper, such as the toy task.
- The run folder is `<out>/paper_N/<label>_pN/`; it is mounted at `/workspace`. A store folder outside it is mounted
  at `/pece_store`.
- The launcher stages `CLAUDE.md`, the paper, `skills/` and an `ENV.md` that names the run's real cores and memory,
  then passes only variables the orchestrator reads, explicitly: `WALLTIME_SEC` = wall - 600 s, `RUN_ID=<label>_p<N>`,
  `PAPER_NUM`, `PECE_NCPUS`, `PECE_MEM_GB` and, in proxy mode, `RA_DSH_SHIM=1` and `DSH_SHIM_PORT`. It writes
  `run_meta.json` with the settings it used.
- Inside the container, `infra/run/run_in_container.sh` runs `ra run --profile <profile>`. `ra run` applies the
  profile to unset variables, runs the preflight, prints the effective configuration, copies the Python interpreter
  to `/tmp/.ra_orch_<RUN_ID>` and starts the orchestrator with that copy and `-X utf8`. The copy has no "python" in
  its name, so an agent's `pkill -f python3` cannot hit the orchestrator. Set `RA_ORCH_NO_RENAME=1` for interpreters
  that cannot be copied (virtual environments, conda).

Exit codes: 0 normal end; 2 configuration error; 3 infrastructure failure (`.infra_failure` in the run folder: the
endpoint stayed unreachable); 128 + signal number when the run was stopped by a signal. A run stopped by SIGTERM,
SIGINT or SIGHUP stops all its agent processes first.

**Try it first with the toy task** and the smoke profile: see `examples/toy_task/README.md` in the repository. For a
benchmark task, see `examples/run_omniq_task.md`.

## 5. Isolation

| | this release | the paper's runs |
|---|---|---|
| home folder | not mounted; the container user's home is empty | the cluster account's home was mounted: packages under `~/.local` were shared between runs, and home files were readable |
| PID namespace | own namespace per run (Docker by default; Apptainer with `--pid`) | own namespace for 12 of the 59 runs; the node's namespace for the other 47 |
| network | `infra/run/compose.yaml` runs the proxy and the runner in one network namespace, with an optional forward proxy that allows PyPI only | limited by the cluster network: pip worked, GitHub and web search did not |
| criteria | `run_task.sh` refuses a task folder with `JUDGE.md`; the preflight fails if `/workspace/JUDGE.md` exists | `JUDGE.md` was never staged into a run folder |

The forward proxy is an approximation of the paper's limited internet access: it does not stop direct socket
connections. These differences can change what the agents find installed or reachable.

## 6. Batch schedulers

`infra/hpc/ra_episode.pbs` and `infra/hpc/ra_episode.slurm` run one task as a batch job. Replace the `<queue>` and
`<account>` placeholders in their headers. They start the proxy on the compute node, outside the container, and run
`apptainer exec --containall --no-home --cleanenv --pid` with every variable passed explicitly through `--env`
(`--nv` for a GPU task). They disable automatic requeue: a requeued job would find the old run folder, and the
orchestrator refuses a second start in the same folder (exit code 3).

`infra/hpc/ra_pack.sh` runs several tasks on one node: each run gets its own block of cores (`taskset`), its own PID
namespace, and one GPU through `CUDA_VISIBLE_DEVICES` and `RA3SR_GPU_FACTS`.

## 7. Several runs at once

Give every run its own `RUN_ID`, run folder and store folder. The orchestrator finds its processes by markers that
contain the `RUN_ID`, so two runs with the same `RUN_ID` on one machine would stop each other's processes. With
Apptainer, add `--pid` so that each run has its own PID namespace. When `RUN_ID` is unset, the orchestrator picks a unique value
(`ra_<pid>_<epoch>`).

## 8. After the run

The run folder holds the deliverables (`report.md`, `reproduced_figure*`), RA's record in `_pece/` and the DSH
session logs in `.dsh_ra3/`. Useful commands:

| command | what it does |
|---|---|
| `python examples/inspect_run.py RUN_DIR` | plan, unit grades, timeline and cost of the run |
| `ra trace RUN_DIR [--out DIR]` | re-renders `_pece/trace.json`, `timeline.html` and `timeline.txt` from the ledger (into `DIR` if given) |
| `ra cost RUN_DIR [--rates U,C,O]` | re-prices the ledger's session records (default: the paper's rates) |
| `python infra/accounting/settle_sessions.py RUN_DIR [...]` | the paper's cost method: re-reads every DSH session file under `.dsh_ra3/calls/`, sub-agent sessions included, and prices it with the rate card in `infra/accounting/rates/` |

The paper's rate card for DeepSeek V4.1 Flash is US$0.15, 0.003 and 0.60 per million uncached-input, cached-input
and output tokens (`infra/accounting/rates/deepseek-v4.1-flash.json`). Current prices may differ.

## 9. Requirements and limits

- `/workspace` is fixed, because the prompts name it. RA must run in a container that mounts the run folder there.
- `/pece_store` must be a writable mount outside the run folder.
- A UTF-8 locale is required; the launcher also starts the orchestrator with `-X utf8`.
- DSH 0.1.5 writes zstd-compressed session logs. If no decoder works (no `zstd` program, no Python `zstandard`, and a
  Node without built-in zstd), every token count reads 0 and the dead-call rule misfires. The preflight decodes a test
  frame to catch this.
- Each concurrent run needs a unique `RUN_ID`; Apptainer users should add `--pid`.
- Linux only.

## 10. When something goes wrong

| symptom | look at |
|---|---|
| exit code 2 at once | the message on standard error: a missing memory folder, direct mode without a key, or a failed preflight check |
| exit code 3, `.infra_failure` in the run folder | the endpoint stopped answering: the proxy's usage log, `_pece/ledger.jsonl` (`infra_*` events) |
| token counts of 0 in the ledger | the zstd decoder (see section 9) |
| a unit waits forever on a job | `_pece/ledger.jsonl` (`wait_start`, `wait_end`) and the unit's `handoff.json` (`waiting` block) |
| the run ends without `report.md` | `_pece/logs/` (the `report_render` logs) and the `report` and `fallback_report` events |
