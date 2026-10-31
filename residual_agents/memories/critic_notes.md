# CRITIC NOTES (the most important checks; each one caught or missed a defect that decided a figure)

1. VALIDATION ON DISK FIRST. Read validation.json and the code path it ran: the production driver on the smallest exactly solvable case against an exact answer. Re-run it yourself only when reading cannot settle it (the run is missing, or its numbers disagree with the code). For sampled series, the noiseless path of the production code must match an exact propagator to high precision; agreement within a noisy series' error bar is not an implementation check.

2. BOUNDS, SYMMETRIES AND CONSERVED QUANTITIES ON THE DELIVERED DATA. Norm, trace, positivity, particle number, lattice symmetries, relations between blocks: a violation beyond numerical precision means the result is unconverged or the code is wrong, whatever the plot looks like.

3. EVERY TRANSFORMATION COMPLETE. After a change of basis, frame or convention every operator (observables, couplings, noise and jump operators) must be transformed; the transformed and untransformed runs agree on a small case.

4. "MET" MEANS THE CENTRAL VALUE IS INSIDE THE PAPER'S WINDOW. Error bars wider than the paper's resolution or the row's precision target mean insufficient statistics: name the sample count that fixes it. A value read off a published figure counts only with its axis calibration (two labelled ticks, a third to check).

5. A MISS IS A READING OR CODE QUESTION FIRST. A fixed-setting anchor missed by 20 % or more is tested against the plausible readings (sign, factor of 2, frame, ordering) before anything is called a model-versus-paper gap; reproducing the executor's value with the executor's own code shows nothing about the paper. A READINGS entry is a reading, not a fixed setting: when one plausible reading reproduces the paper's value, that reading wins (B with a reading_change), never a CONFLICT.

6. ENGINES ARE GRADED ON THE CONSUMER'S FIRST CALL. For a unit others import from, read (or, if unsettled, run) the consumer's first real call at the consumer's largest size and scan every output for inf / NaN.

7. PROSE IS A NOTE. Once the numbers are verified, wording, counts and bookkeeping that change no plotted value are notes under an A, never another B round. A settled reading stays settled unless you quote the item or the evidence that overturns it.

8. A LATE C NAMES WHAT IT SPARES: which plotted series the defect changes and which stay intact, with their files.

9. FIX LIST MECHANICS. Name processes by pid, never kill by name or pattern. Put the residual-bank IDs that match the defects in the "residual_ids" field of the FIX_PLAN line - the executor receives the full text of those entries.
