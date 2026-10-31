# Toy task: the transverse-field Ising chain

A small task written for this repository to test a setup end to end: the model proxy, the container, the
orchestrator, DSH and the model. It has no source paper and contains no third-party material. It is not an OmniQ task
and was not part of the paper's evaluation.

The task (`CLAUDE.md`): compute the ground-state energy of the transverse-field Ising chain on rings of 8, 10 and 12
spins by exact diagonalization, for 21 field values, and compare it with the exact free-fermion result and the
infinite-chain limit, in a two-panel figure. The largest matrix has 4,096 rows, so a few cores and a few GB of memory
are enough.

`CLAUDE.md` follows the layout of the OmniQ task specifications. Keep one line per item in its judged sections:
the orchestrator numbers every non-empty line of those sections as F1 to Fn and repeats them at the top of every
prompt.

## Run it

This is step 2 of the quick start in the repository [README](../../README.md#quick-start): build the image, then

```bash
infra/run/run_task.sh --task examples/toy_task --paper-num 0 --label toy --out runs \
    --ncpus 4 --mem-gb 16 --wall-hours 6 --profile smoke \
    --upstream https://openrouter.ai/api/v1 --key-file ~/.config/ra/upstream_key
```

- The task has no source paper, so no `--paper` is given; `run_task.sh` notes this and stages no paper file.
- The smoke profile keeps the paper's model, image input, rates and proxy mode, and shortens RA's clocks: a wall of
  21,000 s (5 hours 50 minutes, which fits the 6-hour allocation), checkpoints every 30 minutes, and shorter
  probe-agent rounds. The run ends when every unit is finished or the wall is reached.
- To exercise the probe agents and the critic panel as well, set `RA3_SMOKE_FORCE_C=1` (see
  `residual_agents/profiles/smoke.env`): the run's first A or B review is then handled as a C. Never set it in a
  scored run.

## What you get

The run folder `runs/paper_0/toy_p0/` holds the deliverables (`report.md`, `reproduced_figure.png` and `.pdf`,
`results.json`), RA's record in `_pece/` and the DSH session logs in `.dsh_ra3/`. Then:

```bash
python examples/inspect_run.py runs/paper_0/toy_p0     # plan, unit grades, timeline, cost
ra trace runs/paper_0/toy_p0                           # timelines in _pece/timeline.html and timeline.txt
```

## Check the result

No criteria file ships with the toy task; check the numbers yourself. The ground-state energy per site, E0/N, from
the formulas in `CLAUDE.md` (the exact-diagonalization values must agree with the finite-chain columns to 1e-9 or
better):

| h | N = 8 | N = 10 | N = 12 | infinite chain |
|---|---|---|---|---|
| 0.0 | -1 | -1 | -1 | -1 |
| 0.5 | -1.0636352794 | -1.0635604409 | -1.0635474293 | -1.0635444 |
| 1.0 | -1.2814577239 | -1.2784906443 | -1.2768829293 | -1.2732395 (= -4/pi) |
| 1.5 | -1.6731256541 | -1.6723024914 | -1.6720520571 | -1.6719262 |
| 2.0 | -2.1272705588 | -2.1271208819 | -2.1270948585 | -2.1270888 |

For every h > 0 the values increase with N towards the infinite chain. If you want to try the judge on this task,
write your own `JUDGE.md` in the format described in `docs/JUDGING.md` and keep it outside this folder: the launcher
refuses a task folder that contains `JUDGE.md`.
