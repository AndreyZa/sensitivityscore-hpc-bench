#!/bin/bash
# Сверка: миграции в ConfigMap кластера == миграции в репозитории.
#
# Зачем. ConfigMap `clickhouse-migrations` собирается ТОЛЬКО целью
# ch-incluster-deploy. `kubectl apply -k k8s/clickhouse/overlays/...` его
# не трогает — и это не гипотеза: 21.08.2026 на проде в нём лежали только
# 001-003, тогда как в репозитории было шесть. Таблицы metrics_samples,
# колонка bios_profile и prom_ts существовали лишь потому, что их
# накатывали руками; развёртывание с нуля подняло бы стенд без них.
#
# Отказ здесь дешевле молчания: расхождение чинится одной командой, а
# необнаруженное расхождение — потерянной серией.
#
#   scripts/ch-migrations-check.sh [namespace]
set -u
NS=${1:-sensitivityscore-system}
KUBECTL=${KUBECTL:-kubectl}
DIR="$(dirname "$0")/../db/clickhouse/migrations"

repo=$(ls "$DIR"/*.sql 2>/dev/null | xargs -n1 basename | sort | tr '\n' ' ')
[ -n "$repo" ] || { echo "не нашёл миграций в $DIR"; exit 2; }

live=$($KUBECTL -n "$NS" get cm clickhouse-migrations -o jsonpath='{.data}' 2>/dev/null \
    | tr ',' '\n' | grep -oE '[0-9]{3}-[a-z0-9-]+\.sql' | sort -u | tr '\n' ' ')
[ -n "$live" ] || { echo "ConfigMap clickhouse-migrations в $NS не найден или пуст"; exit 1; }

if [ "$repo" = "$live" ]; then
    echo "миграции совпадают ($(echo $repo | wc -w) шт.)"
    exit 0
fi
echo "РАСХОЖДЕНИЕ миграций:"
echo "  в репозитории: $repo"
echo "  в кластере:    $live"
echo "чинить: make ch-incluster-deploy"
exit 1
