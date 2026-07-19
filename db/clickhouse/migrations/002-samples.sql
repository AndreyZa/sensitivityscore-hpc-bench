-- 002-samples.sql — добавить колонку samples к уже существующим таблицам.
--
-- samples — число тиков (~5с), усреднённых в метрики строки (S = <dim>_sum /
-- samples). Живёт в Redis-хеше агента с самого начала, но в parquet и в
-- ClickHouse до сих пор не доезжал: отфильтровать задачу, чей вектор S посчитан
-- по двум-трём тикам (короткая жизнь, инициализация доминирует), было нечем.
--
-- DEFAULT 0 и IF NOT EXISTS: старые строки остаются с samples=0, что читается
-- как «не записано» (для этих серий провенанс тоже пуст — см. 001); повторный
-- прогон миграции безвреден. Ставится AFTER source_file — рядом с диагностикой,
-- как в schema.sql.
--
--   make ch-migrate CH_HOST=<приёмник>

ALTER TABLE sensitivityscore.results
    ADD COLUMN IF NOT EXISTS samples Int32 DEFAULT 0 AFTER source_file;

ALTER TABLE sensitivityscore.baselines
    ADD COLUMN IF NOT EXISTS samples Int32 DEFAULT 0 AFTER source_file;
