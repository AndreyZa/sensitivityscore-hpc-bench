#!/usr/bin/env bash
#
# entrypoint.sh — controllable sensitivity-profile launcher for the Geant4 workload.
#
# Same binary, different interference profile, selected entirely through environment
# variables (see docs/Технический_план_экспериментов.md §1.2):
#
#   G4_THREADS    1 (low-S) .. N physical cores per NUMA domain (high-S)  -> NUMA pressure
#   PHYSICS_LIST  QGSP_BERT (low-S) .. FTFP_BERT_HP (high-S)              -> LLC working set
#   N_PRIMARIES   1e4 (low-S) .. 1e6-1e7 (high-S)                        -> job duration
#   OUTPUT_MODE   none (low-S) .. per-event ntuple (high-S)              -> Disk I/O
#   RNG_SEED      fixed per repetition                                   -> reproducibility
#
set -euo pipefail

G4_THREADS="${G4_THREADS:-1}"
PHYSICS_LIST="${PHYSICS_LIST:-QGSP_BERT}"
N_PRIMARIES="${N_PRIMARIES:-10000}"
OUTPUT_MODE="${OUTPUT_MODE:-none}"
RNG_SEED="${RNG_SEED:-42}"
MACRO="${MACRO:-/macros/run.mac}"

echo "[entrypoint] sensitivity profile:" >&2
echo "[entrypoint]   threads=${G4_THREADS} physics_list=${PHYSICS_LIST} \
n_primaries=${N_PRIMARIES} output_mode=${OUTPUT_MODE} seed=${RNG_SEED}" >&2

# Render the macro with the requested primaries/seed/output settings so the same
# static .mac template can drive both low-S and high-S profiles.
RENDERED_MACRO="/tmp/run.$$.mac"
sed \
  -e "s|__N_PRIMARIES__|${N_PRIMARIES}|g" \
  -e "s|__RNG_SEED__|${RNG_SEED}|g" \
  -e "s|__OUTPUT_MODE__|${OUTPUT_MODE}|g" \
  "${MACRO}" > "${RENDERED_MACRO}"

export G4FORCE_RUN_MANAGER_TYPE="${G4_THREADS}" # MT vs serial selection hint for the app
export PHYSLIST="${PHYSICS_LIST}"               # consumed by TestEm5-style examples

exec geant4-app \
  -t "${G4_THREADS}" \
  -p "${PHYSICS_LIST}" \
  -m "${RENDERED_MACRO}"
