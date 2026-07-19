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
submit/node_pressure.py    — снапшот node:metrics + placement regret (см. ниже)
run_experiment.py           — главный цикл, пишет results/results.parquet
```

## Режимы запуска (три взаимоисключающих)

| Режим | Флаг | Что делает | Выход |
|---|---|---|---|
| Матрица | *(по умолчанию)* | config × profile × overcommit × rep | `results.parquet` |
| Pressure | `--pressure` | агрессоры + поток жертв (H1 «money experiment») | `results.parquet` |
| Baseline | `--baseline` | соло-прогоны профилей на ПУСТОМ кластере | `baselines.parquet` |

`--baseline` гоняет каждый профиль (матричные + жертвы pressure-сценариев)
последовательно в изоляции, PER-NODE: каждый профиль пинуется на каждую
worker-ноду (nodeSelector), потому что «одинаковые» облачные ноды бывают в
разы разной реальной скорости (на STAGE пересозданный worker оказался ~1.9x
медленнее соседей) — общий на все ноды знаменатель приписывал бы разницу
железа интерференции. `baseline.per_node: false` отключает пин для заведомо
однородного стенда (в <число нод> раз дешевле). Из baselines `../analysis/`
берёт знаменатели slowdown (`makespan_isolated` per (profile, node)) и
«эталон» для fingerprint-таблицы (заявленный vs измеренный S). Кластер обязан
быть пустым — сосед или живой агрессор молча занизит все slowdown. Запускать
до боевой матрицы, один раз на стенд:

```bash
python run_experiment.py --baseline        # -> results/baselines.parquet
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
на прод-стенде запуск идёт с bastion-машины без прямого доступа в
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
B/C/D нужна инфраструктура прод-стенда). С хоста:

```bash
python run_experiment.py --config config.yaml   # все конфигурации из config.yaml
python run_experiment.py --configs A            # только одна конфигурация
```

## Pressure-сценарий (агрессоры + поток жертв)

Центральный эксперимент для H1 (`run_experiment.py --pressure`,
`config.yaml: pressure_scenarios`). Однородные одновременные батчи не дают
interference-aware планировщику ничего выиграть: все job одинаковы, а
решения принимаются в t=0, когда давление ещё не построилось. Pressure-
сценарий создаёт условия, где преимущество может проявиться:

1. **Фаза давления** — на часть worker-нод (nodeName-пин, мимо
   планировщиков — ландшафт одинаков для обоих плеч) сажаются агрессоры
   `stress-ng --stream` (см. `aggressor/`) с маленьким cpu-request:
   ресурсная модель default-планировщика видит свободную ноду, metrics-agent
   видит давление на LLC/полосу памяти.
2. **Фаза измерения** — после `stabilize_seconds` через планировщик плеча
   (`A-default`, затем `A-sensitivityscore`) подаётся поток жертв
   (`victim_arrival`: fixed или poisson; паттерн прибытия одинаков для обоих
   плеч). Интервал держать больше лага метрик-пайплайна (~15с).

`aggressors_per_node` — список интенсивностей (dose-response ось); значение
пишется в колонку `overcommit` pressure-строк. Колонка `scenario`
(`batch` | `pressure:<name>`) отделяет эти строки от матрицы, analysis
считает сравнения и поправку Холма отдельно по каждому сценарию.

Тип давления задаёт `aggressor_args` (stress-ng): `--stream` — LLC/полоса
памяти (сценарий `llc`), `--hdd` — диск (сценарий `io`; файлы пишутся в
emptyDir `/scratch`, PSI `io.pressure` ноды растёт). У IO-сценария жертва —
`high-s-io`: тот же Geant4, но с реальным дисковым выводом на критическом
пути (`OUTPUT_MODE=blocking`: compute, затем fsync-сброс результатов; задача
не завершена, пока запись не выполнена) — жертва без собственного IO не
страдала бы от дисковой контенции, и сценарий мерил бы честный ноль. Режим
`burst` (бесконечный фоновый писатель) оставлен для детекционных сценариев:
он создаёт давление, но время самой жертвы от диска не зависит.

Отдельный режим `aggressor_mode: "net"` (сценарий `net`) — не stress-ng, а
iperf3-пары: на каждый слот штормимой ноды свой сервер (`iperf3 -s`; сервер
обслуживает один тест за раз) + UDP-клиент с фиксированным
`net_bitrate_mbps`. Оба конца пары НА ОДНОЙ ноде (трафик pod-to-pod через
veth): cross-node пара засветила бы rx-стороной и чистую ноду. Требует
Net-калибровки агента (`make netcheck-run` -> `NET_REFERENCE_MBPS` на
DaemonSet), иначе net_pressure у всех нод 0 и плагин шторм не видит. В этом
детекционном режиме жертва `high-s-net` трафика не генерирует
(`OUTPUT_MODE=none`) и fingerprint честно флагает ось.

Для измерения ДЕГРАДАЦИИ (цена cˢ_net) есть шторм `mode: net-egress` в
`storms`: iperf3-клиенты на штормовом узле гонят TCP на сервер, пиннутый на
ДРУГОМ узле (`egress_server_node`), насыщая аплинк — локальная veth-пара
обмен соседа не замедляет (замер ×5.2 — `scripts/net_probe.sh`). Жертва при
этом включает `OUTPUT_MODE=stream` (стрим результатов на sink-под по TCP,
критический путь). Собранный пример — серия `net-diff`
(`config-stage-net-diff.yaml` + `run-stage-net-diff.sh`, sink пиннется
манифестом `k8s/net-sink/sink-stage.yaml`).

```bash
python run_experiment.py --pressure                # все сценарии из config.yaml
python run_experiment.py --pressure --scenarios llc
# или внутри кластера:
make image-aggressor && docker push andreyza/aggressor:dev
make harness-run-pressure-incluster
make harness-logs-incluster JOB=harness-pressure
```

## Мониторинг прогресса (HTTP-эндпойнт)

Статус-сервер живёт отдельным пакетом `../statusserver/` (см. его
[README](../statusserver/README.md)): страница статуса идущего прогона — фаза,
прогресс и ETA, план эксперимента, таблица размещения по планировщикам, живые
Job'ы/агрессоры, хвост лога. Только чтение.

Поднимается контейнером через docker compose и **при старте любого прогона
сама**: и через `make series`, и при ручном запуске `run-stage-<серия>.sh`
(вызов вшит в шапку каждого скрипта). Идемпотентно — если нужная страница уже
отвечает, контейнер не трогается.

```bash
make status-page SERIES=<имя>       # поднять вручную (напр. посмотреть законченную серию)
make series-status SERIES=<имя>     # состояние страницы: жива? та ли серия?
make status-page-down               # погасить (make series-stop её НЕ гасит)
# -> http://localhost:8787   (HTML; /json — для скриптов, /healthz — для проб)
```

Хостовым питоном страница больше не запускается: она умирала с SIGSEGV на
чтении parquet (дефолтный аллокатор Arrow, а не версия python, как считалось
сначала) — разбор в README пакета.

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
io_iops | io_pressure | approximation | scenario | batch_size | batch_index |
interference_chosen | placement_regret | sensitivity_{llc,numa,net,io}
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
- `interference_chosen`/`placement_regret` — качество решения планировщика.
  Перед сабмитом харнесс снимает снапшот давления всех нод (`node:metrics:*`)
  и после размещения считает `regret = interference(выбранной) − min по нодам`
  той же скор-функцией, что плагин (`submit/node_pressure.py`; веса —
  `score_weights` в config.yaml, обязаны совпадать с `weights.json` ConfigMap).
  Считается на стороне харнесса, поэтому покрыто и плечо `A-default`, где
  плагин не запущен. NaN, если ноды нет в снапшоте (агент не писал / TTL / имя
  Slurm-ноды не совпало). Наиболее осмысленно на pressure-сценариях; в batch
  снапшот снимается до того, как со-размещённые члены создадут давление, так
  что там regret ≈ 0 у всех плеч.
- `sensitivity_{llc,numa,net,io}` — заявленный S-вектор профиля из
  `profiles.py`, едет в данные для fingerprint-таблицы анализа (analysis не
  импортирует harness).

Пишется инкрементально после каждого прогона — падение харнесса на середине
матрицы не теряет уже выполненные измерения. `--dry-run` пишет в отдельный
файл `results/dry-run-<results_file>` и не трогает боевые результаты.
Дальше — `../analysis/`.

## Особенности реализации относительно плана

- Конфигурация **A** и **B** внутри харнесса разворачиваются в плечи-варианты
  планировщика (`A-default` / `A-sensitivityscore`, аналогично для B) — иначе
  H1/H2 (прямое A/B-сравнение планировщиков внутри одной инфраструктурной
  конфигурации) нечем было бы отличить в результатах. Набор плеч задаётся
  `scheduler_variants` в config.yaml.
- **Trimaran** (`LoadVariationRiskBalancing`) — опциональное третье плечо
  (`A-trimaran`, H1-trimaran): включается добавлением `trimaran` в
  `scheduler_variants`. Load-aware контрольный бейзлайн (видит утилизацию
  CPU/памяти через metrics-server, но слеп к LLC/NUMA/IO) — обыгрыш его на
  pressure-сценариях отделяет interference-awareness от «любого учёта
  загрузки». Добавляет плечо ко всем точкам (дороже) и требует metrics-server
  на стенде (`make trimaran-deps`); профиль планировщика — в
  `k8s/scheduler-config/scheduler-config.yaml`.
- Конфигурация **D** (Slinky) автоматически пропускает точки плана с
  `overcommit > 1.0` — whole-node allocation не поддерживает co-location
  (Программа экспериментов §3.1).
- Конфигурация **B** сабмитится в харнессе как обычный `Job` (не `VirtualMachineInstance`)
  для единообразия submit/wait-логики; для реального прогона на стенде замените
  `k8s_submit.py` на работу с `k8s/config-b-kubevirt/vmi-*.yaml` и polling через
  `kubectl get vmi -o jsonpath='{.status.phase}'`.
