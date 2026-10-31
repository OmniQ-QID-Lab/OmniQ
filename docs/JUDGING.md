# Judging

RA never judges itself. After a run ends, a separate judge agent compares the run with the task's evaluation criteria
and writes the verdict. This page explains the criterion the paper used, how the judge ran, where the verdicts of the
published runs come from, and how to judge a run of your own.

## The strict criterion

Each OmniQ task has two documents. The task specification, `CLAUDE.md`, guides the AI scientist: the target figure,
general guidelines and paper-specific instructions, with no expected values. The evaluation criteria, `JUDGE.md`, are
reserved for the judge: the figure's structure, quantitative checkpoints with reference values and tolerances, and the
required parameters and procedures. An LLM agent drafted both documents and a domain expert revised them.

`JUDGE.md` itself carries the judge's role, rules, audit procedure and output format. Its rules ("strict mode"):

- **Two severity tiers only: CRITICAL and NON-CRITICAL.** Score 1 (pass) means zero critical issues; one or more
  critical issues mean score 0. NON-CRITICAL is reserved for purely cosmetic points.
- **Every quantitative criterion is critical.** A value outside a stated range, tolerance, ordering, sign, slope or
  scaling fails the task. The tolerance is already in the stated range and is not widened.
- **Method and data integrity are critical.** A wrong, substituted or simplified algorithm; fabricated, downloaded or
  hard-coded data; a missing core deliverable; a missing or altered pipeline step.
- **Audit the mechanism, not only the endpoint.** The reproduction must compute the quantity the figure exists to
  show, across the whole swept range.
- **Documented is not excused.** A report that admits a violation still fails.
- **Only reductions the task explicitly permits are exempt.**

The judge reads the run's figure, report, code, logs and saved data, and the source paper. It does not re-run code.
It writes `judge_result.md` in a fixed format:

```text
# Judge Result — Strict Mode
## Score
[0 or 1]
## Figure Analysis
[detailed comparison]
## Issues Found
- [CRITICAL/NON-CRITICAL] ...
## Reason
[2-5 sentences]
```

A task counts as a success only with score 1, which requires every target figure of the task to pass. This is the
paper's strict success. No published run has a partial score. `infra/judge/parse_score.py` reads the score with the
regular expression `^## Score\s*\n\s*([0-9.]+)` and counts only 1 as a success.

## How the paper's judge ran

- Judge: Claude Code with Claude Opus 4.8 (`claude-opus-4-8`), Claude Code CLI 2.1.281, run headless.
- When: after each run ended. The judge worked on a copy of the finished run folder (which already held the paper),
  with `JUDGE.md` and a generic `ENV.md` added. The copy left out RA's per-session DSH logs (`.dsh_ra3/`), Python
  environments and package trees, and files over 200 MB.
- Where: its own container without the home folder, with 4 CPU cores, 16 GB of memory and a 1-hour limit.
- Verdicts depend on the judge model and the CLI version, and these may not stay available. Record both with every
  verdict you make.

## Criteria isolation

RA's critic never sees the criteria; it works from the paper, the task specification and the run's own records.
`JUDGE.md` was never staged into a run folder: in all 59 published runs it was added only after the run had ended.
Two further facts from the published runs:

- In two runs (#27 and #29) agents searched for a file named `JUDGE.md` (14 commands in all). No such file existed in
  the run folder at that time.
- One run (#17) ran on the computing system that was also used for judging, where the folder holding the criteria
  files was reachable through the mounted home folder. The published files of that run show no access to it.

In this release the separation is enforced: `infra/run/run_task.sh` refuses a task folder that contains `JUDGE.md`,
`ra preflight` fails if `/workspace/JUDGE.md` exists, and the judge runs as a separate step afterwards.

## Verdicts of the published runs

Every task folder of the trajectory dataset holds one authoritative pair, `run/JUDGE.md` and `run/judge_result.md`:
the criteria and the verdict the paper counts. Together they give 50 strict successes in 59 tasks (84.7%), the
paper's "approximately 85%".

- For 50 tasks the pair is the one judged after the run.
- For 9 tasks (#9, #14, #26, #28, #34, #39, #40, #51, #52) the criteria were revised in the benchmark's curation audit
  and the run was judged again under the revised criteria; that re-judge is the authoritative pair. Two tasks changed
  their score (#26 and #39, from 0 to 1).
- Earlier verdicts are kept in the task's `judge_history/` folder (10 entries in 9 tasks), each with the
  criteria it was judged under when available. The manifest field `verdict_source` names the source of every
  authoritative verdict.
- **#38.** RA's run of #38 was judged under the criteria in force before the curation audit revised them, and the
  dataset holds those criteria. The re-judge under the revised criteria covered other systems' runs of this task, not
  RA's.

## Judging a run of your own

`infra/judge/judge.sh` reproduces the paper's judging step. You give it the finished run folder, the task's
`JUDGE.md` and your copy of the paper (and the supplementary material, if the task has one); `judge.sh --help` lists
its options. It:

1. copies the run into a fresh folder with `infra/judge/stage_run.py`, leaving out what the paper's judge did not see:
   earlier verdicts and criteria, `.dsh_ra3/`, Python environments and package trees, and files over 200 MB (each
   left-out file is listed);
2. adds the task's `JUDGE.md` and the paper;
3. runs Claude Code headless with the prompt in `infra/judge/JUDGE_PROMPT.md`, in a container whose only network
   access is the Anthropic API, with 4 CPU cores, 16 GB of memory and a 1-hour limit;
4. records the judge model and the CLI version with the verdict. `python infra/judge/parse_score.py judge_result.md`
   prints the score.

**The judge prompt is an equivalent, not the original.** The prompt the paper's judge received is not part of this
release. `JUDGE_PROMPT.md` was written for the release from what the criteria files and verdicts show; it tells the
judge to read `ENV.md` and then `JUDGE.md`, which holds the role, the rules, the procedure and the output format.
Verdicts made with it can differ from the paper's, as verdicts can also differ between judge models and Claude Code
versions.

You need the Claude Code CLI (the paper used 2.1.281) and an Anthropic API key or subscription token. Take `JUDGE.md`
from the task's folder in the trajectory dataset (`run/JUDGE.md`) and keep it out of the task folder you give to
`run_task.sh`.

## What the dataset alone cannot re-judge

A published run is not the folder the judge saw, so judging it again from the dataset would not reproduce its
verdict:

- the source papers are not included (judging needs your own copy);
- numerical arrays and other binary files were left out (each is listed with size and SHA-256), and text files over
  2 MB too, except a few final deliverables; the judge read some of these files;
- third-party packages, code and data that the agents downloaded were removed;
- page images and text extracted from the source papers were removed.

To check a verdict independently, run RA on the task yourself and judge that run.
