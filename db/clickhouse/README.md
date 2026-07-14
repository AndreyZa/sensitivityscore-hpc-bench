# db/clickhouse — центральная агрегация результатов

ClickHouse на отдельном ПК-агрегаторе: результаты всех прогонов и стендов в
одном месте для кросс-стенд запросов и графиков. Модель — **batch-load**:
харнесс как прежде пишет `results.parquet` / `baselines.parquet`, отдельный
шаг заливает их сюда. Прогон от доступности ClickHouse не зависит.

Каждой строке при заливке добавляется провенанс: `stand` (какой стенд),
`run_label` (метка серии), `source_file`, `ingested_at` — прогоны разных
стендов и серий не смешиваются.

## Настройка ПК (один раз)

1. Установить ClickHouse:
   ```bash
   curl https://clickhouse.com/ | sh
   sudo ./clickhouse install
   sudo clickhouse start
   ```
2. Применить схему (две таблицы `results` / `baselines`):
   ```bash
   clickhouse-client --multiquery < schema.sql
   ```
ClickHouse слушает только localhost ПК — из WSL2/харнесс-хоста ходим через
SSH-туннель (нулевая экспозиция, без firewall/пароля, поверх уже настроенного
SSH). Альтернатива — открыть 8123 на LAN (`listen_host` + firewall + пароль
default-юзеру); localhost-заливка на самом ПК ничего этого не требует.

## Заливка (после каждой серии)

Через Makefile (из корня репозитория), поверх SSH-туннеля:
```bash
make ch-tunnel                       # localhost:8123 -> ПК:8123 (CH_SSH=user@host)
make ch-load STAND=stage RUN_LABEL=2026-07-14-io
make ch-tunnel-close                 # когда закончил
# ch-load по умолчанию берёт harness/results/results.parquet + baselines.parquet
# (CH_HOST=localhost по умолчанию); переопределить: RESULTS_FILE=... BASELINES_FILE=...
```

Напрямую (или локально на ПК, скопировав parquet):
```bash
python load_parquet.py --host localhost --stand stage --run-label 2026-07-14-io \
    --results ../../harness/results/results.parquet \
    --baselines ../../harness/results/baselines.parquet
```

`--dry-run` читает и приводит типы, ничего не заливая — проверить парсинг без
ClickHouse.

## Дедупликация

Таблицы — `ReplacingMergeTree(ingested_at)`: повторная заливка того же прогона
(тот же `stand`/`run_label`/точка плана) заменяет строки, а не дублирует.
Схлопывание фоновое, поэтому для точных запросов — `FINAL` или `argMax`:

```sql
SELECT config, avg(makespan_s)
FROM sensitivityscore.results FINAL
WHERE stand = 'stage' AND run_label = '2026-07-14-io' AND scenario = 'pressure:llc'
GROUP BY config;
```

## Схема

`results` и `baselines` делят колонки строки харнесса (§5.1) + провенанс;
отличаются ключом сортировки (у baselines в идентичность входит `node` —
соло-прогон на каждом узле). Метрики агента — `Nullable`: неизмеренное = NULL,
не 0 (иначе `avg()` занижает). Заявленный S-вектор (`sensitivity_*`) — метки
`low`/`high`, не числа. Таймстемпы (`*_ts`) хранятся как UTC `DateTime64(3)`.

Полный список колонок — в `schema.sql`.
