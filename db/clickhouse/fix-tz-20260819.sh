#!/usr/bin/env bash
# fix-tz-20260819.sh — одноразовая починка сдвига таймстампов −3ч в ClickHouse.
#
# Причина: clickhouse_connect трактовал naive datetime как ЛОКАЛЬНОЕ время
# клиента — на MSK-хостах все submit/start/end_ts уезжали на −10800с при
# заливке (см. коммит be8cf1a, там же диагноз). Parquet всегда был честным.
#
# Что делает:
#   1) На обоих приёмниках (home :8123, prod :8124): удаляет прод-строки и
#      перезаливает их из parquet исправленным загрузчиком (идемпотентно по
#      построению — parquet источник правды).
#   2) Только на home: сдвигает НЕ-прод строки (stage, local) на +10800с.
#      От повторного прогона защищает маркер в sensitivityscore.maintenance_log
#      — сдвиг применяется ровно один раз.
#   3) Сверяет якоря: min(start_ts) prod-mixed-calib == 1787096950 (из
#      parquet), максимумы stage до/после.
#
# Запуск с лабы (.72), из корня репо, поверх живых 8123/8124:
#   bash db/clickhouse/fix-tz-20260819.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

MARKER="tz-shift-20260819"
q() { # q <port> <sql>
  local out
  out=$(curl -sS "localhost:$1/" --data-binary "$2") || { echo "CH $1 FAIL: $2" >&2; exit 1; }
  [ -n "$out" ] && echo "$out"
  return 0
}

echo "== 1. Прод-строки: удалить и перезалить из parquet =="
for port in 8123 8124; do
  for t in results baselines; do
    q "$port" "DELETE FROM sensitivityscore.$t WHERE stand = 'prod'"
  done
  echo "  :$port очищен (prod): results=$(q "$port" "SELECT count() FROM sensitivityscore.results WHERE stand='prod'") baselines=$(q "$port" "SELECT count() FROM sensitivityscore.baselines WHERE stand='prod'")"
done

for label in prod-smoke prod-mixed-calib; do
  make ch-load-all STAND=prod RUN_LABEL="$label" \
    RESULTS_FILE="harness/results/results-$label.parquet" \
    BASELINES_FILE="harness/results/baselines-$label.parquet"
done

echo "== 2. Home: сдвиг НЕ-прод строк на +10800с (однократно, маркер $MARKER) =="
q 8123 "CREATE TABLE IF NOT EXISTS sensitivityscore.maintenance_log (id String, applied_at DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY id"
if [ "$(q 8123 "SELECT count() FROM sensitivityscore.maintenance_log WHERE id = '$MARKER'")" != "0" ]; then
  echo "  маркер уже стоит — сдвиг НЕ повторяю"
else
  echo "  stage max(end_ts) до:  $(q 8123 "SELECT max(end_ts) FROM sensitivityscore.results WHERE stand != 'prod'")"
  for t in results baselines; do
    q 8123 "ALTER TABLE sensitivityscore.$t UPDATE submit_ts = submit_ts + 10800, start_ts = start_ts + 10800, end_ts = end_ts + 10800 WHERE stand != 'prod' SETTINGS mutations_sync = 2"
  done
  q 8123 "INSERT INTO sensitivityscore.maintenance_log (id) VALUES ('$MARKER')"
  echo "  stage max(end_ts) после: $(q 8123 "SELECT max(end_ts) FROM sensitivityscore.results WHERE stand != 'prod'")"
fi

echo "== 3. Якоря =="
echo "  ожидание: prod-mixed-calib min(start_ts) = 1787096950 (из parquet)"
for port in 8123 8124; do
  echo "  :$port -> $(q "$port" "SELECT toUnixTimestamp64Milli(min(start_ts))/1000 FROM sensitivityscore.results WHERE run_label = 'prod-mixed-calib'")"
done
echo "готово"
