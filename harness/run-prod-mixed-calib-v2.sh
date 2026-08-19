#!/bin/bash
# Вторая калибровочная серия прода (v2): дозы по урокам v1 — ML-жертва по
# membw, реальный стрим по net, egress-шторм. Дизайн, обкатка и конвейер
# после прогона — в шапке config-prod-mixed-calib-v2.yaml. Запуск:
#   STAND=prod make series SERIES=mixed-calib-v2       # БЕЗ PILOT
#
# ПЕРЕД запуском: kubectl apply -f k8s/net-sink/sink-prod.yaml (sink стрима
# на ss-system) — без него high-s-net упрётся в таймаут NET_TIMEOUT и цена
# cˢ_net не измерится (job не падает, но стрим-фаза = 600с таймаута).
set -x
cd "$(dirname "$0")" || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/prod} REDIS_ADDR=localhost:16379

../scripts/run-series.sh page mixed-calib-v2 || true

# MEM_REQ == MEM_LIM ОБЯЗАТЕЛЬНО (Guaranteed => static-пиннинг; урок 18.08,
# §C5). ml-inference: дозы в самом профиле (28 потоков / 40000 инференсов /
# 16Gi=16Gi), экспорты здесь — только чтобы дозы были видны в одном месте и
# правились без правки кода.
export HARNESS_OVERRIDE_ML_INFERENCE_CPU=28 \
       HARNESS_OVERRIDE_ML_INFERENCE_THREADS=28 \
       HARNESS_OVERRIDE_ML_INFERENCE_N_INFER=40000 \
       HARNESS_OVERRIDE_ML_INFERENCE_MEM_REQ=16Gi \
       HARNESS_OVERRIDE_ML_INFERENCE_MEM_LIM=16Gi \
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
       HARNESS_OVERRIDE_HIGH_S_NET_OUTPUT_MODE=stream \
       HARNESS_OVERRIDE_HIGH_S_NET_NET_TOTAL_MB=8000 \
       HARNESS_OVERRIDE_LOW_S_PRIMARIES=1000000

# SKIP_BASELINE=1 — рестарт серии после сбоя хоста: эталоны уже лежат в
# baselines.parquet (харнесс сбрасывает его после КАЖДОЙ строки, так что
# смерть хоста их не трогает), а повторный --baseline начал бы файл с нуля
# и переписал бы ~3 часа прогонов. Введено 19.08.2026, когда лаба упала
# посреди основной фазы: перезапуск только PRESSURE, эталоны с вечера.
if [ "${SKIP_BASELINE:-0}" != "1" ]; then
    echo "=== BASELINE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
    .venv/bin/python run_experiment.py --config config-prod-mixed-calib-v2.yaml --baseline
    rc=$?
    echo "=== BASELINE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
else
    echo "эталоны пропущены (SKIP_BASELINE=1) — берём готовый baselines.parquet"
fi
echo "=== PRESSURE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-prod-mixed-calib-v2.yaml --pressure --scenarios mixed3v2
rc=$?
echo "=== PRESSURE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
