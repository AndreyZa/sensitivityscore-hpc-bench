# k8s/clickhouse — in-cluster ClickHouse

ClickHouse внутри кластера как приёмник результатов **продовых** прогонов:
данные падают сразу через in-cluster сервис, без parquet→ручной ch-load→
туннель→домашний ПК (удалённый прод-кластер до домашней LAN всё равно не
дотянется). Схема таблиц — единый источник `db/clickhouse/schema.sql`.

## Размещение (критично)

StatefulSet ОБЯЗАН стоять на **системной ноде** (`node-role.kubernetes.io/
ss-system`, taint NoSchedule), не на измерительных: инсерты/мержи CH едят
CPU/IO и загрязнили бы LLC/IO/Net-метрики эксперимента. Это делает
prod-overlay (он же дефолт `CH_KUSTOMIZE`); base — без placement, только для
dev и только явным указанием; lab — оверлей однонодовой лаборатории .72.

## Деплой

```bash
# прод — он же ДЕФОЛТ (пин на ss-system):
make ch-incluster-deploy
# лаборатория .72 (однонодовый k0s: hostPath-том + hostPort на loopback):
make ch-incluster-deploy CH_KUSTOMIZE=k8s/clickhouse/overlays/lab
# dev/docker-desktop, без ограничений по узлам — только ЯВНО:
make ch-incluster-deploy CH_KUSTOMIZE=k8s/clickhouse/base

make ch-incluster-status
make ch-incluster-clean            # ВНИМАНИЕ: удаляет PVC с данными
```

Дефолт — прод-оверлей намеренно: `base` не пинит CH ни к какому узлу, и на
стенде он молча сел бы на измерительный. Обратный промах (прод-оверлей на
dev-кластере без роли ss-system) безобиден и виден сразу — под висит Pending.
Сверх дефолта деплой сторожит `scripts/ch-placement-guard.sh`: если в кластере
есть узлы с ролью bench, а в рендере StatefulSet или Job без привязки к
ss-system — таргет отказывает (обход: `CH_ALLOW_UNPINNED=1`).

Таргет создаёт namespace, ConfigMap `clickhouse-schema` из
`db/clickhouse/schema.sql`, применяет kustomize. Schema-Job ждёт готовности
CH и накатывает таблицы (`schema.sql`: results + baselines), а следом
миграции из `db/clickhouse/migrations/*.sql` (ConfigMap `clickhouse-migrations`,
оттуда же берётся, например, `energy_windows`).

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
(`make ch-load-all CH_SINKS=home ...`).

Долив безопасен: таблицы — `ReplacingMergeTree(ingested_at)`, повторная заливка
того же прогона не ломает данные. Схлопывание версий происходит при фоновом
мерже, поэтому сразу после долива `count()` покажет дубли, а `count() FINAL` —
уже нет (проверено: 2 против 1). Читатели это учитывают —
`analysis/clickhouse_source.py` селектит с `FINAL`; свои ad-hoc запросы пиши
так же, иначе повторный долив будет двоить статистику.

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
