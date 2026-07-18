# k8s/clickhouse — in-cluster ClickHouse

ClickHouse внутри кластера как приёмник результатов **продовых** прогонов:
данные падают сразу через in-cluster сервис, без parquet→ручной ch-load→
туннель→домашний ПК (удалённый прод-кластер до домашней LAN всё равно не
дотянется). Схема таблиц — единый источник `db/clickhouse/schema.sql`.

## Размещение (критично)

StatefulSet ОБЯЗАН стоять на **системной ноде** (`node-role.kubernetes.io/
ss-system`, taint NoSchedule), не на измерительных: инсерты/мержи CH едят
CPU/IO и загрязнили бы LLC/IO/Net-метрики эксперимента. Это делает
prod-overlay; base — без placement (для теста).

## Деплой

```bash
# тест на docker-desktop / dev (без ограничений по нодам):
make ch-incluster-deploy                                   # CH_KUSTOMIZE=k8s/clickhouse/base
# прод (пин на ss-system):
make ch-incluster-deploy CH_KUSTOMIZE=k8s/clickhouse/overlays/prod

make ch-incluster-status
make ch-incluster-clean            # ВНИМАНИЕ: удаляет PVC с данными
```

Таргет создаёт namespace, ConfigMap `clickhouse-schema` из
`db/clickhouse/schema.sql`, применяет kustomize. Schema-Job ждёт готовности
CH и накатывает таблицы (results + baselines).

## Доступ

`default`-юзер образа пускает только с localhost; `users.yaml` открывает его на
кластерную сеть (passwordless — граница безопасности = сеть кластера, снаружи
только через port-forward). **Прод-хардненинг:** заменить на
`<password_sha256_hex>` + Secret, прокинуть `CH_PASSWORD`.

Клиенты (харнесс/анализ) — по DNS сервиса, host:port меняется, код нет:
```bash
# in-cluster (харнесс-Job): CH_HOST=clickhouse.sensitivityscore-system.svc CH_PORT=8123
# с хоста: kubectl -n sensitivityscore-system port-forward svc/clickhouse 8123:8123
make ch-load    CH_HOST=clickhouse.sensitivityscore-system.svc STAND=prod RUN_LABEL=<l>
make ch-analyze CH_HOST=<host> STAND=prod RUN_LABEL=<l>
```

## Два приёмника результатов

Результаты серии льются и в этот in-cluster CH (стенд), и в домашний
ПК-агрегатор (кросс-стендовая агрегация) — `make ch-load-all`:

```bash
make ch-forward &        # in-cluster CH -> localhost:8124
make ch-tunnel           # ПК-агрегатор  -> localhost:8123
make ch-load-all STAND=prod RUN_LABEL=<серия>
```

Порты локальные и разные намеренно: туннель к дому уже занимает 8123, а лить
нужно в оба.

Недоступность одного приёмника не мешает залить во второй: источник истины —
`results.parquet` на диске, поэтому цикл не прерывается на первой ошибке, а в
конце печатает команду долива именно того приёмника, который не взлетел
(`make ch-load-all CH_SINKS=home ...`). Долив безопасен: таблицы —
`ReplacingMergeTree(ingested_at)`, тот же прогон заменит строки, а не
продублирует.

## Центральная агрегация нескольких стендов

CH per стенд (in-cluster) фрагментирует данные — для кросс-стенд анализа один
«центр» (домашний ПК или любой) периодически тянет остальные:
```sql
INSERT INTO sensitivityscore.results
SELECT * FROM remoteSecure('prod-ch:9440', sensitivityscore.results, 'user', 'pass');
```
Колонки `stand`/`run_label` разводят источники — дубли не смешиваются.

## Статус

Манифесты протестированы на docker-desktop (k8s 1.35): StatefulSet+PVC поднялся,
schema-Job накатил таблицы, загрузчик залил данные через port-forward. На проде
не разворачивалось (стенда ещё нет) — деплой входит в `make provision`
провижнера (шаг `clickhouse`, после `storage`: PVC просит том без
`storageClassName` и без класса по умолчанию повиснет в Pending).
