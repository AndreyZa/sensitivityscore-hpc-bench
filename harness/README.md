# harness — оркестрация серий экспериментов

Реализация §4 технического плана: не повторять `kubectl apply`/`sbatch` руками
10 раз на точку плана.

```
config.yaml              — факторы плана (configs/profiles/overcommit/repetitions, §7.1)
profiles.py               — числовые значения low-s/high-s (env + аннотации)
templates/*.j2             — Jinja2-шаблоны манифестов (k8s Job, sbatch)
submit/k8s_submit.py       — backend для A/B/D (kubectl apply/wait)
submit/slurm_submit.py     — backend для C (sbatch/squeue/sacct)
submit/redis_metrics.py    — общее чтение job:metrics:* из Redis
run_experiment.py           — главный цикл, пишет results/results.parquet
```

## Установка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Redis при локальном запуске (вне кластера)

`config.yaml`'s `redis.addr` — это in-cluster DNS-имя
(`redis.sensitivityscore-system.svc.cluster.local`), которое видно из подов, но
НЕ резолвится с хоста, откуда обычно запускается сам харнесс. Первый реальный
пилотный прогон именно так и упал: во всех строках результата
`approximation=no-agent`, хотя metrics-agent исправно писал каждый
`job:metrics:*` ключ — просто харнесс не мог достучаться до Redis. Перед
запуском:

```bash
kubectl -n sensitivityscore-system port-forward svc/redis 16379:6379 &
export REDIS_ADDR=localhost:16379
```

`REDIS_ADDR` перекрывает `config.yaml` (см. `submit/redis_metrics.py`) — тот же
env var, что уже использует metrics-agent и scheduler-плагин.

## Пилотный прогон (шаг 9 чек-листа плана)

Перед полной матрицей — 1 точка плана (`high-s`, `overcommit=2.0`), 3 повтора,
только конфигурация A:

```bash
python run_experiment.py --pilot
```

## Полная матрица

```bash
python run_experiment.py --config config.yaml
```

Только одна конфигурация (например, при поэтапном разворачивании B/C/D по
дорожной карте):

```bash
python run_experiment.py --configs A
```

## Проверка без реального запуска

```bash
python run_experiment.py --dry-run
```

Строит план и логирует все точки (config × profile × overcommit × rep) без
единого вызова `kubectl`/`sbatch` — удобно, чтобы свериться с ожидаемым
количеством прогонов до реального старта серии.

## Результат

`results/results.parquet` со схемой из §5.1:

```
config | profile | overcommit | rep | node | makespan_s | llc_miss_rate | numa_remote_ratio | net_bw | io_iops | approximation
```

Пишется инкрементально после каждого прогона — падение харнесса на середине
матрицы не теряет уже выполненные измерения. Дальше — `../analysis/`.

## Особенности реализации относительно плана

- Конфигурация **A** и **B** внутри харнесса разворачиваются в два варианта
  (`A-default` / `A-sensitivityscore`, аналогично для B) — иначе H1/H2 (прямое
  A/B-сравнение планировщиков внутри одной инфраструктурной конфигурации)
  нечем было бы отличить в результатах.
- Конфигурация **D** (Slinky) автоматически пропускает точки плана с
  `overcommit > 1.0` — whole-node allocation не поддерживает co-location
  (Программа экспериментов §3.1).
- Конфигурация **B** сабмитится в харнессе как обычный `Job` (не `VirtualMachineInstance`)
  для единообразия submit/wait-логики; для реального прогона на стенде замените
  `k8s_submit.py` на работу с `k8s/config-b-kubevirt/vmi-*.yaml` и polling через
  `kubectl get vmi -o jsonpath='{.status.phase}'`.
