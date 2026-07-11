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

## Запуск харнесса в кластере (рекомендуется, без port-forward)

`config.yaml`'s `redis.addr` — это in-cluster DNS-имя
(`redis.sensitivityscore-system.svc.cluster.local`), которое видно из подов, но
НЕ резолвится с хоста, откуда раньше запускался сам харнесс. Первый реальный
пилотный прогон именно так и упал (`approximation=no-agent` на всех строках,
хотя агент исправно писал `job:metrics:*`), и временным решением был
`kubectl port-forward` — рабочий, но хрупкий (процесс периодически отваливался
посреди долгого прогона).

Правильное решение — гонять сам харнесс **как Job внутри кластера**
(`harness/Dockerfile` + `harness/deploy/`), тогда Redis резолвится штатно, без
всякого форвардинга:

```bash
make image-harness              # собрать python+kubectl образ харнесса
make harness-run-pilot-incluster        # пилот (§9 чек-листа) как Job
make harness-run-config-a-incluster     # полная матрица, только config A

make harness-logs-incluster JOB=harness-pilot   # следить за логами
make harness-fetch-results JOB=harness-pilot    # забрать results.parquet на хост (после completion)
make harness-clean-reader                       # убрать read-only под, поднятый для выгрузки
```

`harness-fetch-results` работает через общий PVC (`harness/deploy/pvc.yaml`,
`results/` монтируется в Job) — `kubectl cp` не может достучаться до уже
завершившегося пода, поэтому поднимается маленький `harness-results-reader`
под (busybox, монтирует тот же PVC read-only) исключительно для выгрузки.

RBAC (`harness/deploy/rbac.yaml`) даёт `harness-runner` ServiceAccount только
то, что реально нужно кода `k8s_submit.py`: управление `Job`/чтение `Pod` в
namespace `sensitivityscore-bench`, плюс узкий `ClusterRole`
(`get`/`patch`, только на этот один namespace) под идемпотентный
`_ensure_namespace`.

### Альтернатива: запуск с хоста (порт-форвард вручную)

Если по какой-то причине нужно запускать харнесс с хоста напрямую (например,
на партнёрском стенде запуск идёт с bastion-машины без прямого доступа в
кластерную сеть) — `REDIS_ADDR` по-прежнему перекрывает `config.yaml` (см.
`submit/redis_metrics.py`), тот же env var, что уже использует metrics-agent и
scheduler-плагин:

```bash
kubectl -n sensitivityscore-system port-forward svc/redis 16379:6379 &
export REDIS_ADDR=localhost:16379
python run_experiment.py --pilot
```

## Пилотный прогон (шаг 9 чек-листа плана)

Перед полной матрицей — 1 точка плана (`high-s`, `overcommit=2.0`), 3 повтора,
только конфигурация A. Внутри кластера: `make harness-run-pilot-incluster`
(см. выше). С хоста: `python run_experiment.py --pilot`.

## Полная матрица

Внутри кластера: `make harness-run-config-a-incluster` (только config A —
B/C/D нужна инфраструктура партнёрского стенда). С хоста:

```bash
python run_experiment.py --config config.yaml   # все конфигурации из config.yaml
python run_experiment.py --configs A            # только одна конфигурация
```

## Проверка без реального запуска

```bash
python run_experiment.py --dry-run
```

Строит план и логирует все точки (config × profile × overcommit × rep) без
единого вызова `kubectl`/`sbatch` — удобно, чтобы свериться с ожидаемым
количеством прогонов до реального старта серии.

## Результат

`results/results.parquet` со схемой из §5.1 (расширенной):

```
config | profile | overcommit | rep | node | makespan_s | makespan_source |
submit_ts | start_ts | end_ts | llc_miss_rate | numa_remote_ratio | net_bw |
io_iops | io_pressure | approximation | batch_size | batch_index
```

- `makespan_s` — чистое время исполнения, измеренное самим кластером:
  для K8s-бэкендов это окно `startedAt→finishedAt` терминированного
  контейнера, для Slurm — `sacct Elapsed`. Оба исключают очередь/пулл
  образа/старт пода, поэтому K8s- и Slurm-конфигурации сравнимы напрямую
  (H3/H4). `makespan_source` = `container` | `sacct` | `wallclock`
  (fallback на часы харнесса, если кластер не отдал времена — такие строки
  стоит проверять отдельно).
- `submit_ts`/`start_ts`/`end_ts` — для проверки, что члены батча
  действительно перекрывались по времени на узле (реальный co-location).
- Метрики (`llc_miss_rate` и т.д.) — средние за жизнь job: агент
  накапливает суммы в Redis, харнесс делит на число сэмплов и удаляет ключ
  после чтения (см. `submit/redis_metrics.py`).
- IO-измерение раздвоено: `io_pressure` — PSI-доля времени (cgroup v2
  `io.pressure`, строка `some`), когда задачи пода стояли в ожидании IO,
  нормирована ядром в [0,1] — именно её планировщик читает как IO-давление;
  `io_iops` — сырая активность (ops/с), оставлена для анализа, в score не
  участвует (у неё нет честной шкалы [0,1] без калибровки max-IOPS
  устройства). На ядрах без PSI `io_pressure` будет 0 (агент предупредит в
  логе один раз).

Пишется инкрементально после каждого прогона — падение харнесса на середине
матрицы не теряет уже выполненные измерения. `--dry-run` пишет в отдельный
файл `results/dry-run-<results_file>` и не трогает боевые результаты.
Дальше — `../analysis/`.

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
