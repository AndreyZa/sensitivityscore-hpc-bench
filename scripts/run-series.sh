#!/bin/bash
# Кнопка «прогнать серию»: preflight -> запуск одной сессией (setsid) ->
# статус-страница -> вотчдог. Одна команда вместо ритуала из шести шагов,
# каждый из которых уже стоил ночи стенда, будучи забытым.
#
#   make series SERIES=<имя>          запустить (preflight + фон + вотчдог)
#   make series-status SERIES=<имя>   состояние идущей/законченной серии
#   make series-stop SERIES=<имя>     остановить и прибрать за собой
#
# Конвенция имён (SERIES=placebo -> «stage-placebo»):
#   конфиг     harness/config-stage-<имя>.yaml
#   скрипт     harness/run-stage-<имя>.sh      (эталоны + серия одной сессией)
#   лог        harness/stage-<имя>.log         (старый ротируется с меткой времени)
#   результаты harness/results/<из секции output конфига> (старые ротируются)
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
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage}

SYS_NS=sensitivityscore-system
BENCH_NS=sensitivityscore-bench
REDIS_PORT=16379
PY=harness/.venv/bin/python

ACTION=${1:-}
SERIES=${2:-}
[ -z "$ACTION" ] || [ -z "$SERIES" ] && {
    echo "использование: $0 start|status|stop <серия>  (напр. placebo, mixed-calib)"; exit 2; }

CONFIG=harness/config-stage-$SERIES.yaml
RUNSCRIPT=harness/run-stage-$SERIES.sh
LOG=harness/stage-$SERIES.log
PIDFILE=harness/.series-$SERIES.pid
WDPIDFILE=harness/.series-$SERIES.watchdog.pid
STALLFLAG=harness/.series-$SERIES.stalled
REPORT=analysis/report-stage-$SERIES

fail() { echo "FAIL: $*"; [ "${FORCE:-0}" = "1" ] && echo "      (FORCE=1 — продолжаю)" || exit 1; }
ok()   { echo "  ok: $*"; }

pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# Пути результатов — из секции output конфига (единственный источник правды).
results_paths() {
    "$PY" - "$CONFIG" <<'EOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
out = cfg["output"]
print("harness/" + out["results_dir"] + "/" + out["results_file"])
print("harness/" + out["results_dir"] + "/" + out.get("baselines_file", "baselines.parquet"))
EOF
}

# ---------------------------------------------------------------------------
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
        fail "уже работает run_experiment.py вне кнопки (pgrep -f run_experiment.py)"
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
import json, subprocess, sys, yaml
from submit.node_pressure import split_weights
cfg = yaml.safe_load(open(sys.argv[1]))
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

    if ! "$PY" -c "import redis; redis.Redis(port=$REDIS_PORT, socket_connect_timeout=2).ping()" 2>/dev/null; then
        echo "  ..: поднимаю port-forward redis :$REDIS_PORT"
        setsid nohup kubectl -n $SYS_NS port-forward svc/redis $REDIS_PORT:6379 \
            > harness/.redis-pf.log 2>&1 &
        echo $! > harness/.redis-pf.pid
        sleep 3
        "$PY" -c "import redis; redis.Redis(port=$REDIS_PORT, socket_connect_timeout=2).ping()" 2>/dev/null \
            || fail "redis port-forward не поднялся (harness/.redis-pf.log)"
    fi
    local nkeys
    nkeys=$("$PY" -c "
import redis
r = redis.Redis(port=$REDIS_PORT, decode_responses=True)
print(len(list(r.scan_iter(match='node:metrics:*'))))" 2>/dev/null)
    [ "${nkeys:-0}" -ge 2 ] || fail "в Redis меньше 2 ключей node:metrics:* — агент не пишет?"
    ok "redis :$REDIS_PORT жив, node:metrics ключей: $nkeys"

    local leftovers
    leftovers=$(kubectl -n $BENCH_NS get pods --no-headers 2>/dev/null | wc -l)
    [ "$leftovers" -eq 0 ] || fail "$leftovers чужих подов в $BENCH_NS (эталонам нужен пустой кластер) — make harness-clean-jobs"
    ok "bench-namespace пуст"
    echo "=== preflight пройден ==="
}

rotate() {
    local f=$1 stamp
    [ -f "$f" ] || return 0
    stamp=$(date -r "$f" +%Y%m%d-%H%M%S)
    mv "$f" "$f.$stamp"
    echo "  ..: $f -> $f.$stamp"
}

watchdog() {
    # Прогресс = рост лога. Порог 20 мин > job_timeout (15 мин): даже
    # намертво зависшая жертва даёт строку об ошибке раньше срабатывания.
    # Свой алерт из прогресса исключается (размер перечитывается после
    # записи), флаг гасит повтор — одна запись на эпизод зависания.
    local main_pid=$1 last_size last_change now size
    last_size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
    last_change=$(date +%s)
    while kill -0 "$main_pid" 2>/dev/null; do
        sleep 300
        size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
        now=$(date +%s)
        if [ "$size" != "$last_size" ]; then
            last_size=$size; last_change=$now; rm -f "$STALLFLAG"
        elif [ $((now - last_change)) -ge 1200 ] && [ ! -e "$STALLFLAG" ]; then
            echo "WATCHDOG ERROR $(date '+%F %T'): лог не растёт $(((now - last_change) / 60)) мин — серия зависла? kubectl get pods -n $BENCH_NS" >> "$LOG"
            touch "$STALLFLAG"
            last_size=$(stat -c %s "$LOG")
        fi
    done
    if grep -q "PRESSURE DONE" "$LOG" 2>/dev/null; then
        echo "WATCHDOG $(date '+%F %T'): серия $SERIES завершена (PRESSURE DONE)." >> "$LOG"
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

    setsid nohup bash "$RUNSCRIPT" >> "$LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    ok "серия запущена: pid $pid, лог $LOG"

    LOG="$LOG" CONFIG="$CONFIG" RESULTS="$results" BASELINES="$baselines" \
        REPORT="$REPORT" SCOPE=full ./statusserver/run-stage.sh \
        || echo "WARN: статус-страница не поднялась (серию это не трогает)"

    # setsid не умеет bash-функции — вотчдог перезапускается как скрытый
    # экшен этого же скрипта в собственной сессии.
    setsid nohup bash "$0" __watchdog "$SERIES" "$pid" >/dev/null 2>&1 &
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
for path, label in zip(sys.argv[1:], ("результаты", "эталоны")):
    try:
        df = pd.read_parquet(path)
        errors = int((df.get("status", pd.Series(dtype=str)) == "error").sum())
        print(f"{label}: {len(df)} строк" + (f" ({errors} error!)" if errors else ""))
    except Exception:
        print(f"{label}: файла ещё нет")
EOF
    echo "страница: http://localhost:8787"
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
    echo "уборка кластера: агрессоры + job'ы bench"
    kubectl -n $BENCH_NS delete pods -l app=ss-aggressor --ignore-not-found --timeout=120s
    kubectl -n $BENCH_NS delete jobs -l app=geant4-bench --ignore-not-found --timeout=120s
    echo "готово (статус-страница оставлена — она только читает)"
}

case "$ACTION" in
    start)  start ;;
    status) status ;;
    stop)   stop ;;
    __watchdog) watchdog "${3:?нужен pid серии}" ;;
    *) echo "неизвестное действие: $ACTION (start|status|stop)"; exit 2 ;;
esac
