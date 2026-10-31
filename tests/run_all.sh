#!/bin/bash
# The offline test suite: no model, no key, no network.
#
#   bash tests/run_all.sh                  every test file, then every end-to-end scenario, each in its own process
#   bash tests/run_all.sh NAME [NAME ...]  only these end-to-end scenarios ("python3 tests/test_e2e.py --list" prints the
#                                          names; --scout stands for the scenarios that run scouts or panel critics in parallel)
#
# Environment:
#   PYTHON           the interpreter (default: python3)
#   RA_TEST_KEEP     1 = keep every temporary workspace; failed = keep those of failed scenarios (default: remove them)
#   RA_TEST_REPEAT   run the selection N times in a row (default: 1)
#   RA_TEST_TRAJECTORIES, RA_TEST_ORCH: optional checks and targets (see the test files)
#   RA_TEST_NO_NET   1 = run the suite in its own network namespace with the loopback interface only (unshare and ip from
#                    util-linux and iproute2; unprivileged user namespaces must be allowed). Without it, a dead proxy
#                    (http_proxy=http://127.0.0.1:9 ...) is the cheaper check. Either way, the unit, end-to-end and trace
#                    tests record every connection the orchestrator attempts and require none.
# Output: build/test-output/suite.out (suite_part.out when scenario names are given); the temporary workspaces go to
# build/test-output/tmp. Run one suite per checkout at a time. Exit status: 0 when every file and scenario ran to its end
# with 0 failed checks, 1 otherwise.
set -u
if [ "${RA_TEST_NO_NET:-}" = "1" ] && [ "${RA_TEST_IN_NETNS:-}" != "1" ]; then
  export RA_TEST_IN_NETNS=1
  # outer namespace: mapped root, only to bring the loopback interface up; inner namespace: the caller's own uid, no capabilities
  exec unshare --map-root-user --net bash -c 'ip link set lo up || exit 1; exec unshare --map-user="$0" --map-group="$1" bash "$2" "${@:3}"' \
    "$(id -u)" "$(id -g)" "$0" "$@"
fi
cd "$(dirname "$0")/.." || exit 1
PY=${PYTHON:-python3}
OUT_DIR=build/test-output
mkdir -p "$OUT_DIR/tmp" || exit 1
export TMPDIR="$PWD/$OUT_DIR/tmp"
export PYTHONDONTWRITEBYTECODE=1
REPEAT=${RA_TEST_REPEAT:-1}
FILES=""
if [ $# -eq 0 ]; then
  OUT=$OUT_DIR/suite.out
  FILES="test_units test_behaviour test_provenance test_profiles test_cli test_trace"
  SCENS=$("$PY" tests/test_e2e.py --list) || exit 1
else
  OUT=$OUT_DIR/suite_part.out
  SCENS=""
  for a in "$@"; do
    if [ "$a" = "--scout" ]; then
      SCENS="$SCENS $("$PY" tests/test_e2e.py --list-scout)" || exit 1
    else
      SCENS="$SCENS $a"
    fi
  done
fi
: > "$OUT"
T0=$(date +%s)
echo "=== suite start $(date '+%H:%M:%S'), Python $("$PY" -c 'import sys; print(sys.version.split()[0])'), $REPEAT round(s)" >> "$OUT"
if [ "${RA_TEST_IN_NETNS:-}" = "1" ]; then
  echo "=== network: own namespace; interfaces:$(ip -o link show | cut -d: -f2 | tr '\n' ' ')routes: $(ip route show | wc -l); uid $(id -u)" >> "$OUT"
else
  echo "=== network: the host's (set RA_TEST_NO_NET=1 for a namespace without it)" >> "$OUT"
fi

run_one() {   # label, timeout in seconds, command ...
  local label=$1 limit=$2 t rc
  shift 2
  t=$(date +%s)
  echo "=== $label $(date +%H:%M:%S)" >> "$OUT"
  timeout "$limit" "$@" >> "$OUT" 2>&1
  rc=$?
  echo "@@ end $label rc=$rc $(( $(date +%s) - t ))s" >> "$OUT"
}

for r in $(seq 1 "$REPEAT"); do
  if [ "$REPEAT" -gt 1 ]; then echo "=== round $r of $REPEAT $(date +%H:%M:%S)" >> "$OUT"; fi
  for f in $FILES; do
    if [ -f "tests/$f.py" ]; then run_one "$f" 900 "$PY" "tests/$f.py"; fi
  done
  for s in $SCENS; do
    run_one "e2e $s" 400 "$PY" tests/test_e2e.py "$s"
  done
done

# every test removes its own temporary folders unless RA_TEST_KEEP is set; what is left here (a crashed test) is only counted
LEFT=$(find "$TMPDIR" -mindepth 1 -maxdepth 1 | wc -l)
N_OK=$(grep -c '^OK ' "$OUT")
N_FAIL=$(grep -c '^FAIL ' "$OUT")
N_SKIP=$(grep -c '^SKIP ' "$OUT")
N_RUNS=$(grep -c '^@@ end ' "$OUT")
BAD=$(grep -E '^@@ end .* rc=[1-9][0-9]* [0-9]+s$' "$OUT")
{
  echo "=== DONE $(date +%H:%M:%S) after $(( $(date +%s) - T0 ))s"
  grep -E '^FAIL ' "$OUT" | cut -c1-300
  [ -n "$BAD" ] && echo "$BAD" | sed 's/^@@ end /did not end cleanly: /'
  echo "summary: $N_RUNS process(es), $N_OK check(s) passed, $N_FAIL failed, $N_SKIP skipped; $LEFT temporary folder(s) left by the tests; output $OUT"
} | tee -a "$OUT"
[ "$N_FAIL" -eq 0 ] && [ -z "$BAD" ]
