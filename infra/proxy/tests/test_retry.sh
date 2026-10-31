#!/usr/bin/env bash
# Fault-injection test of the proxy's retry policy (ra_proxy.py + gwretry.py) against mock_upstream.py.
# Offline: everything listens on 127.0.0.1. Fast backoff via GW_RETRY_*. Exit status 1 when a check fails.
# Usage: bash infra/proxy/tests/test_retry.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY="$HERE/../ra_proxy.py"
MOCK="$HERE/mock_upstream.py"
PY="${PYTHON:-python3}"
export GW_RETRY_BASE=0.05 GW_RETRY_CAP=0.3 GW_RETRY_BUDGET=60

T="$(mktemp -d "${TMPDIR:-/tmp}/ra_retry_test.XXXXXX")"
PIDS=()
cleanup() {
  local p
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
  case "$T" in "${TMPDIR:-/tmp}"/ra_retry_test.*) rm -rf -- "$T" ;; esac
}
trap cleanup EXIT
printf 'testkey-42\n' > "$T/key"

free_port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }

wait_health() {   # port
  "$PY" - "$1" <<'PY'
import sys, time, urllib.request
for _ in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:%s/health" % sys.argv[1], timeout=1); sys.exit(0)
    except Exception:
        time.sleep(0.1)
sys.exit(1)
PY
}

call() {   # port stream(0|1) -> one line: "JSON <status> <content>" | "STREAM <status> <n> data lines" | "HTTPERR <code>"
  "$PY" - "$1" "$2" <<'PY'
import sys, json, urllib.request, urllib.error
port, stream = sys.argv[1], sys.argv[2] == "1"
body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
req = urllib.request.Request("http://127.0.0.1:%s/v1/chat/completions" % port, data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json", "Authorization": "Bearer shim-local"})
try:
    r = urllib.request.urlopen(req, timeout=60)
    if stream:
        print("STREAM", r.status, sum(1 for l in r if l.startswith(b"data:")), "data lines")
    else:
        print("JSON", r.status, json.loads(r.read())["choices"][0]["message"]["content"])
except urllib.error.HTTPError as e:
    print("HTTPERR", e.code)
except Exception as e:
    print("ERR", type(e).__name__)
PY
}

FAIL=0
run_case() {   # name seq stream expected_result expected_retries [ENV=VALUE ...]
  local name="$1" seq="$2" stream="$3" want="$4" want_retries="$5"; shift 5
  local mp pp got retries
  mp="$(free_port)"; pp="$(free_port)"
  MOCK_PORT="$mp" MOCK_SEQ="$seq" "$PY" "$MOCK" 2>"$T/mock.log" & PIDS+=($!)
  env "$@" RA_PROXY_UPSTREAM="http://127.0.0.1:$mp/v1" RA_PROXY_KEY_FILE="$T/key" RA_PROXY_PORT="$pp" \
      RA_PROXY_USAGE_LOG="$T/usage.jsonl" "$PY" -I -u "$PROXY" 2>"$T/proxy.log" & PIDS+=($!)
  if ! wait_health "$pp"; then echo "FAIL $name: proxy did not start"; FAIL=1; return; fi
  got="$(call "$pp" "$stream")"
  retries="$(grep -c '^\[.*\] \[gwretry\].*-> retry in' "$T/proxy.log")"
  kill "${PIDS[-1]}" "${PIDS[-2]}" 2>/dev/null; wait "${PIDS[-1]}" "${PIDS[-2]}" 2>/dev/null
  if [ "$got" = "$want" ] && [ "$retries" = "$want_retries" ]; then
    echo "PASS $name: $got, $retries retries"
  else
    echo "FAIL $name: got '$got' with $retries retries, want '$want' with $want_retries"; FAIL=1
  fi
}

run_case "502 then ok"                  "502,ok"            0 "JSON 200 ok"               1
run_case "content-inspection 400"      "inspect,ok"        0 "JSON 200 ok"               1
run_case "dropped connection, stream"  "drop,oksse"        1 "STREAM 200 5 data lines"   1
run_case "plain 400 passed through"    "bad400"            0 "HTTPERR 400"               0
run_case "502, drop, 429, ok"          "502,drop,429,ok"   0 "JSON 200 ok"               3
run_case "permanent 502"               "perm502,ok"        0 "HTTPERR 502"               0
run_case "retries exhausted"           "502,502,502,502"   0 "HTTPERR 502"               2   GW_RETRY_MAX=3
run_case "retry disabled"              "502,ok"            0 "HTTPERR 502"               0   GW_RETRY=0

if [ "$FAIL" = 0 ]; then echo "ALL PASS"; else echo "SOME FAILED"; fi
exit "$FAIL"
