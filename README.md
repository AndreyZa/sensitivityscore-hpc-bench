# SensitivityScore HPC Bench

Экспериментальный стенд для проверки гипотез диссертации *«Adaptive Resource
Management Methods for Computing Resources in Heterogeneous Distributed
Environments for Scientific and Engineering Tasks»*: сравнение
interference-aware планировщика **SensitivityScore** (кастомный Kubernetes
Scheduler Framework plugin) с default kube-scheduler, классическим Slurm и
Slurm-on-K8s (Slinky/slurm-bridge) на эталонной HPC-нагрузке Geant4.

Формальная модель задачи: **Z = {G, R, S}**, где `S = (LLC, NUMA, Net, IO) ∈ [0,1]⁴`
— вектор чувствительности job к интерференции по каждому измерению.

Рабочие документы, из которых собран репозиторий:

- [Сводка результатов STAGE (июль 2026)](<docs/Сводка результатов STAGE (июль 2026).md>)
  — **текущие результаты**: четыре пройденные серии, статус гипотез,
  калибровка цен осей и уточнение модели скоринга. Точка входа для чтения.
- [Программа экспериментов (Geant4)](<docs/Программа экспериментов (Geant4).md>)
  — методология прод-стенда: конфигурации, гипотезы H1–H4, метрики.
- [Методика измерений](<docs/Методика измерений.md>)
  — что и на чём измеряется: роли компонентов, фазы, правила.
- [Технический план экспериментов](<docs/Технический план экспериментов.md>)
  — детальный план (архитектура, чек-лист, псевдокод) по этапам 0–6.
- [Ввод прод-стенда (Этап 0)](<docs/Ввод прод-стенда (Этап 0).md>)
  — пошаговый ввод нового кластера: роли узлов, калибровки, проверки.

## Структура репозитория

```
docs/                    — рабочий план и программа экспериментов (см. выше)
workload/                — Docker-образ Geant4 с управляемым профилем S
k8s/
  config-a-baremetal/    — Job-манифесты для K8s bare-metal (low-s/high-s)
  config-b-kubevirt/     — VMI-манифесты для KubeVirt
  config-d-slinky/       — Job-манифест для Slinky/slurm-bridge
  scheduler-config/      — KubeSchedulerConfiguration + веса score-функции
slurm/config-c/          — sbatch-скрипты для классического Slurm
metrics-agent/           — Go: DaemonSet-агент, perf_event_open() → Redis
harness/                 — Python: оркестрация серии экспериментов (run_experiment.py),
                           запускается как Job внутри кластера (harness/deploy/)
analysis/                — Python: статистика (Mann-Whitney, Cliff's delta) + графики
scripts/                 — bootstrap-скрипты для кластера
```

Сам плагин SensitivityScore (Score extension point) живёт в отдельном форке
`kubernetes-sigs/scheduler-plugins` (`../scheduler-plugins` рядом с этим
репозиторием) — здесь только манифесты деплоя (`k8s/scheduler-config/`) и
Makefile-обёртка над его сборкой.

## Прогон на боевом стенде

`make help` — полный список команд. Порядок ниже — то, что реально нужно
выполнить на новом прод-стенде, от нуля до H1–H4.

### 0. PMU health-check (до разворачивания DaemonSet)

`perf_event_open()` в cgroup-scoped режиме — не то же самое, что просто
CAP_PERFMON на под (см. `docs/Программа экспериментов (Geant4).md` §8) —
проверить это стоит ДО того, как разворачивать весь `metrics-agent`:

```bash
make perfcheck-image                       # локальная сборка образа
make perfcheck-push                        # ноды стенда — отдельные машины, тянут образ из registry
make perfcheck-run NODE=<имя-узла>         # на неоднородном стенде — по каждому узлу; без NODE встаёт куда придётся
make perfcheck-logs                        # ждём Completed; SUCCESS с НЕнулевым числом = PMU честный (read=0 = гипервизор подделывает)
make perfcheck-clean
```

### 1. Собрать и запушить образы

Каждый `make image-*` только собирает образ ЛОКАЛЬНО — на реальном
многоузловом стенде worker-ноды не разделяют Docker-демон с машиной сборки,
поэтому после каждой сборки нужен `docker push` (кластер сам подтянет по
`imagePullPolicy: Always`):

```bash
make image-workload      && docker push andreyza/geant4:11.2
make image-metrics-agent && docker push andreyza/metrics-agent:dev
make image-harness       && docker push andreyza/harness:dev

# Плагин планировщика собирается в форке; тег — переменная SCHEDULER_RELEASE_VER в этом Makefile
make scheduler-plugin-image
make -C ../scheduler-plugins -f sensitivityscore.mk ss-push
```

### 2. Развернуть кластер

```bash
make setup-cluster   # namespace+Redis (bootstrap) + scheduler-deploy + deploy-metrics-agent
make scheduler-status                 # под планировщика, ConfigMap-ы, последние scheduling events
make scheduler-logs                   # логи SensitivityScore.Score (Ctrl+C для выхода)
```

Быстрый smoke-test без Geant4 (сравнить score high-S vs low-S пода на живых нодах):

```bash
make test-pod-highs
make test-pod-lows
make test-pod-clean
```

### 3. Harness — запуск ВНУТРИ кластера (без port-forward)

```bash
make harness-rbac                                  # namespace/ServiceAccount/RBAC/PVC — один раз

make harness-run-pilot-incluster                    # пилот: 1 точка плана, 3 повтора, config A
make harness-logs-incluster JOB=harness-pilot       # следить за прогоном
make harness-fetch-results                          # забрать results.parquet на хост

# Соло-бейзлайны на ПУСТОМ кластере (нужны для slowdown-нормировки и
# fingerprint-таблицы в анализе) — один раз на стенд, до боевой матрицы:
make harness-run-baseline-incluster
make harness-fetch-baselines

# если пилот чистый — полная матрица (пока только config A: B/C/D нужна
# инфраструктура прод-стенда, см. docs §0)
make harness-run-config-a-incluster
make harness-logs-incluster JOB=harness-config-a
make harness-fetch-results

# «money experiment» — pressure-сценарии (агрессоры + поток жертв):
make image-aggressor && docker push andreyza/aggressor:dev
make harness-run-pressure-incluster
make harness-fetch-results

make harness-clean-reader                           # убрать read-only под, поднятый для выгрузки
```

Опционально — контрольный бейзлайн Trimaran (H1-trimaran): раскомментировать
`trimaran` в `scheduler_variants` (`harness/config.yaml`), поставить
metrics-server (`make trimaran-deps`), пересобрать образ харнесса. Добавляет
третье плечо `A-trimaran` ко всем прогонам выше.

### 4. Анализ и проверка H1–H4

Читает локальные `harness/results/{results,baselines}.parquet` (после
`harness-fetch-results` / `harness-fetch-baselines` выше):

```bash
make analyze   # report/: summary.md, comparisons.csv, fingerprint.csv + графики
make report    # analyze + сразу открыть summary.md
```

Сравнения идут по трём метрикам (`makespan_s`, `slowdown` при наличии
бейзлайнов, `placement_regret`), каждая — своя Holm-семья. `placement_regret` и
fingerprint-таблица закрывают типовые вопросы к H1 («разница случайна» —
regret показывает механизм; «аннотации подогнаны» — fingerprint сверяет
заявленный S с измеренным).

### Уборка

```bash
make harness-clean-jobs     # Job'ы харнесса (после ручных прогонов)
make harness-clean-full     # + весь namespace sensitivityscore-bench (включая PVC с результатами!)
make nuke                   # всё вышеперечисленное + Deployment планировщика + venv-ы
```

## Гипотезы

| # | Формулировка (кратко) |
|---|---|
| H1 | SensitivityScore (config A) даёт меньший makespan и меньшую дисперсию, чем default kube-scheduler, при co-location разных профилей S |
| H1-trimaran | Преимущество даёт именно S-вектор, а не «любой учёт загрузки»: SensitivityScore обыгрывает и load-aware Trimaran (слепой к LLC/NUMA/IO) на pressure-сценариях |
| H2 | Оверхед виртуализации (config B) снижает эффективность, но относительное преимущество SensitivityScore над default сохраняется |
| H3 | Slurm (config C) — верхняя граница на однородной нагрузке, но проигрывает SensitivityScore в co-location сценариях |
| H4 | Slinky/slurm-bridge (config D) близок к C на однородной нагрузке, но проигрывает A в сценариях смешанной чувствительности |

Полные формулировки — в `docs/Программа экспериментов (Geant4).md` §6.
**Текущий статус проверки** (H1 — частично подтверждена с уточнением модели,
H1-trimaran — подтверждена, H2–H4 — ждут конфигураций B/C/D) — в
`docs/Сводка результатов STAGE (июль 2026).md`, секция «Статус гипотез».

## Статус реализации

Конфигурация A (K8s, три плеча default / SensitivityScore / trimaran)
полностью прогнана на облачном стенде STAGE: четыре серии фоновой нагрузки
(диск, сеть, кэш, смешанная), по 10 повторений, с пер-узловыми эталонами и
статистикой Манн-Уитни/Холм/Cliff's δ — результаты и статус гипотез в
`docs/Сводка результатов STAGE (июль 2026).md`. Конфигурации B/C/D ждут
прод-стенда (см. дорожную карту Программы §9).

Все четыре измерения S измеряются агентом И участвуют в score: LLC
(промахи/сек к калибровке `LLC_REFERENCE_MISSES_PER_SEC` — отношение
промахов инвертируется под потоковой нагрузкой, см. Методику), NUMA
(`node-load-misses / node-loads`, generic node-события PMU), IO (PSI
`io.pressure` — в score; сырые IOPS — только для анализа), Net
(`net_pressure = net_bw / NET_REFERENCE_MBPS`, калибровка стенда
эмпирическим cross-node `iperf3 --bidir`: `make netcheck-run` → env на
DaemonSet агента; без калибровки ось честно пишется нулём, сырой `net_bw`
остаётся для анализа — методика в `docs/Технический план экспериментов.md`
§3.4).

Скоринг: вклад оси = `давление · (base + sensitivity · s_задачи)` — базовую
цену оси платит любая задача узла, надбавку — чувствительная (уточнение
модели по итогам калибровки цен осей, см. Сводку). Веса — файл
`weights.json` в ConfigMap: `{"base": {…}, "sensitivity": {…}}`; старый
плоский формат читается как sensitivity-часть. Абляция любой оси — нули в
обеих частях, не код.

Экспериментальная часть усилена тремя защитами H1 плюс контрольным
бейзлайном (см. `harness/README.md`, `analysis/README.md`):

- **placement regret** — прямая метрика решения планировщика (давление
  выбранной ноды − минимум доступных), почти без шума исхода;
- **slowdown-нормировка** — `makespan / изолированный` из соло-бейзлайнов
  (`--baseline`, пин per (profile, node) — «одинаковые» облачные ноды бывают
  в ~2x разной скорости), делает профили разной длительности объединяемыми;
- **fingerprint** — таблица «заявленный vs измеренный S» с проверкой
  монотонности, превращает ручные аннотации в верифицированные профили;
- **Trimaran** (`LoadVariationRiskBalancing`) — load-aware, но не
  interference-aware контрольный бейзлайн (H1-trimaran), опционален.

Осознанно оставлены как явные TODO:

- Сетевой `OUTPUT_MODE` у воркера: профиль `high-s-net` (жертва
  net pressure-сценария) декларирует net=high, но сетевого трафика сам не
  генерирует — fingerprint честно флагает ось. Реалистичное уточнение:
  стрим выходных данных на sink-под (аналог fsync-burst для IO).
- `uncore_imc_*` PMU как уточнение NUMA-метрики (истинный bandwidth
  per-socket, но node-wide и специфичен для модели CPU) — см.
  `ReadUncoreNUMABandwidth`.
- Wiring `QemuProcessResolver` к реальному KubeVirt API кластера
  (конфигурация B: харнесс пока сабмитит Job, а не VMI).
- `OUTPUT_MODE=burst` эмулирует периодическую запись выходных данных
  (fsync-burst в `workload/entrypoint.sh`, профиль `high-s-io`) — настоящий
  per-event ntuple через AnalysisManager TestEm5 остаётся возможным
  уточнением методики.

---

## English (short)

Experimental testbed comparing the **SensitivityScore** interference-aware
Kubernetes scheduler plugin (task model `Z = {G, R, S}`, `S` = LLC/NUMA/Net/IO
sensitivity vector) against default kube-scheduler, classical Slurm, and
Slurm-on-K8s (Slinky) on a Geant4 HPC benchmark. See `docs/` for the full
experiment plan and hypotheses H1–H4. Components: `workload/` (parameterized
Geant4 Docker image), `k8s/` + `slurm/` (per-configuration manifests),
`metrics-agent/` (Go: the `perf_event_open()` → Redis metrics pipeline; the
scheduler plugin itself lives in the separate `scheduler-plugins` fork),
`harness/` (Python: experiment orchestration, runs as an in-cluster Job —
see `harness/deploy/`), `analysis/` (Python: Mann-Whitney U / Cliff's delta /
CV statistics + plots). "Прогон на боевом стенде" above is the actual
step-by-step command sequence (Makefile targets) regardless of reading
language — commands are English-agnostic.
