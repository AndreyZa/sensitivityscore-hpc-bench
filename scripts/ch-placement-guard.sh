#!/usr/bin/env bash
# Не дать развернуть in-cluster ClickHouse БЕЗ привязки к системному узлу на
# стенде, где есть измерительные узлы.
#
# Зачем. CH — это инсерты, мержи и TSDB-подобная нагрузка на диск. Оказавшись
# на bench-узле, он загрязняет ровно те LLC/IO-счётчики, ради которых стенд и
# существует, а preflight серии сочтёт его посторонним подом и остановит
# прогон. Раньше от этого защищала одна строчка в runbook: `make
# ch-incluster-deploy` без аргументов брал `base` — оверлей без nodeSelector.
# Прод-оверлей пинит и StatefulSet, и schema-Job (job-placement-patch.yaml).
#
# Проверка намеренно двухступенчатая:
#   1) есть ли в кластере узлы с ролью bench. Нет — рисковать нечем (одноузловая
#      лаба, dev-кластер), выходим молча;
#   2) если есть — каждый рабочий объект рендера (StatefulSet, Job) обязан
#      нести nodeSelector на роль ss-system.
#
# Обойти сознательно: CH_ALLOW_UNPINNED=1 (например, стенд без роли ss-system,
# где CH ставят временно и серий не гоняют).
set -euo pipefail

DIR=${1:?укажи каталог kustomize: $0 <k8s/clickhouse/...>}
KUBECTL=${KUBECTL:-kubectl}
ROLE_SS="node-role.kubernetes.io/ss-system"
ROLE_BENCH="node-role.kubernetes.io/bench"

if [ "${CH_ALLOW_UNPINNED:-0}" = "1" ]; then
    echo "[ch-guard] CH_ALLOW_UNPINNED=1 — проверка размещения пропущена"
    exit 0
fi

bench=$($KUBECTL get nodes --selector="$ROLE_BENCH" -o name 2>/dev/null | wc -l | tr -d ' ')
if [ "$bench" = "0" ]; then
    echo "[ch-guard] измерительных узлов (роль bench) нет — размещение CH не критично"
    exit 0
fi

unpinned=$($KUBECTL kustomize "$DIR" | python3 -c '
import sys
docs = sys.stdin.read().split("\n---")
bad = []
for d in docs:
    kind = ""
    for line in d.splitlines():
        if line.startswith("kind: "):
            kind = line.split(None, 1)[1].strip()
            break
    if kind not in ("StatefulSet", "Job"):
        continue
    name = ""
    for line in d.splitlines():
        if line.strip().startswith("name: "):
            name = line.split("name:", 1)[1].strip()
            break
    if "node-role.kubernetes.io/ss-system" not in d:
        bad.append(f"{kind}/{name}")
print(" ".join(bad))
')

if [ -n "$unpinned" ]; then
    cat >&2 <<MSG
[ch-guard] ОТКАЗ: в кластере $bench измерительн(ый|ых) узл(ов) с ролью bench,
           а в рендере '$DIR' без привязки к $ROLE_SS: $unpinned

  ClickHouse на измерительном узле загрязняет LLC/IO-метрики серии и валит
  preflight. Разворачивай прод-оверлеем:

      make ch-incluster-deploy CH_KUSTOMIZE=k8s/clickhouse/overlays/prod

  Если это осознанно (стенд без роли ss-system, серий не будет):

      make ch-incluster-deploy CH_KUSTOMIZE=$DIR CH_ALLOW_UNPINNED=1
MSG
    exit 1
fi

echo "[ch-guard] ок: все рабочие объекты CH привязаны к $ROLE_SS"
