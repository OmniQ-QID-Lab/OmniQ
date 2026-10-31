#!/bin/bash
#SBATCH --job-name=ra_episode
#SBATCH --partition=<partition>
#SBATCH --account=<account>
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --no-requeue
##SBATCH --gres=gpu:1
# One Residual Agents episode as a Slurm job. A generic template: replace <partition> and <account>, and the resource
# lines with your site's values (for a GPU task, uncomment the --gres line and set RA_GPU=1).
#
# Submit with an explicit variable list (only the listed variables reach the job):
#   sbatch --export=RA_REPO=/path/to/residual-agents,RA_SIF=/path/to/ra.sif,RA_TASK=/path/to/task_folder,\
#   RA_PAPER_PDF=/path/to/paper.pdf,RA_PAPER_NUM=2,RA_LABEL=myrun,RA_OUT=/path/to/runs,\
#   RA_UPSTREAM=https://openrouter.ai/api/v1,RA_KEY_FILE=/path/to/key,RA_WALL_HOURS=24 infra/hpc/ra_episode.slurm
# Optional variables: RA_SM_PDF, RA_GPU=1, RA_PROFILE (paper|smoke), RA_NCPUS and RA_MEM_GB (default: the job's
# allocation), RA_MODEL_FORCE, RA_PROVIDER, RA_EXPECT_MODEL. RA_WALL_HOURS must match --time: the orchestrator plans
# with it (wall - 600 s), and the job's own limit is the hard stop.
# The rest is as in ra_episode.pbs: the proxy runs on the node outside the container, the episode runs under
# apptainer exec --containall --cleanenv --pid, and --no-requeue keeps a requeue from reusing the run folder.
# Exit status: run_task.sh's (0 normal end, 2 configuration error, 3 infrastructure failure, 124 hard stop).
set -euo pipefail
export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"
# module load apptainer        # when your site provides Apptainer as an environment module

: "${RA_REPO:?set RA_REPO}" "${RA_SIF:?set RA_SIF}" "${RA_TASK:?set RA_TASK}" "${RA_PAPER_PDF:?set RA_PAPER_PDF}"
: "${RA_PAPER_NUM:?set RA_PAPER_NUM}" "${RA_LABEL:?set RA_LABEL}" "${RA_OUT:?set RA_OUT}"
: "${RA_UPSTREAM:?set RA_UPSTREAM}" "${RA_KEY_FILE:?set RA_KEY_FILE}"

MEM_MB="${SLURM_MEM_PER_NODE:-}"
args=(--engine apptainer --image "$RA_SIF" --task "$RA_TASK" --paper "$RA_PAPER_PDF" --paper-num "$RA_PAPER_NUM"
      --label "$RA_LABEL" --out "$RA_OUT" --ncpus "${RA_NCPUS:-${SLURM_CPUS_PER_TASK:-16}}"
      --mem-gb "${RA_MEM_GB:-$(( ${MEM_MB:-65536} / 1024 ))}" --wall-hours "${RA_WALL_HOURS:-24}"
      --profile "${RA_PROFILE:-paper}" --upstream "$RA_UPSTREAM" --key-file "$RA_KEY_FILE")
[ -z "${RA_SM_PDF:-}" ] || args+=(--sm "$RA_SM_PDF")
[ "${RA_GPU:-0}" != 1 ] || args+=(--gpu)
[ -z "${RA_MODEL_FORCE:-}" ] || args+=(--model-force "$RA_MODEL_FORCE")
[ -z "${RA_PROVIDER:-}" ] || args+=(--provider "$RA_PROVIDER")
[ -z "${RA_EXPECT_MODEL:-}" ] || args+=(--expect-model "$RA_EXPECT_MODEL")

echo "ra_episode: job ${SLURM_JOB_ID:-?} on $(hostname -s 2>/dev/null || echo node): $RA_LABEL p$RA_PAPER_NUM"
exec bash "$RA_REPO/infra/run/run_task.sh" "${args[@]}"
