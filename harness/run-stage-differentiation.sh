#!/bin/bash
# Серия differentiation на STAGE: эталоны + серия одной сессией. Мотивация и
# дизайн — в шапке config-stage-differentiation.yaml. Запуск:
# make series SERIES=differentiation.
#
# КЛЮЧЕВОЕ: оба профиля получают ИДЕНТИЧНЫЕ ресурсы и compute (500m, 2 потока,
# 300k частиц, память 384Mi/2Gi) — иначе профиль-слепой default развёл бы их
# по узлам по ресурсам, и различение SS нельзя было бы отделить от артефакта
# упаковки. Отличие только в дисковом выводе: high-s-io пишет 512МБ на крит.
# пути (blocking), io-insensitive не пишет (OUTPUT_MODE=none в профиле).
set -x
cd "$(dirname "$0")"
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage} REDIS_ADDR=localhost:16379

# Статус-страница прогона (контейнер, docker compose). Здесь — чтобы она
# поднималась и при РУЧНОМ запуске этого скрипта, а не только через
# `make series`. Идемпотентно: если нужная страница уже отвечает, ничего не
# делает. Падение страницы на прогон не влияет.
../scripts/run-series.sh page differentiation || true
export HARNESS_OVERRIDE_HIGH_S_IO_CPU=500m HARNESS_OVERRIDE_HIGH_S_IO_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_BURST_MB=32 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_INTERVAL_SECONDS=0 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_TOTAL_BURSTS=16 \
       HARNESS_OVERRIDE_IO_INSENSITIVE_CPU=500m HARNESS_OVERRIDE_IO_INSENSITIVE_THREADS=2 \
       HARNESS_OVERRIDE_IO_INSENSITIVE_PRIMARIES=300000 \
       HARNESS_OVERRIDE_IO_INSENSITIVE_MEM_REQ=384Mi HARNESS_OVERRIDE_IO_INSENSITIVE_MEM_LIM=2Gi
echo "=== BASELINE START $(date +%H:%M:%S) ==="
.venv/bin/python run_experiment.py --config config-stage-differentiation.yaml --baseline
echo "=== BASELINE DONE $(date +%H:%M:%S) rc=$? ==="
echo "=== PRESSURE START $(date +%H:%M:%S) ==="
.venv/bin/python run_experiment.py --config config-stage-differentiation.yaml --pressure --scenarios differentiation
echo "=== PRESSURE DONE $(date +%H:%M:%S) rc=$? ==="
