#!/usr/bin/env bash
# Run every offline test of infra/ (nothing leaves 127.0.0.1). Usage: bash infra/run_tests.sh
# Exit status: the number of failed parts (0 = all passed).
set -u
INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
fail=0
part() {   # name command...
  local name="$1"; shift
  echo "=== $name"
  if "$@"; then echo "--- $name: passed"; else echo "--- $name: FAILED"; fail=$((fail + 1)); fi
}

shell_syntax() {
  local f rc=0
  while IFS= read -r f; do
    case "$f" in
      *run_in_container.sh|*build_steps.sh) sh -n "$f" || { echo "syntax: $f"; rc=1; } ;;
      *) bash -n "$f" || { echo "syntax: $f"; rc=1; } ;;
    esac
  done < <(find "$INFRA" -type f \( -name '*.sh' -o -name '*.pbs' -o -name '*.slurm' \) | sort)
  return $rc
}

shellcheck_all() {
  if ! command -v shellcheck >/dev/null 2>&1; then echo "shellcheck not installed: skipped"; return 0; fi
  find "$INFRA" -type f \( -name '*.sh' -o -name '*.pbs' -o -name '*.slurm' \) -print0 | sort -z |
    xargs -0 shellcheck --severity=warning
}

part "shell syntax" shell_syntax
part "shellcheck" shellcheck_all
for d in proxy accounting judge run hpc; do
  part "unit tests: $d" "$PY" -m unittest discover -s "$INFRA/$d/tests" -p "test_*.py"
done
part "retry fault injection" bash "$INFRA/proxy/tests/test_retry.sh"
echo "=== infra tests: $fail part(s) failed"
exit "$fail"
