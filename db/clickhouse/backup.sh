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

# Список таблиц берём ИЗ БАЗЫ, а не зашиваем: до 18.08.2026 здесь были жёстко
# прописаны results и baselines, поэтому energy_windows (миграция 003, данные
# энерговетки) в бэкап не попадала вовсе — молча, без единого предупреждения.
# Любая будущая таблица теперь подхватывается сама.
TABLES=$(q "SELECT name FROM system.tables WHERE database = '$DB' AND engine NOT LIKE '%View' ORDER BY name FORMAT TSV")
[ -n "$TABLES" ] || { echo "в БД '$DB' нет таблиц — нечего бэкапить"; exit 1; }

COUNTS=""
for T in $TABLES; do
  N=$(q "SELECT count() FROM $T")
  COUNTS="$COUNTS  $T=$N"
done
LABELS=$(q "SELECT groupUniqArray(run_label) FROM (SELECT run_label FROM results UNION ALL SELECT run_label FROM baselines)")

echo "CH $VER @ $CH |$COUNTS"

# DDL по таблице (по одному стейтменту — HTTP выполняет по одному запросу)
for T in $TABLES; do
  q "SHOW CREATE TABLE $T FORMAT TSVRaw" > "$BK/$T.sql"
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
# Набор таблиц восстановления задаёт сам архив (файлы *.native), а не список
# в коде — иначе новая таблица снова оказалась бы вне восстановления.
for f in "$here"/*.native; do
  T=$(basename "$f" .native)
  # Схема таблицы. IF NOT EXISTS обязателен: на проде таблицы к моменту
  # восстановления уже созданы schema-Job'ом (ступень 5 runbook идёт после
  # ch-incluster-deploy), и без него restore падает на TABLE_ALREADY_EXISTS
  # (так и случилось 18.08.2026). Если существующая схема вдруг разойдётся с
  # бэкапной — это всплывёт ошибкой на INSERT Native ниже, молча не пройдёт.
  sed '1s/^CREATE TABLE/CREATE TABLE IF NOT EXISTS/' "$here/$T.sql" \
    | curl -sS --fail-with-body "$CH/" --data-binary @- >/dev/null
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
Таблицы     :$COUNTS
              (строки RAW, со всеми версиями ReplacingMergeTree)
Метки серий : $LABELS

Провенанс (stand/run_label/commit/config/weights) — внутри строк: дамп
самодостаточен для воспроизведения отчётов.

СОДЕРЖИМОЕ (по одному комплекту на каждую таблицу выше):
  <таблица>.sql       — DDL (SHOW CREATE TABLE)
  <таблица>.native    — данные, формат ClickHouse Native (точное восстановление)
  <таблица>.parquet   — те же данные, Parquet (переносимо; хедж при version skew)
  restore.sh          — восстановление (CREATE DB + DDL + INSERT Native)

ВОССТАНОВЛЕНИЕ:
  CH=http://<host>:8123 ./restore.sh
  затем сверить счётчики строк с этим файлом.
  Проверка после FINAL схлопывания версий:
    SELECT count() FROM $DB.results   -- ожидаемо <= RAW-числа выше, уникальных меньше
MAN

# --- ВЕРИФИКАЦИЯ: развернуть во временную БД на том же сервере, сверить, снести ---
echo "=== проверка восстановлением во временную БД ==="
TDB=${DB}_bkptest
curl -sS "$CH/" --data-binary "DROP DATABASE IF EXISTS $TDB" >/dev/null
curl -sS "$CH/" --data-binary "CREATE DATABASE $TDB" >/dev/null
ok=1
for T in $TABLES; do
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
