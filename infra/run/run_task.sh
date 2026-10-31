#!/usr/bin/env bash
# Run Residual Agents on one task in a container, the way the paper's runs were launched.
#
# Usage:
#   run_task.sh --task DIR [--paper PDF] [--sm PDF] --paper-num N --label L --out DIR
#               [--ncpus 16] [--mem-gb 64] [--gpu] [--gpu-index K] [--gpu-facts "NAME, MEM MiB"] [--cpuset LIST]
#               [--wall-hours 24] [--profile paper|smoke] [--engine docker|apptainer] [--image IMAGE]
#               [--model ID] [--plan-only] [--env NAME=VALUE]... [--dry-run]
#     proxy mode (the default; the route of the paper's runs; the key stays in the proxy process):
#               --upstream URL --key-file FILE [--model-force ID] [--provider SLUG] [--expect-model ID]
#               [--inject-ids 0|1] [--rewrite SPEC]
#     direct mode (dsh talks to the endpoint itself; the agents' tools can read the key):
#               --direct --key-file FILE [--base-url URL]
#
# --task is a folder with the task's CLAUDE.md (and skills/ when the task has one). A folder that contains a JUDGE.md
# anywhere is refused: the criteria must never be visible during a run. --paper is your own copy of the source paper;
# it is staged as paper_N.pdf (and --sm as paper_N_SM.pdf). Every OmniQ task needs it; leave it out only for a task
# without a source paper, such as examples/toy_task.
#
# Layout, under OUT/paper_N/:
#   L_pN/        the run folder, mounted at /workspace: CLAUDE.md, paper_N.pdf (+ _SM), skills/, ENV.md (from
#                infra/container/ENV.md.template, with this run's cores and memory in the allocation line), then
#                everything the run writes; run_meta.json (the launch settings) is added after the run
#   L_pN.aux/    outside the run folder: store/ (mounted at /pece_store), home/ (the agents' home, empty at start),
#                proxy/ (state, proxy.log, usage.jsonl), console.log, run_meta.json
# The container gets only the variables the orchestrator reads: PAPER_NUM, RUN_ID=L_pN, WALLTIME_SEC (wall - 600 s),
# PECE_NCPUS, PECE_MEM_GB, HOST_WORK_DIR, RA_DSH_SHIM and DSH_SHIM_PORT (proxy mode) or DEEPSEEK_BASE_URL and
# DEEPSEEK_API_KEY (direct mode), and, when given, MODEL, RA3SR_GPU_FACTS, PECE_PLAN_ONLY and --env entries (names
# starting with RA3_, RA3SR_ or PECE_). `ra run --profile P` inside the container fills in the rest (paper profile:
# the model, vision on, the paper's rate card).
# Isolation: docker runs with its own PID namespace, no host home (the home is a fresh folder), --cpus/--memory, the
# host network (the proxy listens on the host's 127.0.0.1) and --init; apptainer runs with --containall --cleanenv
# --pid and a fresh home. Agents run with full shell permissions inside the container.
# A hard stop is applied at the wall (timeout); the orchestrator plans to finish 600 s earlier.
# Exit status: the orchestrator's (0 normal end, 2 configuration error, 3 infrastructure failure); 3 also when the
# proxy cannot start; 124 when the hard stop fired; 64 for a usage error.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python3}"

TASK="" PAPER="" SM="" NUM="" LABEL="" OUT=""
NCPUS=16 MEM_GB=64 GPU=0 GPU_INDEX="" GPU_FACTS="" CPUSET="" WALL_H="" PROFILE=paper ENGINE=docker IMAGE=""
MODEL="" PLAN_ONLY=0 DRY=0 MODE=proxy
UPSTREAM="${RA_PROXY_UPSTREAM:-}" KEY_FILE="${RA_PROXY_KEY_FILE:-}" MODEL_FORCE="" PROVIDER="" EXPECT="" INJECT=1
REWRITE="" BASE_URL="https://api.deepseek.com"
EXTRA_ENV=()

usage() { sed -n '2,/^set -euo/{/^set -euo/!p}' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "run_task: $*" >&2; exit 64; }
need() { [ "$2" -ge 2 ] || die "$1 needs a value"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --task)         need "$1" $#; TASK="$2"; shift 2 ;;
    --paper)        need "$1" $#; PAPER="$2"; shift 2 ;;
    --sm)           need "$1" $#; SM="$2"; shift 2 ;;
    --paper-num)    need "$1" $#; NUM="$2"; shift 2 ;;
    --label)        need "$1" $#; LABEL="$2"; shift 2 ;;
    --out)          need "$1" $#; OUT="$2"; shift 2 ;;
    --ncpus)        need "$1" $#; NCPUS="$2"; shift 2 ;;
    --mem-gb)       need "$1" $#; MEM_GB="$2"; shift 2 ;;
    --gpu)          GPU=1; shift ;;
    --gpu-index)    need "$1" $#; GPU=1; GPU_INDEX="$2"; shift 2 ;;
    --gpu-facts)    need "$1" $#; GPU_FACTS="$2"; shift 2 ;;
    --cpuset)       need "$1" $#; CPUSET="$2"; shift 2 ;;
    --wall-hours)   need "$1" $#; WALL_H="$2"; shift 2 ;;
    --profile)      need "$1" $#; PROFILE="$2"; shift 2 ;;
    --engine)       need "$1" $#; ENGINE="$2"; shift 2 ;;
    --image)        need "$1" $#; IMAGE="$2"; shift 2 ;;
    --model)        need "$1" $#; MODEL="$2"; shift 2 ;;
    --plan-only)    PLAN_ONLY=1; shift ;;
    --env)          need "$1" $#; EXTRA_ENV+=("$2"); shift 2 ;;
    --upstream)     need "$1" $#; UPSTREAM="$2"; shift 2 ;;
    --key-file)     need "$1" $#; KEY_FILE="$2"; shift 2 ;;
    --model-force)  need "$1" $#; MODEL_FORCE="$2"; shift 2 ;;
    --provider)     need "$1" $#; PROVIDER="$2"; shift 2 ;;
    --expect-model) need "$1" $#; EXPECT="$2"; shift 2 ;;
    --inject-ids)   need "$1" $#; INJECT="$2"; shift 2 ;;
    --rewrite)      need "$1" $#; REWRITE="$2"; shift 2 ;;
    --direct)       MODE=direct; shift ;;
    --base-url)     need "$1" $#; BASE_URL="$2"; shift 2 ;;
    --dry-run)      DRY=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------- checks
[ -n "$TASK" ] && [ -d "$TASK" ] || die "--task must be a folder holding the task's CLAUDE.md"
[ -s "$TASK/CLAUDE.md" ] || die "no CLAUDE.md in $TASK"
JM="$(find "$TASK" -iname 'JUDGE.md' -print -quit 2>/dev/null || true)"
[ -z "$JM" ] || die "refusing $TASK: it contains $JM; the criteria must never be visible during a run (keep JUDGE.md elsewhere, for judge.sh)"
if [ -n "$PAPER" ]; then
  [ -s "$PAPER" ] || die "--paper file not found or empty: $PAPER"
else
  echo "run_task: no --paper given: no paper_N.pdf is staged (right only for a task without a source paper, such as examples/toy_task)" >&2
fi
[ -z "$SM" ] || [ -s "$SM" ] || die "--sm file not found: $SM"
[ -z "$SM" ] || [ -n "$PAPER" ] || die "--sm needs --paper"
case "$NUM" in ''|*[!0-9]*) die "--paper-num must be digits" ;; esac
case "$LABEL" in ''|*[!A-Za-z0-9_]*) die "--label must match [A-Za-z0-9_]+" ;; esac
[ -n "$OUT" ] || die "--out is required"
for v in "$NCPUS" "$MEM_GB"; do case "$v" in ''|*[!0-9]*|0) die "--ncpus and --mem-gb take positive integers" ;; esac; done
case "$PROFILE" in paper) WALL_H="${WALL_H:-24}" ;; smoke) WALL_H="${WALL_H:-6}" ;; *) die "--profile must be paper or smoke" ;; esac
WALL_S="$(awk -v h="$WALL_H" 'BEGIN { if (h !~ /^[0-9]+(\.[0-9]+)?$/) exit 1; printf "%d", h * 3600 }')" || die "--wall-hours must be a number"
[ "$WALL_S" -gt 1200 ] || die "--wall-hours too small: the orchestrator needs more than the 600 s margin"
WALLTIME_SEC=$(( WALL_S - 600 ))
case "$ENGINE" in
  docker)    IMAGE="${IMAGE:-residual-agents:1.0.0}" ;;
  apptainer) [ -n "$IMAGE" ] || die "--engine apptainer needs --image <ra.sif>" ;;
  *) die "--engine must be docker or apptainer" ;;
esac
[ -z "$GPU_INDEX" ] || case "$GPU_INDEX" in *[!0-9]*) die "--gpu-index takes a GPU number" ;; esac
case "$INJECT" in 0|1) ;; *) die "--inject-ids must be 0 or 1" ;; esac
if [ "$MODE" = proxy ]; then
  [ -n "$UPSTREAM" ] || die "proxy mode needs --upstream (an OpenAI-compatible base URL, e.g. https://openrouter.ai/api/v1)"
  case "$UPSTREAM" in
    http://*|https://*) ;;
    *) die "--upstream must start with http:// or https://" ;;
  esac
  case "${UPSTREAM#*://}" in
    *@*|*\?*|*\#*) die "--upstream must be a plain base URL (no user:password@, query or fragment)" ;;
  esac
fi
[ -n "$KEY_FILE" ] && [ -r "$KEY_FILE" ] && [ -s "$KEY_FILE" ] || [ "$DRY" = 1 ] || die "--key-file missing, unreadable or empty"
for e in ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"}; do
  n="${e%%=*}"
  [ "$n" != "$e" ] || die "--env takes NAME=VALUE (got '$e')"
  case "$n" in
    PECE_NCPUS|PECE_MEM_GB|PECE_PLAN_ONLY) die "--env $n: set it with its own option" ;;
    RA3_*|RA3SR_*|PECE_*) case "$n" in *[!A-Z0-9_]*) die "--env: bad name $n" ;; esac ;;
    *) die "--env $n: only variables the orchestrator reads (RA3_*, RA3SR_*, PECE_*) can be passed" ;;
  esac
done

# ---------------------------------------------------------------- stage the run folder
RID="${LABEL}_p${NUM}"
mkdir -p "$OUT/paper_$NUM"
PDIR="$(cd "$OUT/paper_$NUM" && pwd)"
RUN="$PDIR/$RID"
AUX="$PDIR/$RID.aux"
if [ -e "$RUN" ] && [ -n "$(ls -A "$RUN" 2>/dev/null)" ]; then
  die "$RUN exists and is not empty: an episode never runs twice in the same folder; choose another --label"
fi
case "$RUN" in *python*) echo "run_task: WARNING: the run path contains 'python'; this is harmless inside the container (/workspace)" >&2 ;; esac
mkdir -p "$RUN" "$AUX/store" "$AUX/home" "$AUX/proxy"
chmod 700 "$AUX"
cp "$TASK/CLAUDE.md" "$RUN/CLAUDE.md"
if [ -d "$TASK/skills" ]; then cp -r "$TASK/skills" "$RUN/skills"; fi
[ -z "$PAPER" ] || cp "$PAPER" "$RUN/paper_${NUM}.pdf"
[ -z "$SM" ] || cp "$SM" "$RUN/paper_${NUM}_SM.pdf"
# ENV.md with this run's allocation, the sentence the paper's job script wrote
ALLOC="- **This job's actual allocation: ${NCPUS} CPU cores.** Design compute code to use them (BLAS threads / multiprocessing over independent points). Hard memory cgroup limit: ${MEM_GB} GB total for ALL processes — \`free\` shows the whole shared node and LIES; budget worker count x per-worker RSS against this limit."
ALLOC="$ALLOC" awk '/^- CPU and RAM are allocated/ { print ENVIRON["ALLOC"]; next } { print }' \
  "$REPO/infra/container/ENV.md.template" > "$RUN/ENV.md"
SHM_GB=$(( MEM_GB / 2 > 1 ? MEM_GB / 2 : 1 ))

write_meta() {   # status exit_code started ended port -> AUX/run_meta.json (and the run folder's copy when finished)
  "$PY" - "$@" <<'PY'
import hashlib, json, os, sys
from urllib.parse import urlsplit, urlunsplit
status, rc, started, ended, port = sys.argv[1:6]
E = os.environ
def sha(p):
    if not p or not os.path.isfile(p):
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()
def public_url(u):
    if not u:
        return None
    s = urlsplit(u)      # user:password@ and query strings are never recorded
    if not s.hostname:
        return None
    return urlunsplit((s.scheme, s.hostname + (":%d" % s.port if s.port else ""), s.path, "", ""))
image_id = None
if E["RT_ENGINE"] == "docker" and status != "dry_run":
    try:
        import subprocess
        out = subprocess.run(["docker", "image", "inspect", E["RT_IMAGE"]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        image_id = json.loads(out.stdout.decode() or "[]")[0].get("Id")
    except Exception:
        image_id = None
meta = {
    "run_id": E["RT_RID"], "label": E["RT_LABEL"], "paper_num": int(E["RT_NUM"]), "profile": E["RT_PROFILE"],
    "status": status, "exit_code": int(rc) if rc not in ("", "-") else None, "started_utc": started or None,
    "ended_utc": ended or None, "launcher": "infra/run/run_task.sh",
    "engine": E["RT_ENGINE"], "image": E["RT_IMAGE"], "image_id": image_id,
    "wall_hours": float(E["RT_WALL_H"]), "walltime_sec": int(E["RT_WALLTIME_SEC"]),
    "allocation": {"ncpus": int(E["RT_NCPUS"]), "mem_gb": int(E["RT_MEM_GB"]), "shm_gb": int(E["RT_SHM_GB"]),
                   "gpu": E["RT_GPU"] == "1", "gpu_index": E["RT_GPU_INDEX"] or None,
                   "gpu_facts": E["RT_GPU_FACTS"] or None, "cpuset": E["RT_CPUSET"] or None},
    "model": E["RT_MODEL"] or "profile default",
    "mode": E["RT_MODE"],
    "proxy": None if E["RT_MODE"] != "proxy" else {
        "upstream": public_url(E["RT_UPSTREAM"]), "model_force": E["RT_MODEL_FORCE"] or None,
        "provider": E["RT_PROVIDER"] or None, "expect_model": E["RT_EXPECT"] or None,
        "inject_ids": E["RT_INJECT"] == "1", "rewrite": E["RT_REWRITE"] or None,
        "port": int(port) if port.isdigit() else None, "usage_log": "proxy/usage.jsonl (in the .aux folder)"},
    "direct": None if E["RT_MODE"] != "direct" else {"base_url": public_url(E["RT_BASE_URL"])},
    "plan_only": E["RT_PLAN_ONLY"] == "1", "extra_env": [x.split("=", 1)[0] for x in E["RT_EXTRA_ENV"].split("\n") if x],
    "inputs": {"claude_md_sha256": sha(os.path.join(E["RT_RUN"], "CLAUDE.md")),
               "paper_sha256": sha(os.path.join(E["RT_RUN"], "paper_%s.pdf" % E["RT_NUM"])),
               "sm_sha256": sha(os.path.join(E["RT_RUN"], "paper_%s_SM.pdf" % E["RT_NUM"])),
               "skills": os.path.isdir(os.path.join(E["RT_RUN"], "skills"))},
}
targets = [os.path.join(E["RT_AUX"], "run_meta.json")]
if status in ("finished", "infra_failure", "stopped"):
    targets.append(os.path.join(E["RT_RUN"], "run_meta.json"))
for t in targets:
    with open(t, "w") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
PY
}
EXTRA_LINES="$(printf '%s\n' ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"})"
export RT_RID="$RID" RT_LABEL="$LABEL" RT_NUM="$NUM" RT_PROFILE="$PROFILE" RT_ENGINE="$ENGINE" RT_IMAGE="$IMAGE"
export RT_WALL_H="$WALL_H" RT_WALLTIME_SEC="$WALLTIME_SEC" RT_NCPUS="$NCPUS" RT_MEM_GB="$MEM_GB" RT_SHM_GB="$SHM_GB"
export RT_GPU="$GPU" RT_GPU_INDEX="$GPU_INDEX" RT_GPU_FACTS="$GPU_FACTS" RT_CPUSET="$CPUSET" RT_MODEL="$MODEL"
export RT_MODE="$MODE" RT_UPSTREAM="$UPSTREAM" RT_MODEL_FORCE="${MODEL_FORCE:-${MODEL:-deepseek/deepseek-v4.1-flash}}"
export RT_PROVIDER="$PROVIDER" RT_EXPECT="$EXPECT" RT_INJECT="$INJECT" RT_REWRITE="$REWRITE" RT_BASE_URL="$BASE_URL"
export RT_PLAN_ONLY="$PLAN_ONLY" RT_EXTRA_ENV="$EXTRA_LINES" RT_RUN="$RUN" RT_AUX="$AUX"

# ---------------------------------------------------------------- the container's environment file
ENVF="$AUX/container.env"
kv() {   # NAME VALUE -> one line of the env file in the engine's format
  if [ "$ENGINE" = apptainer ]; then
    case "$2" in *"'"*) die "a value for $1 contains a single quote" ;; esac
    printf "%s='%s'\n" "$1" "$2"
  else
    case "$2" in *$'\n'*) die "a value for $1 contains a line break" ;; esac
    printf '%s=%s\n' "$1" "$2"
  fi
}
write_envfile() {   # port (proxy mode)
  ( umask 077
    { kv PAPER_NUM "$NUM"; kv RUN_ID "$RID"; kv WALLTIME_SEC "$WALLTIME_SEC"; kv PECE_NCPUS "$NCPUS"
      kv PECE_MEM_GB "$MEM_GB"; kv HOST_WORK_DIR "$RUN"; kv LANG C.UTF-8
      [ -z "$MODEL" ] || kv MODEL "$MODEL"
      [ -z "$GPU_FACTS" ] || kv RA3SR_GPU_FACTS "$GPU_FACTS"
      [ "$PLAN_ONLY" = 0 ] || kv PECE_PLAN_ONLY 1
      if [ "$ENGINE" = apptainer ] && [ -n "$GPU_INDEX" ]; then kv CUDA_VISIBLE_DEVICES "$GPU_INDEX"; fi
      for e in ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"}; do kv "${e%%=*}" "${e#*=}"; done
      if [ "$MODE" = proxy ]; then
        kv RA_DSH_SHIM 1; kv DSH_SHIM_PORT "$1"
      else
        kv RA_DSH_SHIM 0; kv DEEPSEEK_BASE_URL "$BASE_URL"
        # the key goes from its file into this file without passing through a shell variable
        if [ "$ENGINE" = apptainer ]; then printf "DEEPSEEK_API_KEY='"; tr -d "\r\n'" < "$KEY_FILE"; printf "'\n"
        else printf 'DEEPSEEK_API_KEY='; tr -d '\r\n' < "$KEY_FILE"; printf '\n'; fi
      fi
    } > "$ENVF" )
}

# ---------------------------------------------------------------- the container command
CNAME="ra_${RID}_$$"
if [ "$ENGINE" = docker ]; then
  CMD=(docker run --rm --init --name "$CNAME" --network host
       --cpus "$NCPUS" --memory "${MEM_GB}g" --shm-size "${SHM_GB}g"
       --user "$(id -u):$(id -g)" --security-opt no-new-privileges
       -v "$RUN:/workspace" -v "$AUX/store:/pece_store" -v "$AUX/home:/home/ra"
       -e HOME=/home/ra --env-file "$ENVF" -w /workspace)
  [ -z "$CPUSET" ] || CMD+=(--cpuset-cpus "$CPUSET")
  if [ "$GPU" = 1 ]; then
    if [ -n "$GPU_INDEX" ]; then CMD+=(--gpus "device=$GPU_INDEX"); else CMD+=(--gpus all); fi
  fi
  CMD+=("$IMAGE" /opt/ra/infra/run/run_in_container.sh --profile "$PROFILE")
else
  mkdir -p "$AUX/apptainer_tmp"
  CMD=(apptainer exec --containall --cleanenv --pid --home "$AUX/home:/home/ra" --workdir "$AUX/apptainer_tmp"
       --bind "$RUN:/workspace" --bind "$AUX/store:/pece_store" --env-file "$ENVF" --pwd /workspace)
  [ "$GPU" = 0 ] || CMD+=(--nv)
  CMD+=("$IMAGE" /opt/ra/infra/run/run_in_container.sh --profile "$PROFILE")
  [ -z "$CPUSET" ] || CMD=(taskset -c "$CPUSET" "${CMD[@]}")
fi
PROXY_CMD=(bash "$REPO/infra/proxy/start_proxy.sh" --state-dir "$AUX/proxy" --upstream "$UPSTREAM" --key-file "$KEY_FILE"
           --model-force "$RT_MODEL_FORCE" --inject-ids "$INJECT" --usage-log "$AUX/proxy/usage.jsonl"
           --infra-flag "$RUN/.infra_failure")
[ -z "$PROVIDER" ] || PROXY_CMD+=(--provider "$PROVIDER")
[ -z "$EXPECT" ] || PROXY_CMD+=(--expect-model "$EXPECT")
[ -z "$REWRITE" ] || PROXY_CMD+=(--rewrite "$REWRITE")

if [ "$DRY" = 1 ]; then
  write_meta dry_run - "" "" ""
  echo "run_task: dry run; staged $RUN (aux: $AUX). Commands:"
  if [ "$MODE" = proxy ]; then printf ' '; printf ' %q' "${PROXY_CMD[@]}"; echo; fi
  printf ' '; printf ' %q' timeout -s TERM -k 300 "$WALL_S" "${CMD[@]}"; echo
  echo "  environment file (built at run time): PAPER_NUM RUN_ID WALLTIME_SEC=$WALLTIME_SEC PECE_NCPUS PECE_MEM_GB" \
       "HOST_WORK_DIR LANG$( [ -n "$MODEL" ] && printf ' MODEL')$( [ -n "$GPU_FACTS" ] && printf ' RA3SR_GPU_FACTS')$( [ "$MODE" = proxy ] && printf ' RA_DSH_SHIM=1 DSH_SHIM_PORT' || printf ' RA_DSH_SHIM=0 DEEPSEEK_BASE_URL DEEPSEEK_API_KEY')"
  exit 0
fi

# ---------------------------------------------------------------- run
PROXY_PID=""
cleanup() {
  if [ "$ENGINE" = docker ]; then docker kill "$CNAME" >/dev/null 2>&1 || true; fi
  if [ -n "$PROXY_PID" ]; then kill "$PROXY_PID" 2>/dev/null || true; fi
  rm -f "$ENVF"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PORT=""
if [ "$MODE" = proxy ]; then
  set +e
  PORT="$("${PROXY_CMD[@]}")"
  prc=$?
  set -e
  if [ "$prc" != 0 ] || [ -z "$PORT" ]; then
    echo "run_task: the model proxy did not start (see $AUX/proxy/proxy.log)" >&2
    write_meta infra_failure 3 "$STARTED" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" ""
    exit 3
  fi
  PROXY_PID="$(cat "$AUX/proxy/pid")"
else
  echo "run_task: WARNING: direct mode: the agents' tools can read the endpoint key (use proxy mode to keep it out)" >&2
fi
write_envfile "$PORT"
write_meta running - "$STARTED" "" "$PORT"
echo "run_task: $RID: engine=$ENGINE image=$IMAGE profile=$PROFILE ncpus=$NCPUS mem=${MEM_GB}GB wall=${WALL_H}h mode=$MODE${PORT:+ proxy port=$PORT}"
set +e
timeout -s TERM -k 300 "$WALL_S" "${CMD[@]}" 2>&1 | tee -a "$AUX/console.log"
RC=${PIPESTATUS[0]}
set -e
ENDED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STATUS=finished
[ "$RC" != 3 ] || STATUS=infra_failure
[ "$RC" != 124 ] || STATUS=stopped
write_meta "$STATUS" "$RC" "$STARTED" "$ENDED" "$PORT"
echo "run_task: $RID ended with exit status $RC ($STATUS); report: $( [ -f "$RUN/report.md" ] && echo present || echo missing )"
exit "$RC"
