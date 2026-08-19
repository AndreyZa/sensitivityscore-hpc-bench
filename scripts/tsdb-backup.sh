#!/usr/bin/env bash
# Резервная копия TSDB Prometheus: снимок через admin API + выгрузка наружу.
#
# Зачем. Год истории серий лежит в hostPath на одной ВМ (ss-system) и до
# 19.08.2026 не бэкапился никак. Это было защитимо ровно потому, что точка
# правды чисел — ClickHouse, у которого свой backup.sh: Prometheus держал
# наблюдаемость, а не результаты. Защита перестаёт работать в тот день, когда
# в текст пойдёт хоть один график, существующий только здесь — например,
# трасса осей чувствительности во время конкретного прогона.
#
# Почему снимок, а не tar по каталогу на живую. Prometheus держит текущий блок
# в памяти и WAL; tar по каталогу под работающим процессом даёт заведомо
# несогласованную копию головного блока. Admin API делает hardlink-снимок
# согласованного состояния — дёшево по месту (жёсткие ссылки на те же файлы)
# и корректно.
#
#   ./tsdb-backup.sh [каталог-назначения]     (по умолчанию ~/ss-backups)
set -euo pipefail

NS=${MONITORING_NAMESPACE:-sensitivityscore-monitoring}
KUBECTL=${KUBECTL:-kubectl}
DEST=${1:-$HOME/ss-backups}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$DEST"

echo "— снимок ————————————————————————————————————————————————"
snap=$($KUBECTL -n "$NS" exec deploy/prometheus -c prometheus -- \
    wget -q -O- --post-data='' http://localhost:9090/api/v1/admin/tsdb/snapshot 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["name"])')
[ -n "$snap" ] || { echo "снимок не создан — включён ли --web.enable-admin-api?" >&2; exit 1; }
echo "  $snap"

out="$DEST/prometheus-tsdb-$STAMP.tar.gz"
echo "— выгрузка ——————————————————————————————————————————————"
# Через stdin пода, а не kubectl cp: cp тянет tar внутрь контейнера и на
# больших каталогах ведёт себя хуже, а поток сразу пишется сжатым.
$KUBECTL -n "$NS" exec deploy/prometheus -c prometheus -- \
    tar czf - -C /data/snapshots "$snap" > "$out"

# Снимок в поде — жёсткие ссылки, но они держат старые блоки от удаления
# retention'ом. Не убрав его, мы бы медленно съели диск ss-system, где рядом
# лежат данные Grafana и том ClickHouse.
$KUBECTL -n "$NS" exec deploy/prometheus -c prometheus -- \
    rm -rf "/data/snapshots/$snap"

size=$(du -h "$out" | cut -f1)
echo "  $out ($size)"
echo ""
echo "восстановление: распаковать в /var/lib/sensitivityscore/prometheus на ss-system"
echo "  (под остановлен), содержимое снимка кладётся вместо блоков; WAL не нужен."
