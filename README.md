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

- [`docs/Технический_план_экспериментов.md`](docs/Технический_план_экспериментов.md)
  — детальный план (архитектура, чек-лист, псевдокод) по этапам 0–6.
- [`docs/Программа_экспериментов_Geant4.md`](docs/Программа_экспериментов_Geant4.md)
  — методология для партнёров: конфигурации стенда, гипотезы H1–H4, метрики.

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
scheduler-plugin/        — Go: сам плагин SensitivityScore (Score extension point)
metrics-agent/           — Go: DaemonSet-агент, perf_event_open() → Redis
harness/                 — Python: оркестрация серии экспериментов (run_experiment.py)
analysis/                — Python: статистика (Mann-Whitney, Cliff's delta) + графики
scripts/                 — bootstrap-скрипты для кластера
```

## Быстрый старт

```bash
# 1. Инфраструктура: namespace + Redis + веса score-функции
./scripts/bootstrap-cluster.sh

# 2. Собрать образы
docker build -t sensitivityscore-bench/geant4:11.2 ./workload
docker build -t sensitivityscore-bench/scheduler:dev ./scheduler-plugin
docker build -t sensitivityscore-bench/metrics-agent:dev ./metrics-agent

# 3. Развернуть плагин-планировщик и агент метрик (см. README в scheduler-plugin/, metrics-agent/)
kubectl apply -f metrics-agent/deploy/daemonset.yaml

# 4. Пилотный прогон (1 точка плана, 3 повтора, конфигурация A — sanity-check)
cd harness && pip install -r requirements.txt && python run_experiment.py --pilot

# 5. Полная матрица (см. harness/config.yaml для факторов плана)
python run_experiment.py

# 6. Анализ и проверка H1–H4
cd ../analysis && pip install -r requirements.txt
python analyze.py --results ../harness/results/results.parquet --outdir report/
```

## Гипотезы

| # | Формулировка (кратко) |
|---|---|
| H1 | SensitivityScore (config A) даёт меньший makespan и меньшую дисперсию, чем default kube-scheduler, при co-location разных профилей S |
| H2 | Оверхед виртуализации (config B) снижает эффективность, но относительное преимущество SensitivityScore над default сохраняется |
| H3 | Slurm (config C) — верхняя граница на однородной нагрузке, но проигрывает SensitivityScore в co-location сценариях |
| H4 | Slinky/slurm-bridge (config D) близок к C на однородной нагрузке, но проигрывает A в сценариях смешанной чувствительности |

Полные формулировки — в `docs/Программа_экспериментов_Geant4.md` §6.

## Статус реализации

Репозиторий — рабочий каркас всех компонентов плана (не production-ready): код
компилируется/проходит синтаксическую проверку, статистический пайплайн
протестирован end-to-end на синтетических данных. Осознанно оставлены как
явные TODO (см. соответствующие README):

- NUMA-bandwidth счётчики (`uncore_imc_*` PMU) — специфичны для модели CPU стенда.
- Network-byte counters — требуют eBPF `cgroup_skb` hook, отдельная единица работы.
- Wiring `QemuProcessResolver` к реальному KubeVirt API кластера.

---

## English (short)

Experimental testbed comparing the **SensitivityScore** interference-aware
Kubernetes scheduler plugin (task model `Z = {G, R, S}`, `S` = LLC/NUMA/Net/IO
sensitivity vector) against default kube-scheduler, classical Slurm, and
Slurm-on-K8s (Slinky) on a Geant4 HPC benchmark. See `docs/` for the full
experiment plan and hypotheses H1–H4. Components: `workload/` (parameterized
Geant4 Docker image), `k8s/` + `slurm/` (per-configuration manifests),
`scheduler-plugin/` + `metrics-agent/` (Go: the plugin and its
`perf_event_open()` → Redis metrics pipeline), `harness/` (Python: experiment
orchestration), `analysis/` (Python: Mann-Whitney U / Cliff's delta / CV
statistics + plots). Quick start above works the same regardless of reading
language — commands are English-agnostic.
