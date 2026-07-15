# SensitivityScore HPC Bench

Экспериментальный стенд для проверки гипотез диссертации *«Adaptive Resource
Management Methods for Computing Resources in Heterogeneous Distributed
Environments for Scientific and Engineering Tasks»*: сравнение
interference-aware планировщика **SensitivityScore** (плагин Kubernetes
Scheduler Framework) с default kube-scheduler, классическим Slurm и
Slurm-on-K8s (Slinky/slurm-bridge) на эталонной HPC-нагрузке Geant4.

Формальная модель задачи: **Z = {G, R, S}**, где `S = (LLC, NUMA, Net, IO) ∈ [0,1]⁴`
— вектор чувствительности задачи к интерференции по каждому измерению.

## Как это работает

```
metrics-agent (DaemonSet, perf_event_open) ──давление узлов──▶ Redis
                                                                 │ чтение
Geant4-Job (workload, профиль S) ──submit──▶ SensitivityScore scheduler ──размещение──▶ bench-узел
                                                                 ▲ веса weights.json (ConfigMap)
harness (серия = матрица профили×повторы) ──▶ results.parquet ──▶ analysis (H1–H4) + ClickHouse
```

- **metrics-agent** (Go, DaemonSet на каждом узле) измеряет давление по четырём
  осям через `perf_event_open()`/PSI и записывает его в **Redis**.
- **SensitivityScore** (плагин kube-scheduler; исходники — в отдельном форке)
  на фазе Score читает давление узлов из Redis и вектор `S` задачи из аннотаций
  пода, размещает задачу согласно весам.
- **harness** (Python) выполняет серию: отправляет Geant4-Job'ы разных профилей
  `S` с повторами и собирает `results.parquet`.
- **analysis** (Python) вычисляет H1–H4 (Mann-Whitney / Holm / Cliff's δ); те же
  данные дублируются в **ClickHouse** для кросс-стендовой агрегации.

Роли узлов: `ss-system` (taint NoSchedule — Redis, планировщик, metrics-server,
ClickHouse) и `bench` (только измеряемая нагрузка). Системные компоненты на
bench-узлы не допускаются: их фоновая нагрузка исказила бы измерения.

Рабочие документы, из которых собран репозиторий:

- [Глоссарий](<docs/Глоссарий.md>) — термины эксперимента (шторм, жертва,
  токсичность, ошибка размещения…) и их имена в данных. Читать первым.
- [Сводка результатов STAGE (июль 2026)](<docs/Сводка результатов STAGE (июль 2026).md>)
  — итоговые результаты: семь серий (включая плацебо-контроль и различение
  по чувствительности к диску cˢ_io > 0), статус
  гипотез, калибровка цен осей, уточнение модели скоринга и его проверка.
  Точка входа для чтения.
- [Программа экспериментов (Geant4)](<docs/Программа экспериментов (Geant4).md>)
  — методология: конфигурации, гипотезы H1–H4, метрики.
- [Методика измерений](<docs/Методика измерений.md>)
  — что и на чём измеряется: роли компонентов, фазы, правила.
- [Технический план экспериментов](<docs/Технический план экспериментов.md>)
  — детальный план (архитектура, чек-лист, псевдокод) по этапам 0–6.
- [Ввод прод-стенда (Этап 0)](<docs/Ввод прод-стенда (Этап 0).md>)
  — пошаговый ввод нового кластера: роли узлов, калибровки, проверки.

## Структура репозитория

```
docs/                    — программа и методология экспериментов (см. выше)
workload/                — Docker-образ Geant4 с управляемым профилем S
k8s/
  config-a-baremetal/    — Job-манифесты для K8s bare-metal (low-s/high-s)
  config-b-kubevirt/     — VMI-манифесты для KubeVirt
  config-d-slinky/       — Job-манифест для Slinky/slurm-bridge
  scheduler-config/      — KubeSchedulerConfiguration + веса score-функции
  clickhouse/            — in-cluster ClickHouse (StatefulSet) для прод-прогонов
slurm/config-c/          — sbatch-скрипты для классического Slurm
metrics-agent/           — Go: DaemonSet-агент, perf_event_open() → Redis
aggressor/               — генератор LLC/membw-нагрузки (stress-ng) для pressure-сценариев
harness/                 — Python: оркестрация серии экспериментов (run_experiment.py),
                           запускается как Job внутри кластера (harness/deploy/)
statusserver/            — Python: страница прогресса серии (запускается целью make series)
analysis/                — Python: статистика (Mann-Whitney, Cliff's δ) + графики
db/clickhouse/           — схема и загрузчик parquet → ClickHouse (агрегатор результатов)
scripts/                 — bootstrap-кластера и run-series.sh (запуск серии одной командой)
```

Плагин SensitivityScore (extension point Score) размещён в отдельном форке
`kubernetes-sigs/scheduler-plugins` (`../scheduler-plugins` рядом с этим
репозиторием); здесь находятся только манифесты деплоя
(`k8s/scheduler-config/`) и обёртка Makefile над его сборкой.

## Разворачивание стенда

`make help` выводит полный список целей. Приведённый порядок — от нуля до
проверки H1–H4 на новом прод-стенде.

### Предпосылки

- **Инструменты на машине оператора:** Docker, `kubectl`, `make`, Go (сборка
  metrics-agent и плагина), Python 3.11+ (виртуальные окружения harness и
  analysis создаются целями Makefile автоматически).
- **Кластер:** работающий Kubernetes (не менее двух узлов: минимум один
  `ss-system` и один `bench`), kubeconfig указывает на него. Развёртывание
  Kubernetes с нуля и подготовка узлов (ядро, PMU, PSI) описаны в
  [Ввод прод-стенда (Этап 0)](<docs/Ввод прод-стенда (Этап 0).md>).
- **Образы:** `andreyza/*` опубликованы в Docker Hub, поэтому для
  воспроизведения в неизменном виде шаг 1 (сборка и публикация) можно
  пропустить — кластер загрузит их самостоятельно. Собственный registry
  требуется только при изменении кода: `export REGISTRY=<свой>` (переопределяет
  теги сборки/публикации и scheduler-deploy) и правка полей `image:` в
  манифестах `k8s/` и `*/deploy/`, где имя `andreyza/*` задано явно.
- **kubeconfig для серий:** `make series` по умолчанию использует
  `~/.kube/configs/timeweb-stage`; иной путь задаётся как
  `KUBECONFIG=<путь> make series …`.
- **Форк планировщика** `../scheduler-plugins` рядом с этим репозиторием.

### 0. Проверка PMU (до развёртывания DaemonSet)

cgroup-scoped `perf_event_open()` не эквивалентен CAP_PERFMON на поде;
корректность счётчиков проверяется до развёртывания `metrics-agent`:

```bash
make perfcheck-image && make perfcheck-push
make perfcheck-run NODE=<узел>   # по каждому узлу неоднородного стенда; без NODE под размещается произвольно
make perfcheck-logs              # SUCCESS с ненулевым счётчиком — PMU достоверен; read=0 — счётчик подделан (гипервизор не виртуализирует cgroup-PMU)
make perfcheck-clean
```

### 1. Образы (только при изменении кода; иначе см. «Образы» выше)

Сборка выполняется локально; на многоузловом стенде обязательна публикация
(`imagePullPolicy: Always`). Теги заданы переменными `*_IMAGE` в `Makefile`:

```bash
make images && make images-push                                   # workload, metrics-agent, harness, aggressor
make scheduler-plugin-image                                       # плагин — в форке
make -C ../scheduler-plugins -f sensitivityscore.mk ss-push
```

### 2. Кластер

`SS_NODES` — узел (или узлы) под системные компоненты; остальные worker-узлы
получают роль `bench` автоматически (разметка идемпотентна):

```bash
make setup-cluster SS_NODES="<ss-system-узел>"  # роли узлов, namespace+Redis, планировщик, DaemonSet-агент
make scheduler-status                            # под планировщика, ConfigMap-ы, события планирования
make test-pod-highs && make test-pod-lows && make test-pod-clean  # экспресс-проверка: score high-S против low-S
```

### 3. Калибровка Net-оси (опционально; между сериями — перезапускает агент)

```bash
make netcheck-run                            # cross-node iperf3 --bidir
make netcheck-logs                           # → NET_REFERENCE_MBPS
make netcheck-apply NET_REFERENCE_MBPS=<N>   # запись в env DaemonSet; без калибровки ось равна 0
```

### 4. Прогон серии

Одна команда выполняет preflight-проверки, эталоны, серию, страницу статуса и
watchdog (`scripts/run-series.sh`; `SERIES=<имя>` соответствует
`harness/config-stage-<имя>.yaml`):

```bash
make series SERIES=<имя>          # имя ∈ placebo | llc | mixed | mixed-calib
make series-status SERIES=<имя>   # фазы, ошибки, число строк результатов (в реальном времени)
make series-stop SERIES=<имя>     # остановить серию и удалить агрессоры и Job'ы
```

Соответствие сериям из Сводки: `llc` — серия кэша (LLC); `mixed` и
`mixed-calib` — смешанная и смешанная с калиброванным скорингом; `placebo` —
отрицательный контроль. Ранние серии диска и сети выполнялись на другом составе
узлов и отдельными конфигами не поставляются. Trimaran (`scheduler_variants`) и
pressure-агрессоры уже описаны в stage-конфигах; `make trimaran-deps`
(metrics-server) требуется однократно на стенд, если включено плечо trimaran.

Результаты записываются в `harness/results/` (секция `output` конфига).
Низкоуровневые цели для отдельных прогонов и пилота: `make harness-rbac`
(однократно), затем `harness-run-{pilot,config-a,baseline,pressure}-incluster`
и `harness-fetch-results`/`-baselines` (см. `make help`).

### 5. Анализ H1–H4

Локальный parquet (`harness/results/{results,baselines}.parquet`; `make series`
записывает его напрямую, для in-cluster Job-пути — после `harness-fetch-*`):

```bash
make analyze   # report/: summary.md, comparisons.csv, fingerprint.csv и графики
make report    # analyze с последующим открытием summary.md
```

Метрики: `makespan_s`, `slowdown` (при наличии эталонов) и `placement_regret` —
каждая образует отдельное семейство поправки Holm.

Дублирование в ClickHouse (агрегатор нескольких стендов; запись ведётся в оба
места — локальный parquet и CH):

```bash
make ch-tunnel  CH_SSH=user@pc                            # SSH-туннель к ПК-агрегатору
make ch-load    CH_HOST=localhost STAND=<s> RUN_LABEL=<l> # загрузка results и baselines
make ch-analyze STAND=<s> RUN_LABEL=<l>                   # тот же отчёт из ClickHouse
make ch-tunnel-close
```

In-cluster ClickHouse (для прод-стенда: запись напрямую из кластера) описан в
`k8s/clickhouse/README.md`.

### Уборка

```bash
make harness-clean-jobs   # Job'ы харнесса
make harness-clean-full   # и namespace харнесса (вместе с PVC результатов)
make nuke                 # всё перечисленное, Deployment планировщика и виртуальные окружения
```

## Гипотезы

| # | Формулировка (кратко) |
|---|---|
| H1 | SensitivityScore (config A) обеспечивает меньший makespan и меньшую дисперсию, чем default kube-scheduler, при co-location различных профилей S |
| H1-trimaran | Преимущество даёт именно вектор S, а не «любой учёт загрузки»: SensitivityScore превосходит и load-aware Trimaran (слеп к LLC/NUMA/IO) на pressure-сценариях |
| H2 | Накладные расходы виртуализации (config B) снижают эффективность, но относительное преимущество SensitivityScore над default сохраняется |
| H3 | Slurm (config C) — верхняя граница на однородной нагрузке, но уступает SensitivityScore в co-location сценариях |
| H4 | Slinky/slurm-bridge (config D) близок к C на однородной нагрузке, но уступает A в сценариях смешанной чувствительности |

Полные формулировки — в `docs/Программа экспериментов (Geant4).md` §6. Текущий
статус проверки — в `docs/Сводка результатов STAGE (июль 2026).md`, секция
«Статус гипотез».

## Статус реализации

Конфигурации (детали и статус гипотез — `docs/Сводка результатов STAGE (июль 2026).md`):

| Конфиг | Что сравнивает | Статус |
|--------|----------------|--------|
| **A** (K8s) | default / SensitivityScore / trimaran | выполнено на STAGE: 5 серий × 10 повторов, эталоны на каждом узле |
| **B** (KubeVirt) | накладные расходы виртуализации | ожидает прод-стенда |
| **C** (Slurm) | верхняя граница на однородной нагрузке | ожидает прод-стенда |
| **D** (Slinky) | slurm-on-k8s | ожидает прод-стенда |

Цикл конфигурации A замкнут: два проигрыша исходного скоринга → калибровка цен
осей → уточнение модели (базовая цена оси) → проверочная серия закрыла все
заранее объявленные критерии.

Оси S — все четыре измеряются агентом и входят в score:

| Ось | Метрика агента | Примечание |
|-----|----------------|------------|
| LLC | промахи/сек ÷ `LLC_REFERENCE_MISSES_PER_SEC` | отношение промахов инвертируется под потоковой нагрузкой (см. Методику) |
| NUMA | `node-load-misses ÷ node-loads` (PMU) | на STAGE ось нулевая (один NUMA-домен на узел); на прод-стенде активна (2 сокета/узел, Xeon 8462Y+) |
| IO | PSI `io.pressure` | сырые IOPS сохраняются только для анализа |
| Net | `net_bw ÷ NET_REFERENCE_MBPS` | калибровка `make netcheck-run` (iperf3 `--bidir`); без калибровки ось равна 0 |

Скоринг: вклад оси = `давление · (base + sensitivity · s_задачи)`. Базовую цену
узла платит любая задача, надбавку — чувствительная. Веса заданы в `weights.json`
(ConfigMap): `{"base": {…}, "sensitivity": {…}}`; отключение оси — нули в обеих
частях, без изменения кода.

Защиты H1 (см. `harness/README.md`, `analysis/README.md`):

- **placement regret** — прямая метрика решения планировщика (давление
  выбранного узла минус минимум доступных), практически без шума исхода;
- **slowdown** — `makespan / изолированный` из соло-эталонов (привязка по паре
  (профиль, узел): «одинаковые» облачные узлы различаются по скорости до ~2 раз);
- **fingerprint** — сверка заявленного и измеренного S с проверкой
  монотонности, превращающая ручные аннотации в верифицированные профили;
- **Trimaran** (`LoadVariationRiskBalancing`) — load-aware, но не
  interference-aware контрольный бейзлайн (H1-trimaran).

Известные TODO: сетевой стрим для профиля `high-s-net` (сейчас net=high
декларируется, но трафик не генерируется — fingerprint это отмечает);
`uncore_imc_*` PMU как уточнение NUMA-метрики; подключение
`QemuProcessResolver` к KubeVirt API (config B отправляет Job, а не VMI);
per-event ntuple вместо `OUTPUT_MODE=burst` для оси IO.

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
