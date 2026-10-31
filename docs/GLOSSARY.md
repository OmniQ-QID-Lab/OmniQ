# Glossary

The code and the 59 published runs use some fixed internal identifiers in folder names, ledger fields and variable
names. Renaming them in the code would break the link between the code and the runs. This page explains them,
together with the ids and terms you meet in the runs.

## Internal identifiers

| name | meaning |
|---|---|
| PECE, RA3, RA3SR | Fixed prefixes of the orchestrator's variables, folders and ledger values; they are internal identifiers of Residual Agents (RA). |
| `_pece/` | The orchestrator's record inside the run folder: `/workspace/_pece/` (ledger, plans, state, logs, memories, timelines). |
| `.dsh_ra3/` | The DSH homes of the agent sessions: `/workspace/.dsh_ra3/calls/NN_<tag>/`, plus the two DSH patch files. |
| `/pece_store` | The episode store: a writable folder outside the run folder. It holds a copy of the ledger and, per run, the digest, a `meta.json` and copies of the critic reports. It is not part of a published run. |
| `PECE_*`, `RA3_*`, `RA3SR_*` | Environment variables of the orchestrator; [CONFIGURATION.md](CONFIGURATION.md) lists them. |
| `RA_*` | The route and launcher settings (`RA_DSH_SHIM`, `RA_DSH_NODE`, `RA_ORCH_NO_RENAME`), the proxy settings `RA_PROXY_*`, and `RA_TEST_*` for the tests. |
| `"harness": "ra3"`, `"version": "v61"`, `"orch": "v61"` | Fields of the ledger's `episode_start` event: internal identifiers written by the code (`ORCH_VERSION`). |
| `"epic_shim": true` | Field of `episode_start`: DSH talked to the local model proxy (proxy mode, `RA_DSH_SHIM=1`). The key name is kept for compatibility with the published ledgers. It is true in all 59 runs. |
| `"dsh_model"` | Field of `episode_start`: the model id DSH sent (the `MODEL` value without its provider prefix in proxy mode). |
| `pece-dsh`, `ra3_v61` | The `agent` and `harness` values in `token_usage.json`. |
| `PECE_CALL_TAG`, `PECE_UNIT` | Process markers. Every agent session and everything it starts carries `PECE_CALL_TAG=<RUN_ID>:<tag>` and `PECE_UNIT=<RUN_ID>:<unit>` in its environment; the orchestrator finds, waits for and stops processes by these markers. |
| `PECE_GRANT_CORES`, `PECE_GRANT_MEM_GB` | The cores and memory granted to a session, set in its environment together with thread caps. |
| `HOST_WORK_DIR` | The host path of the run folder, written only into the store's `meta.json`. |
| `dsh_shim.log` | The local proxy's request log in the paper's runs. It is not published. |

## Runs

| name | meaning |
|---|---|
| run label | The group a run belongs to: `ra3f61` (the 50-task evaluation fleet), `ra3v61` (four development tasks: #8, #9, #14, #40) and `ra3f61b` (five re-runs after the task specification was corrected: #10, #21, #33, #42, #46). |
| run id | `<label>_p<registry id>`, for example `ra3f61_p47`. It is the `RUN_ID` of the run and the `episode` field of every ledger event. |
| registry id | The task id used inside the runs (folder names, `PAPER_NUM`, the ledger's `paper` field), from 39 to 128. The manifest maps it to the paper number. |
| paper number | #1 to #59: the numbering of the task table in the paper's supplementary information. All prose in this repository uses it. |
| task folder | One task of the dataset: `tasks/paper-NN_pRRR/` (paper number with two digits, registry id). For example `paper-40_p42` is paper #40, registry id 42. |
| development tasks | The five tasks used while the experience layer was built: #8, #9, #10, #14 and #40. The other 54 are the evaluation tasks. |

## Paper terms and code terms

| paper | code and runs |
|---|---|
| task specification ("preliminary document"; `Preliminary.md` in Fig. 1a of the paper) | `CLAUDE.md` in the run folder |
| evaluation criteria (`Criteria.md` in Fig. 1a) | `JUDGE.md`, added to the run folder only after the run |
| task book | The judged lines of `CLAUDE.md`, numbered F1 to Fn and repeated as a "TASK BOOK" block at the top of every prompt |
| sub-plan | A unit: `u1`, `u_cka_lo`, ... with its own folder `/workspace/<uid>/`. The final report is the unit `render` |
| dependency graph | The units' `depends_on` and `needs` fields in `_pece/plan_full.json` |
| requirement assigned to one sub-plan | A requirement row Q1 to Qn, tied to one task-book line and one owner unit |
| shared methodological interpretation | A reading K1 to Kn, published in `/workspace/READINGS.md`; `K7 v2` is the second version of reading K7 |
| planner | `plan` and `replan` sessions |
| executor | `exec`, `restart`, `cont`, `resume` and `fix` sessions |
| critic | `critic` (full audit), `criticd` (delta re-audit), `criticcp` (checkpoint of a running job) and `criticfmt` (format retry) sessions, plus the machine critic, a repair order the orchestrator writes when a mechanical check fails |
| probe agents | Scouts: `scout1` to `scout3` sessions |
| critic panel | The selection panel: `criticsel1` to `criticsel3` sessions |
| Pass / Local repair / New approach / retire a sub-plan | Grades A / B / C / D |
| report | The report agent (`report_render` session) and its review (`critic_render`) |
| planner memory | `planner_notes.md` |
| critic memory | `critic_notes.md` |
| residual memory | The residual bank, `residual_bank.md`, entries R1 to R10 |
| (library table) | `libraries.md`, published to the agents as `/workspace/LIBRARIES.md` |
| experience distiller | Not part of the code. The memories were written between development rounds and each update was approved by a human expert |
| judge agent | Not part of the run. It runs afterwards (`infra/judge/`) and writes `judge_result.md` |
| DSH (DeepSeek Harness) | DeepSeek's agent CLI, `dsh` (npm package `@deepseek-ai/dsh`, version 0.1.5-rc.1 in all runs). Every RA role is one DSH session |
| OmniQ | The benchmark and virtual research environment that turns published studies into reproduction tasks |
| strict success | A judge score of 1: no critical issue in any target figure |

## Ids and markers inside a run

| id or marker | meaning |
|---|---|
| F*n* | A task-book line (see above). `task_book_items.json` in the dataset lists them. |
| Q*n*, K*n* | A requirement row and a reading, both chosen by the planner. |
| R*n* | A residual-memory entry. The ledger stores the ids as numbers 1 to 10 (`bank_selected.ids`, `critic.residual_ids`); critic reports write `R3`. |
| G1, G2, G3 | The critic's gates: G1 provenance (every reported number traces to code and data on disk), G2 implementation (the method as prescribed), G3 task book (the unit's required items are met). Each gate is `pass`, `hit` or `n/a`. |
| `NN_<tag>` | Session number (launch order) and tag, as in `_pece/logs/NN_<tag>.log` and `.dsh_ra3/calls/NN_<tag>/`. Sort `NN` as a number. |
| tag | Role prefix plus unit id, for example `exec_u1`, `critic_u1`, `scout2_u7`, `criticsel1_u7`, `report_render`. `plan` and `replan` carry no unit. |
| `_wrapup` | Tag suffix of a short pens-down session that lets a stopped session save its work. |
| `_gwretryN` | Tag suffix of a retry after a dead session (one that used 0 tokens). |
| `<uid>.rN/` | An archived attempt of a unit, kept before a fresh executor starts (`render.rN/` for the report unit). |
| `_pece/scouts/<uid>.sN/` | One new-approach round of a unit: `s1/` to `s3/` hold the probe agents' work, `select1.md` to `select3.md` the panel's votes. |
| `audit_rN/` | An earlier review of a unit, archived before the next one. |
| `from_scout/`, `from_scout_K/` | Files copied into a fresh attempt from the winning probe agent and from an adopted runner-up. |
| `<<ORCH TIMEOUT>>`, `<<ORCH INTERRUPT ...>>` | Markers the orchestrator appends to the log of a session it stopped (time limit, or the end-of-run override). |
| `MECHANICAL FINDINGS` | Third line of a repair order written by the orchestrator, not by a model (ledger `critic` event with `by: mech`). |
| `FIX_PLAN:` | Last line of a critic report: a JSON object with `est_min`, `steps`, `residual_ids` and `reading_change`. |
| `.infra_failure` | Marker file in the run folder: the model backend stayed unreachable, or a second start in the same folder was refused (exit code 3). |

## Grades, statuses and outcomes

| term | meaning |
|---|---|
| A | Pass. |
| B | Local repair: a named fix that fits in the same attempt. A B with a G1 hit is handled as a C. |
| C | New approach: the root cause is not located or the route must change. Leads to probe agents and a panel when there is time, otherwise to a repair in place. |
| D | Retire on task-book grounds only: items contradict each other, or the specification cannot be met in any time. A D without an F citation and a stated ground is handled as a C. |
| W | Checkpoint verdict "on track" for a running job. |
| unit status | `running`, `done` (possibly with a known gap), `failed`, `blocked` (an upstream unit failed), `flushed` (stopped by the end-of-run override), or none (never started). |
| settle reason | How a unit closed: `A` passed review; `reaudit_A` passed again after a re-check; `gap_no_time` time ran out and the unit was kept with a known gap; `b_all_pass_no_time` time ran out with all gates passing; `g1_no_time` time ran out with a provenance hit (failed); `gap_unaudited` time ran out before the handoff was reviewed; `D` retired; `override` stopped by the end-of-run override. |
| decision (`fix_plan.action`) | What the orchestrator did after a review: `done`, `fix` (local repair), `cpath` (start probe agents), `fix_c` (repair in place after a C), `gap` or `failed`. |
| "done" | Not the same as "passed": a unit can end `done` with a last grade of B or C when time ran out. |

## Time words

| term | meaning |
|---|---|
| wall | The time budget the orchestrator plans against (`WALLTIME_SEC`; 85,800 s = 23 h 50 min in the paper, inside a 24-hour job). |
| usable wall | The time left until the override. |
| report reserve | Time kept for the final report: 75 minutes by default. |
| override | The end-of-run switch, when the remaining time falls to the report reserve plus 15 minutes. Running executors are told to wrap up and open reviews stop. |
| flush | The wrap-up of running executors after the override (at most 15 minutes). |
| slice | The time limit of one executor session: 2 hours, extended by 1 hour up to 3 times while the unit is productive. |
| checkpoint | A critic's look at a unit whose job is still running, at most every 3 hours. |
| pens-down | A short wrap-up session after a timeout (`_wrapup`). |
