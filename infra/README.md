# infra/: running, judging and costing Residual Agents

Everything needed around the orchestrator to run one task the way the paper's runs were run, to judge the result,
and to settle its cost.

| folder | what it holds |
|---|---|
| `proxy/` | the model proxy, the only process that holds the provider key: `ra_proxy.py`, its retry policy `gwretry.py`, the launcher `start_proxy.sh`, offline tests |
| `container/` | the run image and the judge image: `Dockerfile` (targets `runner` and `judge`), `apptainer.def`, `apptainer-judge.def`, the shared `build_steps.sh`, `requirements-paper.txt`, the dsh pin in `dsh/`, `ENV.md.template`, and `VERSIONS.md` (the evidence for every version) |
| `run/` | one episode: `run_task.sh` (stages the task, starts the proxy, runs the container), `run_in_container.sh` (the container's entry point), `compose.yaml` (optional), `egress_proxy.py` (an allow-list forward proxy) |
| `hpc/` | batch templates: `ra_episode.pbs`, `ra_episode.slurm`, and `ra_pack.sh` for several episodes on one node |
| `accounting/` | `settle_sessions.py`, the paper's cost method, and the paper's rate card in `rates/` |
| `judge/` | `judge.sh`, `JUDGE_PROMPT.md` (an equivalent of the paper's judge prompt, **not the original**), `parse_score.py`, `stage_run.py` |

## How the pieces fit (the paper's route)

```
 host or node                                     run container (own PID namespace, fresh home, no host home)
 start_proxy.sh -> ra_proxy.py  <-- 127.0.0.1:P --  ra run -> orchestrator -> dsh 0.1.5-rc.1 (Node 24)
   key read from a file, held only here              RA_DSH_SHIM=1, DSH_SHIM_PORT=P, dummy key
   model id forced, optional provider pin,
   message ids, retries, usage log
       |
       v
 OpenAI-compatible endpoint (the paper: OpenRouter, DeepSeek V4.1 Flash, checkpoint 0910)
```

## Quick start

The quick start is in the repository [README](../README.md#quick-start), with the run image built as

```
docker build -f infra/container/Dockerfile --target runner -t residual-agents:1.0.0 .
```

`run_task.sh --help` lists every option of a run; `--dry-run` stages the folder and prints the commands without
starting anything. The steps after a run:

- Build the judge image, then judge the finished run with the task's `JUDGE.md`:

  ```
  docker build -f infra/container/Dockerfile --target judge -t residual-agents-judge:1.0.0 .
  infra/judge/judge.sh --run runs/paper_2/myrun_p2 --judge-md path/to/JUDGE.md --paper path/to/paper.pdf \
    --paper-num 2 --out verdicts --key-file ~/.config/my_anthropic_key
  ```

- Settle the cost from the dsh session logs (the paper's basis), with the paper's rate card:

  ```
  infra/accounting/settle_sessions.py --by-role runs/paper_2/myrun_p2
  ```

  For a published trajectory, which holds no session logs, add `--ledger-fallback` (the orchestrator's in-run
  figures).

## Safety

- The agents run with full shell permissions (`DSH_PERMISSION_MODE=danger-full-access`). Run them only in the
  container.
- Proxy mode keeps the key in the proxy process; the agents see a dummy key. In direct mode (`--direct`) dsh gets the
  key itself, and so can every tool the agents run.
- `run_task.sh` with Docker uses the host's network, so that dsh reaches the proxy on the host's `127.0.0.1`; the
  agents can then also reach the internet and services that listen on the host's loopback. `compose.yaml` keeps the
  runner in the proxy's network namespace instead, with an optional filter that lets pip reach PyPI only.
- The judge reads agent-written files, which may try to steer it. `judge.sh` with Docker gives the judge container no
  network route except the model API host.

## What differs from the paper's runs

- Isolation is stricter: every run has a fresh, empty home folder and its own PID namespace. In the paper's runs the
  account's home was visible, so packages that earlier runs had installed into the shared user-site folder were
  importable (see `container/VERSIONS.md`).
- `ENV.md` says "a container" and states this image's facts (`container/VERSIONS.md` explains how they differ from
  the copy in the paper's runs).
- The judge prompt is an equivalent written for this release; the verdicts also depend on the judge model and the
  Claude Code version.
- dsh's sub-packages are resolved at build time and may differ from the tree of the paper's runs
  (`container/dsh/README.md`); the exact Node 24 release was not recorded, and the Python versions follow the
  environment description of the runs, so minor versions may differ (`container/VERSIONS.md`).
- The cluster limited internet access (pip worked; GitHub and web search did not). The release only approximates this.

## Tests

`bash infra/run_tests.sh` runs every infra test offline (nothing leaves 127.0.0.1): the proxy against a mock endpoint
and its fault injection, the launchers with a fake `docker`, the accounting on a synthetic session tree, the judge
tools with a fake `claude`, and the shell syntax checks (plus shellcheck when it is installed).
