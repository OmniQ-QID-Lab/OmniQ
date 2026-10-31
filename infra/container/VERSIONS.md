# Versions in the run image, and the evidence for each

The image should give the agents the software the paper's runs had. Where the runs themselves record a version, the
image uses it. Where they do not, the table says so.

How the evidence was collected: every text file under 3 MB in the 59 published run folders (reports, profiles,
validation files, handoffs, logs) was searched for version strings of the packages below, and each hit was read in
context. Hits that quote a third-party README or requirements file, or a deprecation warning, were set aside. "Runs"
counts the run folders with at least one hit in an environment context.

## Base system

| item | in the image | evidence | status |
|---|---|---|---|
| Python | 3.11.15 (`python:3.11.15-trixie`) | `3.11.15 (main, Mar  3 2026, 20:22:44) [GCC 14.2.0]` in validation files (20 hits), `python 3.11.15` in reports and profiles (13 hits) | from the runs |
| OS | Debian 13 (trixie), glibc 2.41 | `platform` strings ending in `-with-glibc2.41` (84 hits); the interpreter was built with GCC 14.2, the compiler of Debian 13 | from the runs; see the note on ENV.md below |
| Node.js | 24 (`node:24-bookworm-slim`, copied in) | dsh ran on Node 24 (it needs at least 23.8, for built-in zstd); the exact release is not in the runs | major version from the runs; exact release not recorded |
| dsh | `@deepseek-ai/dsh` 0.1.5-rc.1 | `dsh_version` of the ledger's `episode_start` event: 0.1.5-rc.1 in 59 of 59 runs | from the runs |
| dsh dependencies | `@deepseek-ai/cordis` 4.0.2 (override); the rest resolved by npm at build time | the production tree: about 230 `@deepseek-ai/*` sub-packages at 0.1.5-rc.2 and cordis 4.0.2 | no lock file; versions may differ (see `dsh/README.md`) |
| zstd, poppler-utils, procps | Debian 13 packages | the orchestrator decodes dsh session logs with the zstd program (or python-zstandard, or Node); agents extracted paper text with `pdftotext` | not pinned (Debian stable updates) |
| Claude Code (judge image only) | 2.1.281 | the CLI version of the paper's judge | from the authors' records |

Note on `ENV.md`: the copy in the paper's runs says Debian 12, glibc 2.36 and Node.js 20, which does not match what
the runs report: glibc 2.41, Python 3.11.15 built with GCC 14.2, and dsh on Node 24 (the orchestrator also put Node 24
first on the agents' PATH). `ENV.md.template` states the facts of this image.

## Python packages (`requirements-paper.txt`)

The packages are the ones `ENV.md` lists as pre-installed. In the paper's runs, most of them were imported from a
shared per-user site folder (`~/.local`) of the account that ran the jobs, not from the image itself: import paths in
the run logs point there. So the versions below are the ones the agents' code actually imported, which is what the
image should reproduce; the production image's own `pip freeze` may differ.

| package | pin | runs | example of the evidence (run folder file: text) |
|---|---|---|---|
| numpy | 2.4.6 | 42 | profile and report lines such as "numpy 2.4.6, scipy 1.17.1, qiskit 2.3.1" |
| scipy | 1.17.1 | 30 | same lines |
| matplotlib | 3.10.8 | 23 | "plots ... with matplotlib 3.10.8 / numpy 2.4.6 / pandas 2.3.3" |
| pandas | 2.3.3 | 1 | same line |
| sympy | 1.14.0 | 1 | "Packages: numpy 2.4.6, scipy 1.17.1, sympy 1.14.0, cvxpy 1.8.1" |
| networkx | 3.6.1 | 1 | "networkx 3.6.1 for the greedy colouring" |
| h5py | UNPINNED | 0 | the only version seen quotes a third-party requirements list |
| qiskit | 2.3.1 | 10 | "qiskit 2.3.1 Statevector" cross-checks |
| qiskit-aer | 0.17.2 | 3 | "qiskit-aer 0.17.2 signature is QuantumError(noise_ops)"; the 0.15.0 hits are deprecation-warning text |
| cirq-core | UNPINNED | 0 | the only version seen (1.5.0) is in a copied third-party README |
| pennylane | 0.44.1 | 2 | "pennylane 0.44.1 default.qubit statevector" |
| openfermion | 1.7.1 | 3 | "engine_setting (pyscf 2.12.1 / openfermion 1.7.1 ...)" |
| pymatching | UNPINNED | 0 | none |
| stim | 1.15.0 | 2 | "use the installed Clifford simulator stim 1.15.0" |
| quimb | 1.12.1 | 3 | "packages: quimb 1.12.1, numpy 2.4.6, scipy 1.17.1, qiskit 2.3.1" (one run also shows 1.15) |
| numba | 0.64.0 | 6 | "numba 0.64.0, device ... A100" (numba is a dependency of quimb) |
| tensornetwork | UNPINNED | 0 | none |
| pyscf | 2.12.1 | 2 | "pyscf 2.12.1 RHF+CASCI"; the 2.9.0 hit is in a copied third-party README |
| cvxpy | 1.8.1 | 2 | "cvxpy 1.8.1 CLARABEL" |

Limitation: the versions follow the environment description of the runs and the version lines the runs printed;
minor versions may differ from what the agents imported, and the UNPINNED lines install the newest release.

## Seen in the runs, not installed

These came from the shared user-site folder or from installs inside a run. The agents can install them at run time
(`pip install`), as ENV.md says:

| package | version seen | runs |
|---|---|---|
| physics-tenpy | 1.1.1 | 11 |
| torch | 2.0.1 (one run 2.9.0) | 6 |
| jax | 0.10.2 (single runs: 0.4.30, 0.4.38, 0.5.2) | 5 |
| scikit-learn | 1.9.0 | 4 |
| qiskit-nature | 0.7.2 | 1 |
| PyMuPDF | 1.27.2 | 1 |

Consequence for reproduction: in the paper's runs, a package that one run installed into the shared user-site folder
was importable in later runs of the same account. The release gives every run a fresh, empty home folder, so each
run starts from the image alone.
