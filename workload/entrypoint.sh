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

# OUTPUT_MODE — Disk-I/O измерение профиля S (docs §1.2):
#   none  — без вывода (по умолчанию);
#   burst — эмуляция периодической записи выходных данных (checkpoint/
#           ntuple-style): каждые IO_INTERVAL_SECONDS секунд пишется
#           IO_BURST_MB МБ с fsync (fsync принципиален: без него запись
#           оседает в page cache и реального дискового I/O — и PSI-стоек
#           у job под IO-контенцией — не возникает).
# burst-писатель выбран вместо настоящего per-event ntuple через
# AnalysisManager TestEm5: даёт ровно то, что нужно измерению — управляемую
# дозируемую IO-нагрузку, привязанную к живому compute-job, — без
# зависимости от деталей analysis-кода конкретного примера Geant4.
# Настоящий ntuple-вывод остаётся возможным уточнением методики.
IO_BURST_MB="${IO_BURST_MB:-64}"
IO_INTERVAL_SECONDS="${IO_INTERVAL_SECONDS:-5}"
case "${OUTPUT_MODE}" in
  none) ;;
  burst)
    echo "[entrypoint] burst writer: ${IO_BURST_MB}MB fsync every ${IO_INTERVAL_SECONDS}s" >&2
    (
      while true; do
        dd if=/dev/zero of=/tmp/output-burst.dat bs=1M \
          count="${IO_BURST_MB}" conv=fsync 2>/dev/null
        sleep "${IO_INTERVAL_SECONDS}"
      done
    ) &
    # Писатель — фоновый процесс контейнера: умирает вместе с ним, когда
    # geant4-app (PID 1 после exec ниже) завершается.
    ;;
  *)
    echo "[entrypoint] warning: unknown OUTPUT_MODE=${OUTPUT_MODE} — running without output" >&2
    ;;
esac

export PHYSLIST="${PHYSICS_LIST}"

exec geant4-app "${RENDERED_MACRO}"
