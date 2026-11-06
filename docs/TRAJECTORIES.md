# Reading the trajectories

The trajectory dataset (Hugging Face, coming soon; licence CC BY 4.0) holds the 59 Residual Agents runs behind the paper's
results: one run per OmniQ task, made with DeepSeek V4.1 Flash through DSH. Each run is RA's complete working folder
plus a few generated files that make it easier to read. This page explains how the dataset maps to the code in this
repository and how to follow a run. The terms are in [GLOSSARY.md](GLOSSARY.md).

`JUDGE.md` holds the benchmark's grading criteria. Please do not train models on the dataset; its card carries a
canary string for that purpose.

## 1. Layout

```text
<dataset>/
├── README.md                    dataset card
├── LICENSE                      CC BY 4.0
├── manifest.jsonl, manifest.csv one row per task
├── excluded_files.csv.gz        every left-out file of all 59 runs, with size and SHA-256
├── third_party.csv              removed third-party packages, code, data and publications
├── ra_traj.py                   loader (Python standard library only)
└── tasks/
    └── paper-40_p42/            paper #40, registry id 42
        ├── README.md            a generated guide to this run
        ├── run_meta.json        this task's manifest row plus run details
        ├── calls.jsonl          one record per agent session
        ├── task_book_items.json the judged lines F1..Fn of the task specification
        ├── files.csv            every original file and what happened to it
        ├── redactions.csv       every changed file, with hashes before and after
        ├── data.zip             data files of the run (paths relative to run/)
        ├── judge_history/       only in 9 tasks: earlier verdicts
        └── run/                 the run folder (/workspace during the run)
            ├── CLAUDE.md  ENV.md  READINGS.md  LIBRARIES.md  skills/ (9 tasks)
            ├── JUDGE.md  judge_result.md   the criteria and the verdict the paper counts
            ├── report.md  reproduced_figure*  token_usage.json
            ├── _pece/                       RA's record: ledger, plans, state, logs, memories, scouts
            └── <unit>/ ...                  one folder per sub-plan
```

## 2. Names

- A task folder is `paper-NN_pRRR`: the paper number with two digits, and the registry id that the run itself uses
  (in folder names, `PAPER_NUM` and the ledger's `paper` field). The manifest maps one to the other.
- Inside `run/` and `data.zip` every path is the run's own path. A path `/workspace/u7/heis_shadow.json` in any file
  means `run/u7/heis_shadow.json`, or the member `u7/heis_shadow.json` of `data.zip`.
- Nothing else was renamed. The internal identifiers `_pece`, `.dsh_ra3`, the run ids (`ra3f61_p45`, ...), `epic_shim` and the `_gwretryN` tags appear as the code wrote them.

## 3. Loose files and `data.zip`

To keep the dataset browsable, frequent data files are packed into one `data.zip` per task:

- always loose: the top-level files of the run folder, the files directly in `_pece/`, and everything under
  `_pece/logs/` and `_pece/notes/`;
- into `data.zip`: in a folder with 100 or more JSON or JSONL files, all of those; in a folder with 100 or more log or
  output files (`.log`, `.out`, `.err`, `.txt`, `.stdout`), all of those; and every data file (`.json`, `.jsonl`,
  `.csv`, `.tsv`, `.dat`, `.points`, `.xyz`) except `handoff*.json`, `mech.json`, `validation.json`, `profile.json`,
  `candidate.json`, `deliverable_manifest.json`, `plotted_data*.json` and `plot_data*.json`, which stay loose so that
  a reader can follow the reasoning; also 8 files whose names some systems cannot handle;
- everything else is loose.

`files.csv` gives the status of every original file: `loose`, `data.zip`, `history` (a verdict moved to
`judge_history/`) or `excluded`, with size, SHA-256 and the original modification time in UTC. The modification
times matter: a Hugging Face download does not keep them, and they tie files to sessions (section 9).

## 4. Opening a run

The loader at the root of the dataset needs only the Python standard library:

```python
from ra_traj import Run
run = Run("tasks/paper-40_p42")
run.units()                                  # [(unit, status, last grade, settle reason)]
run.story("u6")                              # ledger events and agent sessions of one unit, in ledger order
run.text("/workspace/u7/heis_shadow.json")   # a cited file, from run/ or from data.zip
run.files()                                  # every original file: published, in data.zip, moved or left out
```

A file that was left out raises `FileNotFoundError` with a pointer to `files.csv`.

From this repository:

| command | what it shows |
|---|---|
| `python examples/inspect_run.py tasks/paper-40_p42` | the plan, unit grades, a timeline with the log of every session, and the cost by role |
| `ra trace tasks/paper-40_p42/run` | re-renders `_pece/trace.json`, `timeline.html` and `timeline.txt` from the ledger (useful for #6 and #18, whose timelines are missing) |
| `ra cost tasks/paper-40_p42/run [--rates U,C,O]` | re-prices every session from the ledger |

## 5. Where each file comes from

| in the run folder | written by (function in the code) |
|---|---|
| `_pece/ledger.jsonl` | `ledger()`; every event of section 6 |
| `_pece/plan_full.json`, `_pece/requirements.json` | `_accept_plan`, when the planner's plan is accepted |
| `_pece/plan.json` | the planner, then `checkpoint()` (units with live status fields) |
| `_pece/state.json` | `checkpoint()` (the final state) |
| `_pece/clock.json` | `write_clock`, every minute until the report phase |
| `READINGS.md`, `_pece/readings.json` | `write_readings`, `apply_reading_change` |
| `_pece/notes/*.md`, `LIBRARIES.md` | `load_notes` (copies of the memory files) |
| `_pece/logs/NN_<tag>.log` | `run_call` (the session's printed output) |
| `_pece/dsh_argv.mjs` | the prompt-file launcher (`_dsh_argv_cmd`) |
| `_pece/scouts/<uid>.sN/` | probe agents and panel critics (`scout_round`, `scout_select`) |
| `_pece/digest.md` | `build_digest` |
| `_pece/trace.json`, `timeline.*` | `trace.py` |
| `<uid>/spec.md`, `handoff_template.json` | `write_spec` |
| `<uid>/handoff.json`, code, data, `validation.json`, `profile.json` | the executor sessions |
| `<uid>/mech.json` | `mech_check` |
| `<uid>/critic_report.md`, `residual.md`, `probe_*` | critic sessions, or `machine_critic` and `ensure_residual` |
| `<uid>/audit_rN/` | `archive_audit` |
| `<uid>.rN/`, `<uid>/from_scout*/` | `archive_dir`, `scout_seed` |
| `report.md`, `reproduced_figure*`, `render/` | the report agent; `root_copy` copies the final files to the top |
| `token_usage.json` | `epilogue` |
| `CLAUDE.md`, `skills/`, `ENV.md` | the benchmark, staged before the run (`ENV.md`: see section 11) |
| `JUDGE.md`, `judge_result.md` | the judge, after the run ([JUDGING.md](JUDGING.md)) |

`task_book_items.json` numbers the judged lines of `CLAUDE.md` exactly as the orchestrator's `task_book_items` did;
its item count equals the ledger's `task_book` count in all 59 runs. [ARCHITECTURE.md](ARCHITECTURE.md) explains how
the parts work together.

## 6. The ledger

`_pece/ledger.jsonl` is the spine of a run: one JSON object per line, in time order, from `episode_start` to
`episode_end`. Every event has `kind`, `ts` (Unix seconds), `paper` (the registry id) and `episode` (the run id). It
parses completely in all 59 runs.

| kind | when | main fields |
|---|---|---|
| `episode_start` | first line | `model`, `dsh_version`, `wall`, `ncpus`, `mem_gb`, `gpus`, `vision_ok`, slice and checkpoint lengths |
| `task_book`, `notes_loaded` | start | number of task-book items; memory folder and the hash of each memory file |
| `plan_invalid`, `plan` | each planner attempt | the validator's errors; on acceptance the units, requirement rows, readings and critical path |
| `launch` | a unit starts | `unit`, `depends_on`, `provisional` (started behind the file gate), `cores`, `mem_gb`, `start` (`exec` or `reaudit`) |
| `bank_selected` | before a session | which residual-memory entries the next session received, and why (`where`) |
| `call` | **a session ended** | `tag`, `dur`, `rc`, `tokens`, `usage` (`input`, `cached`, `output`, `reasoning`), `cost_usd`, `retried_attempts` |
| `exec` | after an executor session | `outcome`: `HANDOFF`, `CUT` (slice ended, work continues), `INTERRUPTED` or `INFRA` |
| `mech` | after a handoff | the mechanical check: deliverables found, Python files compiled, handoff valid, failures |
| `critic` | after a review | `grade`, `gates`, `by` (`llm` full audit, `delta` re-audit, `checkpoint`, `mech` machine-written), `residual_ids` |
| `fix_plan` | the decision after a review | `action`: `done`, `fix`, `cpath` (probe agents), `fix_c`, `gap` or `failed` |
| `settle` | a unit closes | `status`, `reason`, `grade` |
| `wait_start`, `wait_end`, `checkpoint` | a registered background job | process id, done marker, expected minutes, how the wait ended |
| `slice`, `slice_extended` | an executor reached its slice | extension count and reason |
| `scout_start`, `scout_done`, `scout_select`, `scout_winner` | a new-approach round | per probe agent `valid` and `supports`; the panel's `ranks`, Borda `points`, votes, `winner` and `adopt` |
| `archive`, `reusable_copied`, `code_copied`, `restart` | a fresh executor | the archived folder and the files carried over |
| `reading_change`, `reading_change_rejected` | a shared reading changes | `id`, `version`, `by`, `source`, `evidence`, or why it was refused |
| `input_changed`, `reopen` | a finished unit's inputs changed | the change notice; a reopened unit starts with a delta re-audit |
| `report_start`, `report`, `report_grade`, `root_copy` | report phase | report written or not; the review's grade and action |
| `override`, `flush`, `interrupt` | end of the wall (9 runs) | executors told to wrap up; open reviews stopped |
| `infra_call_dead`, `infra_retry`, `infra_probe`, `infra_unit_hold` | backend failures (4 runs) | dead sessions, holds, probes of the endpoint |
| `episode_end` | last line | `calls`, `cost_usd`, `grades`, `deliverables`, `override`, `infra_dead` |

The runs contain 63 different event kinds. `_pece/state.json` holds the final state of every unit; `_pece/plan.json`
and `_pece/clock.json` are working copies (section 11).

## 7. Agent sessions

Every role call is one fresh DSH session on DeepSeek V4.1 Flash with reasoning effort `max`. The session tag gives the
role:

| tag prefix | role (paper term) | sessions in the 59 runs |
|---|---|---:|
| `exec`, `restart`, `cont`, `resume`, `fix` | executor: first launch, fresh executor after a new approach, continuation after a slice or a job, local repair | 1,293 |
| `critic` | critic, full audit | 717 |
| `criticcp` | critic, checkpoint of a running job | 312 |
| `criticd` | critic, delta re-audit | 279 |
| `scout1..3` | probe agents | 261 |
| `criticsel1..3` | critic panel | 260 |
| `plan`, `replan` | planner | 112 |
| `report` | report agent | 59 |
| `criticfmt` | critic, format retry | 26 |

A tag that ends in `_wrapup` is a short pens-down session after a timeout; `_gwretryN` is a retry after a dead
session (11 in all).

**What a session leaves in `run/`.** `_pece/logs/NN_<tag>.log` holds the session's printed output, which is the text
of its final answer (median 2.2 KB), plus markers the orchestrator appended: `<<ORCH TIMEOUT>>` (the orchestrator
ended the session at its time limit; routine for executors, whose jobs keep running), `<<ORCH INTERRUPT ...>>` (the
end-of-run override), or a DSH stream error. `NN` is the launch order; sort it as a number.

**Matching a log to its ledger record.** The k-th log of a tag, in `NN` order, belongs to the k-th `call` event with
that tag (sessions with the same tag never overlap). `calls.jsonl` applies this rule for you: one record per session
with `call`, `tag`, `ledger_line`, `role`, `mode`, `unit`, `start_utc`, `end_utc`, `dur_s`, `rc`, `end_marker`,
`retry_of_dead_call`, `retried_attempts`, `tokens`, `usage`, `cost_usd`, `outcome`, `scout`, `verdict`, `checkpoint`,
`log` and `final_text`. Use it instead of `_pece/digest.md`, whose rows are numbered in completion order.

**The prompt.** Every prompt starts with the TIME LEDGER and the TASK BOOK (the F-lines), then the role prompt with
the unit's files and the selected residual-memory entries, then, for a continuation, the tail of the previous
session's transcript. The prompt texts are the constants in `fragments/09_prompts.py`.

**Model session logs are not included** (the DSH session logs in `.dsh_ra3/` and the assembled prompts). A run holds
each session's final message, every file the agents wrote and the full event ledger, but not the reasoning and tool
calls in between.

## 8. Tokens and cost

- `usage.input` is the uncached input, `usage.cached` the cache reads, and `usage.output` the visible output plus
  reasoning; `usage.reasoning` is the reasoning part of the output, so do not add it again.
- `tokens` = input + cached + output.
- `cost_usd` = (input x 0.15 + cached x 0.003 + output x 0.60) / 10^6, in US$ at the paper's rate card.
- `token_usage.json` is RA's own total; it equals the sum of the ledger's `call` costs in all 59 runs.
- RA's own count misses DSH sub-agent sessions. One session in each of five runs started a sub-agent (#31, #45, #50,
  #51, #52); its tokens appear nowhere in the run folder. The paper's cost re-reads every DSH session file, sub-agent
  sessions included. The manifest gives both: `cost_usd` (the paper's basis) and `cost_usd_ledger` (RA's own record).
- Over the 59 runs, one per task: 25.7 billion tokens, a mean cost of US$5.56 per task on the paper's basis
  (US$5.55 in RA's own record), and a mean wall time of 14.23 hours. Each run has 10 to 195 agent sessions
  (median 45), depending on how many reviews and repairs its sub-plans needed.

## 9. Following one sub-plan

For any unit: read its entry in `_pece/plan_full.json` and its `spec.md`; filter the ledger by `unit`; open each
session's log by the rule of section 7; read the reviews (`critic_report.md` is the latest, `audit_rN/` the earlier
ones). With the loader, the middle steps are `run.story(uid)`. The examples below are from `paper-40_p42` (paper #40,
a development task: 136 sessions, every repair path, a pass) and `paper-53_p89` (paper #53, a clean run). Times are
hours since the start of the run.

**A local repair: paper #40, unit u6 (the neural-network baseline engine).**

| step | ledger lines | session log | files |
|---|---|---|---|
| plan: "engine only", 4 cores, 24 GB, no dependencies | 4 to 9 | `01..03_plan.log` (two rejected plans, then accepted) | `plan_full.json`, `u6/spec.md` |
| launch at +0.42 h with residual entries R1, R9, R3, R4 | 12, 13 | | |
| executor: 94 minutes, a valid handoff, 3 of 3 deliverables | 21 to 23 | `05_exec_u6.log` | `u6/handoff.json`, `validation.json`, `profile.json` |
| **review: B**, G2 hit, estimate 20 minutes, R3 and R10 cited | 24, 25 | `07_critic_u6.log` | `u6/audit_r1/critic_report.md` |
| decision `fix`; the repair session receives R3 and R10 | 26, 27 | | |
| **repair**: 40 minutes, a valid handoff | 30 to 32 | `08_fix_u6.log` | |
| **accept**: delta re-audit (18 files changed), grade A, done at +2.97 h | 33 to 37 | `10_criticd_u6.log` | `u6/audit_r2/critic_report.md` |
| reopened at +7.31 h because reading K7 changed; delta re-audit A (`reaudit_A`) | 123 to 141 | `32_criticd_u6.log` | `u6/critic_report.md` |

**A new approach: paper #40, unit u7 (2D classical shadow).**

1. **Fail.** `24_critic_u7.log` reviewed the work of `22_exec_u7.log`: grade C, G3 hit, decision `cpath` (ledger
   lines 105 to 107). That report is now in the archived attempt, `u7.r1/critic_report.md`.
2. **Probe agents.** Three ran in parallel (`25_scout1`, `26_scout3`, `27_scout2_u7.log`); each wrote a hypothesis, a
   small test and a plan to `_pece/scouts/u7.s1/sK/candidate.json`.
3. **Critic panel.** `28..30_criticsel*_u7.log`; their votes are in `select1..3.md`. The binding result is the
   ledger's `scout_select` (line 120): Borda points S1 5, S3 4, S2 0, so S1 wins and S3 is adopted.
4. **Shared reading changed.** S1's reading change became `K7 v2` (line 121, `source: scout`), which reopened the
   finished units u2 and u6. S3's reading change was refused, because K7 had already changed once in this run (line
   128).
5. **Fresh executor.** u7 was archived to `u7.r1/`; reusable files and code were copied back, the winner's files went
   to `u7/from_scout/` and the adopted ones to `u7/from_scout_3/` (lines 131 to 135); then `33_restart_u7.log` ran.
6. **Accept.** `37_critic_u7.log` gave an A; u7 settled done at +8.28 h.
7. **Reopen.** At +18.77 h the files of u3 changed, which reopened u7: a delta re-audit gave a B; the repair
   (`96_fix_u7.log`, 94 minutes, one slice extension) failed the mechanical check because its `waiting` field was a
   list, so the orchestrator wrote a mechanical repair order (`u7/audit_r3/critic_report.md`); `108_fix_u7.log`
   repaired it; `112_criticd_u7.log` gave an A, and u7 settled `reaudit_A` at +20.66 h.

**The simplest path: paper #53.** 3.97 hours, 13 sessions, US$1.36, on 8 cores and 64 GB. One plan with 61
requirement rows and 13 readings; `exec_u1` built the shared engine and `critic_u1` passed it; four panel units (u2 to
u5) ran in parallel and each passed its first review; `report_render` wrote the report and `critic_render` passed it.
Each unit's story is `launch`, `bank_selected`, `call`, `exec`, `mech`, `call`, `critic` A, `fix_plan` done, `settle`.

## 10. From the figure back to code, data and verdict

1. **Figure to data.** `render/handoff.json` lists the deliverables, and section 1 of `report.md` usually says which
   file each plotted series comes from. Some runs record hashes: in #40 `render/handoff.json` has a
   `panel_provenance` table (series, unit, path, SHA-256), and all its hashes match the files of the original run
   folder.
2. **Data file to unit.** The path prefix names the unit (`u7/heis_shadow.json`), and that unit's `handoff.json` lists
   the file.
3. **Data file to session.** Compare the file's modification time (`files.csv`, `mtime_utc`) with the unit's session
   windows (`start_utc` and `end_utc` in `calls.jsonl`). Files written by a background job belong to the session that
   registered it (`wait_start`).
4. **Session to review.** The unit's next `critic` event gives the grade; its `critic_report.md` or `audit_rN/` gives
   the argument.
5. **Verdict.** `judge_result.md` cites the same files. The judge read files only; it did not re-run code.

Of the 4,974 distinct `/workspace/...` paths cited in the 59 reports, 92.7% resolve to published files, 4.3% to
arrays that were left out but are listed with their SHA-256, and 3.0% to nothing (mostly notation, the paper PDF, or
scratch files deleted during the run).

## 11. Things that can mislead

- **`ENV.md` is the judge's copy.** The judge's pipeline copied a generic environment guide into the run folder; the
  copy the agents read named the run's cores and memory. The run's real resources are in `episode_start` and in the
  manifest.
- **Unit grades are not the benchmark verdict.** A, B, C, D and W are RA's internal reviews; the verdict is only in
  `judge_result.md`.
- **"done" is not "passed".** 25 work units in 13 runs ended `done` with a last grade of B or C because time ran out;
  9 report units ended `failed` but still delivered the report (all 59 runs have `report.md` and a figure).
- **`_pece/clock.json` is a snapshot.** It stopped before the report phase; in 49 runs some unit status in it differs
  from `state.json`, which holds the final state.
- **`_pece/digest.md` numbers rows by completion**, while log files are numbered by launch.
- **Machine-written reviews.** 115 critic reports have "MECHANICAL FINDINGS" as their third line: repair orders written by the orchestrator
  after a failed mechanical check (`by: mech` in the ledger), not model reviews.
- **Archived attempts and old reviews** (`<uid>.rN/`, `audit_rN/`, `from_scout*/`, agents' own backups such as
  `*.prev*`) hold old or partial results. A unit's final state is its latest `handoff.json` and `critic_report.md`.
- **Stopped sessions look cut off.** 267 logs end with `<<ORCH TIMEOUT>>`; for executors this is routine.
- **Dead sessions and retries.** 12 sessions used 0 tokens and hold only a stream error, and 11 sessions with a
  `_gwretryN` tag are retries of dead sessions. Do not count them as agent failures.
- **Web search never worked.** The search endpoint was unreachable from the compute cluster; failed `web_search`
  calls are not agent mistakes.
- **Non-standard JSON.** 1,248 agent-written JSON files contain `NaN` or `Infinity`, which Python accepts but strict
  parsers do not. No ledger, plan, state, clock or handoff file does.
- **Times.** Generated files use UTC. The ledger uses Unix seconds; `trace.json` and the timelines use time since the
  start; `clock.json` and many agent-written times use UTC+8.
- **Hashes inside a run refer to the files before redaction** (section 12).
- **Launch notes.** Some sessions read a launch-notes file (`backend_fingerprint.txt`) that is not published, nor is
  the local proxy's log (`dsh_shim.log`): they held infrastructure details only. The launch notes gave a wrong DSH
  version; the ledger's is right.
- **The orchestrator was readable.** Inside the container the agents could read the orchestrator source; a few ran its handoff check on their own handoff.
- **Shared machine.** In most runs the agents shared the node's process table with other runs; in #8 a critic looked at
  a process of another run.
- **Shared home folder.** The container's home was the cluster account's home: packages installed with
  `pip install --user`, font caches and the Fashion-MNIST download cache that #21 used came from there.
- **Timeline quirks.** `afford=?` on decision lines, "override start/end" lines that only mark the start and end of
  the run, and "RESTART round N", which counts review rounds. #6 and #18 have no timeline; `ra trace` makes one.
- **Task specifications as given.** The task specifications of #15 and #18 cite the wrong publication year; the task
  files are unchanged because they are what the agents read. The manifest has the correct year. Some task
  specifications also name the file name under which the paper was downloaded.

## 12. What is not included

- **Left out**, each listed with size and SHA-256 in `files.csv` and `excluded_files.csv.gz`: copies of the source
  papers (their text, page renders and crops); third-party packages, code, data and publications that the agents
  downloaded (also named in `third_party.csv`, with source and licence where recorded); numerical arrays and other
  binary data; text files over 2 MB, except a few final deliverables; caches (other than Python bytecode) and process
  files; empty marker files in unit folders. Symbolic links are not published; `files.csv` records their targets.
- **Dropped without a listing:** each run's launch notes and proxy log, a few scheduler markers, Python bytecode
  caches, and the paper PDFs themselves.
- **Redacted:** account names and home paths (now `user` and `/home/user/...`), compute-node names (`node-NN`),
  scheduler job ids (`job-NN`) and one folder name. Line counts and JSON validity are kept. `redactions.csv` lists
  every changed file with its SHA-256 before and after. Hashes recorded inside a run refer to the files before
  redaction; see `redactions.csv`.
- **Consequence:** a run cannot be re-judged exactly from the dataset alone ([JUDGING.md](JUDGING.md)).

## 13. The manifest

`manifest.jsonl` has one row per task, sorted by paper number; `manifest.csv` has the same rows. The main fields,
in groups:

| group | fields |
|---|---|
| identity | `task`, `paper_number`, `registry_id`, `run_id`, `run_group`, `split` (`development` or `evaluation`) |
| paper | `title`, `title_in_task_spec`, `authors`, `venue`, `year`, `volume`, `article`, `doi`, `arxiv_id`, `target_figure`, `research_area`, `subcategory`, `code_availability`, `curation_level`, `curation_handling` |
| agent | `agent`, `executor`, `model`, `reasoning_effort`, `vision`, `model_checkpoint`, `sessions_after_checkpoint_update`, `route` |
| resources and time | `cpus`, `memory_gb`, `gpus`, `gpu_model`, `time_limit_h`, `ra_budget_s`, `run_start_utc`, `run_end_utc`, `ra_wall_s`, `ra_wall_h`, `wall_h_paper` |
| usage | `agent_sessions`, `subagent_sessions`, token counts, `tokens_total_settled`, `cost_usd`, `cost_usd_ledger`, `cost_basis`, `rates_usd_per_mtok` |
| verdict | `judge_score`, `strict_success`, `verdict_source`, `judged_utc`, `criteria_md5`, `judge_file`, `criteria_file`, `judge_history` |
| package | files published, zipped, left out and redacted |
| notes | short notes per task, and the canary |

The manifest reproduces the paper's numbers: 50 strict successes in 59 tasks, a mean cost of US$5.56 per task and a
mean wall time of 14.23 hours.
