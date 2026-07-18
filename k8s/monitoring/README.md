# Мониторинг стенда

Prometheus + Grafana + kube-state-metrics + node_exporter на системном узле
(`ss-system`). Без prometheus-operator и без Helm: репозиторий живёт на сырых
манифестах с kustomize (как `k8s/clickhouse/`), а на 2-ГБ системном узле
оператор с CRD стоил бы ~100 Mi ни за что.

```bash
make monitoring-deploy    # развернуть (создаст секрет с паролем и покажет его)
make monitoring-open      # Grafana :3000, Prometheus :9090 через port-forward
make monitoring-targets   # состояние scrape-целей без открытия UI
make monitoring-password  # показать пароль Grafana
make monitoring-clean     # снести (данные в hostPath на узле остаются)
```

## Главное ограничение: чистота измерений

Стенд меряет интерференцию по LLC/NUMA/IO/Net. Любой посторонний процесс на
измерительном узле попадает в те самые счётчики — поэтому системные компоненты
держат отдельно от `bench`-узлов (docs «Ввод прод-стенда (Этап 0)» §2).

Отсюда несимметричная схема сбора:

| Что | Где стоит | Как снимаются метрики bench-узлов |
|---|---|---|
| Prometheus, Grafana, kube-state-metrics | только `ss-system` | — |
| node_exporter | **только `ss-system`** | — |
| kubelet + cAdvisor | уже на всех узлах | скрейп существующего эндпоинта |
| metrics-agent (`/metrics`) | уже на всех узлах | скрейп существующего эндпоинта |

**На bench-узлы не добавляется ни одного нового процесса.** Узловые метрики
оттуда идут с kubelet/cAdvisor и с самого агента, которые там и так работают;
скрейп — это HTTP-запрос раз в 30 с, а не резидентный сборщик.

Цена решения: у bench-узлов нет узловых деталей уровня node_exporter
(температура, per-device diskstats, textfile-коллектор). Если они понадобятся
для конкретной серии — снимать node_exporter **между** сериями, не во время:

```bash
# временно на все узлы
kubectl -n sensitivityscore-monitoring patch ds node-exporter --type=json \
  -p='[{"op":"remove","path":"/spec/template/spec/nodeSelector"}]'
# вернуть обратно ПЕРЕД серией
kubectl -n sensitivityscore-monitoring patch ds node-exporter -p \
  '{"spec":{"template":{"spec":{"nodeSelector":{"node-role.kubernetes.io/ss-system":""}}}}}'
```

## Дашборды

**SensitivityScore — оси чувствительности** (`ss-axes`) — то, ради чего стенд
существует. Верхний ряд намеренно не про нагрузку, а про **годность сбора**:
нулевая ось означает либо «тихо», либо «датчик выключен», и различать это надо
до того, как цифры уедут в диссертацию.

- `PMU честный` — 0 значит synthetic-devbox, LLC/NUMA серии непригодны;
- `LLC откалиброван` — 0 значит `llc_miss_rate` это сырой ratio, который
  **инвертируется под потоковой нагрузкой** (STAGE 2026-07-14: 0.018 под
  штормом против 0.33 на простое). Пока здесь 0 — график LLC rate читать как
  давление нельзя;
- `Net-ось` / `PSI (IO-ось)` — включена ли ось вообще;
- `Возраст сэмпла` — >30 с значит планировщик скорит на протухшем Redis-ключе.

**Стенд — инфраструктура** (`ss-infra`) — узлы, поды, рестарты, OOM, память и
диск `ss-system`, фазы Job'ов харнесса.

## Метрики агента

Агент (`metrics-agent/pkg/promexport`) зеркалит в `/metrics:9101` тот же
узловой `PressureVector`, что пишет в Redis. **Redis остаётся авторитетным**:
горячий путь планировщика и экспорт харнесса читают только его, Prometheus —
read-only наблюдаемость.

Оси: `ss_node_llc_miss_rate`, `ss_node_llc_misses_per_sec`,
`ss_node_numa_remote_ratio`, `ss_node_io_pressure`, `ss_node_io_iops`,
`ss_node_net_bw_bytes_per_second`, `ss_node_net_pressure`.

Годность сбора: `ss_agent_pmu_hardware_available`, `ss_agent_llc_calibrated`,
`ss_agent_net_calibrated`, `ss_agent_psi_available`,
`ss_agent_last_sample_timestamp_seconds`, `ss_agent_sampled_pods`,
`ss_agent_sample_errors_total`, `ss_agent_redis_write_errors_total`.

## Решения, которые легко откатить назад по ошибке

**kube-state-metrics скрейпится отдельной job'ой с `honor_labels: true`, не
через общую annotation-job.** KSM экспортирует метрики *про другие объекты* и
несёт собственные метки `namespace`/`pod`. Общая job проставляет их как метки
цели, и при `honor_labels: false` (умолчание) метка цели побеждает: настоящий
namespace уезжает в `exported_namespace`, а `namespace` у всех серий
становится тем, где живёт сам KSM. Тогда
`kube_pod_status_phase{namespace="sensitivityscore-bench"}` всегда пуст, а
алерты по namespace тихо считают весь кластер одним неймспейсом. Ошибка не
проявляется никак, кроме пустых панелей.

**У metrics-agent своя job с `scrape_interval: 10s`, а не общие 30s.** Причина
— разрешение по времени, не нагрузка. Планировщик читает `node:metrics:<node>`
с TTL 30 с, и панель «Возраст сэмпла» должна ловить протухание раньше, чем оно
случится. При общем интервале 30 с наблюдаемый возраст сам по себе доходит до
~35 с (время с последнего скрейпа + возраст на момент скрейпа): порог 30 с
давал бы и ложную тревогу на здоровом стенде, и срабатывание уже **после**
потери данных планировщиком. При 10 с возраст держится <15 с (замерено на
STAGE: максимум 10.6 с), и порог означает ровно то, что написано на панели.
Агент при этом исключён из общей annotation-job — иначе попал бы в обе.

**Service'ы — ClusterIP, доступ через port-forward.** У узлов стенда белые IP
(`kubectl get nodes -o wide`); NodePort выставил бы Grafana и Prometheus в
интернет.

**Хранилище — hostPath, не PVC.** В кластере нет ни одного StorageClass
(`kubectl get sc` пуст). Поды прибиты к `ss-system` через nodeSelector, так что
путь стабилен; данные лежат в `/var/lib/sensitivityscore/{prometheus,grafana}`
и переживают пересоздание подов. На стенде с CSI — заменить на PVC.

**Alertmanager не развёрнут** (экономия ~80 Mi). Правила
(`prometheus-rules.yaml`) всё равно вычисляются и видны в Prometheus `/alerts`
и в Grafana.

**Метки kustomize — через `labels: [{pairs, includeSelectors: false}]`, не
`commonLabels`.** Устаревший `commonLabels` пишет метку ещё и в
`spec.selector.matchLabels`, а это неизменяемое поле: после первого деплоя
любая правка набора меток ломает `kubectl apply` с `field is immutable`, и
чинится только пересозданием Deployment'ов.

## Бюджет ресурсов STAGE

Узел `ss-system` на STAGE — 2 vCPU / 2 ГБ / 40 ГБ (на проде планируется
4 vCPU / 8 ГБ, docs §4).

| Компонент | requests | limits |
|---|---|---|
| prometheus | 100m / 256Mi | 500m / 512Mi |
| grafana | 50m / 128Mi | 300m / 256Mi |
| kube-state-metrics | 10m / 32Mi | 100m / 128Mi |
| node-exporter | 10m / 32Mi | 100m / 64Mi |

С уже жившими там Redis, планировщиком и metrics-server — ~900 Mi запросов из
~1.8 Gi allocatable. Retention Prometheus: 30 суток или 6 ГБ, что раньше.

**Когда на STAGE приедет ClickHouse** (`k8s/clickhouse/`, сейчас там только
манифесты) — запаса не хватит: ужимать retention или разносить компоненты.
