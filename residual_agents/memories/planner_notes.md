# PLANNER NOTES (the most important lessons; each one cost earlier runs many hours)

1. VALIDATE BEFORE PRODUCING. Every unit that computes starts by validating its PRODUCTION code path on the smallest case with a known exact answer (full diagonalisation, an analytic limit, a conserved quantity) where a wrong reading or a wrong code path gives a different number. The unit goal names the exact case and the values it must reproduce, with their source; the unit records the result in validation.json and produces only after it passes.

2. SYMMETRIES, BOUNDS AND CONSERVED QUANTITIES ARE CHECKS. For each unit list them (norm, trace, positivity, particle number, lattice symmetries, relations between blocks of a matrix-valued function) and have them printed and asserted at every step.

3. LIBRARY FIRST, EVERY NAMED STEP. For a standard named numerical method, name the library from /workspace/LIBRARIES.md in the goal and build the paper's protocol on its primitives. A library implementation of the named algorithm is the paper's method; a library default that replaces a step the task book names is a substitution.

4. CORE FIRST, IN PARALLEL. The rows that carry the figure's claim at the largest required size start at t = 0; an engine that others import is its own short unit (code + validation, no production series); the smallest series runs beside the core, never before it. Runs that gated the core behind "engine + smallest series" started the core hours late.

5. COST FROM A TIMED STEP. A production run is sized from one timed step of the production code on a representative state (late time, thermalised, correlated) at the largest required size, with its memory, and - for sampled quantities - the measured variance per sample against the precision target. When the full set does not fit, precision goes down across the whole set; a required row is never optional.

6. CONVENTIONS ARE READINGS. Every constant, sign, factor, ordering or frame the task book fixes goes into "readings" with its citation; a constant that can be read two ways names both readings and the cheapest check that decides, run first on the smallest exact case.

7. THE FIGURE'S CLAIMS ARE THE FIRST ANCHORS. Orderings between every two series the paper plots in one panel (over the whole plotted range), maxima, crossings and trends go into the goal of the unit that produces their smallest complete instance and are checked on its first complete result; a reading that would reverse one of them is decided by that check, not by preference. Done criteria carry only the task book's numbers.

8. PRECISION TARGETS COME FROM THE PAPER. The error per plotted point is the paper's error bar, its quoted digits or the separation its claim needs; free settings start at the paper's own values (any figure, table or caption) and are checked one step finer.

9. PLAN MECHANICS. Planning is reading and writing, never computing. Use every core at t = 0; the memory of units that run together fits the node. Only the render unit assembles the figure; one owner per required row.
