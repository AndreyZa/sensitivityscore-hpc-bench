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
# Конвенция имён (STAND=stage по умолчанию, SERIES=placebo -> «stage-placebo»):
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
# kubeconfig по умолчанию. Значение по умолчанию — stage, поэтому все команды
# STAGE работают как раньше, без указания STAND.
STAND=${STAND:-stage}
case "$STAND" in
    stage) DEFAULT_KUBECONFIG=$HOME/.kube/configs/timeweb-stage ;;
    prod)  DEFAULT_KUBECONFIG=$HOME/.kube/configs/prod ;;
    *)     echo "неизвестный STAND='$STAND' (ожидается stage|prod)"; exit 2 ;;
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

pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

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
status_page_up() {
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
        status_page_env_save "$cseries" "$stand_up" "$port" "$results" "$baselines" "$kcfg"
        ok "статус-страница уже поднята: http://localhost:$port"
        return 0
    fi

    # Вывод сборки НЕ в /dev/null: при провале build compose выходит, не
    # тронув контейнеры, и на порту продолжает жить страница прошлой серии —
    # WARN и работающая страница одновременно читаются как «ложная тревога».
    if ! SERIES="$cseries" STAND="$stand_up" STATUS_PORT="$port" \
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
    status_page_env_save "$cseries" "$stand_up" "$port" "$results" "$baselines" "$kcfg"
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
    kubectl -n $SYS_NS logs deploy/sensitivityscore-scheduler --tail=-1 2>/dev/null \
        | grep -q "sensitivity weights loaded" \
        || fail "в логе планировщика нет 'sensitivity weights loaded' — образ без parseWeights?"
    ok "планировщик Ready, веса загружены"

    # weights.json (ConfigMap) == score_weights (конфиг серии), после
    # нормализации обоих форматов через split_weights (зеркало parseWeights).
    (cd harness && ../$PY - "../$CONFIG" <<'EOF'
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
    (cd harness && ../"$PY" - "../contract/redis-fields.yaml" <<EOF
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
    leftovers=$(kubectl -n $BENCH_NS get pods --no-headers 2>/dev/null | wc -l)
    [ "$leftovers" -eq 0 ] || fail "$leftovers чужих подов в $BENCH_NS (эталонам нужен пустой кластер) — make harness-clean-jobs"
    ok "bench-namespace пуст"

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
        # Форвард к Redis: без него regret=NaN на всех последующих задачах,
        # и это не видно ни в логе, ни на странице (см. redis_alive).
        if ! redis_alive; then
            echo "WATCHDOG ERROR $(date '+%F %T'): port-forward к Redis мёртв — placement_regret с этого момента NaN; поднимаю заново" >> "$LOG"
            redis_pf_start
            if redis_alive; then
                echo "WATCHDOG $(date '+%F %T'): port-forward к Redis восстановлен" >> "$LOG"
            else
                echo "WATCHDOG ERROR $(date '+%F %T'): поднять port-forward не удалось (harness/.redis-pf.log)" >> "$LOG"
            fi
        fi
        if [ "$size" != "$last_size" ]; then
            last_size=$size; last_change=$now; rm -f "$STALLFLAG"
        elif [ $((now - last_change)) -ge 1200 ] && [ ! -e "$STALLFLAG" ]; then
            echo "WATCHDOG ERROR $(date '+%F %T'): лог не растёт $(((now - last_change) / 60)) мин — серия зависла? kubectl get pods -n $BENCH_NS" >> "$LOG"
            touch "$STALLFLAG"
            last_size=$(log_size)
        fi
    done
    if grep -q "PRESSURE DONE" "$LOG" 2>/dev/null; then
        echo "WATCHDOG $(date '+%F %T'): серия $SERIES завершена (PRESSURE DONE)." >> "$LOG"
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
        else
            echo "WATCHDOG ERROR $(date '+%F %T'): отчёт не собрался (см. выше) — повторить: make analyze RESULTS_FILE=$results BASELINES_FILE=$baselines" >> "$LOG"
        fi
    else
        echo "WATCHDOG ERROR $(date '+%F %T'): процесс серии вышел ДО маркера PRESSURE DONE — смотри хвост лога." >> "$LOG"
    fi
    rm -f "$PIDFILE" "$WDPIDFILE" "$STALLFLAG"
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

    status_page_up

    # setsid не умеет bash-функции — вотчдог перезапускается как скрытый
    # экшен этого же скрипта в собственной сессии.
    new_session nohup bash "$0" __watchdog "$SERIES" "$pid" >/dev/null 2>&1 &
    echo $! > "$WDPIDFILE"
    ok "вотчдог: алерт в лог, если тишина >20 мин"
    echo
    echo "дальше:  make series-status SERIES=$SERIES   |   http://localhost:8787"
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
    if ! command -v docker >/dev/null 2>&1; then
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
