#!/bin/bash
# Свип веса плагина SensitivityScore (пункт C2 аудита) — ЗАПУСКАТЬ С .72.
#
# Зачем. weight: 5 в scheduler-config — самый решающий параметр выводов H1 — не
# калибровался и не варьировался. Свип отвечает на «вы крутили его, пока не
# начало выигрывать?»: прогоняем набор весов и смотрим, как зависит качество
# размещения (независимый placement_oracle, B4) от веса.
#
# Сценарий — net-diff-v2 (не io-sensitivity). На io первый прогон (28.07) дал
# ПЛОСКУЮ кривую: чувствительная жертва избегала шторма даже при весе 0 (разные
# ресурсные заявки + вдоволь чистой ёмкости), весу нечего было двигать. В
# net-diff базовая цена сети = 0, размещение решает ТОЛЬКО чувствительностная
# компонента, которую вес и масштабирует. Скрипт сам подменяет score-веса в CM
# на base=0/sens net=0.5 (иначе у плагина нет сигнала) и ВОЗВРАЩАЕТ калиброванные
# в cleanup, поднимает приёмник стрима (setup_scenario) и убирает его после.
#
# Оптимизация (втрое меньше прогонов). Эталоны и плечи default/trimaran к весу
# плагина sensitivityscore ИНВАРИАНТНЫ, поэтому:
#   REF-фаза (один раз): эталоны + default + trimaran   -> метка sweep-ref
#   SS-фаза (на каждый вес W): только A-sensitivityscore -> метка sweep-ss-wW
# weight=0 = плагин с нулевым весом = эквивалент default (встроенный контроль).
#
# Критерий выбора (ЗАФИКСИРОВАН ДО ПРОГОНА, иначе это подгонка): минимальный
# вес, про который можно уверенно сказать «хуже лучшего меньше чем на margin»,
# то есть верхняя граница 95% bootstrap-CI ПАРНОЙ разности measured-regret с
# лучшим весом ниже margin. Пары — по номеру повторения: поток задач
# порождается генератором с начальным значением по номеру повтора, то есть
# один и тот же поток проходит через все веса. margin = 10% размаха кривой
# (stats.PLATEAU_MARGIN_FRACTION), печатается в отчёте.
#
# Правка от 25.07.2026, ДО первого прогона свипа: данных ещё нет, подгонять
# нечего — на синтетике с заложенным ответом (sweep-analyze.py --self-test)
# отвергнуты две прежние редакции.
#   1. «Перекрываются маргинальные CI двух весов» — слишком слабо: вес с
#      regret 0,317 против лучшего 0,022 объявлялся плато только потому, что
#      нижняя граница его интервала заходила под верхнюю границу лучшего.
#   2. «CI парной разности накрывает 0» — слишком строго: дизайн парный,
#      мощность высока, и устойчивая разница в 1% отвергает плато. Но вопрос
#      свипа не «есть ли хоть какая-то разница», а «мал ли проигрыш настолько,
#      что весом можно не рисковать» — утверждение о неменьшей эффективности,
#      и без границы оно не формулируется.
# Обе прежние редакции печатаются рядом справочно.
#
# Ниже плато сигнал SensitivityScore перекрывается суммой дефолтных
# score-плагинов; выше — избыточен. Рядом анализ печатает прямую ступеньку
# размещения (доля high-s-net на штормовом узле по весу); решение остаётся за
# человеком.
#
#   bash scripts/weight-sweep.sh ref           только REF-фаза (один раз)
#   bash scripts/weight-sweep.sh ss            SS-фаза по всем весам
#   bash scripts/weight-sweep.sh analyze       свести placement_oracle по меткам
#   bash scripts/weight-sweep.sh all           ref -> ss -> analyze
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage}

NS=sensitivityscore-system
BENCH_NS=sensitivityscore-bench
SCHED=sensitivityscore-scheduler
REDIS_PORT=16379
PY=harness/.venv/bin/python
CHPY=db/clickhouse/.venv/bin/python
# Анализ (фаза analyze) — ОТДЕЛЬНЫЙ venv. sweep-analyze.py импортирует из
# analysis/ и stats (scipy), и clickhouse_source (clickhouse_connect); оба
# пакета есть только в analysis/.venv. harness/.venv на .72 их не имеет, а на
# рабочей машине имеет лишь scipy — поэтому запуск через $PY проходил --self-test
# (он CH не трогает), но на реальном analyze падал бы на clickhouse_connect.
APY=analysis/.venv/bin/python
# CH на .72 локальный (не туннель — туннель это для Mac).
CH_HOST=${CH_HOST:-localhost}
CH_PORT=${CH_PORT:-8123}

# --- Сценарий свипа: net-diff-v2. Профиль-жертва, чьё размещение решает вес;
# приёмник стрима (на ss-system, ёмкости bench не ест); конфиг, откуда берутся
# net-diff score-веса для CM.
SENSITIVE_PROFILE=high-s-net
SINK_MANIFEST=k8s/net-sink/sink-stage-v2.yaml
NETDIFF_CONFIG=harness/config-stage-net-diff-v2.yaml

# Усиленный свип (C2): 10 повторов (совпадает с n=10 остального исследования,
# дискретный пол Уилкоксона 2/2^10) и гуще сетка весов + один сверху (40),
# чтобы гарантированно накрыть плато. 8 весов × 10 повторов ≈ 80 SS-прогонов +
# REF — ночной прогон (~5-8ч), запускается однократно.
WEIGHTS=${WEIGHTS:-"0 1 2 3 5 10 20 40"}
REPS=${REPS:-10}

# --- Оверрайды профилей под STAGE (2 vCPU / ~1.9Gi на узел). КРИТИЧНО.
# Свип зовёт run_experiment.py напрямую, минуя run-stage-net-diff-v2.sh, поэтому
# обязан САМ выставить те же env, что и штатный раннер. Без них профили берут
# прод-дефолты (profiles.py) и не встают на 2-ядерный узел.
#
# Сценарий net-diff-v2, а НЕ io-sensitivity: на io чувствительная жертва
# избегала шторма даже при весе 0 (кривая свипа плоская, прогон 28.07). Здесь
# базовая цена сети = 0, размещение решает ТОЛЬКО чувствительностная компонента,
# которую вес и масштабирует — есть чему зависеть от веса.
#
# Двойники high-s-net / net-insensitive: ИДЕНТИЧНЫЕ compute/ресурсы (500m, 1
# поток, 100k частиц, 384Mi), отличие лишь в сетевом выводе. Значения — 1:1 с
# harness/run-stage-net-diff-v2.sh (не менять по отдельности).
export HARNESS_OVERRIDE_HIGH_S_NET_CPU=500m HARNESS_OVERRIDE_HIGH_S_NET_THREADS=1 \
       HARNESS_OVERRIDE_HIGH_S_NET_PRIMARIES=100000 \
       HARNESS_OVERRIDE_HIGH_S_NET_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_NET_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_HIGH_S_NET_OUTPUT_MODE=stream \
       HARNESS_OVERRIDE_HIGH_S_NET_NET_SINK_HOST=ss-sink \
       HARNESS_OVERRIDE_HIGH_S_NET_NET_SINK_PORT=9000 \
       HARNESS_OVERRIDE_HIGH_S_NET_NET_TOTAL_MB=2048 \
       HARNESS_OVERRIDE_NET_INSENSITIVE_CPU=500m HARNESS_OVERRIDE_NET_INSENSITIVE_THREADS=1 \
       HARNESS_OVERRIDE_NET_INSENSITIVE_PRIMARIES=100000 \
       HARNESS_OVERRIDE_NET_INSENSITIVE_MEM_REQ=384Mi HARNESS_OVERRIDE_NET_INSENSITIVE_MEM_LIM=2Gi

fail() { echo "FAIL: $*" >&2; exit 1; }
say()  { echo "[sweep $(date +%H:%M:%S)] $*"; }

# --- Статус-страница. Свип был единственным раннером серий, который зовёт
# run_experiment.py напрямую и страницу не трогает вовсе: на 8787 продолжала
# висеть ПРЕДЫДУЩАЯ серия (27.07 — июльский stage-ablation), выглядя при этом
# совершенно живой. Отсюда два следствия для путей:
#   * конфиг фазы пишется в РЕПОЗИТОРИЙ, а не в /tmp: контейнер страницы
#     монтирует только корень репозитория (/repo), и /tmp ему не виден;
#   * лог фазы — harness/stage-<серия>.log с теми же маркерами, что у штатных
#     harness/run-stage-*.sh: по ним статус-страница определяет фазу и ETA
#     (statusserver/progress.py). Без маркеров прогон виден, а фаза — нет.
# Подъём делегируется run-series.sh page: там уже решены выбор kubeconfig
# (каталог вместо файла ломает kubectl), пути parquet из секции output конфига
# и запись harness/.status-page.env для systemd-юнита ss-status.
page_up() {
    local series=$1
    bash scripts/run-series.sh page "$series" || \
        say "статус-страница для $series не поднялась — на прогон это не влияет"
}

# harness_run <серия> <BASELINE|PRESSURE> <аргументы run_experiment.py...>
harness_run() {
    local series=$1 marker=$2 rc=0
    shift 2
    local log="harness/stage-$series.log"
    echo "=== $marker START $(date +%H:%M:%S) epoch=$(date +%s) ===" | tee -a "$log"
    ( cd harness && ../$PY run_experiment.py "$@" ) 2>&1 | tee -a "$log"
    rc=${PIPESTATUS[0]}
    echo "=== $marker DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ===" | tee -a "$log"
    return "$rc"
}

command -v kubectl >/dev/null || fail "kubectl не найден"
[ -x "$PY" ] || fail "нет $PY — сначала make venv-harness"
[ -x "$CHPY" ] || fail "нет $CHPY — сначала make venv-clickhouse"

# --- Score-веса (weights.json в CM sensitivity-config). Свип net-diff идёт на
# base=0/sens net=0.5 — иначе у плагина нет сетевого сигнала и весу нечего
# масштабировать. Калиброванные веса СНИМАЕМ живьём (на разных стендах разные) и
# возвращаем в CM после свипа (cleanup, даже при падении). НЕ путать с плагинным
# weight в scheduler-config — тот трогает set_weight/restore_weight.
CALIB_WEIGHTS=$(kubectl -n "$NS" get cm sensitivity-config -o jsonpath='{.data.weights\.json}' 2>/dev/null)
[ -n "$CALIB_WEIGHTS" ] || fail "не снял текущий weights.json из CM sensitivity-config"
printf '%s' "$CALIB_WEIGHTS" > harness/.sweep-calib-weights.json   # для ручного восстановления
WEIGHTS_SWAPPED=0

netdiff_weights_json() {   # score_weights из конфига net-diff-v2 -> JSON одной строкой
    "$PY" - "$NETDIFF_CONFIG" <<'EOF'
import sys, json
sys.path.insert(0, "harness")
from config_loader import load_config
print(json.dumps(load_config(sys.argv[1])["score_weights"]))
EOF
}

set_score_weights() {   # set_score_weights <json>; CM sensitivity-config несёт один ключ weights.json
    kubectl -n "$NS" create configmap sensitivity-config \
        --from-literal=weights.json="$1" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

# --- Redis port-forward: метрика решения (placement_regret) читается из Redis.
RF_PID=""
redis_up() {
    kubectl -n "$NS" port-forward svc/redis "$REDIS_PORT:6379" >/tmp/sweep-redis.log 2>&1 &
    RF_PID=$!
    export REDIS_ADDR="localhost:$REDIS_PORT"
    for _ in $(seq 20); do
        "$PY" -c "import redis,os; redis.Redis.from_url('redis://'+os.environ['REDIS_ADDR']).ping()" 2>/dev/null && return 0
        sleep 1
    done
    fail "redis port-forward не поднялся (см. /tmp/sweep-redis.log)"
}
cleanup() {
    [ -n "${RF_PID:-}" ] && kill "$RF_PID" 2>/dev/null
    # Вернуть калиброванные score-веса, даже если свип упал на середине — иначе
    # стенд остаётся на net-diff-весах и следующая серия/анализ считает не тем.
    if [ "${WEIGHTS_SWAPPED:-0}" = 1 ] && [ -n "${CALIB_WEIGHTS:-}" ]; then
        set_score_weights "$CALIB_WEIGHTS" 2>/dev/null \
            && echo "[sweep] калиброванные score-веса возвращены в CM sensitivity-config"
        kubectl rollout restart deployment/"$SCHED" -n "$NS" >/dev/null 2>&1 || true
    fi
    # Убрать приёмник стрима (следующая серия ждёт пустой bench-namespace).
    kubectl delete -f "$SINK_MANIFEST" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

set_weight() {
    local w=$1
    say "weight(SensitivityScore) = $w -> ConfigMap + rollout restart"
    "$PY" scripts/sweep-weight-config.py "$w" > "/tmp/sched-w$w.yaml" \
        || fail "патч конфига веса $w"
    kubectl create configmap scheduler-config \
        --from-file=scheduler-config.yaml="/tmp/sched-w$w.yaml" \
        -n "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl rollout restart deployment/"$SCHED" -n "$NS" >/dev/null
    kubectl rollout status deployment/"$SCHED" -n "$NS" --timeout=120s >/dev/null \
        || fail "планировщик не поднялся после смены веса на $w"
    # Плагин перечитывает веса метрик из ConfigMap сам, но само значение weight
    # в KubeSchedulerConfiguration применяется только рестартом (сделан выше).
    sleep 5
}

restore_weight() {
    say "восстанавливаю исходный scheduler-config (weight=5 из git)"
    kubectl create configmap scheduler-config \
        --from-file=k8s/scheduler-config/scheduler-config.yaml \
        -n "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl rollout restart deployment/"$SCHED" -n "$NS" >/dev/null
    kubectl rollout status deployment/"$SCHED" -n "$NS" --timeout=120s >/dev/null || true
}

bench_clean() {
    kubectl -n "$BENCH_NS" delete pods -l app=ss-aggressor --ignore-not-found --timeout=120s >/dev/null 2>&1
    kubectl -n "$BENCH_NS" delete jobs -l app=geant4-bench --ignore-not-found --timeout=120s >/dev/null 2>&1
}

# Инфраструктура сценария net-diff: приёмник стрима + net-веса в CM. Идемпотентно
# (зовут и ref, и ss). Приёмник bench_clean НЕ трогает (метка ss-sink), он живёт
# весь свип; убирает его cleanup. net-веса ставим один раз (флаг), первый
# set_weight с rollout их и подхватит.
setup_scenario() {
    kubectl apply -f "$SINK_MANIFEST" >/dev/null || fail "приёмник $SINK_MANIFEST не применился"
    kubectl -n "$BENCH_NS" wait --for=condition=Ready pod/ss-sink --timeout=120s >/dev/null 2>&1 \
        || say "приёмник ss-sink не Ready за 120с — стрим жертв может не подняться"
    if [ "$WEIGHTS_SWAPPED" = 0 ]; then
        say "score-веса CM -> net-diff (base=0, sens net=0.5); калиброванные вернутся в cleanup"
        set_score_weights "$(netdiff_weights_json)" || fail "подмена weights.json на net-diff"
        WEIGHTS_SWAPPED=1
    fi
}

ch_load() {
    local label=$1 results=$2
    [ -f "$results" ] || { say "нет $results — пропуск загрузки $label"; return 0; }
    "$CHPY" db/clickhouse/load_parquet.py --host "$CH_HOST" --port "$CH_PORT" \
        --stand stage --run-label "$label" --results "$results" \
        --allow-existing || fail "ch-load $label"
    say "залито в ClickHouse: $label"
}

phase_ref() {
    redis_up
    setup_scenario
    say "REF-фаза: эталоны + default/trimaran (один раз, вес не влияет)"
    "$PY" scripts/sweep-series-config.py --variants default,trimaran --reps "$REPS" \
        --results-file results-sweep-ref.parquet \
        > harness/config-stage-sweep-ref.yaml || fail "конфиг ref"
    page_up sweep-ref
    bench_clean
    harness_run sweep-ref BASELINE --config config-stage-sweep-ref.yaml --baseline \
        || fail "эталоны ref"
    harness_run sweep-ref PRESSURE --config config-stage-sweep-ref.yaml --pressure \
        || fail "pressure ref (default/trimaran)"
    ch_load sweep-ref harness/results/results-sweep-ref.parquet
    # baselines загружаем отдельно (нужны оракулу как знаменатель slowdown)
    "$CHPY" db/clickhouse/load_parquet.py --host "$CH_HOST" --port "$CH_PORT" \
        --stand stage --run-label sweep-ref \
        --baselines harness/results/baselines-sweep-ref.parquet --allow-existing \
        2>/dev/null || say "baselines ref не залиты (проверь per_node)"
}

phase_ss() {
    redis_up
    setup_scenario
    for w in $WEIGHTS; do
        set_weight "$w"
        say "SS-фаза: только A-sensitivityscore при weight=$w, reps=$REPS"
        "$PY" scripts/sweep-series-config.py --variants sensitivityscore --reps "$REPS" \
            --results-file "results-sweep-ss-w$w.parquet" \
            > "harness/config-stage-sweep-ss-w$w.yaml" || fail "конфиг ss w$w"
        page_up "sweep-ss-w$w"
        bench_clean
        harness_run "sweep-ss-w$w" PRESSURE --config "config-stage-sweep-ss-w$w.yaml" --pressure \
            || fail "pressure ss w$w"
        ch_load "sweep-ss-w$w" "harness/results/results-sweep-ss-w$w.parquet"
    done
    restore_weight
}

phase_analyze() {
    [ -x "$APY" ] || fail "нет $APY — сначала make venv-analysis (нужны scipy + clickhouse_connect)"
    say "анализ: measured-regret по весам (оракул B4)"
    # A-ss под каждым весом + эталоны/default из sweep-ref в один датафрейм.
    # --sensitive-profile: для net-diff жертва — high-s-net (не high-s-io);
    # от неё зависят детект штормового узла и ступенька размещения.
    "$APY" scripts/sweep-analyze.py --weights "$WEIGHTS" \
        --sensitive-profile "$SENSITIVE_PROFILE" \
        --ch-host "$CH_HOST" --ch-port "$CH_PORT"
}

case "${1:-all}" in
    ref)     phase_ref ;;
    ss)      phase_ss ;;
    analyze) phase_analyze ;;
    all)     phase_ref; phase_ss; phase_analyze ;;
    *) echo "использование: $0 ref|ss|analyze|all"; exit 2 ;;
esac
