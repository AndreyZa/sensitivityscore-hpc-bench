#!/bin/bash
# run-prod-smoke.sh — первая (смоук) серия на прод-стенде: эталоны и серию
# гоняем ОДНОЙ сессией, иначе замедление считается от эталонов другого дня
# (на STAGE межсессионный дрейф доходил до 23% — больше любой цены оси).
#
# Запуск через обвязку (preflight + фон + статус-страница + вотчдог):
#   STAND=prod make series-preflight SERIES=smoke
#   STAND=prod make series SERIES=smoke
#
# Дозы профилей заданы env-оверрайдами (HARNESS_OVERRIDE_*) — они же попадают
# в провенанс строки результата (profile_overrides), так что «чем именно
# снято» видно из данных, а не только из скрипта.
#
# ОТЛИЧИЯ ОТ STAGE, ради которых значения ниже другие:
#   * полные ядра вместо квоты 500m — THREADS по числу ядер NUMA-домена (32),
#     CPU-заявка целыми ядрами; на STAGE было 500m/2 потока;
#   * больше первичных частиц: на 32 потоках 300 тыс. частиц отсчитываются за
#     секунды, и на таком времени интерференция утонет в шуме запуска;
#   * дисковая доза крупнее: NVMe против облачного тома, мелкая запись
#     утонет в кэше контроллера.
# Все три — ПЕРВОЕ ПРИБЛИЖЕНИЕ. После смоука сверить с эталонным временем:
# задача должна считаться 3-10 минут, иначе правим PRIMARIES.
set -x
cd "$(dirname "$0")" || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/prod} REDIS_ADDR=localhost:16379

../scripts/run-series.sh page smoke || true

# Пара близнецов: одинаковый расчёт и заявки, различие — только реальный вывод
# на диск и декларация io. Равенство CPU/памяти/частиц здесь принципиально:
# оно и делает пару близнецами (см. docs/Методика измерений.md).
export HARNESS_OVERRIDE_HIGH_S_IO_CPU=32 HARNESS_OVERRIDE_HIGH_S_IO_THREADS=32 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=5000000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=8Gi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_OUTPUT_MODE=blocking \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_BURST_MB=256 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_INTERVAL_SECONDS=0 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_TOTAL_BURSTS=16 \
       HARNESS_OVERRIDE_IO_INSENSITIVE_CPU=32 HARNESS_OVERRIDE_IO_INSENSITIVE_THREADS=32 \
       HARNESS_OVERRIDE_IO_INSENSITIVE_PRIMARIES=5000000 \
       HARNESS_OVERRIDE_IO_INSENSITIVE_MEM_REQ=8Gi HARNESS_OVERRIDE_IO_INSENSITIVE_MEM_LIM=16Gi

echo "=== BASELINE START $(date +%H:%M:%S) ==="
.venv/bin/python run_experiment.py --config config-prod-smoke.yaml --baseline
echo "=== BASELINE DONE $(date +%H:%M:%S) rc=$? ==="
echo "=== PRESSURE START $(date +%H:%M:%S) ==="
.venv/bin/python run_experiment.py --config config-prod-smoke.yaml --pressure --scenarios smoke
echo "=== PRESSURE DONE $(date +%H:%M:%S) rc=$? ==="
