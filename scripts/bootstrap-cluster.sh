#!/usr/bin/env bash
# scripts/bootstrap-cluster.sh — первичная настройка кластера под стенд:
# разметка узлов ролями, неймспейс, in-cluster Redis. Идемпотентен —
# безопасно перезапускать (kubectl label/taint --overwrite, apply).
#
# Топология (см. k8s/scheduler-config/*.yaml и docs/«Методика измерений.md»):
#   - системный узел (роль ss-system): redis, sensitivityscore-scheduler,
#     metrics-server — на ВМ рядом с control-plane, с taint'ом, чтобы туда
#     не попадали экспериментальные поды. Урок 14.07.2026: планировщик,
#     прибитый к произвольному worker'у, умер вместе с ним при пересборке
#     кластера и утопил ночную серию;
#   - измерительные узлы (роль bench): только задачи Geant4 и агрессоры.
#
# Роли — штатный механизм node-role.kubernetes.io/*: видны в колонке ROLES
# `kubectl get nodes`, понятны админу прод-стенда, и это ЕДИНСТВЕННЫЙ
# критерий разметки (харнесс и statusserver селектят только по ролям,
# ad-hoc лейблы вроде node=ss-system не используются).
#
# Использование:
#   ./scripts/bootstrap-cluster.sh <ss-system-узел> [<ещё-узел>...]
#   SKIP_TAINT=1 ...   — только лейблы, без taint (миграция при живом прогоне:
#                        сначала дать компонентам toleration, потом taint)
#   KUBECTL=...        — нестандартный kubectl (kind, /snap/bin/kubectl)
set -euo pipefail

KUBECTL=${KUBECTL:-kubectl}
NAMESPACE="sensitivityscore-system"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ $# -lt 1 ]; then
    echo "usage: $0 <ss-system-node> [<ss-system-node>...]" >&2
    echo "  все остальные worker-узлы (без control-plane) получат роль bench" >&2
    exit 1
fi
SS_NODES=("$@")

echo "[bootstrap] роли узлов: ss-system = ${SS_NODES[*]}"
for n in "${SS_NODES[@]}"; do
    $KUBECTL label node "$n" node-role.kubernetes.io/ss-system= --overwrite
    $KUBECTL label node "$n" node-role.kubernetes.io/bench- >/dev/null 2>&1 || true
    if [ -z "${SKIP_TAINT:-}" ]; then
        $KUBECTL taint node "$n" node-role.kubernetes.io/ss-system=:NoSchedule --overwrite
    else
        echo "[bootstrap]   SKIP_TAINT=1 — taint на $n не ставится"
    fi
done

echo "[bootstrap] роль bench остальным worker-узлам"
workers=$($KUBECTL get nodes --selector='!node-role.kubernetes.io/control-plane,!node-role.kubernetes.io/ss-system' -o jsonpath='{.items[*].metadata.name}')
for n in $workers; do
    $KUBECTL label node "$n" node-role.kubernetes.io/bench= --overwrite
done

echo "[bootstrap] неймспейс ${NAMESPACE}"
$KUBECTL create namespace "${NAMESPACE}" --dry-run=client -o yaml | $KUBECTL apply -f -

echo "[bootstrap] in-cluster Redis (метрик-бэкенд, docs §3.2) — из манифеста"
$KUBECTL apply -f "${REPO_ROOT}/k8s/scheduler-config/redis.yaml"

echo "[bootstrap] итоговая разметка:"
$KUBECTL get nodes

echo "[bootstrap] готово. Дальше:"
echo "  1. Собрать образ плагина в форке scheduler-plugins: make scheduler-plugin-image"
echo "  2. Задеплоить: make scheduler-apply-config scheduler-deploy"
echo "  3. make trimaran-deps  (metrics-server; после установки перевести на"
echo "     системный узел — см. патч в docs/«Методика измерений.md» или CLAUDE)"
echo "  4. kubectl apply -f metrics-agent/deploy/daemonset.yaml + калибровки"
echo "     NET_REFERENCE_MBPS / LLC_REFERENCE_MISSES_PER_SEC (kubectl set env)"
echo "  5. Sanity-check: make pilot (см. harness/README.md)"
