# fragments/

The orchestrator is one Python module with one shared namespace. Its functions read module globals at call time,
`_main_body` and `run_report` rebind `POOL`, and the tests rebind paths and replace functions. A package split with
imports would freeze stale bindings, so the source is kept as fragments that are concatenated into one module.

- `MANIFEST.txt` gives the order. `python tools/build.py` writes `residual_agents/orchestrator.py`: one header line,
  then the fragments byte for byte. The built file is committed; `python tools/build.py --check` fails when it
  differs from a fresh build.
- Edit the fragments, never the built file. At import time a statement may only use names that earlier statements
  define (a constant used as a default argument, a function called at module level).
- Lint the built file, not the fragments (a fragment alone has undefined names):
  `python -m pyflakes residual_agents/orchestrator.py`.
- The prompt constants are frozen. They all live in `09_prompts.py`; their sha256 values are in
  `tools/provenance/prompt_sha256.json`.

| fragment | content |
|---|---|
| `00_config.py` | run identity, paths, knobs, cost rates, run-wide state and locks |
| `01_util.py` | file, log and ledger helpers; node facts (`NCPUS`, `MEM_GB`) |
| `02_dsh_backend.py` | dsh: constants, patch files, version, model id, the route switch, per-call home and environment; endpoint probe; prompt-file launcher |
| `03_sessions.py` | dsh session logs: zstd decoding, usage accounting, transcript tails, continuations |
| `04_procs.py` | the process registry: call and unit markers, `/proc` scans, kill tree, reaping |
| `05_calls.py` | the call layer: tags, the per-call header (time ledger, node line, grant), interrupts, `run_call`, `slot` |
| `06_clock_taskbook.py` | the clock and the report reserve; the TASK BOOK parser |
| `07_checks.py` | path helpers, deliverable checks, `handoff_check`, `waiting_spec`; the mechanical check and its prompt blocks |
| `08_jobs.py` | waiting on a unit's running job: registration helpers, progress signals, `wait_job`, resume text |
| `09_prompts.py` | every prompt constant |
| `10_planner.py` | plan validator, readings, run state, `checkpoint`, plan acceptance and repair, unit spec |
| `11_memory.py` | memory loading and residual retrieval |
| `12_critic.py` | audits, machine critic, report parsers, `run_critic`, `run_checkpoint` |
| `13_restart.py` | archives, residuals, REUSABLE copies, `restart_unit` |
| `14_scouting.py` | C-grade scouting: scouts and the selection panel |
| `15_readings.py` | reading changes and input propagation |
| `16_pool_dag.py` | memory sampler, `ResourcePool`, readiness, file gate, `settle` |
| `17_unit.py` | the executor's prompts, `call_outcome`, `decide`, `settle_in_place`, `unit_worker` |
| `18_scheduler.py` | launches, the branch replan, `schedule_loop`, override, live clock, flush |
| `19_report_main.py` | report prompt helpers, `root_copy`; `run_report`, digest, epilogue, `main` |
