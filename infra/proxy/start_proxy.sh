#!/usr/bin/env bash
# Start the RA model proxy (ra_proxy.py) on a free local port and print the port.
#
# Usage:
#   start_proxy.sh --state-dir DIR [--upstream URL] [--key-file FILE] [--model-force ID] [--provider SLUG]
#                  [--expect-model ID] [--inject-ids 0|1] [--rewrite SPEC] [--usage-log FILE] [--timeout S]
#                  [--infra-flag FILE] [--bind ADDR] [--port-range LO-HI] [--tries N] [--python PATH]
#   Each option falls back to the RA_PROXY_* variable of the same meaning (RA_PROXY_UPSTREAM, RA_PROXY_KEY_FILE,
#   RA_PROXY_MODEL_FORCE, RA_PROXY_PROVIDER, RA_PROXY_EXPECT_MODEL, RA_PROXY_INJECT_IDS, RA_PROXY_REWRITE,
#   RA_PROXY_USAGE_LOG, RA_PROXY_TIMEOUT, RA_PROXY_BIND, RA_PROXY_PORT_RANGE, RA_PROXY_PYTHON); GW_RETRY_* are passed on.
#
# What it does:
#   - checks that the key file exists and is not empty, without reading it: the proxy reads the key itself, so the
#     key never enters this shell, an environment variable or a command line;
#   - copies the interpreter to DIR/.ra_proxy_bin, a name without "python", so that an agent's `pkill -f python3`
#     cannot hit the proxy, and starts the proxy with a clean environment that holds only its own settings;
#   - picks a random port in the range (default 8900-9199), skips ports that already answer, and accepts the proxy
#     only when its /health answer carries this launch's nonce, so a neighbour's proxy is never adopted; up to N tries
#     (default 6);
#   - on success prints the port on stdout (nothing else) and writes DIR/port, DIR/pid and DIR/nonce; the proxy keeps
#     running in the background; stop it with: kill "$(cat DIR/pid)";
#   - on failure prints the end of DIR/proxy.log on stderr, touches the --infra-flag file (when given) and exits 66.
# The proxy's log is DIR/proxy.log. Keep DIR and the usage log outside the run folder.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_PY="$HERE/ra_proxy.py"

STATE=""
UPSTREAM="${RA_PROXY_UPSTREAM:-}"
KEY_FILE="${RA_PROXY_KEY_FILE:-}"
MODEL_FORCE="${RA_PROXY_MODEL_FORCE:-}"
PROVIDER="${RA_PROXY_PROVIDER:-}"
EXPECT="${RA_PROXY_EXPECT_MODEL:-}"
INJECT="${RA_PROXY_INJECT_IDS:-0}"
REWRITE="${RA_PROXY_REWRITE:-}"
USAGE_LOG="${RA_PROXY_USAGE_LOG:-}"
TIMEOUT="${RA_PROXY_TIMEOUT:-}"
BIND="${RA_PROXY_BIND:-127.0.0.1}"
RANGE="${RA_PROXY_PORT_RANGE:-8900-9199}"
PYBIN="${RA_PROXY_PYTHON:-python3}"
TRIES=6
INFRA_FLAG=""

usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
need() { [ "$2" -ge 2 ] || { echo "start_proxy: $1 needs a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --state-dir)    need "$1" $#; STATE="$2"; shift 2 ;;
    --upstream)     need "$1" $#; UPSTREAM="$2"; shift 2 ;;
    --key-file)     need "$1" $#; KEY_FILE="$2"; shift 2 ;;
    --model-force)  need "$1" $#; MODEL_FORCE="$2"; shift 2 ;;
    --provider)     need "$1" $#; PROVIDER="$2"; shift 2 ;;
    --expect-model) need "$1" $#; EXPECT="$2"; shift 2 ;;
    --inject-ids)   need "$1" $#; INJECT="$2"; shift 2 ;;
    --rewrite)      need "$1" $#; REWRITE="$2"; shift 2 ;;
    --usage-log)    need "$1" $#; USAGE_LOG="$2"; shift 2 ;;
    --timeout)      need "$1" $#; TIMEOUT="$2"; shift 2 ;;
    --infra-flag)   need "$1" $#; INFRA_FLAG="$2"; shift 2 ;;
    --bind)         need "$1" $#; BIND="$2"; shift 2 ;;
    --port-range)   need "$1" $#; RANGE="$2"; shift 2 ;;
    --tries)        need "$1" $#; TRIES="$2"; shift 2 ;;
    --python)       need "$1" $#; PYBIN="$2"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "start_proxy: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done

die() {
  echo "start_proxy: FATAL: $*" >&2
  if [ -n "$INFRA_FLAG" ]; then touch "$INFRA_FLAG" 2>/dev/null; fi
  exit 66
}

[ -n "$STATE" ] || { echo "start_proxy: --state-dir is required" >&2; exit 2; }
[ -n "$UPSTREAM" ] || die "no upstream (--upstream or RA_PROXY_UPSTREAM): the base URL of an OpenAI-compatible endpoint"
[ -n "$KEY_FILE" ] || die "no key file (--key-file or RA_PROXY_KEY_FILE)"
[ -r "$KEY_FILE" ] && [ -s "$KEY_FILE" ] || die "key file missing, unreadable or empty: $KEY_FILE"
[ -f "$PROXY_PY" ] || die "proxy missing at $PROXY_PY"
case "$INJECT" in 0|1) ;; *) die "--inject-ids must be 0 or 1 (got '$INJECT')" ;; esac
case "$TRIES" in ''|*[!0-9]*) die "--tries must be a number" ;; esac
LO="${RANGE%-*}"; HI="${RANGE#*-}"
case "$LO$HI" in ''|*[!0-9]*) die "--port-range must be LO-HI (got '$RANGE')" ;; esac
[ "$LO" -ge 1 ] && [ "$HI" -le 65535 ] && [ "$LO" -le "$HI" ] || die "--port-range out of bounds: $RANGE"

PY="$(command -v "$PYBIN" 2>/dev/null)" || die "no Python interpreter: $PYBIN"
mkdir -p "$STATE" || die "cannot create $STATE"
chmod 700 "$STATE" 2>/dev/null
STATE="$(cd "$STATE" && pwd)"
KEY_ABS="$(cd "$(dirname "$KEY_FILE")" && pwd)/$(basename "$KEY_FILE")"
LOG="$STATE/proxy.log"

# renamed interpreter: the process name and command line carry no "python"
BIN="$STATE/.ra_proxy_bin"
if cp "$PY" "$BIN" 2>/dev/null && chmod 700 "$BIN" && "$BIN" -I -c "import http.server, json, urllib.request" 2>/dev/null; then
  :
else
  echo "start_proxy: WARNING: the copied interpreter does not run here; starting the proxy as $PY" >&2
  BIN="$PY"
fi
case "$BIN $PROXY_PY" in
  *python*) echo "start_proxy: WARNING: the proxy's command line contains 'python' ($BIN $PROXY_PY): an agent's" \
                 "'pkill -f python' would stop it; move the repository or the state dir to a path without 'python'" >&2 ;;
esac

CHECK_HOST="$BIND"
case "$BIND" in 0.0.0.0|"") CHECK_HOST=127.0.0.1 ;; ::) CHECK_HOST=::1 ;; esac
port_answers() {   # something already listens on the port
  "$PY" -I -c 'import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1).close()
except Exception:
    sys.exit(1)' "$CHECK_HOST" "$1"
}
nonce_ok() {       # /health answers with this launch's nonce
  "$PY" -I -c 'import json, sys, urllib.request
try:
    h = json.loads(urllib.request.urlopen("http://%s:%s/health" % (sys.argv[1], sys.argv[2]), timeout=2).read().decode())
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(h, dict) and h.get("nonce") == sys.argv[3] else 1)' "$CHECK_HOST" "$1" "$2"
}

RETRY_ENV=()
for v in GW_RETRY GW_RETRY_MAX GW_RETRY_BASE GW_RETRY_CAP GW_RETRY_BUDGET GW_RETRY_INSPECT; do
  if [ -n "${!v:-}" ]; then RETRY_ENV+=("$v=${!v}"); fi
done

ok="" port="" pid="" nonce=""
for ((try = 1; try <= TRIES; try++)); do
  r="$(od -An -N2 -tu2 /dev/urandom 2>/dev/null | tr -d ' ')"
  port=$(( LO + ${r:-$RANDOM} % (HI - LO + 1) ))
  if port_answers "$port"; then
    echo "start_proxy: port $port is in use, trying another" >&2
    continue
  fi
  nonce="$$-$try-$RANDOM-$(od -An -N8 -tx8 /dev/urandom 2>/dev/null | tr -d ' ')"
  env -i PATH="/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 \
      RA_PROXY_UPSTREAM="$UPSTREAM" RA_PROXY_KEY_FILE="$KEY_ABS" RA_PROXY_PORT="$port" RA_PROXY_BIND="$BIND" \
      RA_PROXY_NONCE="$nonce" RA_PROXY_MODEL_FORCE="$MODEL_FORCE" RA_PROXY_PROVIDER="$PROVIDER" \
      RA_PROXY_EXPECT_MODEL="$EXPECT" RA_PROXY_INJECT_IDS="$INJECT" RA_PROXY_REWRITE="$REWRITE" \
      RA_PROXY_USAGE_LOG="$USAGE_LOG" ${TIMEOUT:+RA_PROXY_TIMEOUT="$TIMEOUT"} ${RETRY_ENV[@]+"${RETRY_ENV[@]}"} \
      "$BIN" -I -u "$PROXY_PY" >>"$LOG" 2>&1 </dev/null &
  pid=$!
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
    sleep 0.5
    if nonce_ok "$port" "$nonce"; then ok=1; break; fi
    kill -0 "$pid" 2>/dev/null || break      # the proxy died (port taken in a race, bad settings): next port
  done
  [ -n "$ok" ] && break
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
done

if [ -z "$ok" ]; then
  tail -5 "$LOG" >&2 2>/dev/null
  die "proxy not answering after $TRIES port attempts in $LO-$HI"
fi
printf '%s\n' "$port" > "$STATE/port"
printf '%s\n' "$pid" > "$STATE/pid"
printf '%s\n' "$nonce" > "$STATE/nonce"
echo "start_proxy: proxy up: pid=$pid port=$port log=$LOG" >&2
printf '%s\n' "$port"
