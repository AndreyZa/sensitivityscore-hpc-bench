#!/usr/bin/env bash
#SBATCH --job-name=geant4-lows
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:30:00
#SBATCH --output=results/C-lows-%j.out
#
# Config C (classical Slurm) — low-S profile.
# Same env-parameterized workload image as config A/B/D, run as a plain Slurm batch
# job (no K8s layer) — the "as-is" HPC performance ceiling for H3.
#
# Env vars are passed via `sbatch --export=` from the harness (see
# harness/submit/slurm_submit.py); the defaults below let this script also be run
# standalone for a sanity check.
set -euo pipefail

G4_THREADS="${G4_THREADS:-1}"
PHYSICS_LIST="${PHYSICS_LIST:-QGSP_BERT}"
N_PRIMARIES="${N_PRIMARIES:-10000}"
OUTPUT_MODE="${OUTPUT_MODE:-none}"
RNG_SEED="${RNG_SEED:-42}"

echo "[sbatch] config=C profile=low-s threads=${G4_THREADS} physics_list=${PHYSICS_LIST} \
n_primaries=${N_PRIMARIES} output_mode=${OUTPUT_MODE} seed=${RNG_SEED} job_id=${SLURM_JOB_ID}"

srun --container-image=sensitivityscore-bench/geant4:11.2 \
  --container-env=G4_THREADS,PHYSICS_LIST,N_PRIMARIES,OUTPUT_MODE,RNG_SEED \
  /entrypoint.sh

# Makespan recorded by the harness via:
#   sacct -j ${SLURM_JOB_ID} --format=JobID,Elapsed,Start,End
