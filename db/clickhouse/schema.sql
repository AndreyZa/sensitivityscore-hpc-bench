-- schema.sql — ClickHouse-хранилище для агрегации результатов стенда.
-- Применяется один раз при настройке ПК-агрегатора:
--   clickhouse-client --host <PC> --multiquery < schema.sql
-- (или `make ch-schema CH_HOST=<PC>`).
--
-- Модель: batch-load. Харнесс как прежде пишет results.parquet /
-- baselines.parquet; load_parquet.py заливает их сюда, добавляя провенанс
-- (stand / run_label / source_file / ingested_at), чтобы прогоны разных
-- стендов и серий не смешивались.
--
-- Обе таблицы делят схему строки харнесса (§5.1); отличаются только ключом
-- сортировки: у results идентичность job — (сценарий, точка плана, повтор,
-- индекс в батче); у baselines важен узел (соло-прогон на КАЖДОМ узле).
--
-- ReplacingMergeTree(ingested_at): повторная заливка того же прогона заменяет
-- строки, а не дублирует (по ключу сортировки, версия — ingested_at). Для
-- точных запросов добавляй FINAL или argMax(ingested_at) — см. README.

CREATE DATABASE IF NOT EXISTS sensitivityscore;

CREATE TABLE IF NOT EXISTS sensitivityscore.results
(
    -- провенанс (заполняет загрузчик, в parquet этого нет)
    stand              LowCardinality(String),   -- какой стенд: stage / prod / ...
    run_label          LowCardinality(String),   -- метка серии (напр. 2026-07-14-llc)
    -- идентичность точки плана (§5.1)
    config             LowCardinality(String),   -- A-default / A-sensitivityscore / C / D ...
    profile            LowCardinality(String),   -- low-s / high-s / high-s-io / high-s-net
    scenario           LowCardinality(String),   -- llc / io / net / batch / baseline
    overcommit         Float64,
    rep                Int32,
    batch_size         Int32,
    batch_index        Int32,
    -- исход
    node               String,                   -- куда планировщик поставил (''=нет/ошибка)
    makespan_s         Nullable(Float64),
    makespan_source    LowCardinality(String),   -- container / sacct / wallclock / ''
    submit_ts          Nullable(DateTime64(3)),
    start_ts           Nullable(DateTime64(3)),
    end_ts             Nullable(DateTime64(3)),
    -- метрики агента (NULL = не измерено; НЕ 0 — важно для avg)
    llc_miss_rate      Nullable(Float64),
    numa_remote_ratio  Nullable(Float64),
    net_bw             Nullable(Float64),
    net_pressure       Nullable(Float64),
    io_iops            Nullable(Float64),
    io_pressure        Nullable(Float64),
    -- качество решения планировщика
    interference_chosen Nullable(Float64),
    placement_regret   Nullable(Float64),
    -- заявленный S-вектор профиля (метки low/high, для fingerprint)
    sensitivity_llc    LowCardinality(String),
    sensitivity_numa   LowCardinality(String),
    sensitivity_net    LowCardinality(String),
    sensitivity_io     LowCardinality(String),
    -- диагностика
    approximation      String,                   -- ok / missing / synthetic / error:<...>
    source_file        String,
    ingested_at        DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(ingested_at)
ORDER BY (stand, run_label, scenario, config, profile, overcommit, rep, batch_index);

CREATE TABLE IF NOT EXISTS sensitivityscore.baselines
(
    stand              LowCardinality(String),
    run_label          LowCardinality(String),
    config             LowCardinality(String),
    profile            LowCardinality(String),
    scenario           LowCardinality(String),   -- всегда 'baseline'
    overcommit         Float64,
    rep                Int32,
    batch_size         Int32,
    batch_index        Int32,
    node               String,                   -- часть идентичности: соло-прогон на каждом узле
    makespan_s         Nullable(Float64),
    makespan_source    LowCardinality(String),
    submit_ts          Nullable(DateTime64(3)),
    start_ts           Nullable(DateTime64(3)),
    end_ts             Nullable(DateTime64(3)),
    llc_miss_rate      Nullable(Float64),
    numa_remote_ratio  Nullable(Float64),
    net_bw             Nullable(Float64),
    net_pressure       Nullable(Float64),
    io_iops            Nullable(Float64),
    io_pressure        Nullable(Float64),
    interference_chosen Nullable(Float64),
    placement_regret   Nullable(Float64),
    sensitivity_llc    LowCardinality(String),
    sensitivity_numa   LowCardinality(String),
    sensitivity_net    LowCardinality(String),
    sensitivity_io     LowCardinality(String),
    approximation      String,
    source_file        String,
    ingested_at        DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(ingested_at)
ORDER BY (stand, run_label, config, profile, node, rep);
