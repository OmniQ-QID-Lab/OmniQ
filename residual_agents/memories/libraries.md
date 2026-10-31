# LIBRARIES - established open-source implementations of standard numerical methods (orchestrator note, generic)
Rule (LIBRARY FIRST): where a library implements the numerical method the paper names, build the paper's protocol on its primitives - that IS the
paper's method, not a substitution; every step the task book names is still implemented as named. Install into your OWN directory and import from there:
    pip install --target /workspace/<uid>/lib <pypi-name>      then in Python:  import sys; sys.path.insert(0, "/workspace/<uid>/lib")
Test the import in two lines BEFORE designing around it; record the package and version in profile.json notes. PyPI names differ from import
names, and `pip download` works on this farm: a "No matching distribution" error is a wrong PyPI name or a GitHub-only package (clone it), never
the network. Downloaded CODE is allowed; downloaded DATA is not.

| method class | pip name | import | entry points | notes |
|---|---|---|---|---|
| 1D tensor networks on chains: ground states, real / imaginary time evolution, finite temperature | physics-tenpy | tenpy | tenpy.algorithms and tenpy.models (see the package index for the ground-state, time-evolution and purification algorithms) | `pip install tenpy` FAILS - the PyPI name is physics-tenpy |
| general tensor networks (1D chains and 2D lattices), contraction, 2D simple / full update | quimb | quimb.tensor | tensor_1d, tensor_2d (2D lattice states, boundary contraction), tensor_2d_tebd (simple and full update), tensor_core | quimb has no corner-transfer environment class; its 2D environments are boundary contractions, memory-heavy at a large auxiliary dimension - for corner-transfer environments use the next row |
| infinite 2D tensor networks with corner-transfer environments: simple / full update, thermal states, fermions | NOT on PyPI: `git clone https://github.com/yastn/yastn && pip install --target /workspace/<uid>/lib ./yastn` (python >= 3.10) | yastn | the 2D and 1D tensor-network subpackages (unit cells, corner-transfer environments, evolution steps), yastn.Tensor with symmetries - see yastn.readthedocs.io | `pip install yastn` FAILS (404) - clone it |
| open quantum systems: master equations, quantum trajectories, steady states, superoperators | qutip | qutip | Qobj, mesolve, mcsolve, sesolve, steadystate, propagator | for a generator with features shorter than the integrator's internal step, integrate segment by segment or cap the step below the shortest feature; validate one noiseless case against an exact per-segment propagator before production |
| sparse linear algebra, exact diagonalisation, matrix exponentials | scipy (present) | scipy.sparse, scipy.sparse.linalg | eigsh, eigs, expm_multiply, LinearOperator | build Hamiltonians as scipy.sparse.kron products; for a piecewise-constant generator, expm_multiply per segment is an exact propagator with no step-size knob - the reference an integrator is validated against |
| exact diagonalisation of spin / fermion / boson lattices with symmetry sectors | quspin | quspin | quspin.basis (spin_basis_1d, spinful_fermion_basis_general), quspin.operators.hamiltonian | |
| variational / neural quantum states, Markov-chain and variational Monte Carlo | netket | netket | netket.hilbert, netket.operator, netket.sampler, netket.vqs, netket.driver | jax based, heavy install |
| statevector / density-matrix simulation of small circuits and spin models | numpy (present) | numpy | einsum / tensordot on reshaped state vectors | |
| self-consistent fixed points | scipy (present) | scipy.optimize | anderson, newton_krylov, broyden1 on the residual F(x) = Phi(x) - x | a root solver finds fixed points that damped iteration misses; to reach a symmetric branch, solve inside the symmetry sector, then seed it to test its stability |
| cross interpolation of high-dimensional functions (rank-revealing pivoting) | no verified package route on this farm | - | - | budget one short build test of a reference implementation (downloaded code is allowed) and record its output; otherwise hand-write the engine and test it against the function at every pivot and on held-out points |

A library that does not cover the paper's exact protocol still supplies the engine (tensors, environments, solvers); write only the paper-specific
layer on top. A hand-written engine is the fallback when no library covers the method - say so in profile.json notes.
Environment methods on infinite lattices: reuse the converged environment of the previous step as the start of the next instead of re-converging it
from scratch at every step; time one step at the largest required auxiliary dimension before committing a ladder.
