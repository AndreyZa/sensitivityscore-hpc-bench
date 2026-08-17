-- 003-energy.sql — таблица энергоокон для энерговетки (Peaks, партнёрская).
--
-- Одна строка = энергия одного узла за одно окно серии из одного источника.
-- Энергия — РАЗНОСТЬ НАКОПИТЕЛЬНОГО СЧЁТЧИКА на границах окна (регистр кВт·ч
-- PDU, джоули RAPL, счётчик IPMI), не интеграл мгновенной мощности: точность
-- определяется прибором, а не частотой опроса. Средняя мощность хранится
-- избыточно для дашбордов (energy_j / длительность), при расхождении точка
-- правды — energy_j.
--
-- Источники пишутся ПАРАЛЛЕЛЬНО (pdu / rapl-pkg / rapl-dram / ipmi) — их
-- взаимная сверка и есть фаза P0; поэтому source входит в ключ.
-- Окно 'idle' — калибровка холостого хода, 'calib-step-N' — ступени фазы P1,
-- 'pressure' — рабочее окно серии (по epoch'ам PRESSURE START/DONE харнесса).
--
-- Заполняет scripts/energy-window.py (опрос Prometheus на границах окна).
-- Повторный прогон миграции безвреден (IF NOT EXISTS); повторная вставка того
-- же окна схлопывается ReplacingMergeTree по inserted_at.
--
--   make ch-migrate CH_HOST=<приёмник>

CREATE TABLE IF NOT EXISTS sensitivityscore.energy_windows
(
    stand           LowCardinality(String),
    run_label       LowCardinality(String),
    config          LowCardinality(String) DEFAULT '',  -- плечо (A-peaks, ...) либо '' для калибровок
    window          LowCardinality(String),             -- pressure | idle | calib-step-<n>
    node            String,
    source          LowCardinality(String),             -- pdu | rapl-pkg | rapl-dram | ipmi
    ts_start        DateTime64(3, 'UTC'),
    ts_end          DateTime64(3, 'UTC'),
    energy_j        Float64,
    avg_power_w     Nullable(Float64),
    meta            String DEFAULT '',                  -- json: имя метрики, instance, примечания
    harness_commit  LowCardinality(String) DEFAULT '',
    source_file     String DEFAULT '',
    inserted_at     DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY (stand, run_label, config, window, node, source, ts_start);
