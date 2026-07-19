#!/bin/bash
# LLC-абляционная серия STAGE (запуск с хоста): бейзлайны (2 профиля x 3 ноды
# x 3 репы, ~35 мин) + pressure-сценарий llc (3 плеча x 10 реп x 6 жертв,
# ~3 часа). ПЕРЕД запуском выполнить чеклист из шапки config-stage-llc.yaml
# (rollout агента, weights.json -> llc-only, smoke llc_miss_rate)!
#
# high-s по умолчанию рассчитан на 8-ядерные ноды — на 2-vCPU STAGE обязателен
# полный набор оверрайдов (CPU/THREADS/PRIMARIES/MEM), иначе Filter не пустит
# жертву ни на одну ноду (mem 4Gi > 1.87Gi ноды).
set -x
cd "$(dirname "$0")"
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage} REDIS_ADDR=localhost:16379

# Статус-страница прогона (контейнер, docker compose). Здесь — чтобы она
# поднималась и при РУЧНОМ запуске этого скрипта, а не только через
# `make series`. Идемпотентно: если нужная страница уже отвечает, ничего не
# делает. Падение страницы на прогон не влияет.
../scripts/run-series.sh page llc || true
export HARNESS_OVERRIDE_HIGH_S_CPU=500m HARNESS_OVERRIDE_HIGH_S_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_MEM_LIM=1Gi \
       HARNESS_OVERRIDE_LOW_S_PRIMARIES=300000
echo "=== BASELINE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-stage-llc.yaml --baseline
echo "=== BASELINE DONE $(date +%H:%M:%S) epoch=$(date +%s) ==="
echo "=== PRESSURE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-stage-llc.yaml --pressure --scenarios llc
echo "=== PRESSURE DONE $(date +%H:%M:%S) epoch=$(date +%s) ==="
