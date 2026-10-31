#!/usr/bin/env bash
# Judge a finished RA run with Claude Code against the task's JUDGE.md (strict: pass only with zero CRITICAL issues).
#
# Usage:
#   judge.sh --run DIR --judge-md FILE --paper PDF [--sm PDF] --paper-num N --out DIR --key-file FILE
#            [--key-kind api|oauth] [--model claude-opus-4-8] [--effort high] [--engine docker|apptainer|host]
#            [--image IMAGE] [--cpus 4] [--mem-gb 16] [--timeout-s 3600] [--allow-host HOST]... [--dry-run]
#
# Steps:
#   1. Copies the finished run to OUT/<run name>/workspace without earlier verdicts, without .dsh_ra3/ and without
#      the other items the paper's judge did not see (stage_run.py; the list goes to OUT/<run name>/staged_excluded.tsv).
#      Adds the task's JUDGE.md and your copy of the paper as paper_N.pdf (and paper_N_SM.pdf). The run's own ENV.md
#      is kept (the generic template is added when the run has none).
#   2. Runs Claude Code headless (claude -p) with the prompt in JUDGE_PROMPT.md, an equivalent of the paper's judge
#      prompt, not the original. Defaults as in the paper's judge job: model claude-opus-4-8, 4 CPUs, 16 GB, 1 hour.
#      - docker (default): the judge container sits on an internal Docker network; its one way out is an allow-list
#        proxy (infra/run/egress_proxy.py) that admits the model API host (default api.anthropic.com) only;
#      - apptainer: the same proxy runs on the host and the container is told to use it (HTTPS_PROXY); without
#        privileges Apptainer cannot block direct connections, so this isolation is weaker;
#      - host: no container. For tests only: the prompt names /workspace, which is not the staged copy on the host.
#   3. Copies the verdict to OUT/<run name>/judge_result.md, ending with an HTML comment that names the judge model,
#      the Claude Code version and the prompt's sha256; writes judge_meta.json; prints the strict score.
# The key (an Anthropic API key, or a Claude subscription token with --key-kind oauth) goes from the key file into a
# mode-600 environment file read by the container; it is never printed or put on a command line. The judge runs with
# --dangerously-skip-permissions inside the container: the run folder is untrusted, agent-written content, so keep
# the container. The original run folder is never changed. The image is built with:
#   docker build -f infra/container/Dockerfile --target judge -t residual-agents-judge:1.0.0 .
# Exit status: 0 a verdict was written and parsed; 1 no parseable verdict; 2 usage error.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python3}"

RUN="" JUDGE_MD="" PAPER="" SM="" NUM="" OUT="" KEY_FILE="" KEY_KIND="api"
MODEL="claude-opus-4-8" EFFORT="high" ENGINE="docker" IMAGE="" CPUS=4 MEM_GB=16 TIMEOUT_S=3600 DRY=0
ALLOW=()

usage() { sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
fail() { echo "judge: $*" >&2; exit 2; }
need() { [ "$2" -ge 2 ] || fail "$1 needs a value"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --run)        need "$1" $#; RUN="$2"; shift 2 ;;
    --judge-md)   need "$1" $#; JUDGE_MD="$2"; shift 2 ;;
    --paper)      need "$1" $#; PAPER="$2"; shift 2 ;;
    --sm)         need "$1" $#; SM="$2"; shift 2 ;;
    --paper-num)  need "$1" $#; NUM="$2"; shift 2 ;;
    --out)        need "$1" $#; OUT="$2"; shift 2 ;;
    --key-file)   need "$1" $#; KEY_FILE="$2"; shift 2 ;;
    --key-kind)   need "$1" $#; KEY_KIND="$2"; shift 2 ;;
    --model)      need "$1" $#; MODEL="$2"; shift 2 ;;
    --effort)     need "$1" $#; EFFORT="$2"; shift 2 ;;
    --engine)     need "$1" $#; ENGINE="$2"; shift 2 ;;
    --image)      need "$1" $#; IMAGE="$2"; shift 2 ;;
    --cpus)       need "$1" $#; CPUS="$2"; shift 2 ;;
    --mem-gb)     need "$1" $#; MEM_GB="$2"; shift 2 ;;
    --timeout-s)  need "$1" $#; TIMEOUT_S="$2"; shift 2 ;;
    --allow-host) need "$1" $#; ALLOW+=("$2"); shift 2 ;;
    --dry-run)    DRY=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) fail "unknown option $1 (see --help)" ;;
  esac
done

[ -d "$RUN" ] || fail "--run must be a finished run folder"
[ -f "$JUDGE_MD" ] || fail "--judge-md must be the task's JUDGE.md"
[ -f "$PAPER" ] || fail "--paper must be your copy of the source paper (PDF)"
[ -z "$SM" ] || [ -f "$SM" ] || fail "--sm file not found: $SM"
case "$NUM" in ''|*[!0-9]*) fail "--paper-num must be the task's number (digits)" ;; esac
[ -n "$OUT" ] || fail "--out is required"
case "$KEY_KIND" in api) KEYVAR=ANTHROPIC_API_KEY ;; oauth) KEYVAR=CLAUDE_CODE_OAUTH_TOKEN ;;
  *) fail "--key-kind must be api or oauth" ;; esac
[ "$DRY" = 1 ] || { [ -r "$KEY_FILE" ] && [ -s "$KEY_FILE" ]; } || fail "--key-file missing or empty"
case "$ENGINE" in
  docker)    IMAGE="${IMAGE:-residual-agents-judge:1.0.0}" ;;
  apptainer) [ -n "$IMAGE" ] || fail "--engine apptainer needs --image <judge .sif>" ;;
  host)      ;;
  *) fail "--engine must be docker, apptainer or host" ;;
esac
for v in "$CPUS" "$MEM_GB" "$TIMEOUT_S"; do case "$v" in ''|*[!0-9]*) fail "--cpus, --mem-gb and --timeout-s take integers" ;; esac; done
[ "${#ALLOW[@]}" -gt 0 ] || ALLOW=(api.anthropic.com)
[ -f "$REPO/infra/judge/JUDGE_PROMPT.md" ] || fail "JUDGE_PROMPT.md not found next to judge.sh"

RUN_ABS="$(cd "$RUN" && pwd)"
RID="$(basename "$RUN_ABS")"
mkdir -p "$OUT"
JOUT="$(cd "$OUT" && pwd)/$RID"
[ ! -e "$JOUT" ] || fail "$JOUT exists already: choose another --out or remove it"
mkdir -p "$JOUT"

# 1. stage the judged copy
"$PY" "$HERE/stage_run.py" "$RUN_ABS" "$JOUT/workspace" --excluded "$JOUT/staged_excluded.tsv"
cp "$JUDGE_MD" "$JOUT/workspace/JUDGE.md"
cp "$PAPER" "$JOUT/workspace/paper_${NUM}.pdf"
[ -z "$SM" ] || cp "$SM" "$JOUT/workspace/paper_${NUM}_SM.pdf"
[ -f "$JOUT/workspace/ENV.md" ] || cp "$REPO/infra/container/ENV.md.template" "$JOUT/workspace/ENV.md"
mkdir -p "$JOUT/home"

PROMPT="$(awk '/<!-- BEGIN PROMPT -->/{f=1; next} /<!-- END PROMPT -->/{f=0} f' "$HERE/JUDGE_PROMPT.md")"
[ -n "$PROMPT" ] || fail "no prompt between the markers of JUDGE_PROMPT.md"
PROMPT_SHA="$(printf '%s' "$PROMPT" | sha256sum | cut -d' ' -f1)"
CLAUDE=("${RA_JUDGE_CLAUDE:-claude}" -p "$PROMPT" --model "$MODEL" --dangerously-skip-permissions
        --output-format stream-json --verbose)
[ -z "$EFFORT" ] || CLAUDE+=(--effort "$EFFORT")
ALLOW_ARGS=()
for h in "${ALLOW[@]}"; do ALLOW_ARGS+=(--allow "$h"); done

ENVF="$JOUT/.judge.env"
NET="" EPID=""
cleanup() {
  rm -f "$ENVF"
  if [ -n "$NET" ]; then
    docker rm -f "$NET-judge" "$NET-egress" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
  fi
  if [ -n "$EPID" ]; then kill "$EPID" 2>/dev/null || true; fi
}
trap cleanup EXIT
write_env() {   # the key goes from its file into the env file; it never passes through a shell variable
  ( umask 077
    { printf '%s=' "$KEYVAR"; tr -d '\r\n' < "$KEY_FILE"; printf '\n'
      printf 'DISABLE_AUTOUPDATER=1\nDISABLE_TELEMETRY=1\nDISABLE_ERROR_REPORTING=1\n'
      printf 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\nLANG=C.UTF-8\n'
      [ -z "${1:-}" ] || printf 'HTTPS_PROXY=%s\nHTTP_PROXY=%s\nhttps_proxy=%s\nhttp_proxy=%s\n' "$1" "$1" "$1" "$1"
    } > "$ENVF" )
}
show() {        # print a command, with the long prompt shown as a short label
  local a out=()
  for a in "$@"; do
    if [ "$a" = "$PROMPT" ]; then out+=("<prompt of JUDGE_PROMPT.md>"); else out+=("$a"); fi
  done
  printf ' '
  printf ' %q' "${out[@]}"
  echo
}

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CLI_VERSION="" RC=0
case "$ENGINE" in
  docker)
    NET="ra-judge-$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')"
    RUNCMD=(docker run --rm --init --name "$NET-judge" --network "$NET" --cpus "$CPUS" --memory "${MEM_GB}g"
            --user "$(id -u):$(id -g)" --security-opt no-new-privileges
            -v "$JOUT/workspace:/workspace" -v "$JOUT/home:/home/ra" -e HOME=/home/ra --env-file "$ENVF" -w /workspace
            "$IMAGE" timeout -s TERM -k 60 "$TIMEOUT_S" "${CLAUDE[@]}")
    if [ "$DRY" = 1 ]; then
      echo "judge: dry run; staged $JOUT/workspace; commands:"
      show docker network create --internal "$NET"
      show docker run -d --rm --name "$NET-egress" "$IMAGE" python3 -I -u /opt/ra/infra/run/egress_proxy.py \
           --listen 0.0.0.0:3128 "${ALLOW_ARGS[@]}"
      show docker network connect "$NET" "$NET-egress"
      show "${RUNCMD[@]}"
      NET=""
      exit 0
    fi
    docker network create --internal "$NET" >/dev/null
    docker run -d --rm --name "$NET-egress" "$IMAGE" python3 -I -u /opt/ra/infra/run/egress_proxy.py \
        --listen 0.0.0.0:3128 "${ALLOW_ARGS[@]}" >/dev/null
    docker network connect "$NET" "$NET-egress"
    write_env "http://$NET-egress:3128"
    CLI_VERSION="$(docker run --rm --network none "$IMAGE" claude --version 2>/dev/null | head -1 || true)"
    set +e
    "${RUNCMD[@]}" > "$JOUT/judge_transcript.jsonl" 2> "$JOUT/judge_stderr.log"
    RC=$?
    set -e
    ;;
  apptainer)
    EPORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    mkdir -p "$JOUT/tmp"
    RUNCMD=(apptainer exec --containall --cleanenv --home "$JOUT/home:/home/ra" --workdir "$JOUT/tmp"
            --bind "$JOUT/workspace:/workspace" --pwd /workspace --env-file "$ENVF"
            "$IMAGE" timeout -s TERM -k 60 "$TIMEOUT_S" "${CLAUDE[@]}")
    if [ "$DRY" = 1 ]; then
      echo "judge: dry run; staged $JOUT/workspace; commands:"
      show "$PY" -I -u "$REPO/infra/run/egress_proxy.py" --listen "127.0.0.1:$EPORT" "${ALLOW_ARGS[@]}"
      show "${RUNCMD[@]}"
      exit 0
    fi
    "$PY" -I -u "$REPO/infra/run/egress_proxy.py" --listen "127.0.0.1:$EPORT" "${ALLOW_ARGS[@]}" \
        > "$JOUT/egress.log" 2>&1 < /dev/null &
    EPID=$!
    write_env "http://127.0.0.1:$EPORT"
    CLI_VERSION="$(apptainer exec --containall --cleanenv "$IMAGE" claude --version 2>/dev/null | head -1 || true)"
    set +e
    "${RUNCMD[@]}" > "$JOUT/judge_transcript.jsonl" 2> "$JOUT/judge_stderr.log"
    RC=$?
    set -e
    ;;
  host)
    echo "judge: WARNING: --engine host runs the judge without a container (tests only)" >&2
    if [ "$DRY" = 1 ]; then echo "judge: dry run; staged $JOUT/workspace"; show "${CLAUDE[@]}"; exit 0; fi
    write_env ""
    CLI_VERSION="$("${CLAUDE[0]}" --version 2>/dev/null | head -1 || true)"
    set +e
    ( cd "$JOUT/workspace"
      while IFS= read -r line; do export "${line%%=*}=${line#*=}"; done < "$ENVF"
      export HOME="$JOUT/home"
      exec timeout -s TERM -k 60 "$TIMEOUT_S" "${CLAUDE[@]}" ) > "$JOUT/judge_transcript.jsonl" 2> "$JOUT/judge_stderr.log"
    RC=$?
    set -e
    ;;
esac
ENDED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
rm -f "$ENVF"

# 3. the verdict, its provenance and the score
if [ -f "$JOUT/workspace/judge_result.md" ]; then
  cp "$JOUT/workspace/judge_result.md" "$JOUT/judge_result.md"
  printf '\n<!-- judge: model=%s cli=%s effort=%s engine=%s prompt=infra/judge/JUDGE_PROMPT.md (an equivalent, not the original) prompt_sha256=%s judged_utc=%s -->\n' \
    "$MODEL" "${CLI_VERSION:-unknown}" "${EFFORT:-default}" "$ENGINE" "$PROMPT_SHA" "$ENDED" >> "$JOUT/judge_result.md"
fi
"$PY" - "$JOUT" "$RUN_ABS" "$JUDGE_MD" "$PAPER" "$MODEL" "${CLI_VERSION:-}" "$EFFORT" "$ENGINE" "${IMAGE:-}" \
      "$PROMPT_SHA" "$STARTED" "$ENDED" "$RC" "$TIMEOUT_S" "$CPUS" "$MEM_GB" "${ALLOW[*]}" "$HERE/parse_score.py" <<'PY'
import hashlib, json, os, sys
(jout, run, judge_md, paper, model, cli, effort, engine, image, psha, started, ended, rc, timeout_s, cpus, mem,
 allow, parser) = sys.argv[1:]
sys.path.insert(0, os.path.dirname(parser))
import parse_score
def digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()
verdict = os.path.join(jout, "judge_result.md")
score = parse_score.score_of_file(verdict) if os.path.exists(verdict) else None
meta = {"run": os.path.basename(run), "judge_model": model, "judge_cli": cli or None, "effort": effort or None,
        "engine": engine, "image": image or None, "prompt_file": "infra/judge/JUDGE_PROMPT.md",
        "prompt_is_original": False, "prompt_sha256": psha, "criteria_md5": digest(judge_md, "md5"),
        "paper_sha256": digest(paper, "sha256"), "started_utc": started, "ended_utc": ended,
        "exit_code": int(rc), "timeout_s": int(timeout_s), "cpus": int(cpus), "mem_gb": int(mem),
        "allowed_hosts": allow.split(), "score": score, "strict_pass": score == 1.0,
        "verdict_file": "judge_result.md" if os.path.exists(verdict) else None}
with open(os.path.join(jout, "judge_meta.json"), "w") as f:
    json.dump(meta, f, indent=2)
    f.write("\n")
PY
if [ ! -f "$JOUT/judge_result.md" ]; then
  echo "judge: no verdict written (exit $RC); see $JOUT/judge_stderr.log" >&2
  exit 1
fi
set +e
"$PY" "$HERE/parse_score.py" "$JOUT/judge_result.md"
S=$?
set -e
[ "$S" -le 1 ] || { echo "judge: the verdict has no readable score" >&2; exit 1; }
exit 0
