# Residual Agents (RA)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

RA is a self-improving AI scientist for computational quantum science, and this repository holds the RA code used in
the paper "Reimagining computational quantum research with AI scientists". Given a published paper and a target
figure, a planner splits the reproduction into sub-plans linked in a dependency graph, executors carry them out in
separate agent sessions, and a critic checks every result without seeing the evaluation criteria. The critic passes a
result, sends it back for local repair, or calls for a new approach. Failures and their verified corrections are
distilled into a residual memory of failure signatures, which later runs retrieve. In the paper, RA runs DeepSeek
V4.1 Flash through DeepSeek's agent CLI (DSH), with 24 hours per task.

**Paper:** [arXiv:2611.02833](https://arxiv.org/abs/2611.02833) · **Project page:** https://omniq-qid-lab.github.io/OmniQ/ ·
**Trajectories:** Hugging Face, coming soon

## Contents

| folder | content |
|---|---|
| `residual_agents/` | the orchestrator (built from `fragments/`), the `ra` command, the timeline renderer, the four memory files, run profiles |
| `fragments/` | the orchestrator source; `tools/build.py` joins it into one module |
| `tests/` | offline unit and end-to-end tests that run the real orchestrator against a fake DSH |
| `infra/` | model proxy, container image, run scripts, batch-scheduler templates, cost settlement, judge runner |
| `examples/` | a toy task, a guide to running a benchmark task, and a script that summarizes a run |
| `docs/` | documentation and the project page |

## Requirements

- Linux with Docker or Apptainer, and Python 3.8 or newer.
- An OpenAI-compatible endpoint that serves DeepSeek V4.1 Flash with image input.
- Per task: 8 to 64 CPU cores, 64 to 432 GB of memory, and for eight tasks one NVIDIA A100 GPU
  (`residual_agents/profiles/task_resources.tsv`); up to 24 hours.

## Quick start

**1. Offline checks** (no key, no network):

```bash
git clone https://github.com/OmniQ-QID-Lab/OmniQ.git && cd OmniQ
python -m pip install -e .
ra verify                 # build check, prompt fingerprints, memory hashes
bash tests/run_all.sh     # the offline test suite (about 9 minutes)
```

**2. Run the toy task** in the container, through the model proxy. The toy task has no source paper, so no `--paper`
is given:

```bash
docker build -f infra/container/Dockerfile --target runner -t residual-agents:1.0.0 .
infra/run/run_task.sh --task examples/toy_task --paper-num 0 --label toy --out runs \
    --ncpus 4 --mem-gb 16 --wall-hours 6 --profile smoke \
    --upstream https://openrouter.ai/api/v1 --key-file ~/.config/ra/upstream_key
python examples/inspect_run.py runs/paper_0/toy_p0
```

`--upstream` is any OpenAI-compatible endpoint; `--key-file` is a file holding your key (mode 600). The build installs
DSH 0.1.5-rc.1 from npm; its sub-packages are resolved on the day of the build
([infra/container/dsh/README.md](infra/container/dsh/README.md)).

**3. Run and judge a benchmark task.** Take the task's `CLAUDE.md` and `JUDGE.md` from the trajectory dataset, add
your own copy of the source paper (`--paper`), run it with `--profile paper`, and judge it with
`infra/judge/judge.sh`. [examples/run_omniq_task.md](examples/run_omniq_task.md) walks through it.

## Configuration

`ra run --profile paper` (the default) applies the paper's settings; the code defaults differ. All variables are in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md). Further documentation: [ARCHITECTURE](docs/ARCHITECTURE.md) ·
[RUNNING](docs/RUNNING.md) · [JUDGING](docs/JUDGING.md) · [TRAJECTORIES](docs/TRAJECTORIES.md) ·
[KNOWN_ISSUES](docs/KNOWN_ISSUES.md) · [GLOSSARY](docs/GLOSSARY.md).

## Safety

Every agent session runs shell commands without asking: agents install packages, start long jobs and stop processes.
Run RA only inside a container, with only the run folder and a store folder mounted. Use proxy mode (the default of
the launchers) so that the API key stays outside the container.

## Citation

```bibtex
@article{du2026reimagining,
  title         = {Reimagining computational quantum research with {AI} scientists},
  author        = {Du, Yuxuan and Zhao, Hanqing and Wang, Xinbiao and Lu, Yiming and Zhang, Kaining and Tao, Dacheng},
  journal       = {arXiv preprint arXiv:2611.02833},
  eprint        = {2611.02833},
  archivePrefix = {arXiv},
  primaryClass  = {quant-ph},
  year          = {2026}
}
```

## License

MIT (`LICENSE`). The trajectory dataset is released under CC BY 4.0. DSH is installed from npm when the container image
is built and is not part of this repository.
