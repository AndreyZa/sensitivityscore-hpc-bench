-- 005-bios-profile.sql — настройки BIOS измерительных узлов в провенанс строки.
--
-- Зачем отдельной колонкой, а не «посмотрим в Prometheus». Настройки BIOS —
-- это ПАРАМЕТРЫ МОДЕЛИ, а не свойство железа: SysProfile и ProcPwrPerf задают,
-- кто и по какому критерию управляет частотой; EnergyEfficientTurbo разрешает
-- платформе самой снижать турбо на памяти-зависимых задачах (а их и создают
-- агрессоры, и тогда замедление от интерференции и от снижения частоты
-- неразличимы); префетчеры формируют промахи LLC; SubNumaCluster задаёт
-- знаменатель NUMA-оси. Две серии, снятые при разных настройках, сравнивать
-- нельзя — а по цифрам этого не видно.
--
-- В Prometheus отпечаток тоже есть (idrac_bios_profile_hash, едет в
-- metrics_samples), но он живёт со своим ретеншеном и требует джойна по
-- времени. Строка результата обязана нести описание платформы В СЕБЕ: именно
-- так уже терялись калибровки 18.07.2026 — они были «где-то», но не рядом с
-- данными, и восстановить их постфактум не удалось.
--
-- Заполняет harness/provenance.py (bios_profile) из метрик поллера iDRAC.
-- DEFAULT '' и IF NOT EXISTS: старые строки честно читаются как «платформу
-- восстановить нельзя»; повторный прогон безвреден.
--
--   make ch-migrate CH_HOST=<приёмник>

ALTER TABLE sensitivityscore.results
    ADD COLUMN IF NOT EXISTS bios_profile String DEFAULT '' AFTER storm_nodes;

ALTER TABLE sensitivityscore.baselines
    ADD COLUMN IF NOT EXISTS bios_profile String DEFAULT '' AFTER storm_nodes;
