#!/usr/bin/env bash
# Не дать применить к стенду ЧУЖОЙ оверлей мониторинга.
#
# Зачем. `MONITORING_OVERLAY` по умолчанию указывал на stage, а прод-оверлей
# несёт вещи, без которых стенд теряет функции молча:
#   - Grafana: hostPort 3000 + анонимный просмотр (это единственный способ,
#     которым коллеги партнёра видят дашборды — Prometheus снаружи не
#     публикуется вовсе);
#   - Prometheus: retention 365 дней и 60 ГБ вместо 30 дней и 6 ГБ (год истории
#     — это материал диссертации, серии сравниваются спустя месяцы), лимит
#     памяти 2 ГиБ вместо 512 МиБ (cAdvisor на 128-поточных узлах даёт кратно
#     больше рядов, чем на облачной ноде STAGE), метка провенанса STAND=prod.
#
# Поймано 19.08.2026 на живом проде: `make monitoring-reload` без переменной
# снял с Grafana hostPort и анонимный доступ (эндпоинт для коллег умер), а
# Prometheus уехал на 6-ГБ retention и 512-МиБ лимит. Тот же класс ловушки, что
# у CH_KUSTOMIZE (scripts/ch-placement-guard.sh) — дефолт, безопасный для
# разработки и разрушительный для прода.
#
# Проверка двухступенчатая и опирается на кластер, а не на переменные:
#   1) есть ли в кластере узлы с ролью bench. Нет — это не измерительный стенд
#      (лаба, dev-кластер), выходим молча;
#   2) если есть — оверлей обязан рендерить STAND=prod. Так проверка не зависит
#      от имени каталога и ловит подмену в обе стороны.
#
# Обойти сознательно: MONITORING_ALLOW_OVERLAY=1.
set -euo pipefail

DIR=${1:?укажи каталог kustomize: $0 <k8s/monitoring/overlays/...>}
KUBECTL=${KUBECTL:-kubectl}
ROLE_BENCH="node-role.kubernetes.io/bench"

if [ "${MONITORING_ALLOW_OVERLAY:-0}" = "1" ]; then
    echo "[mon-guard] MONITORING_ALLOW_OVERLAY=1 — проверка оверлея пропущена"
    exit 0
fi

bench=$($KUBECTL get nodes --selector="$ROLE_BENCH" -o name 2>/dev/null | wc -l | tr -d ' ')
if [ "$bench" = "0" ]; then
    echo "[mon-guard] измерительных узлов (роль bench) нет — оверлей мониторинга не критичен"
    exit 0
fi

stand=$($KUBECTL kustomize "$DIR" | python3 -c '
import sys, re
# Значение env STAND у Deployment prometheus. Разбор построчный, без внешних
# зависимостей: у стенда в CI нет pyyaml, а рендер kustomize стабилен.
lines = sys.stdin.read().splitlines()
val = ""
for i, ln in enumerate(lines):
    if ln.strip() == "- name: STAND":
        for nxt in lines[i + 1:i + 3]:
            m = re.match(r"\s+value:\s*(\S+)", nxt)
            if m:
                val = m.group(1).strip("\"'"'"'")
                break
        break
print(val)
')

if [ "$stand" != "prod" ]; then
    cat >&2 <<MSG
[mon-guard] ОТКАЗ: в кластере $bench измерительн(ый|ых) узл(ов) с ролью bench,
            то есть это прод-стенд, а рендер '$DIR' даёт STAND='${stand:-<пусто>}'.

  Этот оверлей снимет с Grafana hostPort 3000 и анонимный просмотр (эндпоинт,
  по которому дашборды смотрят коллеги, перестанет отвечать) и уронит
  retention Prometheus до 30 дней / 6 ГБ, а лимит памяти — до 512 МиБ.

  Правильно:

      make monitoring-deploy   MONITORING_OVERLAY=k8s/monitoring/overlays/prod
      make monitoring-reload   MONITORING_OVERLAY=k8s/monitoring/overlays/prod

  (прод-оверлей и так стоит по умолчанию — переменную задавать не нужно)

  Если это осознанно:

      make monitoring-reload MONITORING_OVERLAY=$DIR MONITORING_ALLOW_OVERLAY=1
MSG
    exit 1
fi

echo "[mon-guard] ок: оверлей '$DIR' рендерит STAND=prod"
