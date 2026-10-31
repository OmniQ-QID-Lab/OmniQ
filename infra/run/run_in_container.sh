#!/bin/sh
# Entry point of the run container: one Residual Agents episode in /workspace.
# Usage (inside the container): run_in_container.sh [--profile paper|smoke|none]
# The launcher (run_task.sh, compose.yaml or an HPC template) mounts the run folder at /workspace and a store folder
# at /pece_store, and passes only the variables the orchestrator reads. `ra run` applies the profile to the variables
# that are still unset, runs the preflight checks (fatal on error), prints the effective configuration and starts the
# orchestrator under a renamed copy of the interpreter with -X utf8.
# Exit status: the orchestrator's (0 normal end, 2 configuration error, 3 infrastructure failure).
set -eu
PROFILE=paper
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)
      [ $# -ge 2 ] || { echo "run_in_container: --profile needs a value" >&2; exit 2; }
      PROFILE="$2"; shift 2 ;;
    --profile=*) PROFILE="${1#*=}"; shift ;;
    *) echo "usage: run_in_container.sh [--profile paper|smoke|none]" >&2; exit 2 ;;
  esac
done
case "$PROFILE" in paper|smoke|none) ;; *) echo "run_in_container: unknown profile '$PROFILE'" >&2; exit 2 ;; esac
cd /workspace || exit 2
exec ra run --profile "$PROFILE"
