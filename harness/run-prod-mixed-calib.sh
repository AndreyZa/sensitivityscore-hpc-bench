#!/bin/bash
# Калибровочная серия прода: эталоны + серия ОДНОЙ сессией (межсессионные
# эталоны непригодны — дрейф стенда, урок STAGE +13..23%). Дизайн и чеклист —
# в шапке config-prod-mixed-calib.yaml. Запуск:
#   STAND=prod make series SERIES=mixed-calib          # БЕЗ PILOT
#
# Дозы: 28 CPU/28 потоков (SMT off: 64 ядра, минус 4 зарезервированных, по
# 30 эксклюзивных на NUMA-домен -> 2 задачи на узел), 60M частиц ≈ 4 мин
# (замер смоука 18.08: 5M = 18-22 с на этих узлах). Память 8/16Gi — как в
# смоуке. IO-опции high-s-io — те же, что в смоуке (blocking-писатель,
# 16 бёрстов по 256МБ). low-s остаётся лёгким (1 поток) — его роль контраст
# fingerprint в эталонах; 1M частиц ≈ 30-60 с на ядре SPR (проверить по
# факту в baselines: сильно быстрее минуты — поднять).
set -x
cd "$(dirname "$0")" || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/prod} REDIS_ADDR=localhost:16379

# Статус-страница (идемпотентно; при запуске через make series уже поднята).
../scripts/run-series.sh page mixed-calib || true

# MEM_REQ == MEM_LIM ОБЯЗАТЕЛЬНО (найдено 18.08 перед первой калибровкой):
# static cpuManager выдаёт эксклюзивные ядра только Guaranteed-подам
# (requests == limits по CPU И памяти). Со смоуковскими 8Gi/16Gi жертвы были
# Burstable — пиннинг и single-numa-node для них не работали вовсе, ровно
# как предупреждал §C5 аудита, и numa_remote_ratio мерил бы миграцию
# потоков, а не последствия размещения.
export HARNESS_OVERRIDE_HIGH_S_CPU=28 HARNESS_OVERRIDE_HIGH_S_THREADS=28 \
       HARNESS_OVERRIDE_HIGH_S_PRIMARIES=60000000 \
       HARNESS_OVERRIDE_HIGH_S_MEM_REQ=16Gi HARNESS_OVERRIDE_HIGH_S_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_CPU=28 HARNESS_OVERRIDE_HIGH_S_IO_THREADS=28 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=60000000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=16Gi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_OUTPUT_MODE=blocking \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_BURST_MB=256 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_INTERVAL_SECONDS=0 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_TOTAL_BURSTS=16 \
       HARNESS_OVERRIDE_HIGH_S_NET_CPU=28 HARNESS_OVERRIDE_HIGH_S_NET_THREADS=28 \
       HARNESS_OVERRIDE_HIGH_S_NET_PRIMARIES=60000000 \
       HARNESS_OVERRIDE_HIGH_S_NET_MEM_REQ=16Gi HARNESS_OVERRIDE_HIGH_S_NET_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_LOW_S_PRIMARIES=1000000

echo "=== BASELINE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-prod-mixed-calib.yaml --baseline
rc=$?
echo "=== BASELINE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
echo "=== PRESSURE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-prod-mixed-calib.yaml --pressure --scenarios mixed3
rc=$?
echo "=== PRESSURE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
