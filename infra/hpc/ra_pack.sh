#!/usr/bin/env bash
# Run several Residual Agents episodes side by side on one node: inside one scheduler allocation (call it from a
# PBS or Slurm job script) or on a workstation.
#
# Usage: ra_pack.sh MEMBERS.tsv [--stagger S] -- RUN_TASK_OPTIONS...
#   MEMBERS.tsv: one episode per line, tab-separated; empty lines and lines starting with '#' are skipped:
#       paper_num  label  task_dir  paper_pdf  sm_pdf_or_-  ncpus  mem_gb  gpu(0|1)
#   RUN_TASK_OPTIONS go to every run_task.sh call: --out (required), --engine, --image, --upstream, --key-file,
#   --profile, --wall-hours, ...
#
# Each member gets
#   - its own block of cores: --cpuset, taken in order from this process's CPU affinity (docker: --cpuset-cpus;
#     apptainer: taskset), and its own memory budget (mem_gb, which the orchestrator plans against);
#   - its own PID namespace (the container's) and its own model proxy on a free port (nonce-checked, so two members
#     never share one);
#   - with gpu=1, one GPU of its own: --gpu-index k (CUDA_VISIBLE_DEVICES=k inside apptainer; --gpus device=k for
#     docker) and RA3SR_GPU_FACTS set to that GPU's name and memory as nvidia-smi reports them, so the orchestrator
#     plans for one GPU.
# The members' sum of ncpus must fit the affinity mask; otherwise they run unpinned (a warning is printed).
# Launches are staggered by S seconds (default 20). Each member's output goes to <out>/pack_logs/<label>_p<N>.log.
# Exit status: the number of members that did not end with status 0 (0 = all ended normally); 64 on a usage error.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TASK="$HERE/../run/run_task.sh"
PY="${PYTHON:-python3}"
usage() { sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ra_pack: $*" >&2; exit 64; }

[ $# -ge 1 ] || { usage; exit 64; }
case "$1" in -h|--help) usage; exit 0 ;; esac
MEMBERS="$1"; shift
STAGGER=20
while [ $# -gt 0 ] && [ "$1" != "--" ]; do
  case "$1" in
    --stagger) [ $# -ge 2 ] || die "--stagger needs a value"; STAGGER="$2"; shift 2 ;;
    *) die "unknown option $1 (common run_task.sh options go after --)" ;;
  esac
done
[ "${1:-}" = "--" ] && shift
COMMON=("$@")
[ -f "$MEMBERS" ] || die "members file not found: $MEMBERS"
case "$STAGGER" in ''|*[!0-9]*) die "--stagger takes seconds" ;; esac

OUT=""
for ((i = 0; i < ${#COMMON[@]}; i++)); do
  if [ "${COMMON[$i]}" = "--out" ] && [ "$((i + 1))" -lt "${#COMMON[@]}" ]; then OUT="${COMMON[$((i + 1))]}"; fi
  case "${COMMON[$i]}" in --cpuset|--gpu|--gpu-index|--gpu-facts|--ncpus|--mem-gb|--paper-num|--label|--task|--paper|--sm)
    die "${COMMON[$i]} is set per member in $MEMBERS" ;; esac
done
[ -n "$OUT" ] || die "the common options need --out"
mkdir -p "$OUT/pack_logs"

# members
P=() L=() T=() PDF=() SM=() NC=() MG=() G=()
while IFS=$'\t' read -r p l t pdf sm nc mg g rest; do
  case "$p" in ''|'#'*) continue ;; esac
  [ -n "$g" ] || die "member line for paper $p: expected 8 tab-separated fields"
  case "$nc$mg" in *[!0-9]*) die "member $l p$p: ncpus and mem_gb must be integers" ;; esac
  case "$g" in 0|1) ;; *) die "member $l p$p: gpu must be 0 or 1" ;; esac
  P+=("$p"); L+=("$l"); T+=("$t"); PDF+=("$pdf"); SM+=("$sm"); NC+=("$nc"); MG+=("$mg"); G+=("$g")
done < "$MEMBERS"
N=${#P[@]}
[ "$N" -gt 0 ] || die "no members in $MEMBERS"

# cores
read -r -a CPUS <<< "$("$PY" -c 'import os; print(" ".join(map(str, sorted(os.sched_getaffinity(0)))))')"
NEED=0
for nc in "${NC[@]}"; do NEED=$((NEED + nc)); done
PIN=1
if [ "${#CPUS[@]}" -lt "$NEED" ]; then
  echo "ra_pack: WARNING: the affinity mask has ${#CPUS[@]} cores, the members need $NEED: members run unpinned" >&2
  PIN=0
fi

# GPUs
NG_NEED=0
for g in "${G[@]}"; do NG_NEED=$((NG_NEED + g)); done
NG=0
if [ "$NG_NEED" -gt 0 ]; then
  NG="$(nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true)"
  [ "$NG" -ge "$NG_NEED" ] || die "$NG_NEED members need a GPU, nvidia-smi lists $NG"
fi

pids=() names=()
OFF=0 GPU_NEXT=0
for ((i = 0; i < N; i++)); do
  name="${L[$i]}_p${P[$i]}"
  args=(--paper-num "${P[$i]}" --label "${L[$i]}" --task "${T[$i]}" --paper "${PDF[$i]}" --ncpus "${NC[$i]}"
        --mem-gb "${MG[$i]}")
  [ "${SM[$i]}" = "-" ] || [ -z "${SM[$i]}" ] || args+=(--sm "${SM[$i]}")
  if [ "$PIN" = 1 ]; then
    block="$(printf '%s,' "${CPUS[@]:$OFF:${NC[$i]}}")"
    args+=(--cpuset "${block%,}")
  fi
  OFF=$((OFF + NC[i]))
  if [ "${G[$i]}" = 1 ]; then
    facts="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader -i "$GPU_NEXT" 2>/dev/null | head -1)"
    [ -n "$facts" ] || die "nvidia-smi gave no name for GPU $GPU_NEXT"
    args+=(--gpu-index "$GPU_NEXT" --gpu-facts "$facts")
    GPU_NEXT=$((GPU_NEXT + 1))
  fi
  log="$OUT/pack_logs/$name.log"
  echo "ra_pack: launch $name cores=${block:-unpinned} gpu=$( [ "${G[$i]}" = 1 ] && echo $((GPU_NEXT - 1)) || echo none ) log=$log"
  bash "$RUN_TASK" "${args[@]}" "${COMMON[@]}" > "$log" 2>&1 &
  pids+=($!); names+=("$name")
  block=""
  if [ $((i + 1)) -lt "$N" ] && [ "$STAGGER" -gt 0 ]; then sleep "$STAGGER"; fi
done

bad=0
for ((k = 0; k < ${#pids[@]}; k++)); do
  set +e
  wait "${pids[$k]}"
  rc=$?
  set -e
  echo "ra_pack: finished ${names[$k]} with status $rc"
  [ "$rc" = 0 ] || bad=$((bad + 1))
done
exit "$bad"
