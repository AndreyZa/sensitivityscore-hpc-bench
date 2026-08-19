-- 004-metrics.sql — долговременное хранение рядов Prometheus в ClickHouse.
--
-- Зачем. Результаты серий уезжают в CH, а ТРАССЫ (оси чувствительности,
-- мощность, RAPL, утилизация глазами load-watcher) живут только в Prometheus —
-- то есть в hostPath на одной ВМ, с ретеншеном и без SQL. Из-за этого:
--   * сопоставить «замедление точки плана» с «что творилось на узле в эту
--     минуту» можно было только глазами по дашборду;
--   * через год ретеншен (365 дней) молча срежет самое начало кампании;
--   * бэкап приходилось держать в двух местах разной природы — Native-дампы CH
--     и tar снимка TSDB.
-- Здесь ряды становятся обычной таблицей: их можно джойнить с samples по
-- времени и узлу, они попадают в общий ch-backup и переживают Prometheus.
--
-- ЧТО СЮДА НЕ ЛЬЁТСЯ. Не весь TSDB (в нём ~115 тыс. рядов — это сотни
-- миллионов строк в сутки и никакой пользы: cAdvisor и kubelet нужны для
-- дежурства, а не для диссертации). Список метрик закрытый и лежит в
-- scripts/ch-load-metrics.py рядом с доводом по каждой.
--
-- Дедупликация: ReplacingMergeTree по inserted_at, ключ включает series_key —
-- каноническую строку меток. Повторный прогон на пересекающемся окне (а он
-- будет: watermark округляется вниз до шага) схлопнется, а не удвоит ряд.
--
--   make ch-migrate CH_HOST=<приёмник>

CREATE TABLE IF NOT EXISTS sensitivityscore.metrics_samples
(
    stand       LowCardinality(String),                 -- провенанс, как у samples/energy_windows
    metric      LowCardinality(String),                 -- ss_node_llc_miss_rate, idrac_power_watts, ...
    node        LowCardinality(String) DEFAULT '',      -- вынесен из меток: по нему джойнят с samples
    series_key  String,                                 -- остальные метки канонически: "k=v,k=v"
    labels      Map(LowCardinality(String), String),    -- они же в разбираемом виде
    ts          DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
    value       Float64 CODEC(Gorilla, ZSTD(1)),
    inserted_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(ts)
ORDER BY (stand, metric, node, series_key, ts);
