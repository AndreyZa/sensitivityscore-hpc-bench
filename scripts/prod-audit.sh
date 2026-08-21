#!/bin/bash
# Сводная проверка прод-стенда: всё ли развёрнутое совпадает с репозиторием.
#
# Зачем одной командой. Проверки существуют по отдельности и потому
# запускаются по случаю — а расходится обычно то, о чём не вспомнили.
# 21.08.2026 за один вечер нашлись три расхождения подряд: ConfigMap
# миграций отставал на три файла, prometheus.xml в поде был старой формы
# (том смонтирован через subPath и не обновляется), окна энергии жили в
# одном экземпляре. Каждое по отдельности незаметно, вместе — стенд,
# который нельзя воспроизвести.
#
#   KUBECONFIG=~/.kube/configs/prod scripts/prod-audit.sh
set -u
cd "$(dirname "$0")/.." || exit 2
KUBECTL=${KUBECTL:-kubectl}
NS=${NS:-sensitivityscore-system}
fails=0

step() { printf '\n== %s\n' "$1"; }
ok()   { printf '  ok: %s\n' "$1"; }
bad()  { printf '  ПРОБЛЕМА: %s\n' "$1"; fails=$((fails+1)); }

step "миграции ClickHouse"
scripts/ch-migrations-check.sh "$NS" | sed 's/^/  /' || fails=$((fails+1))

step "конфиг ClickHouse в поде == ConfigMap"
# Том смонтирован через subPath: kubelet кладёт файл при старте и больше
# не трогает. Значит apply без рестарта не доезжает — и это молча.
for f in prometheus.xml; do
    live=$($KUBECTL -n "$NS" exec clickhouse-0 -- cat "/etc/clickhouse-server/config.d/$f" 2>/dev/null | md5sum | cut -c1-32)
    # Точка в имени ключа экранируется: без этого jsonpath читает
    # data.prometheus.xml как вложенность, возвращает пустоту, и сравнение
    # всегда «не совпало» — страж, который кричит вместо проверки.
    want=$($KUBECTL -n "$NS" get cm clickhouse-config -o "jsonpath={.data.${f/./\\.}}" 2>/dev/null | md5sum | cut -c1-32)
    empty=$(printf '' | md5sum | cut -c1-32)
    if [ -z "$live" ] || [ "$want" = "$empty" ]; then
        bad "$f не прочитан ни из пода, ни из ConfigMap — проверка недействительна"
    elif [ "$live" = "$want" ]; then
        ok "$f совпадает"
    else
        bad "$f в поде отличается от ConfigMap — нужен rollout restart statefulset/clickhouse"
    fi
done

step "движок TimeSeries включён"
v=$($KUBECTL -n "$NS" exec clickhouse-0 -- clickhouse-client -q \
    "SELECT value FROM system.settings WHERE name='allow_experimental_time_series_table'" 2>/dev/null | tr -d '\r')
[ "$v" = "1" ] && ok "allow_experimental_time_series_table=1" \
    || bad "движок выключен ($v) — приём remote_write работать не будет"

step "приём рядов из Prometheus"
n=$($KUBECTL -n "$NS" exec clickhouse-0 -- clickhouse-client -q \
    "SELECT count() FROM timeSeriesData(sensitivityscore.prom_ts) WHERE timestamp > now() - INTERVAL 5 MINUTE" 2>/dev/null | tr -d '\r')
[ "${n:-0}" -gt 0 ] 2>/dev/null && ok "за 5 минут пришло $n сэмплов" \
    || bad "за 5 минут не пришло ни одного сэмпла — проверь remote_write"

step "зеркалирование в агрегатор"
DRY_RUN=1 scripts/ch-sync.sh 2>/dev/null | sed 's/^/  /' \
    | grep -qE "расходятся: нет|к переносу 0" && ok "расхождений нет" \
    || bad "базы разошлись — scripts/ch-sync.sh"

printf '\n'
[ "$fails" -eq 0 ] && { echo "аудит пройден"; exit 0; }
echo "аудит: проблем $fails"
exit 1
