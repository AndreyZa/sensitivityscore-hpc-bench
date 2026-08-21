#!/bin/bash
# Запуск серии одной командой: preflight -> запуск одной сессией (setsid) ->
# статус-страница -> вотчдог. Пропуск любого из этих шагов при ручном
# запуске приводил к потере прогона — поэтому они объединены.
#
#   make series SERIES=<имя>          запустить (preflight + фон + вотчдог)
#   make series-status SERIES=<имя>   состояние идущей/законченной серии
#   make series-preflight SERIES=<имя>  проверить стенд, ничего не запуская
#   make series-stop SERIES=<имя>     остановить серию и удалить её поды
#
# Конвенция имён (STAND=prod по умолчанию, SERIES=smoke -> «prod-smoke»):
#   конфиг     harness/config-<стенд>-<имя>.yaml
#   скрипт     harness/run-<стенд>-<имя>.sh    (эталоны + серия одной сессией)
#   лог        harness/<стенд>-<имя>.log       (старый ротируется с меткой времени)
#   результаты harness/results/<из секции output конфига> (старые ротируются)
#
# Стенд задаётся STAND=<имя> (stage | prod): STAND=prod make series SERIES=smoke
# возьмёт harness/config-prod-smoke.yaml и положит отчёт в report-prod-smoke.
# KUBECONFIG по умолчанию тоже зависит от стенда — см. ниже.
#
# Preflight проверяет то, что харнесс проверить не может или узнаёт слишком
# поздно: доступность кластера, готовность агентов и планировщика, СОВПАДЕНИЕ
# weights.json в ConfigMap со score_weights конфига (расхождение = регрет
# считается не теми весами, что реально планируют), живой Redis port-forward,
# отсутствие чужих подов в bench-namespace (эталоны требуют пустой кластер)
# и уже идущей серии. FORCE=1 превращает проверки в предупреждения.
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1

# Стенд: определяет префикс всех имён (конфиг, скрипт, лог, каталог отчёта) и
# kubeconfig по умолчанию. По умолчанию — ПРОД (с 19.08.2026): раньше стоял
# stage, и это осталось от времён, когда STAGE был единственным стендом. После
# сноса STAGE такой умолчательный стенд стал ловушкой того же рода, что
# MONITORING_OVERLAY=stage: команда без STAND целилась в несуществующий
# кластер, а имена файлов (config-stage-*, отчёт report-stage-*) при этом
# выглядели правдоподобно.
STAND=${STAND:-prod}
case "$STAND" in
    prod)  DEFAULT_KUBECONFIG=$HOME/.kube/configs/prod ;;
    # STAGE снесён в августе 2026 (кластер Timeweb удалён, kubeconfig убран).
    # Ветка оставлена ради внятного отказа: гонять серию негде, а её данные
    # живут в ClickHouse и читаются анализом по --stand stage.
    stage) echo "STAND=stage: стенд STAGE снесён 08.2026 — гонять серию негде."
           echo "  данные STAGE живы в ClickHouse: make ch-report STAND=stage RUN_LABEL=<метка>"
           exit 2 ;;
    *)     echo "неизвестный STAND='$STAND' (ожидается prod)"; exit 2 ;;
esac
export KUBECONFIG=${KUBECONFIG:-$DEFAULT_KUBECONFIG}

SYS_NS=sensitivityscore-system
BENCH_NS=sensitivityscore-bench
REDIS_PORT=16379
PY=harness/.venv/bin/python

ACTION=${1:-}
SERIES=${2:-}
[ -z "$ACTION" ] || [ -z "$SERIES" ] && {
    echo "использование: $0 start|preflight|page|status|stop <серия>  (напр. placebo, mixed-calib)"; exit 2; }

CONFIG=harness/config-$STAND-$SERIES.yaml
RUNSCRIPT=harness/run-$STAND-$SERIES.sh
LOG=harness/$STAND-$SERIES.log
PIDFILE=harness/.series-$SERIES.pid
WDPIDFILE=harness/.series-$SERIES.watchdog.pid
STALLFLAG=harness/.series-$SERIES.stalled
# Параметры последней поднятой страницы. Файл читает scripts/status-page-boot.sh
# — тот самый, что зовёт systemd-юнит ss-status.service после перезагрузки
# хоста. Без него `docker compose up` поднял бы страницу серии ПО УМОЛЧАНИЮ, то
# есть чужие данные на знакомом порту; молча и убедительно.
STATUS_PAGE_ENV=harness/.status-page.env
# Путь отчёта страница выводит сама из SERIES (statusserver/docker-compose.yaml).

fail() { echo "FAIL: $*"; [ "${FORCE:-0}" = "1" ] && echo "      (FORCE=1 — продолжаю)" || exit 1; }
ok()   { echo "  ok: $*"; }
# Третий класс, которого не было: замечание, не влияющее на ЧИСЛА, но
# влияющее на то, узнаешь ли ты о беде. fail() тут неуместен (данные будут
# годные), молчание — тоже (четыре часа вслепую).
warn() { echo "  ВНИМАНИЕ: $*"; }

pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# Уведомление о том, что случилось с прогоном, пока за ним никто не следил.
#
# Доставку делает ОТДЕЛЬНАЯ служба (репозиторий ss-notifier): она держит токен
# бота у себя одна и ловит хук. Здесь поэтому ровно один curl — ни docker, ни
# установленных инструментов, ни секретов на машине, с которой запускают серию.
# Обнаружение остаётся здесь: что считать бедой, знает вотчдог, а не
# уведомлятель.
#
# С 19.08.2026 служба живёт в ПРОД-КЛАСТЕРЕ (на .72 контейнер погашен), а
# адрес в harness/.notify.env остался тем же — 127.0.0.1:8790 — потому что до
# неё ведёт постоянный проброс `ss-forward@ss-notifier` (systemd на .72,
# scripts/monitoring-forward.sh). Если проброса нет, notify() молча ничего не
# делает: адрес отвечать перестанет, а функция по построению не имеет права
# ронять четырёхчасовой прогон. Проверять до серии: curl -sf
# http://127.0.0.1:8790/healthz — либо просто `make alerts-test`.
#
# Уведомляем ТОЛЬКО о том, что происходит после ухода оператора. Preflight и
# `series-stop` идут при нём, синхронно и с выводом на экран — дублировать их
# в чат значит приучить себя не читать оттуда ничего.
#
# Адрес службы не задан — функция молча ничего не делает, и прогон одинаков с
# уведомлениями и без них.
# Маркер «в этот момент шла такая-то серия» для Grafana. Через месяцы, сравнивая
# ряды, отличить «всплеск непонятно от чего» от «вот эта серия» иначе нечем:
# результаты уезжают в ClickHouse, а трассы осей живут только в Prometheus, и
# границы прогонов в них ничем не отмечены.
#
# Почему метрикой, а не аннотацией через API Grafana: аннотация требует записи,
# то есть учётки и токена на машине, откуда гоняют серию, — а метрика ложится в
# ТУ ЖЕ TSDB, что и данные, и переживает вместе с ними ретеншен и бэкап.
#
# Адрес не задан — функции молча ничего не делают, как и notify(): маркер
# полезен, но четырёхчасовой прогон ронять не имеет права.
PUSHGATEWAY_URL=${PUSHGATEWAY_URL:-http://127.0.0.1:9091}

series_marker() {   # series_marker set|clear
    local base="${PUSHGATEWAY_URL%/}/metrics/job/ss_series/series/${SERIES}/stand/${STAND}"
    case "$1" in
        set)
            # Heartbeat обязателен: pushgateway помнит последнее значение
            # ВЕЧНО, и без признака свежести оборванная серия оставила бы
            # маркер «идёт» навсегда. Вотчдог обновляет его каждый цикл.
            printf 'ss_series_running 1\nss_series_heartbeat_seconds %s\n' "$(date +%s)" \
                | curl -sf --max-time 10 --data-binary @- "$base" >/dev/null 2>&1 || true
            ;;
        clear)
            curl -sf --max-time 10 -X DELETE "$base" >/dev/null 2>&1 || true
            ;;
    esac
}

# Что из класса «годность чисел» срабатывало за прогон.
#
# Эти правила НЕ идут в чат по построению (маршрут validity в Alertmanager):
# они срабатывают именно от нагрузки эксперимента — насыщенная LLC-ось это то,
# ради чего запускают агрессора, — и ночью слали бы в чат собственную работу.
# Но и терять их нельзя: это состояния, при которых стенд продолжает писать
# цифры, а нести их в диссертацию уже нельзя. Поэтому спрашиваем итог ОДИН РАЗ,
# в конце, и кладём в завершающее уведомление: узнаёшь утром, вместе с
# результатом.
#
# Prometheus опрашивается через kubectl (он тут и так повсюду), а не через
# проброс — постоянный туннель ради одного запроса не нужен.
validity_report() {   # validity_report <секунд от старта>
    local window=${1:-14400}
    local q="count by (alertname) (max_over_time(ALERTS{class=\"validity\",alertstate=\"firing\"}[${window}s]))"
    kubectl -n sensitivityscore-monitoring exec deploy/prometheus -c prometheus -- \
        wget -qO- --post-data="query=$q" http://localhost:9090/api/v1/query 2>/dev/null \
        | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)["data"]["result"]
except Exception:
    sys.exit(0)                       # мониторинг недоступен — молчим, как notify()
names = sorted(r["metric"].get("alertname", "?") for r in rows)
if names:
    print("ГОДНОСТЬ ЧИСЕЛ под вопросом, срабатывало за прогон: " + ", ".join(names))
    print("(в чат эти правила не идут: они ожидаемы под нагрузкой. Смотреть панель")
    print(" «Активные алерты» и раздел годности сбора на дашборде осей.)")
' 2>/dev/null || true
}

NOTIFY_ENV=harness/.notify.env
# Файл хоста (в git его нет): вотчдог стартует отдельным процессом через
# setsid/nohup, и полагаться на то, что переменные доехали из интерактивной
# оболочки, не стоит.
# shellcheck disable=SC1090
[ -f "$NOTIFY_ENV" ] && . "$NOTIFY_ENV"

# `|| true` обязателен: недоступная служба не имеет права ронять
# четырёхчасовой прогон. Время ограничено сверху (--max-time на попытку,
# всего две) — вотчдог зовёт эту функцию из своего цикла и не должен вставать
# на сетевом таймауте.
notify() {   # notify <уровень> <заголовок> <текст> [файл]
    local url=${SS_NOTIFY_URL:-}
    [ -n "$url" ] || return 0
    local args=(--silent --show-error --connect-timeout 5 --max-time 20
                --retry 1 --retry-delay 2 -o /dev/null
                -X POST "${url%/}/notify"
                --data-urlencode "level=$1"
                --data-urlencode "title=$2"
                --data-urlencode "text=$3")
    [ -n "${SS_NOTIFY_TOKEN:-}" ] && args+=(-H "X-SS-Token: $SS_NOTIFY_TOKEN")
    if [ -n "${4:-}" ] && [ -f "$4" ]; then
        # base64 без переносов: GNU base64 заворачивает строки на 76 символах,
        # macOS — нет, и без tr тело формы различалось бы между хостами.
        args+=(--data-urlencode "file_name=$(basename "$4")"
               --data-urlencode "file_b64=$(base64 < "$4" | tr -d '\n')")
    fi
    curl "${args[@]}" >/dev/null 2>&1 || true
}

# Redis виден харнессу только через port-forward, и это единственная точка,
# где берётся снимок давления узлов для placement_regret. Если форвард умрёт
# посреди прогона, харнесс не падает (так задумано: сломанный метрик-пайплайн
# не должен ронять сабмит) — он молча пишет regret=NaN. 20.07 хост ушёл в сон
# на 6.4 ч, форвард не пережил заморозку, и МЕТРИКА РЕШЕНИЯ пропала у 120
# строк из 180 — при этом ни лог, ни страница об этом не сказали ни слова.
# Поэтому: проверка живости вынесена в функцию, а вотчдог поднимает форвард
# заново и пишет в лог, что случилось.
redis_alive() {
    "$PY" -c "import redis; redis.Redis(port=$REDIS_PORT, socket_connect_timeout=2).ping()" 2>/dev/null
}

redis_pf_start() {
    new_session nohup kubectl -n $SYS_NS port-forward svc/redis $REDIS_PORT:6379 \
        > harness/.redis-pf.log 2>&1 &
    echo $! > harness/.redis-pf.pid
    sleep 3
}

# Своя сессия для фоновых процессов. setsid — из util-linux, на macOS его нет:
# без подмены `new_session nohup ...` падал с «command not found», port-forward к
# Redis не поднимался и preflight валился на ровном месте, а серия не
# запускалась вовсе. Perl есть в базовой системе и на macOS, и на Linux.
# Новая сессия нужна не для красоты: серия останавливается через
# `kill -TERM -- -<pid>`, то есть по группе процессов, а без своей сессии в
# группу попал бы и вызывающий shell.
if command -v setsid >/dev/null 2>&1; then
    new_session() { setsid "$@"; }
else
    new_session() { perl -e 'use POSIX qw(setsid); setsid(); exec @ARGV or die $!;' -- "$@"; }
fi

# Пути результатов — из секции output конфига (единственный источник правды).
results_paths() {
    "$PY" - "$CONFIG" <<'EOF'
import sys
sys.path.insert(0, "harness")   # конфиг серии — слой поверх родителя (extends)
from config_loader import load_config
cfg = load_config(sys.argv[1])
out = cfg["output"]
print("harness/" + out["results_dir"] + "/" + out["results_file"])
print("harness/" + out["results_dir"] + "/" + out.get("baselines_file", "baselines.parquet"))
EOF
}

# Запомнить, с какими параметрами поднята страница. Пишем ФАКТ, а не намерение:
# функцию зовут только там, где страница уже отвечает на /healthz и её аргументы
# сверены с нужной серией.
status_page_env_save() {
    cat > "$STATUS_PAGE_ENV" <<EOF
# Сгенерировано scripts/run-series.sh, $(date '+%F %T'). Руками не править:
# файл перезаписывается при каждом подъёме страницы.
#
# Отсюда scripts/status-page-boot.sh (его зовёт ss-status.service) берёт
# параметры compose после перезагрузки хоста, чтобы вернуть ТУ ЖЕ серию.
SERIES='$1'
STAND='$2'
STATUS_PORT='$3'
RESULTS='$4'
BASELINES='$5'
KUBECONFIG='$6'
STAND_FILES='$7'
EOF
}

# ---------------------------------------------------------------------------
# Подъём статус-страницы. Отдельная функция и отдельный экшен `page`, потому
# что стартовать прогон можно не только через `make series`: ручным
# harness/run-<стенд>-<имя>.sh, целями make pilot / run-all / run-config-a. Пока
# подъём был зашит внутрь start(), все эти пути оставляли оператора либо без
# страницы, либо — хуже — со страницей ПРЕДЫДУЩЕЙ серии на том же порту.
#
# Страница — контейнер, а не хостовый питон: на хосте она умирала с SIGSEGV.
# Причина, вопреки прежнему объяснению, не в версии python, а в дефолтном
# аллокаторе pyarrow (см. ARROW_DEFAULT_MEMORY_POOL в statusserver/Dockerfile);
# в контейнере он выключен.
# Бэкенд статус-страницы: prod -> k0s лабы (миграция 19.08.2026 по образцу
# переезда ClickHouse: цельная инсталляция, страница переживает ребут силами
# кластера, докер-демон не нужен); stage и прочее -> docker compose, как
# раньше. Переопределение: STATUS_BACKEND=compose|k8s.
status_page_backend() {
    if [ -n "${STATUS_BACKEND:-}" ]; then
        printf %s "$STATUS_BACKEND"
    elif [ "$STAND" = prod ]; then
        printf %s k8s
    else
        printf %s compose
    fi
}

# Страница в k0s лабы: рендер k8s/statusserver/statusserver-lab.yaml
# (sed-подстановка путей серии в args — тот же приём, что подстановка образа
# в scheduler-deploy) -> kubectl apply. Прод-серии гоняются с .72, где этот
# k0s и живёт; на чужой машине без local72-kubeconfig — WARN и выход
# (страница никогда не валит серию).
status_page_up_k8s() {
    local port=${STATUS_PORT:-8787}
    # Адрес для человека и для healthz: страница живёт на ЛАБЕ и с 19.08
    # открыта в домашнюю сеть (hostPort на всех интерфейсах) — по IP лабы
    # она доступна и с самой лабы, и с рабочего ПК, localhost — только с лабы.
    local host=${PAGE_HOST:-192.168.1.72}
    local cseries=$SERIES
    case "$SERIES" in pressure|baseline|stage) cseries="" ;; esac
    local logpat="$STAND-${cseries:-pressure}\.log"

    local kc=${PAGE_KUBECONFIG:-$HOME/.kube/configs/local72.yaml}
    if [ ! -f "$kc" ]; then
        echo "WARN: kubeconfig лабного k0s '$kc' не найден — статус-страница пропущена"
        echo "      (k8s-бэкенд живёт на .72; на другой машине — STATUS_BACKEND=compose)"
        return 0
    fi

    local results baselines
    { read -r results; read -r baselines; } < <(results_paths) 2>/dev/null || true
    local kcfg=${KUBECONFIG%%:*}
    [ -f "$kcfg" ] || kcfg=/dev/null
    local stand_up
    stand_up=$(printf %s "$STAND" | tr "[:lower:]" "[:upper:]")

    # Идемпотентность — как у compose-пути: нужная страница уже отвечает,
    # значит не трогаем (передеплой на ходу перезапустил бы её посреди серии).
    local curargs
    curargs=$(kubectl --kubeconfig "$kc" -n sensitivityscore-system get deploy ss-status \
        -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null || true)
    if grep -q -- "$logpat" <<<"$curargs" \
       && curl -sf -o /dev/null "http://$host:$port/healthz" 2>/dev/null; then
        status_page_env_save "$cseries" "$stand_up" "$port" "$results" "$baselines" "$kcfg" "$STAND"
        ok "статус-страница уже поднята (k0s лабы): http://$host:$port"
        return 0
    fi

    # Докерная страница (старый бэкенд) держит тот же порт 8787 — hostPort
    # пода с ней не уживётся: сначала уступает порт.
    if command -v docker >/dev/null 2>&1 && docker inspect ss-status >/dev/null 2>&1; then
        echo "  докерная статус-страница уступает порт k0s-странице (compose down)"
        docker compose -f statusserver/docker-compose.yaml down >/dev/null 2>&1 \
            || docker rm -f ss-status >/dev/null 2>&1 || true
    fi

    local log_p="/repo/harness/$STAND-${cseries:-pressure}.log"
    local config_p="/repo/harness/config-$STAND${cseries:+-$cseries}.yaml"
    local results_p="/repo/${results:-harness/results/results-$STAND${cseries:+-$cseries}.parquet}"
    local baselines_p="/repo/${baselines:-harness/results/baselines-$STAND${cseries:+-$cseries}.parquet}"
    local report_p="/repo/analysis/report-$STAND${cseries:+-$cseries}"
    local rendered=harness/.statuspage-$SERIES.yaml
    sed -e "s|__LOG__|$log_p|" -e "s|__CONFIG__|$config_p|" \
        -e "s|__RESULTS__|$results_p|" -e "s|__BASELINES__|$baselines_p|" \
        -e "s|__REPORT__|$report_p|" -e "s|__STAND__|$stand_up|" \
        k8s/statusserver/statusserver-lab.yaml > "$rendered"
    if ! kubectl --kubeconfig "$kc" apply -f "$rendered" > "harness/.statuspage-$SERIES.log" 2>&1; then
        echo "WARN: статус-страница не применилась в k0s лабы (серию это не трогает)"
        tail -5 "harness/.statuspage-$SERIES.log" | sed 's/^/      /'
        return 0
    fi
    # Первый запуск тянет образ по Wi-Fi лабы — ждём щедро, но серию не валим.
    kubectl --kubeconfig "$kc" -n sensitivityscore-system rollout status deploy/ss-status \
        --timeout=300s >/dev/null 2>&1 || true
    for _ in $(seq 60); do
        curl -sf -o /dev/null "http://$host:$port/healthz" 2>/dev/null && break
        sleep 1
    done
    if ! curl -sf -o /dev/null "http://$host:$port/healthz" 2>/dev/null; then
        echo "WARN: страница в k0s не отвечает (серию это не трогает); смотреть:"
        echo "      kubectl --kubeconfig $kc -n sensitivityscore-system describe deploy ss-status"
        return 0
    fi
    status_page_env_save "$cseries" "$stand_up" "$port" "$results" "$baselines" "$kcfg" "$STAND"
    ok "статус-страница (k0s лабы): http://$host:$port"
}

status_page_up() {
    if [ "$(status_page_backend)" = k8s ]; then
        status_page_up_k8s
        return $?
    fi
    local port=${STATUS_PORT:-8787}
    local buildlog=harness/.statuspage-$SERIES.log

    # Соглашение об именах здесь не сплошное: исторические pressure/baseline
    # ходят не в config-<стенд>-pressure.yaml (такого файла нет), а в
    # config-<стенд>.yaml с логом <стенд>-pressure.log. Для compose это «серия по
    # умолчанию» — пустой SERIES. Остальные серии называются единообразно.
    local cseries=$SERIES
    case "$SERIES" in pressure|baseline|stage) cseries="" ;; esac
    # Лог, который ДОЛЖЕН быть у нужной нам страницы, — по нему сверяем,
    # что на порту отвечает не контейнер предыдущей серии.
    local logpat="$STAND-${cseries:-pressure}\.log"

    command -v docker >/dev/null 2>&1 || {
        echo "WARN: docker не найден — статус-страница пропущена (серию это не трогает)"
        return 0; }

    # Плагин compose проверяем ОТДЕЛЬНО от docker: в Ubuntu он приезжает
    # отдельным пакетом, и с системным docker'ом его может не быть. Без
    # плагина `docker compose -f …` вырождается в `docker -f …`, а тот отвечает
    # «unknown shorthand flag: 'f' in -f» — по такой ошибке причина не
    # угадывается (наступили на это при запуске серии с JumpHost 20.07).
    docker compose version >/dev/null 2>&1 || {
        echo "WARN: docker есть, а плагина compose нет — статус-страница пропущена"
        echo "      (серию это не трогает); поставить: sudo apt-get install -y docker-compose-v2"
        return 0; }

    # Пути к parquet берём из конфига, а не из соглашения об именах: секция
    # output — единственный источник правды, и compose не должен её дублировать.
    local results baselines
    { read -r results; read -r baselines; } < <(results_paths) 2>/dev/null || true

    # kubeconfig ОБЯЗАН существовать как файл. Если его нет, docker молча
    # создаёт на его месте ПУСТУЮ ДИРЕКТОРИЮ — и это уже случалось: под
    # $HOME/.kube/configs/timeweb-stage появился каталог, после чего хостовый
    # kubectl стал падать с «is a directory» на любой команде. Подставляем
    # /dev/null (файл существует всегда) и говорим вслух, что кластера не
    # будет: страница без секции кластера лучше, чем сломанный kubectl.
    local kcfg=${KUBECONFIG%%:*}
    if [ ! -f "$kcfg" ]; then
        echo "WARN: kubeconfig '$kcfg' не найден — секции кластера на странице не будет"
        echo "      (укажите KUBECONFIG=<файл>; каталог вместо файла ломает kubectl и на хосте)"
        kcfg=/dev/null
    fi
    # Подпись стенда в шапке страницы — заглавными (STAGE/PROD): STAND здесь
    # уже всегда задан, а страница показывает его как есть.
    local stand_up
    stand_up=$(printf %s "$STAND" | tr "[:lower:]" "[:upper:]")
    # Отдельная копия для префикса compose-команды: там STAND="$stand_up"
    # выполняется раньше, и STAND_FILES="$STAND" увидел бы уже ЗАГЛАВНОЕ
    # значение (присваивания в префиксе идут слева направо).
    local stand_files=$STAND

    # Идемпотентность: функцию зовут и start(), и сам harness/run-stage-<имя>.sh
    # (чтобы страница поднималась и при ручном запуске скрипта серии). Если
    # нужная страница уже отвечает — выходим сразу, не трогая контейнер:
    # пересборка на ходу перезапустила бы страницу посреди прогона. Параметры
    # всё равно записываем: страницу могло поднять что угодно (прошлый прогон,
    # сам юнит), а файл для восстановления после перезагрузки нужен всегда.
    if docker inspect ss-status --format '{{.State.Status}}' 2>/dev/null | grep -q running \
       && docker inspect ss-status --format '{{json .Args}}' 2>/dev/null \
          | grep -q -- "$logpat" \
       && curl -sf -o /dev/null "http://localhost:$port/healthz" 2>/dev/null; then
        status_page_env_save "$cseries" "$stand_up" "$port" "$results" "$baselines" "$kcfg" "$STAND"
        ok "статус-страница уже поднята: http://localhost:$port"
        return 0
    fi

    # Вывод сборки НЕ в /dev/null: при провале build compose выходит, не
    # тронув контейнеры, и на порту продолжает жить страница прошлой серии —
    # WARN и работающая страница одновременно читаются как «ложная тревога».
    if ! SERIES="$cseries" STAND="$stand_up" STAND_FILES="$stand_files" STATUS_PORT="$port" \
         RESULTS="$results" BASELINES="$baselines" KUBECONFIG="$kcfg" \
         docker compose -f statusserver/docker-compose.yaml up -d --build \
         > "$buildlog" 2>&1; then
        echo "WARN: статус-страница не поднялась (серию это не трогает)"
        echo "      причина — хвост $buildlog:"
        tail -20 "$buildlog" | sed 's/^/      /'
        return 0
    fi

    # `up -d` возвращает 0, как только контейнер СОЗДАН: python ещё
    # импортирует pandas/pyarrow и сокет не забинден. Без ожидания «ok» врал
    # на холодной машине, а мгновенно упавший контейнер вообще не отличался
    # от здорового.
    for _ in $(seq 30); do
        curl -sf -o /dev/null "http://localhost:$port/healthz" 2>/dev/null && break
        sleep 1
    done
    if ! curl -sf -o /dev/null "http://localhost:$port/healthz" 2>/dev/null; then
        echo "WARN: страница не отвечает за 30 с (серию это не трогает)"
        docker compose -f statusserver/docker-compose.yaml ps 2>&1 | sed 's/^/      /'
        docker compose -f statusserver/docker-compose.yaml logs --tail=20 2>&1 | sed 's/^/      /'
        return 0
    fi

    # Ответ на порту ещё не значит «наша серия»: там мог остаться контейнер
    # прошлой (restart: unless-stopped переживает и остановку серии, и
    # перезагрузку хоста). Сверяем по фактическим аргументам контейнера.
    if ! docker inspect ss-status --format '{{json .Args}}' 2>/dev/null \
         | grep -q -- "$logpat"; then
        echo "WARN: на порту $port отвечает страница ДРУГОЙ серии — данные не те!"
        echo "      docker compose -f statusserver/docker-compose.yaml down && повторить"
        return 0
    fi
    status_page_env_save "$cseries" "$stand_up" "$port" "$results" "$baselines" "$kcfg" "$STAND"
    ok "статус-страница: http://localhost:$port"
}

preflight() {
    echo "=== preflight: серия $SERIES ==="
    [ -f "$CONFIG" ]    || { echo "FAIL: нет $CONFIG"; exit 1; }
    [ -f "$RUNSCRIPT" ] || { echo "FAIL: нет $RUNSCRIPT"; exit 1; }
    [ -x "$PY" ]        || { echo "FAIL: нет venv харнесса ($PY) — make venv-harness"; exit 1; }
    ok "конфиг и скрипт на месте"

    for f in harness/.series-*.pid; do
        [ -f "$f" ] && pid_alive "$f" && fail "уже идёт серия (pid $(cat "$f"), $f)"
    done
    pgrep -f "run_experiment.py --config" >/dev/null && \
        fail "уже работает run_experiment.py, запущенный вручную (pgrep -f run_experiment.py)"
    ok "других серий нет"

    kubectl get nodes >/dev/null 2>&1 || fail "кластер недоступен (KUBECONFIG=$KUBECONFIG)"
    ok "кластер доступен"

    local ds_state
    ds_state=$(kubectl -n $SYS_NS get ds sensitivityscore-metrics-agent \
        -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}' 2>/dev/null)
    [ -n "$ds_state" ] && [ "${ds_state%/*}" = "${ds_state#*/}" ] \
        || fail "metrics-agent не весь Ready ($ds_state)"
    ok "metrics-agent $ds_state Ready"

    kubectl -n $SYS_NS get deploy sensitivityscore-scheduler \
        -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]' \
        || fail "планировщик не Ready"
    # Учётка реестра: без неё Docker Hub режет анонимные вытягивания по
    # внешнему адресу (100/час на весь стенд), и длинная серия сыплется в
    # ErrImagePull на середине. Предупреждение, а не отказ: на стендах, где
    # образы уже локально или реестр свой, учётка не нужна.
    if [ -z "$(kubectl -n $BENCH_NS get sa default -o jsonpath='{.imagePullSecrets}' 2>/dev/null)" ]; then
        echo "  ВНИМАНИЕ: в $BENCH_NS нет imagePullSecrets — вытягивание образов анонимное"
        echo "            (лимит Docker Hub 100/час на внешний адрес; make registry-secret)"
    else
        ok "учётка реестра прописана в $BENCH_NS"
    fi

    kubectl -n $SYS_NS logs deploy/sensitivityscore-scheduler --tail=-1 2>/dev/null \
        | grep -q "sensitivity weights loaded" \
        || fail "в логе планировщика нет 'sensitivity weights loaded' — образ без parseWeights?"
    ok "планировщик Ready, веса загружены"

    # weights.json (ConfigMap) == score_weights (конфиг серии), после
    # нормализации обоих форматов через split_weights (зеркало parseWeights).
    # Изнутри harness/ venv зовётся без ../: путь с «..» до .venv даёт
    # RuntimeWarning про sys.prefix на python 3.12.
    (cd harness && .venv/bin/python - "../$CONFIG" <<'EOF'
import json, subprocess, sys
from config_loader import load_config
from submit.node_pressure import split_weights
cfg = load_config(sys.argv[1])
sw = cfg.get("score_weights")
if sw is None:
    sys.exit(0)  # серия на дефолтных весах — сверять нечего
cm = json.loads(subprocess.check_output(
    ["kubectl", "-n", "sensitivityscore-system", "get", "cm", "sensitivity-config",
     "-o", r"jsonpath={.data.weights\.json}"], text=True))
want, got = split_weights(sw), split_weights(cm)
for part, w, g in zip(("base", "sensitivity"), want, got):
    for axis in set(w) | set(g):
        if abs(float(w.get(axis, 0)) - float(g.get(axis, 0))) > 1e-9:
            sys.exit(f"{part}.{axis}: конфиг={w.get(axis, 0)} ConfigMap={g.get(axis, 0)}")
EOF
    ) || fail "weights.json в ConfigMap НЕ совпадает со score_weights конфига (регрет считался бы не теми весами) — kubectl patch cm sensitivity-config"
    ok "weights.json == score_weights конфига"

    # Конфиг планировщика в кластере == конфиг в репозитории, И под поднят
    # ПОСЛЕ его применения. Две разные ошибки, обе бесшумные: «поправил
    # файл, забыл применить» и «применил, забыл перезапустить»
    # (KubeSchedulerConfiguration читается один раз при старте процесса,
    # обновление тома ConfigMap на живой процесс не действует). Цена ошибки
    # — вся серия: плечи меряются настроенными не так, как написано в
    # репозитории и в статье. Поймано 21.08.2026 на смене целевой
    # утилизации плеча упаковки с 40 на 75 %.
    (python3 - <<'EOF'
import json, subprocess, sys
from datetime import datetime

def kget(*a):
    return subprocess.check_output(
        ["kubectl", "-n", "sensitivityscore-system", *a], text=True)

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

want = open("k8s/scheduler-config/scheduler-config.yaml", encoding="utf-8").read()
cm = json.loads(kget("get", "cm", "scheduler-config", "-o", "json"))
if cm["data"]["scheduler-config.yaml"] != want:
    sys.exit("ConfigMap расходится с k8s/scheduler-config/scheduler-config.yaml "
             "— make scheduler-apply-config")

stamps = [f["time"] for f in cm["metadata"].get("managedFields", []) if f.get("time")]
if not stamps:
    sys.exit(0)          # нечем сравнивать — молчим, а не выдумываем вердикт
applied = max(ts(s) for s in stamps)

pods = json.loads(kget("get", "pods", "-l", "component=scheduler", "-o", "json"))
starts = [p["status"]["startTime"] for p in pods["items"] if p["status"].get("startTime")]
if not starts:
    sys.exit("не вижу подов планировщика (component=scheduler)")
started = min(ts(s) for s in starts)

if started < applied:
    sys.exit(f"под поднят {started:%d.%m %H:%M}, ConfigMap применён "
             f"{applied:%d.%m %H:%M} — процесс работает со СТАРЫМ конфигом; "
             f"kubectl -n sensitivityscore-system rollout restart "
             f"deployment/sensitivityscore-scheduler")
EOF
    ) || fail "планировщик настроен не так, как задаёт репозиторий"
    ok "scheduler-config == репозиторий, под поднят после применения"

    if ! redis_alive; then
        echo "  ..: поднимаю port-forward redis :$REDIS_PORT"
        redis_pf_start
        redis_alive || fail "redis port-forward не поднялся (harness/.redis-pf.log)"
    fi
    local nkeys
    nkeys=$("$PY" -c "
import redis
r = redis.Redis(port=$REDIS_PORT, decode_responses=True)
print(len(list(r.scan_iter(match='node:metrics:*'))))" 2>/dev/null)
    [ "${nkeys:-0}" -ge 2 ] || fail "в Redis меньше 2 ключей node:metrics:* — агент не пишет?"
    ok "redis :$REDIS_PORT жив, node:metrics ключей: $nkeys"

    # Контракт имён полей: сначала по исходникам (расхождение трёх копий),
    # затем по ЖИВЫМ данным (развёрнутый образ агента старше исходников —
    # такой version skew уже случался, см. node_pressure.py). Оба отказа
    # молчаливые: читатели подставляют 0.0, планировщик раздаёт одинаковый
    # score, плечо A-sensitivityscore вырождается в default, а серия честно
    # отрабатывает часы и выдаёт «различий нет».
    "$PY" scripts/check-redis-contract.py >/dev/null 2>&1 \
        || fail "контракт Redis-полей нарушен — python3 scripts/check-redis-contract.py"
    (cd harness && .venv/bin/python - "../contract/redis-fields.yaml" <<EOF
import sys, yaml, redis
spec = yaml.safe_load(open(sys.argv[1]))
want = set(spec["node_metrics"]["sources"]["scheduler_reader"]["fields"])
r = redis.Redis(port=$REDIS_PORT, decode_responses=True)
bad = []
for key in r.scan_iter(match="node:metrics:*"):
    missing = want - set(r.hgetall(key))
    if missing:
        bad.append(f"{key}: нет {sorted(missing)}")
if bad:
    print("; ".join(bad), file=sys.stderr)
    sys.exit(1)
EOF
    ) || fail "живой агент не пишет поля, которые читает планировщик (образ агента старше исходников?)"
    ok "контракт Redis-полей цел (исходники + живые данные)"

    local leftovers
    # ss-sink — приёмник стрима high-s-net (k8s/net-sink), живёт в bench-ns
    # ПО КОНСТРУКЦИИ (NET_SINK_HOST=ss-sink резолвится в namespace джоба) и
    # чужим подом не считается; на измерительном узле его ловит СЛЕДУЮЩАЯ
    # проверка (на проде sink пиннится к ss-system — sink-prod.yaml).
    leftovers=$(kubectl -n $BENCH_NS get pods --no-headers 2>/dev/null | awk '$1 != "ss-sink"' | wc -l)
    [ "$leftovers" -eq 0 ] || fail "$leftovers чужих подов в $BENCH_NS (эталонам нужен пустой кластер) — make harness-clean-jobs"
    ok "bench-namespace пуст (ss-sink — инфраструктура стрима, не в счёт)"

    # Ни один служебный под не должен стоять на измерительном узле. Проверка
    # не дублирует предыдущую: та требует ПУСТОЙ bench-namespace, а эта ловит
    # инфраструктуру в ЛЮБОМ неймспейсе (мониторинг, статус-страница, харнесс-
    # Job'ы, reader) — то есть ровно тот случай, когда посторонний процесс
    # шумит на LLC и памяти узла, чувствительность которого серия измеряет.
    # Смещение систематическое и в логах не видно, поэтому ловим до старта.
    # Исключения — то, чему на bench быть ПОЛОЖЕНО:
    #   ss-aggressor      генераторы фоновой нагрузки, они и есть интерференция
    #   geant4-*, bench-* жертвы (собственно измеряемые задачи)
    #   ss-sink           приёмник стрима, пиннится к bench-узлу манифестом
    #                     k8s/net-sink/sink-stage.yaml (серия net-diff)
    #   metrics-agent     DaemonSet, сам измерительный инструмент: он ОБЯЗАН
    #                     быть на каждом bench-узле, иначе оси не считаются
    #   kube-system       базовая обвязка k0s (calico, coredns, kube-proxy) —
    #                     не наша, снять её нельзя, и она одинакова на всех
    #                     узлах, то есть в разность плеч не входит
    local bench_nodes intruders
    bench_nodes=$(kubectl get nodes -l node-role.kubernetes.io/bench \
        -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
    if [ -n "$bench_nodes" ]; then
        intruders=$(kubectl get pods -A -o \
            custom-columns=NS:.metadata.namespace,N:.metadata.name,NODE:.spec.nodeName \
            --no-headers 2>/dev/null \
            | awk -v nodes="$bench_nodes" '
                BEGIN { split(nodes, a, " "); for (i in a) bench[a[i]] = 1 }
                $3 in bench &&
                $2 !~ /^(ss-aggressor|ss-sink|geant4|bench-)/ &&
                $2 !~ /metrics-agent/ &&
                $1 !~ /^(kube-system|kube-node-lease|kube-public)$/ { print "        " $1 "/" $2 " -> " $3 }')
        if [ -n "$intruders" ]; then
            echo "$intruders"
            fail "служебные поды на измерительных узлах (замеры будут смещены)"
        fi
        ok "на измерительных узлах нет посторонних подов"
    fi
    # Канал уведомлений. Проверка появилась 19.08.2026, когда ss-notifier
    # переехал в кластер: адрес в harness/.notify.env остался прежним
    # (127.0.0.1:8790), но ведёт теперь через проброс ss-forward@ss-notifier,
    # и без него notify() молчит ПО ПОСТРОЕНИЮ — он не имеет права ронять
    # четырёхчасовой прогон. То есть отказ канала выглядит точно так же, как
    # спокойная серия, и это ровно та беда, ради которой служба написана.
    #
    # Намеренно warn, а не fail: мёртвый канал не портит ни одной цифры, он
    # лишает только осведомлённости. Все остальные проверки preflight — про
    # то, что числа будут неверными, и смешивать эти два класса нельзя.
    if [ -z "${SS_NOTIFY_URL:-}" ]; then
        warn "SS_NOTIFY_URL не задан ($NOTIFY_ENV) — уведомлений о серии не будет вовсе"
    elif curl -sf -o /dev/null --max-time 5 "${SS_NOTIFY_URL%/}/healthz"; then
        ok "канал уведомлений отвечает ($SS_NOTIFY_URL)"
    else
        warn "канал уведомлений МОЛЧИТ ($SS_NOTIFY_URL) — про зависшую серию никто не сообщит"
        warn "  проверить проброс: systemctl status ss-forward@ss-notifier"
        warn "  поднять:           make monitoring-uptime-unit SERVICES=ss-notifier"
    fi

    echo "=== preflight пройден ==="
}

rotate() {
    local f=$1 stamp
    [ -f "$f" ] || return 0
    stamp=$(date -r "$f" +%Y%m%d-%H%M%S)
    mv "$f" "$f.$stamp"
    echo "  ..: $f -> $f.$stamp"
}

# Размер лога через wc -c, а не stat: у stat ключ размера различается между
# GNU (-c %s) и BSD/macOS (-f %z). Прежний `stat -c %s ... || echo 0` на macOS
# молча возвращал 0 ВСЕГДА, поэтому размер «не менялся» и вотчдог объявлял
# зависшей любую здоровую серию. wc -c есть в POSIX и ведёт себя одинаково.
log_size() {
    wc -c < "$LOG" 2>/dev/null | tr -d ' ' || echo 0
}

watchdog() {
    # Прогресс = рост лога. Порог 20 мин > job_timeout (15 мин): даже
    # намертво зависшая жертва даёт строку об ошибке раньше срабатывания.
    # Свой алерт из прогресса исключается (размер перечитывается после
    # записи), флаг гасит повтор — одна запись на эпизод зависания.
    local main_pid=$1 last_size last_change now size
    last_size=$(log_size)
    last_change=$(date +%s)
    while kill -0 "$main_pid" 2>/dev/null; do
        sleep 300
        size=$(log_size)
        now=$(date +%s)
        # Признак жизни маркера серии: по нему Grafana отличает идущую серию
        # от оборванной, чей маркер остался лежать в pushgateway.
        series_marker set
        # Форвард к Redis: без него regret=NaN на всех последующих задачах,
        # и это не видно ни в логе, ни на странице (см. redis_alive).
        if ! redis_alive; then
            echo "WATCHDOG ERROR $(date '+%F %T'): port-forward к Redis мёртв — placement_regret с этого момента NaN; поднимаю заново" >> "$LOG"
            redis_pf_start
            # Уведомление — одно на эпизод, обеими ветками: «падал и поднят»
            # тоже новость, потому что задачи, попавшие в разрыв, ушли в
            # результаты с regret=NaN, и об этом надо знать при разборе.
            if redis_alive; then
                echo "WATCHDOG $(date '+%F %T'): port-forward к Redis восстановлен" >> "$LOG"
                notify warn "$STAND-$SERIES: port-forward к Redis падал" \
                    "поднят заново; у задач, попавших в разрыв, placement_regret = NaN"
            else
                echo "WATCHDOG ERROR $(date '+%F %T'): поднять port-forward не удалось (harness/.redis-pf.log)" >> "$LOG"
                notify error "$STAND-$SERIES: Redis недоступен" \
                    "port-forward умер и не поднялся — placement_regret с этого момента NaN.
причина: harness/.redis-pf.log"
            fi
        fi
        if [ "$size" != "$last_size" ]; then
            last_size=$size; last_change=$now; rm -f "$STALLFLAG"
        elif [ $((now - last_change)) -ge 1200 ] && [ ! -e "$STALLFLAG" ]; then
            echo "WATCHDOG ERROR $(date '+%F %T'): лог не растёт $(((now - last_change) / 60)) мин — серия зависла? kubectl get pods -n $BENCH_NS" >> "$LOG"
            notify error "$STAND-$SERIES: тишина в логе" \
                "лог не растёт $(((now - last_change) / 60)) мин — похоже, серия зависла.
проверить: kubectl get pods -n $BENCH_NS"
            touch "$STALLFLAG"
            last_size=$(log_size)
        fi
    done
    if grep -q "PRESSURE DONE" "$LOG" 2>/dev/null; then
        echo "WATCHDOG $(date '+%F %T'): серия $SERIES завершена (PRESSURE DONE)." >> "$LOG"
        # Два уведомления, а не одно: «серия кончилась» — сигнал к тому, что
        # стенд свободен под следующую, и ждать ради него сборки отчёта
        # (минуты) незачем.
        # Окно прогона считаем от файла pid: он создаётся в start() и живёт
        # ровно столько, сколько идёт серия.
        local ran_for=14400
        [ -f "$PIDFILE" ] && ran_for=$(( $(date +%s) - $(date -r "$PIDFILE" +%s) ))
        local validity; validity=$(validity_report "$ran_for")
        notify "done" "$STAND-$SERIES завершена" "стенд свободен; считаю отчёт, пришлю следом${validity:+

$validity}"
        # Отчёт H1 (Манн-Уитни+Холм+δ, графики) — панель «Анализ» статус-
        # страницы читает analysis/report-<стенд>-<серия>/. Генерируется здесь,
        # ПОСЛЕ выхода процесса серии (кластер уже свободен); неудача отчёта
        # серию не трогает — данные в parquet/ClickHouse, отчёт повторим руками.
        local results baselines
        { read -r results; read -r baselines; } < <(results_paths)
        if (cd analysis && .venv/bin/python analyze.py \
                --results "../$results" --baselines "../$baselines" \
                --outdir "report-$STAND-$SERIES") >> "$LOG" 2>&1; then
            echo "WATCHDOG $(date '+%F %T'): отчёт готов — analysis/report-$STAND-$SERIES (статус-страница «Анализ»)." >> "$LOG"
            # summary.md приложением: вердикты H1-H4 читаются с телефона сразу,
            # без доступа к стенду. Файла может не оказаться (analyze.py его
            # не написал) — тогда уедет один текст, см. ss-notify.
            notify "done" "отчёт по $STAND-$SERIES готов" \
                "analysis/report-$STAND-$SERIES — панель «Анализ» статус-страницы" \
                "analysis/report-$STAND-$SERIES/summary.md"
            # Трассы прогона — в ClickHouse, пока их не срезал ретеншен и пока
            # понятно, к какой серии они относятся. Оси, мощность и RAPL живут
            # только в Prometheus, и без этого шага «что творилось на узле в
            # минуту этой точки плана» через год ответить будет нечем.
            # Инкремент идёт по водяному знаку, так что повтор безвреден, а
            # неудача не трогает ни данные, ни отчёт — потому и `|| true`.
            if make ch-load-metrics >> "$LOG" 2>&1; then
                echo "WATCHDOG $(date '+%F %T'): ряды Prometheus догружены в ClickHouse." >> "$LOG"
            else
                echo "WATCHDOG $(date '+%F %T'): ряды в ClickHouse НЕ догрузились — повторить: make ch-load-metrics" >> "$LOG"
            fi
            # Зеркалирование в агрегатор. Без этого шага расхождение копится
            # каждой серией: у окон энергии многоприёмникового пути нет
            # вовсе, и они остаются в одном экземпляре (21.08.2026 — 42
            # окна в агрегаторе против 411 в проде). Неудача не критична,
            # серия уже записана, но молчать о ней нельзя.
            if ./scripts/ch-sync.sh >> "$LOG" 2>&1; then
                echo "WATCHDOG $(date '+%F %T'): база зеркалирована в агрегатор." >> "$LOG"
            else
                echo "WATCHDOG $(date '+%F %T'): зеркалирование НЕ прошло — повторить: scripts/ch-sync.sh" >> "$LOG"
            fi || true
        else
            echo "WATCHDOG ERROR $(date '+%F %T'): отчёт не собрался (см. выше) — повторить: make analyze RESULTS_FILE=$results BASELINES_FILE=$baselines" >> "$LOG"
            notify warn "$STAND-$SERIES: отчёт не собрался" \
                "данные целы (parquet + ClickHouse), повторить руками:
make analyze RESULTS_FILE=$results BASELINES_FILE=$baselines"
        fi
    else
        echo "WATCHDOG ERROR $(date '+%F %T'): процесс серии вышел ДО маркера PRESSURE DONE — смотри хвост лога." >> "$LOG"
        notify error "$STAND-$SERIES оборвалась" \
            "процесс вышел до маркера PRESSURE DONE. Хвост лога:
$(tail -5 "$LOG")"
    fi
    rm -f "$PIDFILE" "$WDPIDFILE" "$STALLFLAG"
    # Маркер снимается на ЛЮБОМ исходе — и на успешном, и на обрыве: иначе на
    # графиках навсегда осталась бы «идущая» серия.
    series_marker clear
}

start() {
    preflight
    local results baselines
    { read -r results; read -r baselines; } < <(results_paths)

    rotate "$LOG"
    rotate "$results"
    rotate "$baselines"

    # PILOT=1 — смоук самой ОБВЯЗКИ (preflight, вотчдог, статус-страница) без
    # многочасовой серии: одна точка плана вместо эталонов и полного
    # pressure-прогона. Окружение берётся из того же run-скрипта, что и у
    # настоящей серии: там живут оверрайды дозы (CPU/THREADS/PRIMARIES/MEM),
    # без которых на 2-vCPU узлах задачи вообще не влезают — смоук на других
    # значениях проверял бы не тот путь. Строки export склеиваются по
    # переносам и выполняются, сам скрипт при этом НЕ запускается.
    if [ "${PILOT:-0}" = "1" ]; then
        ok "PILOT=1 — одна точка плана вместо полной серии"
        local piloted="harness/.pilot-$SERIES.sh"
        {
            echo '#!/bin/bash'
            echo 'cd "$(dirname "$0")"'
            sed -e :a -e '/\\$/N; s/\\\n//; ta' "$RUNSCRIPT" | grep '^export '
            echo 'echo "=== PRESSURE START $(date +%H:%M:%S) (PILOT) ==="'
            echo ".venv/bin/python run_experiment.py --config $(basename "$CONFIG") --pilot"
            echo 'echo "=== PRESSURE DONE $(date +%H:%M:%S) (PILOT) ==="'
        } > "$piloted"
        new_session nohup bash "$piloted" >> "$LOG" 2>&1 &
    else
        new_session nohup bash "$RUNSCRIPT" >> "$LOG" 2>&1 &
    fi
    local pid=$!
    echo "$pid" > "$PIDFILE"
    ok "серия запущена: pid $pid, лог $LOG"

    # Маркер «здесь началась серия» — до status_page_up и уведомления, чтобы
    # граница на графиках совпала с реальным стартом, а не с концом обвязки.
    series_marker set

    status_page_up

    # setsid не умеет bash-функции — вотчдог перезапускается как скрытый
    # экшен этого же скрипта в собственной сессии.
    new_session nohup bash "$0" __watchdog "$SERIES" "$pid" >/dev/null 2>&1 &
    echo $! > "$WDPIDFILE"
    ok "вотчдог: алерт в лог, если тишина >20 мин"

    # Уведомление на старте нужно не для отчёта о запуске, а чтобы канал
    # проверился, пока оператор ещё рядом: протухший токен иначе обнаружится
    # через четыре часа — ровно в тот момент, когда уведомление и требовалось.
    notify info "$STAND-$SERIES запущена" "лог: $LOG
статус: make series-status SERIES=$SERIES"

    echo
    # Адрес страницы — по бэкенду: k8s-страница живёт на лабе и открыта в
    # домашнюю сеть, compose — на машине запуска.
    local page_host=localhost
    [ "$(status_page_backend)" = k8s ] && page_host=${PAGE_HOST:-192.168.1.72}
    echo "дальше:  make series-status SERIES=$SERIES   |   http://$page_host:${STATUS_PORT:-8787}"
}

status() {
    echo "=== серия $SERIES ==="
    if pid_alive "$PIDFILE"; then
        echo "процесс: ЖИВ (pid $(cat "$PIDFILE"))"
    else
        echo "процесс: не запущен / завершился"
    fi
    [ -e "$STALLFLAG" ] && echo "!!! ЗАВИСАНИЕ: лог не растёт (см. WATCHDOG ERROR в $LOG)"
    if [ -f "$LOG" ]; then
        echo "--- фазы ---"
        grep -h -E "^=== (BASELINE|PRESSURE) (START|DONE)" "$LOG" || echo "(маркеров ещё нет)"
        echo "--- ошибки (последние) ---"
        grep -E "ERROR|Traceback" "$LOG" | tail -3 || true
        echo "--- хвост лога ---"
        tail -4 "$LOG"
    else
        echo "(лога $LOG нет)"
    fi
    local results baselines
    { read -r results; read -r baselines; } < <(results_paths)
    "$PY" - "$results" "$baselines" <<'EOF' 2>/dev/null || true
import sys
import pandas as pd
# Ошибки помечаются префиксом error: в колонке approximation
# (run_experiment.py:140). Колонки status в схеме нет — прежняя проверка
# по ней всегда давала 0, то есть статус серии не мог показать ни одной
# ошибки. Заодно считаем missing: строка без метрик тоже не годится.
for path, label in zip(sys.argv[1:], ("результаты", "эталоны")):
    try:
        df = pd.read_parquet(path)
        appr = df.get("approximation", pd.Series(dtype=str)).astype(str)
        errors = int(appr.str.startswith("error:").sum())
        missing = int((appr == "missing").sum())
        flags = []
        if errors:
            flags.append(f"{errors} error!")
        if missing:
            flags.append(f"{missing} без метрик")
        print(f"{label}: {len(df)} строк" + (f" ({', '.join(flags)})" if flags else ""))
    except Exception:
        print(f"{label}: файла ещё нет")
EOF
    # Состояние страницы, а не просто её адрес: печатать URL безусловно
    # значило выдавать мёртвый (или показывающий чужую серию) контейнер за
    # рабочую страницу.
    local port=${STATUS_PORT:-8787}
    echo "--- статус-страница ---"
    if [ "$(status_page_backend)" = k8s ]; then
        local kc=${PAGE_KUBECONFIG:-$HOME/.kube/configs/local72.yaml}
        local cseries=$SERIES
        case "$SERIES" in pressure|baseline|stage) cseries="" ;; esac
        local pstate pargs
        pstate=$(kubectl --kubeconfig "$kc" -n sensitivityscore-system get pods -l app=ss-status \
            -o jsonpath='{.items[0].status.phase} (рестартов {.items[0].status.containerStatuses[0].restartCount})' 2>/dev/null)
        if [ -z "$pstate" ]; then
            echo "не запущена в k0s лабы (поднять: STAND=$STAND make status-page SERIES=$SERIES)"
        else
            pargs=$(kubectl --kubeconfig "$kc" -n sensitivityscore-system get deploy ss-status \
                -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null)
            echo "под (k0s): $pstate"
            if grep -q -- "$STAND-${cseries:-pressure}\.log" <<<"$pargs"; then
                echo "серия:     $SERIES — совпадает"
            else
                echo "серия:     !!! страница показывает ДРУГУЮ серию, цифрам на ней не верить"
            fi
            local phost=${PAGE_HOST:-192.168.1.72}
            curl -sf -o /dev/null "http://$phost:$port/healthz" 2>/dev/null \
                && echo "адрес:     http://$phost:$port" \
                || echo "адрес:     не отвечает на http://$phost:$port"
        fi
    elif ! command -v docker >/dev/null 2>&1; then
        echo "docker не найден — страница не поднималась"
    elif ! docker inspect ss-status >/dev/null 2>&1; then
        echo "не запущена (поднять: make status-page SERIES=$SERIES)"
    else
        local state args cseries
        cseries=$SERIES
        case "$SERIES" in pressure|baseline|stage) cseries="" ;; esac
        state=$(docker inspect ss-status --format '{{.State.Status}} (код {{.State.ExitCode}}, рестартов {{.RestartCount}})')
        args=$(docker inspect ss-status --format '{{json .Args}}')
        echo "контейнер: $state"
        if grep -q -- "$STAND-${cseries:-pressure}\.log" <<<"$args"; then
            echo "серия:     $SERIES — совпадает"
        else
            echo "серия:     !!! контейнер показывает ДРУГУЮ серию, цифрам на странице не верить"
        fi
        curl -sf -o /dev/null "http://localhost:$port/healthz" 2>/dev/null \
            && echo "адрес:     http://localhost:$port" \
            || echo "адрес:     не отвечает на http://localhost:$port"
    fi
}

stop() {
    if pid_alive "$PIDFILE"; then
        local pid; pid=$(cat "$PIDFILE")
        echo "останавливаю группу процессов серии (pgid $pid)"
        kill -TERM -- "-$pid" 2>/dev/null
        sleep 2
        kill -0 "$pid" 2>/dev/null && kill -KILL -- "-$pid" 2>/dev/null
    else
        echo "процесс серии не найден"
    fi
    pid_alive "$WDPIDFILE" && kill "$(cat "$WDPIDFILE")" 2>/dev/null
    rm -f "$PIDFILE" "$WDPIDFILE" "$STALLFLAG"
    # Остановка руками убивает вотчдог раньше, чем тот дойдёт до уборки, —
    # маркер снимаем здесь же.
    series_marker clear
    echo "уборка кластера: агрессоры + job'ы bench + sink"
    kubectl -n $BENCH_NS delete pods -l app=ss-aggressor --ignore-not-found --timeout=120s
    kubectl -n $BENCH_NS delete jobs -l app=geant4-bench --ignore-not-found --timeout=120s
    # ss-sink разворачивает run-stage-net-diff.sh и сам же убирает в конце —
    # но при остановке серии на середине он остался бы и завалил preflight
    # следующей серии («чужие поды в bench ns»).
    kubectl -n $BENCH_NS delete pod,svc -l app=ss-sink --ignore-not-found --timeout=60s
    echo "готово (статус-страница оставлена — она только читает)"
}

case "$ACTION" in
    start)  start ;;
    status) status ;;
    stop)   stop ;;
    # Отдельно от start: проверить стенд, ничего не запуская. Раньше единственным
    # способом узнать, готов ли кластер, было стартовать многочасовую серию.
    preflight) preflight ;;
    # Поднять только статус-страницу. Нужен тем путям запуска, которые идут
    # мимо start(): ручной harness/run-<стенд>-<имя>.sh, make pilot/run-all.
    page) status_page_up ;;
    __watchdog) watchdog "${3:?нужен pid серии}" ;;
    *) echo "неизвестное действие: $ACTION (start|preflight|page|status|stop)"; exit 2 ;;
esac
