#!/bin/bash
# Pressure-серия STAGE (запуск с хоста): сценарии io + net,
# 3 плеча x 10 реп x 6 жертв каждый (~6 часов).
# Env-оверрайды ДОЛЖНЫ совпадать с run-stage-baseline.sh — это знаменатели
# slowdown. Требует: port-forward Redis (localhost:16379), свежие образы
# metrics-agent/aggressor/scheduler на стенде (Net-ось), NET_REFERENCE_MBPS
# на DaemonSet агента (make netcheck-run).
set -x
cd "$(dirname "$0")"
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage} REDIS_ADDR=localhost:16379

# Статус-страница прогона (контейнер, docker compose). Здесь — чтобы она
# поднималась и при РУЧНОМ запуске этого скрипта, а не только через
# `make series`. Идемпотентно: если нужная страница уже отвечает, ничего не
# делает. Падение страницы на прогон не влияет.
../scripts/run-series.sh page pressure || true
export HARNESS_OVERRIDE_HIGH_S_IO_CPU=500m HARNESS_OVERRIDE_HIGH_S_IO_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_HIGH_S_NET_CPU=500m HARNESS_OVERRIDE_HIGH_S_NET_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_NET_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_NET_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_NET_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_LOW_S_PRIMARIES=300000
echo "=== PRESSURE START $(date +%H:%M:%S) ==="
.venv/bin/python run_experiment.py --config config-stage.yaml --pressure --scenarios io net
echo "=== PRESSURE DONE $(date +%H:%M:%S) ==="
