# Running an OmniQ task

This guide runs RA on one task of the benchmark and judges the result, as in the paper. It uses paper #53 as the
example: Liu et al., "Detecting entanglement in quantum many-body systems via permutation moments", Phys. Rev. Lett.
129, 260501 (2022), Figure 3. Its published run is the simplest in the dataset (8 cores, 64 GB, about 4 hours,
13 agent sessions, a pass), so it is a good first task. Read `docs/RUNNING.md` first for the image and the proxy.

Expect up to 24 hours per task. Over the 59 published runs the mean wall time was 14.2 hours (median 13.1) and the
mean cost US$5.56 per task (median US$4.35) at the paper's rate card.

## 1. Take the task from the dataset

Each task folder of the trajectory dataset (on Hugging Face, coming soon) holds the task specification and the criteria. For
#53 the folder is `tasks/paper-53_p89/`; 89 is the task's registry id, which the run uses in its file names.

- `run/CLAUDE.md`: the task specification. Copy it into a new, empty task folder, for example `omniq/paper-53/`.
- `run/skills/`: for 9 tasks, best-practice notes that the specification points to. Copy the folder too when it
  exists.
- `run/JUDGE.md`: the criteria. Keep it **outside** the task folder, for example in `omniq/criteria/paper-53/`.
  `run_task.sh` refuses a task folder that contains `JUDGE.md`, because the agents must never see the criteria.

## 2. Get the paper

The dataset does not contain the source papers. Get your own copy of the version that the "Paper" section of
`CLAUDE.md` names (the manifest also gives the DOI or arXiv id). The specification refers to it as
`paper_<registry id>.pdf`, here `paper_89.pdf`; `run_task.sh` stages your file under that name. Four tasks also use
supplementary material, staged as `paper_<registry id>_SM.pdf`; their specification says so.

## 3. Look up the resources

`residual_agents/profiles/task_resources.tsv` lists the cores, memory and GPU of every published run (the manifest
has them too, as `cpus`, `memory_gb` and `gpus`). For #53: 8 cores, 64 GB, no GPU. Give a task at least these;
the plan is sized to what you give it.

## 4. Run it with the paper profile

```bash
infra/run/run_task.sh --task omniq/paper-53 --paper path/to/paper_89.pdf --paper-num 89 \
    --label mine --out runs --ncpus 8 --mem-gb 64 --wall-hours 24 --profile paper \
    --upstream https://openrouter.ai/api/v1 --key-file ~/.config/ra/upstream_key
```

The proxy forces the model id `deepseek/deepseek-v4.1-flash` and gives every message a stable id, as in the paper's
runs (`--model-force` and `--inject-ids` change this).

Add `--gpu` for a task whose row lists a GPU. The run folder is `runs/paper_89/mine_p89/`. Remember that the model
behind the endpoint may not be the checkpoint the paper used (`docs/RUNNING.md`).

## 5. Look at the run

```bash
python examples/inspect_run.py runs/paper_89/mine_p89
```

It prints the plan, the grades of every unit, a timeline with the log file of every agent session, and the cost by
role. `docs/TRAJECTORIES.md` explains how to follow a unit through the ledger, the logs and the reviews.

## 6. Judge it

Give `infra/judge/judge.sh` the finished run folder, the task's `JUDGE.md` (`omniq/criteria/paper-53/JUDGE.md`) and
your copy of the paper; `infra/judge/judge.sh --help` lists the options. It judges a fresh copy of the run with Claude
Code and writes `judge_result.md`; `python infra/judge/parse_score.py judge_result.md` prints the score. A score of 1
is a strict success. See `docs/JUDGING.md`; note that the release's judge prompt is an equivalent of the paper's, not
the original, so verdicts can differ.

## 7. Compare with the published run

The dataset's run of the same task shows what RA did in the paper: `tasks/paper-53_p89/README.md` summarizes it,
`run_meta.json` gives its settings and cost, `calls.jsonl` its sessions, and `run/judge_result.md` its verdict. Agent
runs are not deterministic, so your run can take a different path and get a different verdict.
