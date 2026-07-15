#!/bin/bash
# Серия net-diff на STAGE: эталоны + серия одной сессией. Мотивация и дизайн —
# в шапке config-stage-net-diff.yaml. Запуск: make series SERIES=net-diff.
#
# КЛЮЧЕВОЕ:
#  - оба профиля получают ИДЕНТИЧНЫЕ ресурсы и compute (500m, 1 поток, 100k
#    частиц, память 384Mi/2Gi) — иначе профиль-слепой default развёл бы их по
#    ресурсам, и различение SS нельзя было бы отделить от артефакта упаковки.
#    Отличие только в сетевом выводе: high-s-net стримит 2ГБ на sink на крит.
#    пути (OUTPUT_MODE=stream), net-insensitive не стримит (none в профиле).
#  - sink (ss-sink) ПРИБИТ к w8 — НЕ штормимая нода (w9) и НЕ egress-приёмник
#    шторма (w10); жертва на w9 стримит cross-node -> делит uplink w9 со
#    штормом. Разворачиваем ДО эталонов (в пустой namespace после preflight).
set -x
cd "$(dirname "$0")" || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage} REDIS_ADDR=localhost:16379

# sink для стрима жертв (пиннут на w8); ждём Ready перед эталонами.
kubectl apply -f ../k8s/net-sink/sink-stage.yaml
kubectl -n sensitivityscore-bench wait --for=condition=Ready pod/ss-sink --timeout=120s

# Двойники: ИДЕНТИЧНЫЕ compute/ресурсы; отличие только в стриме.
export HARNESS_OVERRIDE_HIGH_S_NET_CPU=500m HARNESS_OVERRIDE_HIGH_S_NET_THREADS=1 \
       HARNESS_OVERRIDE_HIGH_S_NET_PRIMARIES=100000 \
       HARNESS_OVERRIDE_HIGH_S_NET_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_NET_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_HIGH_S_NET_OUTPUT_MODE=stream \
       HARNESS_OVERRIDE_HIGH_S_NET_NET_SINK_HOST=ss-sink \
       HARNESS_OVERRIDE_HIGH_S_NET_NET_SINK_PORT=9000 \
       HARNESS_OVERRIDE_HIGH_S_NET_NET_TOTAL_MB=2048 \
       HARNESS_OVERRIDE_NET_INSENSITIVE_CPU=500m HARNESS_OVERRIDE_NET_INSENSITIVE_THREADS=1 \
       HARNESS_OVERRIDE_NET_INSENSITIVE_PRIMARIES=100000 \
       HARNESS_OVERRIDE_NET_INSENSITIVE_MEM_REQ=384Mi HARNESS_OVERRIDE_NET_INSENSITIVE_MEM_LIM=2Gi
# NET_TIMEOUT в entrypoint дефолтит 600с — под штормом 2ГБ ~104с, запас есть.

echo "=== BASELINE START $(date +%H:%M:%S) ==="
.venv/bin/python run_experiment.py --config config-stage-net-diff.yaml --baseline
echo "=== BASELINE DONE $(date +%H:%M:%S) rc=$? ==="
echo "=== PRESSURE START $(date +%H:%M:%S) ==="
.venv/bin/python run_experiment.py --config config-stage-net-diff.yaml --pressure --scenarios net-diff
echo "=== PRESSURE DONE $(date +%H:%M:%S) rc=$? ==="

# Прибрать sink (следующая серия ждёт пустой namespace).
kubectl -n sensitivityscore-bench delete -f ../k8s/net-sink/sink-stage.yaml --ignore-not-found --wait=false
