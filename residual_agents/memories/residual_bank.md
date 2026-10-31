# RESIDUAL BANK (generic failure signatures; the harness shows you the entries that match your unit or event)

R1 VALIDATE THE PRODUCTION PATH ON THE SMALLEST EXACT CASE
TAGS: stage=any; event=restart,anchor_miss; keys=always,exact answer,smallest case,diagonalisation,propagator,analytic,permanent test,wired
SIGNATURE: plausible output from code never checked against an exact answer: a term with a wrong order, sign or normalisation, or a fix that production never calls.
CHECK: run the unchanged production driver on the smallest exactly solvable case (full diagonalisation, exact propagator) where every term matters; compare all outputs; test each term alone against its definition.
FIX: produce only after agreement; keep the check as a permanent test; after every fix rerun it through the real driver: the number must move as predicted.

R2 A NAMED METHOD COMPONENT SUBSTITUTED
TAGS: stage=plan,validate,produce; event=substitution; keys=library,libraries,algorithm,named,substitute,substituted,substitution,replaced,omits,not implemented,approximation,stand-in,hand-written
SIGNATURE: a step, estimator, term or setting the task book names is swapped for a library default, a cheaper variant or a hand-built stand-in, often after a library was wrongly called unable.
CHECK: list each named component beside the code line implementing it; no line, or one computing something else, is a substitution; a library failure must reproduce on an independent minimal case.
FIX: implement every named step as stated, on LIBRARIES.md library primitives where they exist; a documented substitute still fails: implement it or stop with evidence.

R3 A MISSED ANCHOR IS A READING OR CODE QUESTION FIRST
TAGS: stage=plan,validate,handoff; event=restart,anchor_miss; keys=anchor,anchors,convention,decision,reading,sign,factor,ordering,calibration
SIGNATURE: a stated value, ordering or crossing is missed and found late; rounds go to scaling up, arguing readings, or a factor fitted to the published curve.
CHECK: on the smallest case where the readings differ, compute the anchor under each plausible reading (sign, factor, units, frame, ordering) and with independent code.
FIX: settle it before any scale-up and keep the reading the anchor selects; if independent code gives the paper's value, find your bug; if it gives yours, hand off a CONFLICT with evidence; never fit to the figure.

R4 A BINDING CAP OR WRONG ESTIMATOR PASSED AS CONVERGED
TAGS: stage=validate,produce; event=precision_miss; keys=convergence,converged,tolerance,cap,truncation,estimator,error estimate,cutoff,iteration,plateau,self-consistent
SIGNATURE: an iteration or size cap binds, an error curve plateaus, or the error estimate uses another quantity, norm or sample set than the paper's; capped points pass as lower bounds.
CHECK: every production step exits on its tolerance, not its cap; the hardest point with the cap doubled moves less than the paper's resolution; the estimator matches the exact error where known.
FIX: implement the paper's estimator and stated settings; size caps from a pilot with headroom; a cap exit is a failed step, never plotted as converged.

R5 A BOUND, CONSERVATION LAW OR SYMMETRY BROKEN
TAGS: stage=validate,produce,render; event=none; keys=trace,hermiticity,hermitian,positivity,density matrix,eigenvalue,symmetry,conserved,fidelity,entropy,nan,probability
SIGNATURE: a trace or norm drifts, a density matrix turns non-Hermitian or negative, an entropy or probability leaves its range, a symmetry breaks, or values go non-finite.
CHECK: log bound, conservation and symmetry residuals per step and on every delivered file; anything beyond roundoff or truncation error is a defect.
FIX: assert bounds in production and stop at the first violation; suspect index order, a conjugate, an overflow, an inner solve, a normaliser or a truncation rule; never clip, project or symmetrise to hide a violation (a step the paper prescribes is a named component).

R6 RESULTS OR HANDOFF NOT ON DISK AT THE CUT
TAGS: stage=produce,register,wait,handoff; event=slice_cut,deliverable_missing,handoff_invalid,reg_invalid,stalled,orphan,mech; keys=per-point,checkpoint,checkpoints,heartbeat,progress,resume,partial results,relaunch,overwrite,atomic
SIGNATURE: a cut or crash leaves nothing renderable; handoff.json shows an old state (a dead pid under waiting, superseded numbers); a live job looks stalled; a relaunch overwrites finished files.
CHECK: handoff.json names only live pids and current numbers; each progress glob lists a file rewritten every few minutes; a relaunch leaves finished files untouched.
FIX: write one file per point as it finishes, plus a heartbeat under progress; write handoff.json when you hand off, naming only live pids; each launch writes to its own tag and renames complete files into place.

R7 A LONG RUN OUTSIDE ONE REGISTERED LEADER
TAGS: stage=register,wait,produce; event=reg_invalid,orphan,slice_cut,stalled; keys=background job,registered,detached,leader,watcher,watchdog,kill,pid,frozen copy,workers
SIGNATURE: a job registered under an exited launcher, a sleep loop or a watcher dies or orphans its workers at the slice end; helpers beside it vanish; a kill by name hits siblings; editing an imported file kills it.
CHECK: `ps -o pid,cmd --ppid $(cat bg.pid)` lists the workers; every helper is in the leader's tree; no imported file is newer than the launch.
FIX: one setsid leader writes its own pid and runs workers, guards and finalizers as children; kill only recorded pids; run from a frozen copy, edit only the working copy.

R8 REQUIRED POINTS OR DECIDING PRECISION GIVEN UP
TAGS: stage=plan,timing,produce,handoff; event=unaffordable,deliverable_missing,precision_miss; keys=required set,precision,statistics,error bars,samples,seeds,sweeps,missing,dropped,ladder,bias
SIGNATURE: required sizes or series are dropped while precision stays costly, or precision is cut until error bars, fit spreads or a bias exceed the paper's resolution.
CHECK: from the timing run, project each required point's cost and error bar at the planned precision; one point at half and full per-run setting must agree within errors.
FIX: use the cheapest precision that still decides the claim and fits the full set; every required point before extras; when even that does not fit, stop launching and hand off the measured cost - the row stays required.

R9 COST TIMED ON AN EASY UNIT
TAGS: stage=timing,register; event=unaffordable,stalled; keys=cost,memory,workers,threads,parallel,scaling,timed,per step,production run,largest size,gpu
SIGNATURE: a ladder runs far slower than planned or dies of memory: the timed unit was easy (smallest size, initial state, one worker alone), or a step costs far more than the method's scaling.
CHECK: profile.json holds one step's time and peak memory at the largest required size and precision, on a saturated state, with all workers running; fit the cost exponent from two sizes.
FIX: time that regime before registering; profile a slow step and fix what dominates; use every granted core and device; never kill a running job on its first minutes' rate.

R10 PLOTTED OR QUOTED NUMBERS FROM MIXED OR STALE FILES
TAGS: stage=produce,handoff,render,report; event=stale_data,handoff_invalid,mech,writes_outside; keys=upstream,compared,sha256,md5,checksums,key numbers,known limits,threshold,superseded,stale,arms
SIGNATURE: a plot or compared pair mixes code versions, settings or superseded upstream files; a quoted number is hand-typed, stale, or judged by a threshold the task book never states.
CHECK: each output carries its engine hash, settings and input checksums; the files one plot reads form one group; every quoted number traces to a file by one grep.
FIX: after a fix or upstream change regenerate all dependent files; plot the best validated run; one setting for compared arms; script every quoted number; judge only by task-book numbers.

<!-- MERGED (V6.1 cut 50 -> 10; old id -> new id; the old ids no longer exist)
- R1 <- R50 R28 R29            - R6 <- R12 R45 R43 R41
- R2 <- R2 R53 (+ the judge's method-substitution failures)
- R3 <- R9 R11 R8              - R7 <- R14 R16 R42 R7 R47
- R4 <- R23 R4 R31             - R8 <- R54 R5 R27 R18 R10
- R5 <- R22 R32 R36 R37        - R9 <- R3 R30 R6 R21
- R10 <- R24 R26 R40 R20 R25
- already covered elsewhere (removed from the bank): R1, R13, R34, R46, R48 -> LIBRARIES.md (header / rows);
  R15 -> planner note 4; R44 -> planner note 9; R17 -> B_FIX_PROMPT ("apply exactly the fixes below")
- dropped: R33 (never cited), R35 R38 R39 (method-specific, rarely cited; the generic checks live in R1 and R5)
-->

<!-- INDEX-CHECK
entries: 10; always: R1 (start and restart launches)
stage any: 1 (R1); plan: 3 (R2 R3 R8); validate: 4 (R2 R3 R4 R5); timing: 2 (R8 R9); register: 3 (R6 R7 R9); produce: 7 (R2 R4 R5 R6 R7 R8 R10)
stage wait: 2 (R6 R7); handoff: 4 (R3 R6 R8 R10); render: 2 (R5 R10); report: 1 (R10)
event restart: R1 R3 | anchor_miss: R1 R3 | precision_miss: R4 R8 | substitution: R2 | stale_data: R10
event stalled: R6 R7 R9 | unaffordable: R8 R9 | orphan: R6 R7 | slice_cut: R6 R7 | reg_invalid: R6 R7
event deliverable_missing: R6 R8 | handoff_invalid: R6 R10 | mech: R6 R10 | writes_outside: R10
(order within an event = bank order; every single event maps to at most three entries, so ev_cap=3 shows all of them)
-->
