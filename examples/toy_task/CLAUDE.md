# Task: Reproduce Figure 1 (toy task)

<!-- One line per item: the orchestrator numbers every non-empty line of the judged sections as F1..Fn, so a wrapped sentence would become two items. -->

## Paper
- **Title**: Ground-state energy of the transverse-field Ising chain (a synthetic test task)
- **Source**: there is no source paper. This specification is the whole source: wherever an instruction refers to "the paper", use this specification and the formulas in it.
- **PDF**: none

## Objective
Reproduce **Figure 1** as specified below: the ground-state energy per site of the transverse-field Ising chain on a ring, computed by exact diagonalization for chains of N = 8, 10 and 12 spins and compared with the exact free-fermion result. This is a small classical-simulation task for testing a setup end to end.

## General Guidelines
- Compute every plotted energy by exact diagonalization of the full Hamiltonian defined below (dense or sparse, your choice). The free-fermion formula is the reference: never use it to produce the exact-diagonalization points.
- All numbers must come from your own computation. Do not download data and do not hard-code expected values.
- Follow the stated model, sizes and field values exactly; do not reduce them.
- Work fully autonomously. Make your best judgment on any ambiguity. Do not ask questions.

## Figure Fidelity Standard
- Scientific accuracy: every plotted value is computed by the stated method and meets every quantitative checkpoint below.
- Same axes: each axis shows the stated quantity, on the stated scale, over the stated range.
- Required series and legend: every required series and panel is present and identified in a legend.
- Style is free: colours, marker shapes, line styles, fonts and legend positions need not match anything.

## Paper-Specific Preliminaries

### Model
- Hamiltonian: H = -J * sum_{i=1..N} Z_i Z_{i+1} - h * sum_{i=1..N} X_i, with periodic boundary conditions (Z_{N+1} = Z_1), where X_i and Z_i are the Pauli matrices on site i, and J = 1.
- Chain lengths N = 8, 10 and 12. Fields h = 0.0, 0.1, 0.2, ..., 2.0 (21 values).

### Reference results
- Finite chain: for even N and periodic boundary conditions the exact ground-state energy is E0_ff(N, h) = -2 * sum_{m=1..N/2} sqrt(J^2 + h^2 - 2*J*h*cos(k_m)), with k_m = (2m - 1) * pi / N.
- Infinite chain: the ground-state energy per site is e_inf(h) = -(1/pi) * integral_0^pi sqrt(J^2 + h^2 - 2*J*h*cos(k)) dk, so e_inf(0) = -J and e_inf(J) = -4J/pi.

### Figure 1
- Panel (a): x axis the field h from 0 to 2 (linear); y axis the ground-state energy per site E0/N (linear); markers for the exact-diagonalization values of N = 8, 10 and 12 (one series each) and a solid line for e_inf(h).
- Panel (b): x axis the field h from 0 to 2 (linear); y axis |E0_ed(N, h) - E0_ff(N, h)| / N on a logarithmic scale, one series per N; a difference of exactly 0 may be drawn at 1e-16.

### Quantitative checkpoints
- For every N and h: |E0_ed(N, h) - E0_ff(N, h)| / N <= 1e-9.
- At h = 0: E0/N = -1 for every N, to within 1e-9.
- For every h > 0: E0/N(N=8) < E0/N(N=10) < E0/N(N=12) < e_inf(h).
- e_inf(1) is computed with an absolute error below 1e-6.

## Deliverables
- `reproduced_figure.png` and `reproduced_figure.pdf` (both panels of Figure 1).
- `results.json`: for every N and h, the exact-diagonalization energy per site, the free-fermion energy per site and their absolute difference; and e_inf(h) for every h.
- `report.md` (method, validation, comparison with the reference results).
- All Python source code you wrote.
