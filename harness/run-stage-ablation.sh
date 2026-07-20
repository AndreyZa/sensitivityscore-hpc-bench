#!/bin/bash
# ТРЕТИЙ РЕЖИМ ВЕСОВ для абляции C3 (см. шапку config-stage-ablation.yaml):
# калиброванные цены осей, но В РЕЖИМЕ SENSITIVITY (base = 0). Ожидание
# объявлено в шапке конфига ДО прогона: ждём провал, близкий к stage-mixed.
#
# Эталоны + серия ОДНОЙ сессией (межсессионный дрейф стенда +13..23%).
# Чеклист перед запуском — в шапке конфига: weights.json ОБЯЗАН быть в режиме
# sensitivity, иначе preflight не пустит (и правильно сделает).
# Эталоны 4 профиля x 3 узла x 3 репы ~1 ч; серия 3 плеча x 10 реп x 6 жертв
# ~3.2 ч.
set -x
cd "$(dirname "$0")"
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage} REDIS_ADDR=localhost:16379

../scripts/run-series.sh page ablation || true

# Дозы профилей те же, что у calib и final: сравнивать режимы весов можно
# только при неизменном всём остальном.
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
.venv/bin/python run_experiment.py --config config-stage-ablation.yaml --baseline
rc=$?
echo "=== BASELINE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
echo "=== PRESSURE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-stage-ablation.yaml --pressure --scenarios mixed3
rc=$?
echo "=== PRESSURE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
