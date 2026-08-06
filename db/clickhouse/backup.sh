#!/usr/bin/env bash
# Самодостаточный бэкап БД sensitivityscore из ClickHouse-приёмника.
#
#   make ch-backup                       # поверх make ch-tunnel (localhost:8123 -> .72)
#   CH=http://host:8123 BACKUP_DIR=~/phd ./db/clickhouse/backup.sh
#
# Снимает DDL (SHOW CREATE TABLE) + данные в двух форматах (Native — точное
# восстановление; Parquet — переносимый хедж при version skew), кладёт рядом
# restore.sh и MANIFEST.txt, ВЕРИФИЦИРУЕТ восстановлением во временную БД на
# том же сервере и пакует в $BACKUP_DIR/sensitivityscore-ch-backup-<дата>.tar.gz.
# Обкатан 06.08.2026: этим архивом засеян in-cluster CH на STAGE (копия .72
# сошлась побайтно). Зависимости: только curl + tar, venv не нужен.
set -euo pipefail
CH="${CH:-http://localhost:8123}"
DB="${DB:-sensitivityscore}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/phd}"
DATE=$(date +%F)
NAME="sensitivityscore-ch-backup-$DATE"
WORK=$(mktemp -d)
BK="$WORK/$NAME"
mkdir -p "$BK"
trap 'rm -rf "$WORK"' EXIT

q(){ curl -sS --fail-with-body "$CH/?database=$DB" --data-binary "$1"; }

VER=$(curl -sS --fail-with-body "$CH/" --data-binary 'SELECT version()')
RES_N=$(q 'SELECT count() FROM results')
BAS_N=$(q 'SELECT count() FROM baselines')
LABELS=$(q "SELECT groupUniqArray(run_label) FROM (SELECT run_label FROM results UNION ALL SELECT run_label FROM baselines)")

echo "CH $VER @ $CH | results=$RES_N baselines=$BAS_N"

# DDL по таблице (по одному стейтменту — HTTP выполняет по одному запросу)
q 'SHOW CREATE TABLE results FORMAT TSVRaw'   > "$BK/results.sql"
q 'SHOW CREATE TABLE baselines FORMAT TSVRaw' > "$BK/baselines.sql"

for T in results baselines; do
  q "SELECT * FROM $T FORMAT Native"  > "$BK/$T.native"
  q "SELECT * FROM $T FORMAT Parquet" > "$BK/$T.parquet"
done

# --- restore.sh ---
cat > "$BK/restore.sh" <<'RST'
#!/usr/bin/env bash
# Восстановление БД sensitivityscore из этого бэкапа в ClickHouse.
#   CH=http://localhost:8123 ./restore.sh          # по умолчанию localhost:8123
# Требует поднятого CH (на прод — in-cluster через make ch-forward; локально —
# свой инстанс/ch-tunnel).
set -euo pipefail
CH="${CH:-http://localhost:8123}"
DB=sensitivityscore
here="$(cd "$(dirname "$0")" && pwd)"
echo "CH=$CH  DB=$DB"
curl -sS --fail-with-body "$CH/" --data-binary "CREATE DATABASE IF NOT EXISTS $DB" >/dev/null
for T in results baselines; do
  curl -sS --fail-with-body "$CH/" --data-binary @"$here/$T.sql" >/dev/null   # схема таблицы
  # данные: Native (точно); при несовместимости версий CH — заменить на Parquet
  curl -sS --fail-with-body "$CH/?query=INSERT%20INTO%20$DB.$T%20FORMAT%20Native" \
       --data-binary @"$here/$T.native" >/dev/null
  n=$(curl -sS "$CH/" --data-binary "SELECT count() FROM $DB.$T")
  echo "  $T восстановлено: $n строк"
done
echo "готово. (числа сверить с MANIFEST.txt)"
RST
chmod +x "$BK/restore.sh"

# --- MANIFEST ---
cat > "$BK/MANIFEST.txt" <<MAN
Бэкап ClickHouse БД '$DB' — исследование SensitivityScore
Дата бэкапа : $DATE
Источник    : $CH, сервер ClickHouse $VER
Таблицы     : results ($RES_N строк, RAW со всеми версиями ReplacingMergeTree)
              baselines ($BAS_N строк)
Метки серий : $LABELS

Провенанс (stand/run_label/commit/config/weights) — внутри строк: дамп
самодостаточен для воспроизведения отчётов.

СОДЕРЖИМОЕ:
  results.sql / baselines.sql   — DDL (SHOW CREATE TABLE)
  results.native / *.native     — данные, формат ClickHouse Native (точное восстановление)
  results.parquet / *.parquet   — те же данные, Parquet (переносимо; хедж при version skew)
  restore.sh                    — восстановление (CREATE DB + DDL + INSERT Native)

ВОССТАНОВЛЕНИЕ:
  CH=http://<host>:8123 ./restore.sh
  затем сверить счётчики строк с этим файлом.
  Проверка после FINAL схлопывания версий:
    SELECT count() FROM $DB.results   -- ожидаемо <= $RES_N (RAW), уникальных меньше
MAN

# --- ВЕРИФИКАЦИЯ: развернуть во временную БД на том же сервере, сверить, снести ---
echo "=== проверка восстановлением во временную БД ==="
TDB=${DB}_bkptest
curl -sS "$CH/" --data-binary "DROP DATABASE IF EXISTS $TDB" >/dev/null
curl -sS "$CH/" --data-binary "CREATE DATABASE $TDB" >/dev/null
ok=1
for T in results baselines; do
  curl -sS "$CH/" --data-binary "CREATE TABLE $TDB.$T AS $DB.$T" >/dev/null
  curl -sS "$CH/?query=INSERT%20INTO%20$TDB.$T%20FORMAT%20Native" --data-binary @"$BK/$T.native" >/dev/null
  got=$(curl -sS "$CH/" --data-binary "SELECT count() FROM $TDB.$T")
  src=$(q "SELECT count() FROM $T")
  if [ "$got" = "$src" ]; then echo "  ✓ $T: развернулось $got == источник $src"; else echo "  ✗ $T: $got != $src"; ok=0; fi
done
curl -sS "$CH/" --data-binary "DROP DATABASE $TDB" >/dev/null
[ "$ok" = 1 ] || { echo "ВЕРИФИКАЦИЯ ПРОВАЛЕНА — архив не пишем"; exit 1; }
echo "верификация пройдена — бэкап разворачивается"

OUT="$BACKUP_DIR/$NAME.tar.gz"
mkdir -p "$BACKUP_DIR"
tar -C "$WORK" -czf "$OUT" "$NAME"
echo "=== АРХИВ ==="
ls -la "$OUT"
echo "sha256: $(sha256sum "$OUT" | cut -d' ' -f1)"
echo "напоминание: копию — ВНЕ этой машины (облако/второй диск)."
