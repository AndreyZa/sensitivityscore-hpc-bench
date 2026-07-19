#!/bin/bash
# Серия проверки cˢ_io > 0 на STAGE (диск-чувствительность): эталоны + серия
# ОДНОЙ сессией. Мотивация и дизайн — в шапке config-stage-io-sensitivity.yaml.
# Запуск: make series SERIES=io-sensitivity.
#
# Оверрайды под STAGE (2 vCPU): те же, что у прочих серий (500m, 2 потока,
# 300k частиц ~ сопоставимый compute у обоих профилей), плюс доза дискового
# вывода high-s-io: IO_TOTAL_BURSTS=16 × 32МБ = 512МБ. Под диск-штормом сброс
# 512МБ растёт с ~1.2с до ~50-90с (throttle ×~23); ограничен, чтобы при
# одновременном попадании нескольких high-s-io на штормовой узел (плечо
# trimaran) суммарный сброс не упёрся в job_timeout (900с).
set -x
cd "$(dirname "$0")"
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage} REDIS_ADDR=localhost:16379

# Статус-страница прогона (контейнер, docker compose). Здесь — чтобы она
# поднималась и при РУЧНОМ запуске этого скрипта, а не только через
# `make series`. Идемпотентно: если нужная страница уже отвечает, ничего не
# делает. Падение страницы на прогон не влияет.
../scripts/run-series.sh page io-sensitivity || true
export HARNESS_OVERRIDE_HIGH_S_IO_CPU=500m HARNESS_OVERRIDE_HIGH_S_IO_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_BURST_MB=32 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_INTERVAL_SECONDS=0 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_TOTAL_BURSTS=16 \
       HARNESS_OVERRIDE_LOW_S_PRIMARIES=300000
echo "=== BASELINE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-stage-io-sensitivity.yaml --baseline
rc=$?
echo "=== BASELINE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
echo "=== PRESSURE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
.venv/bin/python run_experiment.py --config config-stage-io-sensitivity.yaml --pressure --scenarios io-sensitivity
rc=$?
echo "=== PRESSURE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="
