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
#   OUTPUT_MODE   none (low-S) .. burst / blocking (high-S)              -> Disk I/O
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
# Бинарь Geant4 — переопределяем для локального smoke entrypoint без полного
# образа (stub вместо geant4-app); в проде остаётся дефолт.
G4_BIN="${G4_BIN:-geant4-app}"

# Куда пишет дисковый писатель. Дефолт /scratch (emptyDir из шаблона Job,
# см. job-template.yaml.j2) — реальный диск узла, тот же физический девайс,
# что насыщает диск-агрессор (он тоже пишет в /scratch), поэтому fsync
# писателя честно стоит в очереди к придавленному устройству. На прод-стенде
# с отдельным NVMe это тем более важно: писать надо на тот же диск, что и
# шторм, а не в overlay-слой контейнера. Fallback /tmp — для локального smoke.
IO_SCRATCH_DIR="${IO_SCRATCH_DIR:-/scratch}"
[ -d "${IO_SCRATCH_DIR}" ] && [ -w "${IO_SCRATCH_DIR}" ] || IO_SCRATCH_DIR=/tmp

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

# --- Дисковый писатель профиля S (docs §1.2) --------------------------------
#
# OUTPUT_MODE управляет дисковой нагрузкой профиля:
#   none     — без вывода (low-S, кэш/сеть-профили);
#   burst    — БЕСКОНЕЧНЫЙ фоновый писатель, умирает вместе с контейнером.
#              Создаёт живое io.pressure на узле (агент его видит, планировщик
#              уводит жертв) — но makespan самой жертвы от дисковой латентности
#              НЕ зависит (писатель не на критическом пути). Измеряет ДЕТЕКЦИЮ
#              и УКЛОНЕНИЕ, а не деградацию. Оставлен для совместимости.
#   blocking — КОНЕЧНЫЙ писатель (IO_TOTAL_BURSTS порций) + gate выхода: job
#              не завершается, пока вывод не сброшен на диск (wait ниже).
#              На простое диска писатель успевает за время compute и спрятан
#              (makespan ≈ compute); под штормом fsync-хвост стоит в очереди,
#              писатель финиширует ПОСЛЕ compute и удлиняет makespan — это и
#              делает диск-чувствительную задачу реально МЕДЛЕННЕЕ под штормом
#              (cˢ_io > 0), а не просто обнаружимой. Моделирует HPC-job,
#              который «не готов», пока результаты не персистентны на диске.
#
# fsync принципиален в обоих режимах: без него запись оседает в page cache,
# реального дискового I/O (и стояния жертвы в очереди к устройству) не
# возникает.
IO_BURST_MB="${IO_BURST_MB:-32}"
IO_INTERVAL_SECONDS="${IO_INTERVAL_SECONDS:-1}"
IO_TOTAL_BURSTS="${IO_TOTAL_BURSTS:-40}"

# Записывает порцию IO_BURST_MB МБ с fsync; повторяет.
#   $1 = число порций (0 => бесконечно, для burst).
run_disk_writer() {
  local total="${1:-0}" n=0
  local out="${IO_SCRATCH_DIR}/output-burst.$$.dat"
  while :; do
    dd if=/dev/zero of="${out}" bs=1M count="${IO_BURST_MB}" conv=fsync 2>/dev/null
    n=$((n + 1))
    if [ "${total}" -gt 0 ] && [ "${n}" -ge "${total}" ]; then
      break
    fi
    sleep "${IO_INTERVAL_SECONDS}"
  done
  rm -f "${out}" 2>/dev/null || true
}

export PHYSLIST="${PHYSICS_LIST}"

case "${OUTPUT_MODE}" in
  none)
    exec "${G4_BIN}" "${RENDERED_MACRO}"
    ;;
  burst)
    echo "[entrypoint] burst writer (infinite bg): ${IO_BURST_MB}MB fsync every ${IO_INTERVAL_SECONDS}s -> ${IO_SCRATCH_DIR}" >&2
    run_disk_writer 0 &
    # Писатель — фоновый процесс контейнера: умирает вместе с ним, когда
    # geant4-app (PID 1 после exec) завершается.
    exec "${G4_BIN}" "${RENDERED_MACRO}"
    ;;
  blocking)
    echo "[entrypoint] blocking writer: ${IO_TOTAL_BURSTS}×${IO_BURST_MB}MB fsync (interval ${IO_INTERVAL_SECONDS}s) -> ${IO_SCRATCH_DIR}, gate exit on flush" >&2
    run_disk_writer "${IO_TOTAL_BURSTS}" &
    WRITER_PID=$!
    # compute — на переднем плане (НЕ exec: после него нужно дождаться диска).
    set +e
    "${G4_BIN}" "${RENDERED_MACRO}"
    G4_RC=$?
    set -e
    # Gate makespan на персистентность вывода: пока fsync-хвост не слит,
    # job не «готов». Под дисковым штормом этот wait и есть цена cˢ_io.
    wait "${WRITER_PID}" 2>/dev/null || true
    exit "${G4_RC}"
    ;;
  *)
    echo "[entrypoint] warning: unknown OUTPUT_MODE=${OUTPUT_MODE} — running without output" >&2
    exec "${G4_BIN}" "${RENDERED_MACRO}"
    ;;
esac
