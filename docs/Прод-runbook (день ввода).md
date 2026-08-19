# Прод-runbook: день ввода стенда

Исполняемая последовательность — каждая ступень: команда → проверка. Порядок
строгий; следующая ступень не начинается, пока проверка текущей не зелёная.
Контекст и обоснования — в «Переход STAGE→прод (статус и чеклист).md»;
здесь только «что жать».

Составлен 07.08.2026 по итогам STAGE (ступени 5 и 7 обкатаны на живом STAGE
06.08 теми же командами).

---

## 0. Входные условия (до площадки)

- От партнёра: IP и SSH-доступ к 7 хостам (3 CP-ВМ, 3× Dell R760, 1 ss-system ВМ).
- Репозитории свежие: `sensitivityscore-hpc-bench`, `hpc-k0s-provision`.
- Бэкап STAGE-истории под рукой: `~/phd/sensitivityscore-ch-backup-*.tar.gz`.
- Образы в Docker Hub (стендовые — уже там; плагин — релиз CI
  `SCHEDULER_RELEASE_VER` в Makefile, руками не пушить).

## 1. Провижн кластера

Гонять **с лаба (.72)**: прод-сеть видна только из его WG-туннеля, а k0sctl
ходит своим SSH-клиентом и `~/.ssh/config` (ProxyJump) не читает.

```bash
cd ~/phd/hpc-k0s-provision
$EDITOR inventory/hosts.yml        # IP 7 хостов (заполнен 18.08 от партнёра)
make provision                     # Ansible OS-prep + k0sctl; kubeconfig-кнопка
```
**Проверка:** kubeconfig лёг в `~/.kube/configs/prod` и стал дефолтом
(симлинк `~/.kube/config` — цель kubeconfig-install в конце provision;
лабный local72 остаётся в `~/.kube/configs/local72.yaml`);
`kubectl get nodes` — все узлы Ready, роли согласно inventory.

## 2. Роли, базовые сервисы, мониторинг

```bash
cd ~/phd/sensitivityscore-hpc-bench
export KUBECONFIG=$HOME/.kube/configs/prod
make bootstrap SS_NODES="<имя ss-system-узла>"   # роли+taint, namespace, Redis
# Учётка реестра — ДО setup-cluster: без неё Docker Hub считает стенд анонимным
# и режет 100 вытягиваний в час на внешний адрес (общий на все узлы за NAT),
# из-за чего длинная серия сыплется в ErrImagePull на середине.
make registry-secret DOCKERHUB_USER=<логин> DOCKERHUB_TOKEN=<токен>
make setup-cluster                               # планировщик (релиз CI) + агент + load-watcher
make trimaran-deps                               # metrics-server — его читает load-watcher
make monitoring-deploy                           # прод-оверлей стоит по умолчанию
```
Секрет `ss-notifier-config` (токен бота + токен приёма) `monitoring-deploy`
собирает из `config.env`, оставшегося на лабе от прежнего экземпляра службы, —
поэтому этот шаг гонится **с лабы**. Другой путь: `make notifier-secret
NOTIFIER_ENV=<файл>`. Дальше правда о канале живёт в Secret'е кластера: чат
меняется `make notifier-chat CHAT_ID=…`, а не правкой файла.
**Проверка:** `make scheduler-status` — под жив, ConfigMap-ы на месте;
`make monitoring-targets` — все цели up; `kubectl get ds -n
sensitivityscore-system` — metrics-agent на всех bench-узлах;
`kubectl -n sensitivityscore-bench get sa default -o jsonpath='{.imagePullSecrets}'`
— учётка реестра прописана (её же проверяет preflight серии).

**Учесть при появлении новых компонентов:** `registry-secret` патчит
ServiceAccount'ы, которые существуют НА МОМЕНТ запуска. Развернул новый
компонент — прогони таргет ещё раз.

## 2а. Метрики control-plane (окно между кампаниями)

Контроллеры k0s работают без kubelet — это не узлы Kubernetes, обычный
мониторинг до них не достаёт. Поэтому два отдельных шага, оба уже в IaC.

**Экспортёр на ВМ** — приезжает с `make provision` (роль `nodeexporter`,
`playbooks/prep.yml`, только группа controllers). Отдельно ставится так:

```bash
cd ~/phd/hpc-k0s-provision
ansible-playbook -i inventory/hosts.yml playbooks/prep.yml --limit controllers
```

**Слушатели etcd, планировщика и controller-manager.** По умолчанию k0s держит
их на localhost. Конфиг уже в шаблоне (`storage.etcd.extraArgs.listen-metrics-urls`,
`bind-address` у scheduler/controllerManager), применяется вместе с
**перезапуском контроллеров**, поэтому — только когда серия не идёт:

```bash
cd ~/phd/hpc-k0s-provision
make cluster            # рендер k0sctl.yaml из inventory + k0sctl apply
```

k0sctl перезапускает контроллеры последовательно, кворум etcd сохраняется.
После этого — перечитать конфиг сбора (рестарт пода не нужен, у Prometheus
включён `--web.enable-lifecycle`):

```bash
cd ~/phd/sensitivityscore-hpc-bench
kubectl apply -k k8s/monitoring/overlays/prod
sleep 70   # ConfigMap доезжает до пода
kubectl -n sensitivityscore-monitoring exec deploy/prometheus -- \
    wget -q -O- --post-data="" http://localhost:9090/-/reload
```

**Проверка:** в Prometheus цели `control-plane-nodes`, `etcd`, `kube-scheduler`
и `kube-controller-manager` — все `up` (3 экземпляра каждая); дашборд
«Control plane — ВМ и API» показывает fsync WAL и лидера etcd вместо пустых
панелей.

**Дашборды правятся ТОЛЬКО через git.** Grafana провижнит их из ConfigMap
(`k8s/monitoring/base/grafana-dashboards.yaml`), поэтому правка в UI живёт до
ближайшего `make monitoring-reload` и исчезает без предупреждения. Порядок:
поправить JSON в репозитории → `make monitoring-reload`. Панель, добавленная
через UI «на минутку», не переживёт следующий деплой.

**Коллегам:** `http://10.40.10.0:3000` — Grafana открыта в сети стенда
(hostPort на ss-system, анонимный вход с правами просмотра). Наружу порт не
публикуется; админ-вход — по секрету `grafana-admin`.

**Ловушка, стоившая живого эндпоинта (19.08.2026).** hostPort и анонимный вход
живут ТОЛЬКО в прод-оверлее, а `MONITORING_OVERLAY` по умолчанию указывал на
stage: `make monitoring-reload` без переменной пересобрал Grafana без hostPort —
адрес выше перестал отвечать, — а Prometheus заодно уехал с retention 365d/60GB
на 30d/6GB и с лимита памяти 2 ГиБ на 512 МиБ. Ничего не сломалось громко:
поды Running, цели up, дашборды на месте. Исправлено двумя способами сразу —
дефолт переведён на прод-оверлей (ошибка в эту сторону безобидна), и добавлен
страж `scripts/monitoring-overlay-guard.sh`, который на стенде с
измерительными узлами отказывается применять оверлей с `STAND != prod`.
Обход — `MONITORING_ALLOW_OVERLAY=1`.

## 2б. Канал оповещений (проверить ДО первой длинной серии)

Правила стенда описывают состояния, в которых он **продолжает писать цифры, но
нести их в диссертацию уже нельзя** (нечестный PMU, вставший агент,
некалиброванная ось). Серия идёт 4 часа, свип веса — сутки, и всё это время за
стендом никто не сидит: до 19.08.2026 правила вычислялись, но адресата не имели.

```bash
make alerts-status        # правила, активные алерты, счётчики доставки, сторож
make alerts-test          # синтетический алерт: в чат придёт сообщение и через минуту снятие
```

**Проверка:** `alerts-test` даёт «принято Alertmanager'ом», в Telegram приходит
сообщение, `alerts-status` показывает ненулевое «отправлено» и нулевое
«не удалось». Сторож `SSWatchdog` обязан быть `firing` — это единственное
правило с `expr: vector(1)`, и его смысл в том, чтобы молчание канала было
отличимо от здоровья: два сообщения в сутки, их пропажа и есть сигнал.

**Ловушка партнёрской сети.** `api.telegram.org` резолвится в единственный
адрес `149.154.166.110`, и он из сети стенда **не отвечает** (ни :443, ни :80),
хотя интернет в целом открыт (github.com 200, Docker Hub 200, ntfy.sh 200).
Работает другой фронт того же Bot API — `149.154.167.220`, и он отдаёт валидный
сертификат для настоящего имени. Поэтому под ss-notifier получает `hostAliases`
с этим адресом (`overlays/prod/ss-notifier-prod-patch.yaml`): подменяется
разрешение имени, а не адрес API — иначе SNI поехал бы IP-адресом и проверка
сертификата отвалилась бы. **Это хрупко:** запасного рабочего адреса на
19.08.2026 не нашлось (проверены ещё четыре). Если канал встанет — вырастет
`alertmanager_notifications_failed_total`, сработает `SSAlertDeliveryFailing`
(видно на панели «Активные алерты», в чат оно по понятной причине не доедет), и
лечить по возрастанию усилий: найти живой адрес фронта → переключить канал на
ntfy (`SS_NOTIFY_BACKEND=ntfy` + `NTFY_TOPIC` в секрете, отправители об этом не
узнают) → попросить партнёра открыть `149.154.166.110:443`.

**Куда пишет бот.** В групповой чат (`TELEGRAM_CHAT_ID` отрицательный).
Посмотреть, какие чаты боту вообще видны — `make notifier-chats`; переключить —
`make notifier-chat CHAT_ID=…` (правит Secret и перезапускает под: `envFrom`
читается только при старте контейнера). Ловушка обычных групп: при превращении
в супергруппу (темы, история, >200 участников) `chat_id` меняется на `-100…`, и
бот замолкает **молча** — сторож `SSWatchdog` это поймает через 12 часов.

**Уведомления о серии — через проброс на .72.** `notify()` в
`scripts/run-series.sh` шлёт на `127.0.0.1:8790`, а служба теперь в кластере:
адрес держит `ss-forward@ss-notifier` (раздел 9). Без него функция по
построению молчит — то есть про зависшую серию не сообщит никто.

**Почему ss-notifier поднят в кластере, а не взят с лабы.** Маршрута
кластер→лаба нет: WG-туннель поднимает лаба, обратно в `10.8.10.0/24`
партнёрская сеть не маршрутизирует (проверено и из пода, и с узла ss-system).
Обратный SSH-туннель с лабы поставил бы оповещения прода в зависимость от
ноутбука с моргающим Wi-Fi. Цена — токен бота живёт в двух местах; бот и чат
при этом одни и те же.

## 3. Прогноз A5 — проверить ДО серий

Прогноз зарегистрирован 20.07 (`c19a721`); критерии и действия при
опровержении — «A5-прогноз (прод).md».

```bash
make perfcheck-run NODE=<bench-1>   # и так для каждого из трёх bench-узлов
make perfcheck-logs                 # доля окна при одном событии — 1.000
make perfcheck-clean
```
**Проверка:** 1.000 на всех трёх узлах → вписать результат в
«A5-прогноз (прод).md». Меньше 0.99 — СТОП, серии не начинать (см. файл
прогноза: отказ от NUMA-пары или узловые счётчики).

## 4. Калибровки датчиков (опорные значения осей)

```bash
make netcheck-run && make netcheck-logs      # -> NET_REFERENCE_MBPS
# LLC_REFERENCE_MISSES_PER_SEC — это llc_misses_per_sec УЗЛА ПОД ЭТАЛОННЫМ
# ШТОРМОМ (2 пода aggressor --stream 2 на одном bench-узле; см. «Ввод
# прод-стенда (Этап 0)» и Методику), НЕ число из perfcheck. Снять: поды
# шторма -> 45с -> Prometheus ss_node_llc_misses_per_sec{node=<шторм>}.
# ВАЖНО: mem_limit агрессора на проде 4Gi — stream-буферы кратны L3 (60МБ
# на сокет у 8462Y+), со STAGE-лимитом 512Mi поды падают OOMKilled.
make calibration-apply NET_REFERENCE_MBPS=<N> LLC_REFERENCE_MISSES_PER_SEC=<M>
make netcheck-clean
```
**Проверка:** `make calibration-show` — оба значения непустые (пустой Net =
ось выключена; пустой LLC = сырой инвертирующий ratio).
**Выполнено 18.08.2026:** NET_REFERENCE_MBPS=16718 (bond 2×25G, realizable
rx+tx), LLC_REFERENCE_MISSES_PER_SEC=735000000 (шторм на wrk-b7; соседние
узлы при этом ~0 — стенд чист). ~50× против STAGE по LLC — bare-metal.

## 5. In-cluster CH — чистый приёмник прода

```bash
make ch-incluster-deploy CH_KUSTOMIZE=k8s/clickhouse/overlays/prod   # ТОЛЬКО оверлей
# Оверлей теперь дефолт, а страж scripts/ch-placement-guard.sh не даст
# развернуть CH без привязки к ss-system там, где есть измерительные узлы.
make ch-incluster-status                    # под Running на ss-system, schema-Job Complete
make ch-forward   # заодно проверяет NetworkPolicy: port-forward обязан работать &                           # localhost:8124 -> in-cluster CH
```
**Проверка:** под Running на ss-system, schema-Job Complete, таблицы пустые
(`SELECT count()` == 0 в results и baselines).
`base` вместо оверлея — нельзя: CH сядет на измерительный узел, preflight
серий его завалит.

**Решение 18.08.2026 — STAGE-историю сюда НЕ восстанавливать.** Ранняя
редакция ступени (зеркало STAGE-обкатки 06.08) заливала сюда бэкап .72;
на вводе прода так и сделали — и откатили (TRUNCATE): in-cluster CH держит
только прод-серии, кросс-стендовый агрегатор и точка правды — CH на .72
(`ch-analyze`/`axis-costs` ходят именно туда). Restore из архива остаётся
механизмом disaster-recovery для .72; генерируемый restore.sh с 18.08 умеет
таблицы, уже созданные schema-Job'ом (CREATE TABLE IF NOT EXISTS).

## 6. Смоук-серия

```bash
kubectl get nodes                                   # имена bench-узлов
grep -rn FILL harness/config-prod-*.yaml            # заполнить все <FILL>
STAND=prod make series-preflight SERIES=smoke
STAND=prod make series SERIES=smoke PILOT=1
```
**Проверка:** в `harness/prod-smoke.log` фазы BASELINE/PRESSURE c rc=0;
задача считается 3–10 мин (нет — править PRIMARIES в `run-prod-smoke.sh`);
статус-страница показывает узлы и давление; `ss_agent_pmu_multiplex_ratio`
= 1.000 на bench-узлах под нагрузкой (закрытие ступени 3 в бою).

**Выполнено 18.08.2026.** PRESSURE (PILOT): 36 строк, rc=0, без ошибок.
Нюанс, которого нет в тексте выше: `PILOT=1` по устройству гоняет ТОЛЬКО
pressure-пилот (это смоук обвязки) — фазы BASELINE в логе не будет; эталоны
догнаны следом отдельной обвязкой (`harness/.baseline-smoke.sh` — те же
export'ы дозы + `--baseline`, кластер к этому моменту пуст). PMU в бою:
`min_over_time(...[37m])` = 1.000 × wrk-b6/b7/b8 — A5 закрыт полностью.
**Задача на 5M primaries считается ~19 с** (18–22 с по 36 задачам, все
плечи) — вместо целевых 3–10 мин: Sapphire Rapids на 28 полных ядрах.
Дозу смоука задним числом НЕ менять (эталоны и результаты обязаны быть
одной дозы); для РАБОЧИХ серий закладывать **PRIMARIES ≈ 60–75M** (~4–5
мин линейной экстраполяцией) и проверить на первой калибровочной.

## 7. Заливка в оба CH (обкатано на STAGE 06.08)

```bash
make ch-tunnel        # localhost:8123 -> .72 (первичный)
# ch-forward со ступени 5 ещё жив (localhost:8124 -> in-cluster)
make ch-load-all STAND=prod RUN_LABEL=prod-smoke \
    RESULTS_FILE=harness/results/results-prod-smoke.parquet \
    BASELINES_FILE=harness/results/baselines-prod-smoke.parquet
```
**Проверка:** «залито во все приёмники: prod home»; упавший приёмник цель
называет и печатает команду долива. Так — после КАЖДОЙ серии.

## 8. Цены осей (после смоука, до рабочих серий)

Веса в `config-prod-base.yaml` — ЗАГЛУШКА (равные). Снять калибровочную
серию (аналог stage-mixed-calib — конфиг писать по её образцу), затем:

```bash
make axis-costs        # calibrate_axis_costs.py ИЗ ClickHouse (поверх ch-tunnel)
```
Полученные веса вписать в `config-prod-base.yaml` (score_weights) и в
ConfigMap (`make scheduler-apply-config`). До этого все серии — разведочные.

**Прогон 19.08.2026 (prod-mixed-calib, ночь, обе фазы rc=0, 180+48 строк) —
ступень ЗАКРЫТА в два захода, веса ПРИМЕНЕНЫ.**

*Заход 1 (обращение скор-функции — путь STAGE):* все α/β ≈ 0, R²=0.04.
Три причины: (а) high-s кэш-резидентна на 120 МБ LLC (RSS ~190 МБ, miss
ratio 1%, DRAM ~7К соб/с — stream-шторм её не задевает, замедление 0.000);
(б) PSI io в моменты решения пульсирует около нуля, хотя настоящая
токсичность io×io = +6.8% на wrk-b7 есть; (в) high-s-net номинальна.

*Заход 2 (давления за ИНТЕРВАЛ задачи из Prometheus,
`calibrate_axis_costs.py --prometheus-url`, коммит 2cd440b; по дороге
найден и починен сдвиг таймстампов −3ч в CH — be8cf1a, перезаливка
fix-tz-20260819.sh):* модель заиграла — **R²=0.64, клетка io×io
предсказана 0.068/0.068**. Цены: **β_io = 1.31 [1.02; 1.59]** (единственная
большая), α_llc = 0.014 [0.012; 0.016], α_net = 0.009 [0.006; 0.012],
β_llc = β_net = 0 (честные нули текущих жертв, не стенда), γ = 0.003.
Урок для методики: на проде давления в фит брать за интервал задачи из
Prometheus, а не обращением interference_chosen — пульсирующий PSI
на снапшотах решения слеп.

**Веса вписаны 19.08 (санкция пользователя)** в config-prod-base +
ConfigMap: base={llc 0.014, net 0.009}, sensitivity={io 1.31}, numa=0
(цена не мерена — шторма оси не было; вес-заглушка 1.0 был шумом).
Серии с этого момента НЕ разведочные. Рекалибровка v2 (ML-жертва
по membw, stream-профиль + egress-шторм по net, NUMA-шторм
--taskset/--mbind) поднимет нулевые цены — дозы v1 их не раскрывают.
Регрет-метрика серии: SS значимо лучше default (Holm p 0.01–0.03).

## 9. Вернуть постоянные сервисы .72 (отключены 07.08 при заморозке STAGE)

```bash
ssh andrey@192.168.1.72
sudo systemctl enable ss-status               # страницу поднимет первая же серия
# в /etc/systemd/system/ss-forward@.service поправить:
#   Environment=KUBECONFIG=/home/andrey/.kube/configs/prod
sudo systemctl daemon-reload && sudo systemctl enable --now ss-forward@grafana
```
ss-notifier на .72 **погашен 19.08.2026** — служба переехала в прод-кластер
(раздел 2б). Но проброс до неё нужен, иначе уведомления о серии пропадут без
следа: `notify()` в `run-series.sh` шлёт на `127.0.0.1:8790`, и при мёртвом
адресе он молча ничего не делает.
```bash
make monitoring-uptime-unit SERVICES="grafana ss-notifier pushgateway"
curl -sf http://127.0.0.1:8790/healthz && echo " проброс уведомлений жив"
curl -sf http://127.0.0.1:9091/-/healthy && echo " проброс pushgateway жив"
```
Проброс pushgateway нужен под маркеры серий: `run-series.sh` кладёт туда
`ss_series_running`, из которого Grafana рисует границы прогонов на дашбордах
осей, энергии и кампании. Без него маркеров просто не будет — как и с
уведомлениями, функция молчит при мёртвом адресе.
Прод-kubeconfig на .72 уже лежит в `~/.kube/configs/prod` и является
дефолтом — его кладёт сам provision (ступень 1), отдельного шага нет.

## 10. Регулярное

- После каждой серии: ступень 7 (`ch-load-all`) + отчёт `make ch-analyze`.
- После каждой серии (с 18.08, энерговетка): энергия окон в оба CH —
  `./scripts/energy-windows-from-log.sh harness/<стенд>-<серия>.log <стенд> <run_label>`
  (RAPL pkg+dram по epoch-маркерам фаз; нужен форвард Prometheus на :19090).
- Раз в неделю (и перед любыми работами на .72): `make ch-backup` — архив
  верифицируется восстановлением сам; копию — в облако.
- Прогноз A5 со статусом — в диссертацию (пример предрегистрации).

## 11. Опционально: ML-профиль нагрузки (после ступени 8)

Обоснование, пилот STAGE 07.08 (инференс-жертва различает оси: membw −5%
throughput против −2% у дискового плацебо) и скрипт пробы — в
«ML-направление — вопрос №1 и ML-нагрузка (07.08.2026).md».

0. Быстрая репетиция пилота на железе: готовый самодостаточный образ
   `andreyza/mlprobe:dev` (Dockerfile — в доке ML-направления), фазы A/B/C
   руками, полчаса — сразу видно масштаб membw-эффекта на полных ядрах.
1. Собрать образ `workload-ml`: ONNX Runtime + модель (int8 — на Xeon 8462Y+
   работает AMX), скрипт по образцу пилотного `ml_probe.py`; батч-инференс
   фиксированного N — ложится в makespan-модель харнесса без изменения схемы.
2. Профили `ml-inference` (llc-high) + близнец `ml-insensitive` (декларации
   low) — пара по образцу близнецов io/net.
3. Серия по образцу смоука (ступень 6): штормы membw и дисковый (плацебо).
   Ожидание из пилота: membw-шторм бьёт по инференсу кратно сильнее диска.
4. Расширение (не условие): перцентили латентности p50/p95 в результаты —
   отдельная колонка/лог; на первом заходе достаточно makespan.

Открывает: глава «обобщение на класс ML-нагрузок» / статья №2; J/inference
для энергоплана партнёров.
