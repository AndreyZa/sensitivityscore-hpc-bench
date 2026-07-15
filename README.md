# SensitivityScore HPC Bench

Экспериментальный стенд для проверки гипотез диссертации *«Adaptive Resource
Management Methods for Computing Resources in Heterogeneous Distributed
Environments for Scientific and Engineering Tasks»*: сравнение
interference-aware планировщика **SensitivityScore** (кастомный Kubernetes
Scheduler Framework plugin) с default kube-scheduler, классическим Slurm и
Slurm-on-K8s (Slinky/slurm-bridge) на эталонной HPC-нагрузке Geant4.

Формальная модель задачи: **Z = {G, R, S}**, где `S = (LLC, NUMA, Net, IO) ∈ [0,1]⁴`
— вектор чувствительности job к интерференции по каждому измерению.

## Как это работает

```
metrics-agent (DaemonSet, perf_event_open) ──давление узлов──▶ Redis
                                                                 │ читает
Geant4-Job (workload, профиль S) ──submit──▶ SensitivityScore scheduler ──размещает──▶ bench-узел
                                                                 ▲ веса weights.json (ConfigMap)
harness (серия = матрица профили×повторы) ──▶ results.parquet ──▶ analysis (H1–H4) + ClickHouse
```

- **metrics-agent** (Go, DaemonSet на каждом bench-узле) меряет давление по 4 осям
  через `perf_event_open()`/PSI и пишет в **Redis**.
- **SensitivityScore** (плагин kube-scheduler, форк рядом) на Score-фазе читает
  давление узлов из Redis и вектор `S` пода из аннотаций, размещает по весам.
- **harness** (Python) гоняет серию: сабмитит Geant4-Job'ы разных профилей `S`
  с повторами, собирает `results.parquet`.
- **analysis** (Python) считает H1–H4 (Mann-Whitney/Holm/Cliff's δ); те же данные
  дублируются в **ClickHouse** для кросс-стенд агрегации.

Роли узлов: `ss-system` (taint NoSchedule — Redis, планировщик, metrics-server)
vs `bench` (только измеряемая нагрузка). CH/системное на bench не пускаем — испортит метрики.

Рабочие документы, из которых собран репозиторий:

- [Глоссарий](<docs/Глоссарий.md>) — термины эксперимента (шторм, жертва,
  токсичность, ошибка размещения…) и их имена в данных. Читать первым.
- [Сводка результатов STAGE (июль 2026)](<docs/Сводка результатов STAGE (июль 2026).md>)
  — **итоговые результаты**: пять серий, статус гипотез, калибровка цен
  осей, уточнение модели скоринга и его проверка. Точка входа для чтения.
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
  clickhouse/            — in-cluster ClickHouse (StatefulSet) для прод-прогонов
slurm/config-c/          — sbatch-скрипты для классического Slurm
metrics-agent/           — Go: DaemonSet-агент, perf_event_open() → Redis
aggressor/               — LLC/membw stress-под для pressure-сценариев
harness/                 — Python: оркестрация серии экспериментов (run_experiment.py),
                           запускается как Job внутри кластера (harness/deploy/)
statusserver/            — Python: live-страница прогресса серии (make series поднимает её сам)
analysis/                — Python: статистика (Mann-Whitney, Cliff's delta) + графики
db/clickhouse/           — схема + загрузчик parquet→ClickHouse (агрегатор результатов)
scripts/                 — bootstrap + run-series.sh («кнопка» прогона серии)
```

Сам плагин SensitivityScore (Score extension point) живёт в отдельном форке
`kubernetes-sigs/scheduler-plugins` (`../scheduler-plugins` рядом с этим
репозиторием) — здесь только манифесты деплоя (`k8s/scheduler-config/`) и
Makefile-обёртка над его сборкой.

## Разворачивание стенда

`make help` — все команды. Порядок ниже — от нуля до H1–H4 на новом прод-стенде.

### Предпосылки

- **Инструменты на машине оператора:** Docker, `kubectl`, `make`, Go (сборка
  metrics-agent/плагина), Python 3.11+ (venv'ы harness/analysis создаются
  таргетами сами).
- **Кластер:** рабочий Kubernetes (≥2 узла: минимум один `ss-system` + один
  `bench`), kubeconfig указывает на него. Поднять k8s с нуля и подготовить узлы
  (ядро, PMU, PSI) — [Ввод прод-стенда (Этап 0)](<docs/Ввод прод-стенда (Этап 0).md>).
- **Образы:** `andreyza/*` публичны на Docker Hub — для воспроизведения как
  есть шаг 1 (сборка/пуш) можно пропустить, кластер тянет их сам. Свой registry
  нужен только если меняешь код: `export REGISTRY=<свой>` (ретегает build/push и
  scheduler-deploy) **и** правка `image:` в манифестах `k8s/`, `*/deploy/` — они
  пока хардкодят `andreyza/*`.
- **kubeconfig для серий:** `make series` по умолчанию берёт
  `~/.kube/configs/timeweb-stage`; свой — `KUBECONFIG=<путь> make series ...`.
- **Форк планировщика** `../scheduler-plugins` рядом с этим репозиторием.

### 0. PMU health-check (до DaemonSet)

cgroup-scoped `perf_event_open()` ≠ CAP_PERFMON на под — проверить ДО
разворачивания `metrics-agent`:

```bash
make perfcheck-image && make perfcheck-push
make perfcheck-run NODE=<узел>   # по каждому узлу неоднородного стенда; без NODE встаёт куда придётся
make perfcheck-logs              # SUCCESS + ненулевой счётчик = PMU честный (read=0 = гипервизор врёт)
make perfcheck-clean
```

### 1. Образы (только если менял код — иначе см. «Образы» выше)

Сборка локальная; на многоузловом стенде обязателен push (`imagePullPolicy: Always`).
Теги — переменные `*_IMAGE` в `Makefile`:

```bash
make images && make images-push                                   # workload, metrics-agent, harness, aggressor
make scheduler-plugin-image                                       # плагин — в форке
make -C ../scheduler-plugins -f sensitivityscore.mk ss-push
```

### 2. Кластер

`SS_NODES` — узел(ы) под системные компоненты; остальные worker'ы получат роль
`bench` автоматически (разметка идемпотентна):

```bash
make setup-cluster SS_NODES="<ss-system-узел>"  # роли узлов + namespace+Redis + планировщик + DaemonSet-агент
make scheduler-status                            # под планировщика, ConfigMap-ы, scheduling events
make test-pod-highs && make test-pod-lows && make test-pod-clean  # smoke: score high-S vs low-S
```

### 3. Калибровка Net-оси (опц., между сериями — перезапускает агент)

```bash
make netcheck-run                            # cross-node iperf3 --bidir
make netcheck-logs                           # → NET_REFERENCE_MBPS
make netcheck-apply NET_REFERENCE_MBPS=<N>   # env на DaemonSet; без калибровки ось честно = 0
```

### 4. Прогоны — «кнопка» серии

Одна команда: preflight → эталоны → серия → live-статус-страница → вотчдог
(`scripts/run-series.sh`, `SERIES=<имя>` → `harness/config-stage-<имя>.yaml`):

```bash
make series SERIES=<имя>          # имя ∈ placebo | llc | mixed | mixed-calib
make series-status SERIES=<имя>   # фазы, ошибки, строки результатов (live)
make series-stop SERIES=<имя>     # остановить и прибрать (агрессоры, job'ы)
```

Результаты пишутся в `harness/results/` (секция `output` конфига). Trimaran
(`scheduler_variants`) и pressure-агрессоры уже вписаны в stage-конфиги —
отдельных ручных шагов нет; `make trimaran-deps` (metrics-server) нужен раз на
стенд, если плечо trimaran включено.

Ниже уровнем, для ad-hoc/пилота, — точечные in-cluster Job-таргеты
(`make harness-rbac` один раз, затем `harness-run-{pilot,config-a,baseline,pressure}-incluster`
+ `harness-fetch-results`/`-baselines`); см. `make help`.

### 5. Анализ H1–H4

Локальный parquet (`harness/results/{results,baselines}.parquet` — `make series`
кладёт туда сам; для in-cluster Job-пути — после `harness-fetch-*`):

```bash
make analyze   # report/: summary.md, comparisons.csv, fingerprint.csv + графики
make report    # analyze + открыть summary.md
```

Метрики: `makespan_s`, `slowdown` (при бейзлайнах), `placement_regret` —
каждая своя Holm-семья.

Дубль в ClickHouse (агрегатор нескольких стендов; пишем в 2 места — локальный
parquet и CH):

```bash
make ch-tunnel  CH_SSH=user@pc                            # SSH-туннель к ПК-агрегатору
make ch-load    CH_HOST=localhost STAND=<s> RUN_LABEL=<l> # залить results+baselines
make ch-analyze STAND=<s> RUN_LABEL=<l>                   # тот же отчёт из CH
make ch-tunnel-close
```

In-cluster ClickHouse (прод: лить сразу из кластера) — `k8s/clickhouse/README.md`.

### Уборка

```bash
make harness-clean-jobs   # Job'ы харнесса
make harness-clean-full   # + namespace харнесса (PVC с результатами!)
make nuke                 # всё + Deployment планировщика + venv-ы
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

**Конфигурации** (детали и статус гипотез — `docs/Сводка результатов STAGE (июль 2026).md`):

| Конфиг | Что сравнивает | Статус |
|--------|----------------|--------|
| **A** (K8s) | default / SensitivityScore / trimaran | ✅ прогнан на STAGE: 5 серий ×10 повторов, эталоны на узел |
| **B** (KubeVirt) | оверхед виртуализации | ⏳ ждёт прод-стенда |
| **C** (Slurm) | верхняя граница на однородной нагрузке | ⏳ ждёт прод-стенда |
| **D** (Slinky) | slurm-on-k8s | ⏳ ждёт прод-стенда |

Цикл A замкнут: два проигрыша исходного скоринга → калибровка цен осей →
уточнение модели (базовая цена оси) → проверочная серия закрыла все заранее
объявленные критерии.

**Оси S** — все четыре меряются агентом И входят в score:

| Ось | Метрика агента | Калибровка |
|-----|----------------|------------|
| LLC | промахи/сек ÷ `LLC_REFERENCE_MISSES_PER_SEC` | отношение промахов инвертируется под потоком (см. Методику) |
| NUMA | `node-load-misses ÷ node-loads` (PMU) | — |
| IO | PSI `io.pressure` | сырые IOPS — только для анализа |
| Net | `net_bw ÷ NET_REFERENCE_MBPS` | `make netcheck-run` (iperf3 `--bidir`); без калибровки ось = 0 |

**Скоринг:** вклад оси = `давление · (base + sensitivity · s_задачи)` — базовую
цену узла платит любой под, надбавку — чувствительный. Веса — `weights.json`
(ConfigMap): `{"base": {…}, "sensitivity": {…}}`; абляция оси = нули в обеих
частях, не код.

**Защиты H1** (см. `harness/README.md`, `analysis/README.md`):

- **placement regret** — прямая метрика решения планировщика (давление выбранного узла − минимум доступных), почти без шума исхода;
- **slowdown** — `makespan / изолированный` из соло-бейзлайнов (пин per (profile, node): «одинаковые» облачные ноды бывают ~2× разной скорости);
- **fingerprint** — таблица «заявленный vs измеренный S» с проверкой монотонности, превращает ручные аннотации в верифицированные профили;
- **Trimaran** (`LoadVariationRiskBalancing`) — load-aware, но не interference-aware контрольный бейзлайн (H1-trimaran).

**Известные TODO:** сетевой стрим для профиля `high-s-net` (сейчас net=high декларируется, трафик не генерится — fingerprint флагает); `uncore_imc_*` PMU как уточнение NUMA; wiring `QemuProcessResolver` к KubeVirt API (config B сабмитит Job, не VMI); per-event ntuple вместо `OUTPUT_MODE=burst` для IO.

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
CV statistics + plots). "Разворачивание стенда" above is the actual
step-by-step command sequence (Makefile targets) regardless of reading
language — commands are English-agnostic.
