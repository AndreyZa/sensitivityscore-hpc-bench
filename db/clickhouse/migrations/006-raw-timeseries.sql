-- 006-raw-timeseries.sql — сырые ряды Prometheus в точке правды.
--
-- До этой миграции в CH попадали только пост-фактум агрегаты
-- (energy_windows, результаты серий), а сырые сэмплы умирали с ретеншеном
-- Prometheus — «пересчитайте с другим окном» через месяц было невыполнимо.
-- Таблица принимает прометеевский remote_write через prometheus-порт
-- сервера (9363/write, форма handlers в base/config.yaml); Prometheus
-- остаётся скрейп- и алерт-слоем, CH — долговечным архивом.
--
-- Движок экспериментальный (флаг ниже действует на сессию schema-Job'а);
-- обкатка на лабе 20.08.2026 — k8s/monitoring/energy/README.md: рестарт CH
-- без потерь (WAL Prometheus дослал буфер), ~6 Б/сэмпл до слияний.
-- Чтение — timeSeriesData()/timeSeriesTags()/timeSeriesMetrics().
SET allow_experimental_time_series_table = 1;
CREATE TABLE IF NOT EXISTS sensitivityscore.prom_ts ENGINE = TimeSeries;
