#!/bin/bash
# Смешанная серия STAGE с калиброванным скорингом (базовая цена оси):
# эталоны + серия ОДНОЙ сессией (дрейф стенда между сессиями +13..23% —
# см. шапку config-stage-mixed-calib.yaml). Чеклист перед запуском — там же
# (weights.json в формате {base, sensitivity} + образ с parseWeights).
# Эталоны 4 профиля x 3 узла x 3 репы ~1 ч; серия 3 плеча x 10 реп x 6 жертв
# ~3.2 ч.
set -x
cd "$(dirname "$0")"
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage} REDIS_ADDR=localhost:16379

# Статус-страница прогона (контейнер, docker compose). Здесь — чтобы она
# поднималась и при РУЧНОМ запуске этого скрипта, а не только через
# `make series`. Идемпотентно: если нужная страница уже отвечает, ничего не
# делает. Падение страницы на прогон не влияет.
../scripts/run-series.sh page mixed-calib || true
export HARNESS_OVERRIDE_HIGH_S_CPU=500m HARNESS_OVERRIDE_HIGH_S_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_MEM_LIM=1Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_CPU=500m HARNESS_OVERRIDE_HIGH_S_IO_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_HIGH_S_NET_CPU=500m HARNESS_OVERRIDE_HIGH_S_NET_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_NET_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_NET_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_NET_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_LOW_S_PRIMARIES=300000
echo "=== BASELINE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-stage-mixed-calib.yaml --baseline
rc=$?
echo "=== BASELINE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
echo "=== PRESSURE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-stage-mixed-calib.yaml --pressure --scenarios mixed3
rc=$?
echo "=== PRESSURE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
