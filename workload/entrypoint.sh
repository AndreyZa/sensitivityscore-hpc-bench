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
# IMPORTANT (fixed after an earlier wrong assumption): TestEm5 — like every
# standard Geant4 example — takes ONLY a macro file path as a positional
# CLI argument. There is no -t/-p/-m flag parsing in Geant4 examples'
# main(argc, argv); argv[1] (if present) is executed via
# "/control/execute <file>" internally. So:
#   - threads are set via the /run/numberOfThreads UI command INSIDE the
#     macro, which must precede /run/initialize (PreInit-state requirement,
#     see Geant4's "Quick migration guide for Geant4 version 10.x series").
#   - physics list is set via the PHYSLIST environment variable, which
#     G4PhysListFactory::ReferencePhysList() reads directly
#     (source: Geant4/geant4 source/physics_lists/lists/src/G4PhysListFactory.cc)
#     — this part of the original design was actually correct.
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

# Render the macro with the requested threads/primaries/seed settings so the
# same static .mac template can drive both low-S and high-S profiles.
RENDERED_MACRO="/tmp/run.$$.mac"
sed \
  -e "s|__G4_THREADS__|${G4_THREADS}|g" \
  -e "s|__N_PRIMARIES__|${N_PRIMARIES}|g" \
  -e "s|__RNG_SEED__|${RNG_SEED}|g" \
  "${MACRO}" > "${RENDERED_MACRO}"

# OUTPUT_MODE (Disk I/O sensitivity dimension) is not yet wired into the
# macro — the ntuple/analysis UI commands needed for per-event output
# depend on TestEm5's own AnalysisManager setup, which needs verification
# against the actual example source before scripting it here. Tracked as a
# follow-up; low-S/high-S both currently run with default (aggregate-only)
# output regardless of OUTPUT_MODE. See docs §1.2 for the intended behavior.
if [ "${OUTPUT_MODE}" != "none" ]; then
  echo "[entrypoint] warning: OUTPUT_MODE=${OUTPUT_MODE} requested but not yet implemented — running with default output" >&2
fi

export PHYSLIST="${PHYSICS_LIST}"

exec geant4-app "${RENDERED_MACRO}"
