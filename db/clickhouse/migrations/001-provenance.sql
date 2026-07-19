-- 001-provenance.sql — добавить колонки провенанса к уже существующим таблицам.
--
-- schema.sql создаёт таблицы через CREATE TABLE IF NOT EXISTS, поэтому на
-- приёмнике с уже залитыми сериями он не добавит ничего: новые колонки нужно
-- накатить отдельно. Применяется к приёмникам, где лежат серии, снятые до
-- введения провенанса (девять STAGE-серий).
--
-- DEFAULT '' и IF NOT EXISTS: старые строки остаются на месте с пустым
-- провенансом, что честно читается как «чем снято — восстановить нельзя»;
-- повторный прогон миграции безвреден.
--
--   make ch-migrate CH_HOST=<приёмник>

ALTER TABLE sensitivityscore.results
    ADD COLUMN IF NOT EXISTS harness_commit LowCardinality(String) DEFAULT '' AFTER source_file,
    ADD COLUMN IF NOT EXISTS config_sha256  LowCardinality(String) DEFAULT '' AFTER harness_commit,
    ADD COLUMN IF NOT EXISTS workload_image LowCardinality(String) DEFAULT '' AFTER config_sha256,
    ADD COLUMN IF NOT EXISTS calibration    LowCardinality(String) DEFAULT '' AFTER workload_image,
    ADD COLUMN IF NOT EXISTS score_weights  String                 DEFAULT '' AFTER calibration,
    ADD COLUMN IF NOT EXISTS profile_overrides String              DEFAULT '' AFTER score_weights;

ALTER TABLE sensitivityscore.baselines
    ADD COLUMN IF NOT EXISTS harness_commit LowCardinality(String) DEFAULT '' AFTER source_file,
    ADD COLUMN IF NOT EXISTS config_sha256  LowCardinality(String) DEFAULT '' AFTER harness_commit,
    ADD COLUMN IF NOT EXISTS workload_image LowCardinality(String) DEFAULT '' AFTER config_sha256,
    ADD COLUMN IF NOT EXISTS calibration    LowCardinality(String) DEFAULT '' AFTER workload_image,
    ADD COLUMN IF NOT EXISTS score_weights  String                 DEFAULT '' AFTER calibration,
    ADD COLUMN IF NOT EXISTS profile_overrides String              DEFAULT '' AFTER score_weights;
