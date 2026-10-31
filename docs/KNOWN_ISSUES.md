# Known issues

This code keeps the behaviour that produced the paper's results. A review of the code found the issues below. Fixing
any of them would change what RA does in a normal run (its prompts, decisions, scheduling or verdicts), so they are
documented here and left in the code.

Each entry gives what happens, where in the code, the evidence in the 59 published runs (by paper number and unit),
the effect on results, and the proposed fix. Severity is the code review's.

| id | issue | severity | runs affected |
|---|---|---|---|
| [KI-01](#ki-01) | the file gate starts dependent units on files the critic has just graded C | high | #29, #34, #9, #57 |
| [KI-02](#ki-02) | the fix list given to repair sessions is often a fragment | high | 48 runs |
| [KI-03](#ki-03) | after a mechanical repair round, the re-audit checks the machine's list | medium | #35, #40, #24, #43, #46, #49, #57 |
| [KI-04](#ki-04) | input changes during an audit raise no change notice | medium | 19 reading changes, e.g. #9 |
| [KI-05](#ki-05) | the "same B three times" guard cannot fire for some units | medium | 16 runs |
| [KI-06](#ki-06) | a D counts only if its grounds are in the first 12,000 characters | medium | #33 |
| [KI-07](#ki-07) | the CPU-based slice extension almost never fires | medium | unknown; 1 extension granted on CPU (#3) |
| [KI-08](#ki-08) | the per-unit time used is overstated after a new-approach round | low | 21 runs |
| [KI-09](#ki-09) | a reading added during a run is shown as "v2, was: itself" | low | #31, #26, #9, #48, #50, #27 |
| [KI-10](#ki-10) | relative paths in `handoff.json` are resolved against `/workspace` | low | #24 |

## Behaviour issues

### KI-01

**The file gate starts dependent units on files the critic has just graded C.** Severity: high.

- **What happens.** A unit is marked as having a valid handoff when its handoff passes the mechanical check. Only a
  later failed mechanical check or a restart removes the mark; a C or D verdict does not. After a C, the new-approach
  round (probe agents and panel) can take up to 75 minutes, so the unit's files stay unchanged for longer than the
  30-minute file gate, and dependent units start provisionally on files the critic has just ruled defective. Such a
  launch also marks the upstream unit as having released its files, so its fresh attempt copies the condemned
  deliverables back into its new folder, where the dependents keep reading them.
- **Where.** `_file_gate_ok` (the gate), `unit_worker` (sets and clears the mark), `restart_unit`.
- **Evidence.** 6 provisional launches in 4 runs: #29, #34, #9 and #57. In #29, u1 was graded C at 80.6 minutes; u2
  and u3 started on u1's files at 103.1 minutes and u4 at 118.1 minutes; u1 restarted at 120.2 minutes and was graded C
  again at 194.7 minutes. The restart copied 9 deliverables of the C-graded round into the fresh folder.
- **Effect.** Only #29 used condemned code; later audits caught it, and #29 passed.
- **Proposed fix.** Remove the mark on every C or D verdict (full audit or checkpoint) and on a B that becomes a C
  because of a G1 hit; the next passing mechanical check sets it again.

### KI-02

**The fix list given to repair sessions is often a fragment.** Severity: high.

- **What happens.** `extract_fixes` takes the first mention of "FIX LIST" in the critic's report (other than "previous
  FIX LIST") and copies the text up to the next heading or `FIX_PLAN:` line. Critics often mention the fix list in
  running text before the real section ("see FIX LIST", "The r1 FIX LIST was carried out"). The extract then starts
  at that mention and stops at the next heading, and the real section further down is never reached. The extract is
  what the repair prompt ("Apply EXACTLY the fixes below") and its continuations carry, and what the delta re-audit
  lists as items to verify (cut at 3,000 characters).
- **Where.** `extract_fixes`; used by `unit_worker` (repair and checkpoint prompts), `_fix_carry`, `run_critic` (delta
  re-audit) and `run_report` (report repair).
- **Evidence.** 305 of 566 model-written B reports in 48 runs, for example #30 u1 and #34 u5. In #30,
  `u1/audit_r1/critic_report.md` gives the extract "/ reading_change). No G3 hit either way.", while its real fix list
  starts at character 16,206.
- **Effect.** The agents usually read the full critic report themselves (the repair session of #30 u1 reports that
  all three critic fixes were applied). No run was found whose result changed.
- **Proposed fix.** Prefer the last heading that starts a line with "FIX LIST" (`(?m)^[ \t#*>_-]*FIX LIST\b`), and fall
  back to the first mention.

### KI-03

**After a mechanical repair round, the delta re-audit checks the machine's list instead of the critic's.** Severity:
medium.

- **What happens.** A model-written B with G1 and G3 passing marks the unit for a delta re-audit. If the repair round
  then fails the mechanical check, `machine_critic` archives the model's report, writes its own report whose fix list
  is the mechanical failures, and leaves the mark set. After the mechanical repair, the delta re-audit takes the
  machine's list as the items to verify, the machine report's time as the "changed since" baseline and the machine
  report's archive as the earlier audit, and it is told that the earlier audit's anchors, criteria and gate proofs
  stand for every unchanged file, although the machine report has none. Edits made for the model's fix list happened
  before the machine report, so they are not even listed as changed.
- **Where.** `unit_worker` (the delta condition), `machine_critic`, `run_critic`.
- **Evidence.** 7 units: #35, #40, #24, #43, #46, #49 and #57. In #43 the G2 fix was never re-audited. In #46 u1 the
  next critic noticed the gap ("audit_r2 was mechanical-only ... so its 'unchanged file' protection covers nothing")
  and re-checked the G2 fix on its own.
- **Effect.** No score changed.
- **Proposed fix.** Clear the mark whenever the machine critic runs, or take the delta re-audit's items and baseline
  from the newest model-written report.

### KI-04

**Input changes during an audit raise no change notice.** Severity: medium.

- **What happens.** The change notice a critic sees is computed when its audit starts, but the unit's new input
  fingerprints are recorded when the audit ends, up to 45 minutes later for a full audit and 25 minutes for a
  checkpoint. An upstream rewrite that lands inside the audit becomes part of the new baseline: it is never reported,
  never shown in a change notice, and never reopens the unit once it is done. Dependent units often run beside their
  upstream (they start provisionally through the file gate), so such overlaps are common.
- **Where.** `run_critic`, `run_checkpoint`, `record_inputs`, `upstream_changes`.
- **Evidence.** 19 reading changes landed inside another unit's audit. Example, #9 u7: u6's data files and its
  `validation.json` were rewritten at 1,300.7 minutes, inside u7's audit (1,298.3 to 1,302.7 minutes); u7's last audit
  got no notice of the rewrite, and u7 settled done with a gap.
- **Effect.** No wrong result found.
- **Proposed fix.** Take the fingerprint snapshot when the notice is computed, before the session starts, and store
  that snapshot after the verdict.

### KI-05

**The "same B three times" guard cannot fire for units that list their own folder.** Severity: medium.

- **What happens.** `decide` turns the third B in a row with the same gate hits and no new data into a C. "No new
  data" means that a signature of the unit's deliverables is unchanged. For a folder, the signature covers every file
  below it. The planner prompt allows a unit to list its own folder as a deliverable, and then the signature includes
  files that change every round whatever the executor does (`critic_report.md`, `audit_rN/`, `mech.json`,
  `handoff.json`), so the count starts again at 1 after every verdict.
- **Where.** `decide`, `_deliv_sig`, `_quick_sig`.
- **Evidence.** 59 of 267 producing units in 16 runs list their own folder; the guard fired 0 times in the 59 runs.
  For example, #24 u_cka_hi had 10 B verdicts in a row with the same G3 hit (from 226 to 469 minutes) before an A.
- **Effect.** Likely none in the paper's runs.
- **Proposed fix.** Leave bookkeeping out of the signature: `audit_r*`, `probe_*`, logs, `handoff.json`, `mech.json`
  and the other files the orchestrator writes.

### KI-06

**A D grade counts only if its grounds appear in the first 12,000 characters of the report.** Severity: medium.

- **What happens.** A D is accepted only if the first 12,000 characters of `critic_report.md` contain both an
  `F<n>` reference and "(a)" or "(b)"; otherwise the ledger records `d_rejected` and the grade becomes a C. The critic
  prompt puts the section that states the D's grounds near the end of the report, after EVIDENCE, GATE CHECK, ANCHORS
  and CRITERIA. The test also passes on incidental text such as a list label "(a)" or "Panels (a)-(d)".
- **Where.** `run_critic`.
- **Evidence.** #33 u1: the report is 33 KB and its D sections start at character 29,201. The D became a C, and the
  unit settled done with a gap instead of failed, which changes how the report agent treats its data. Three other D
  reports passed the test only because of incidental text.
- **Effect.** #33 scored 0 either way.
- **Proposed fix.** Search the whole report, and test for the D section headings rather than any "(a)".

### KI-07

**The CPU-based slice extension almost never fires.** Severity: medium.

- **What happens.** At the end of a 2-hour slice, an executor session is extended if the unit wrote a file in the last
  15 minutes or, for the first extension only, if the session used at least 60 CPU-seconds in the last 5 minutes. The
  5-minute CPU sample is refreshed in the same poll that tests the deadline, before the test. With the paper's
  constants (a poll every 30 seconds, a 300-second CPU window, slices that are multiples of 300 seconds), the first
  poll after the deadline is usually also a refresh poll, so the CPU use is measured over a few milliseconds and stays
  below 60 seconds. A cut stops the session's processes, including a computation running in the foreground.
- **Where.** `run_call`, `_slice_progress`.
- **Evidence.** Of 216 first extensions in the 59 runs, 1 was granted on CPU (#3); 119 executor sessions were cut at
  their first slice boundary.
- **Effect.** Unknown: some long foreground computations that wrote no file for 15 minutes were probably cut. The
  offline tests cannot show this, because they shorten the slices but the CPU window is a fixed 300 seconds.
- **Proposed fix.** Compute the CPU use before refreshing the sample, or test the deadline first.

### KI-08

**The per-unit time used is overstated after a new-approach round.** Severity: low.

- **What happens.** The TIME LEDGER line at the top of every prompt says "THIS UNIT u: used X h of the wall", the sum
  of the durations of all sessions whose tag ends in the unit id. The three probe agents and the three panel critics
  run in parallel, but their durations are added as if they ran one after another.
- **Where.** `time_ledger`.
- **Evidence.** 74 new-approach rounds in 21 runs; each adds about 2.1 to 2.4 times its wall time. For example, in #31
  u1, 0.54 hours of wall time are shown as 1.26 hours.
- **Effect.** A misleading number in the unit's later prompts, which agents may read as urgency. No decision of the
  orchestrator reads it.
- **Proposed fix.** Use the union of the session intervals.

### KI-09

**A reading added during a run is shown as "v2, was: itself".** Severity: low.

- **What happens.** For a reading id the plan does not have, `apply_reading_change` creates the entry with the new
  text, then records a history version 1 with the same text, sets the version to 2, and writes "CHANGED (version 2):
  <new text> (was: <new text> ...)" into `READINGS.md`. Because the history is then not empty, the rule that a reading
  changes at most once per run refuses any later correction of the reading that was just added.
- **Where.** `apply_reading_change`.
- **Evidence.** 9 reading ids in 6 runs: #31, #26, #9, #48, #50 and #27. For example #31 K16, proposed by a probe agent
  of u1.
- **Effect.** Display only; the one-change rule is intended.
- **Proposed fix.** Treat a new id as version 1, with no history, marked "added". It is not applied because it changes
  the text of `READINGS.md` and of the change notices that agents read.

### KI-10

**Relative paths in `handoff.json` are resolved against `/workspace`, not the unit folder.** Severity: low.

- **What happens.** An executor works in `/workspace/<uid>/`. A handoff that lists `raw/x.npz` means
  `/workspace/<uid>/raw/x.npz`, but the mechanical check looks for `/workspace/raw/x.npz`, reports "listed path does not
  exist" and starts a mechanical repair round.
- **Where.** `handoff_check`, `_ws_path`.
- **Evidence.** #24 u_bpa (one extra repair session).
- **Effect.** None on results.
- **Proposed fix.** Resolve a relative handoff path against the unit folder first.

## Requirements and limits (not defects)

- **Panel votes must use bare numbers.** `_parse_rank` reads every digit on the `RANK:`, `ADOPT:` and `DISQUALIFIED:`
  lines, so "DISQUALIFIED: 1 (its plan changes F3)" would count probe agents 1 and 3. All 252 panel files of the
  paper's runs used bare numbers, as the prompt asks; a chattier model could trip it.
- **Node with built-in zstd.** DSH 0.1.5 writes zstd-compressed session logs. If neither the `zstd` program, nor the
  Python `zstandard` module, nor Node's built-in zstd (Node 23.8 or newer) can decode them, every token count reads 0
  and the dead-call rule re-issues normal executor turns. `ra preflight` decodes a test frame to catch this.
- **UTF-8 locale.** Model-written text often contains characters outside Latin-1; under another locale the run can
  end without `report.md`. The launcher starts the orchestrator with `-X utf8`.
- **A writable `/pece_store`.** Without it the epilogue cannot write its store record and every ledger write prints
  an error. The launchers mount a writable folder outside the run folder, and the preflight checks it.
- **`/workspace` is fixed**, because the prompts name it, so RA must run in a container. Each concurrent run needs a
  unique `RUN_ID`, and Apptainer users should add `--pid`.

## Display quirks of the timelines

`ra trace` produces the same timelines as the published runs (only the `generated_by` string differs), including
three quirks: decision lines show `afford=?` (a placeholder); lines named "override start" and
"override end" only mark the start and end of the run; and "RESTART round N" counts review rounds, not restarts.
