#!/bin/bash
# Свип веса плагина SensitivityScore (пункт C2 аудита) — ЗАПУСКАТЬ С .72.
#
# Зачем. weight: 5 в scheduler-config — самый решающий параметр выводов H1 — не
# калибровался и не варьировался. Свип отвечает на «вы крутили его, пока не
# начало выигрывать?»: прогоняем набор весов и смотрим, как зависит качество
# размещения (независимый placement_oracle, B4) от веса.
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
# размещения (доля high-s-io на штормовом узле по весу); решение остаётся за
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
# CH на .72 локальный (не туннель — туннель это для Mac).
CH_HOST=${CH_HOST:-localhost}
CH_PORT=${CH_PORT:-8123}

# Усиленный свип (C2): 10 повторов (совпадает с n=10 остального исследования,
# дискретный пол Уилкоксона 2/2^10) и гуще сетка весов + один сверху (40),
# чтобы гарантированно накрыть плато. 8 весов × 10 повторов ≈ 80 SS-прогонов +
# REF — ночной прогон (~5-8ч), запускается однократно.
WEIGHTS=${WEIGHTS:-"0 1 2 3 5 10 20 40"}
REPS=${REPS:-10}

# --- Оверрайды профилей под STAGE (2 vCPU / ~1.9Gi на узел). КРИТИЧНО.
# Свип зовёт run_experiment.py напрямую, минуя run-stage-io-sensitivity.sh,
# поэтому обязан САМ выставить те же env, что и штатный раннер. Без них
# профиль high-s-io берёт прод-дефолт cpu=8/mem=4Gi (profiles.py) и ни один
# под жертвы не встаёт на 2-ядерный узел — планировщик вечно держит его в
# Pending («Insufficient cpu/memory»), а свип клинит на первом же io-плече.
# Значения — 1:1 с harness/run-stage-io-sensitivity.sh (не менять по отдельности).
export HARNESS_OVERRIDE_HIGH_S_IO_CPU=500m HARNESS_OVERRIDE_HIGH_S_IO_THREADS=2 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=300000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=384Mi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=2Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_BURST_MB=32 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_INTERVAL_SECONDS=0 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_TOTAL_BURSTS=16 \
       HARNESS_OVERRIDE_LOW_S_PRIMARIES=300000

fail() { echo "FAIL: $*" >&2; exit 1; }
say()  { echo "[sweep $(date +%H:%M:%S)] $*"; }

command -v kubectl >/dev/null || fail "kubectl не найден"
[ -x "$PY" ] || fail "нет $PY — сначала make venv-harness"
[ -x "$CHPY" ] || fail "нет $CHPY — сначала make venv-clickhouse"

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
cleanup() { [ -n "$RF_PID" ] && kill "$RF_PID" 2>/dev/null; }
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
    say "REF-фаза: эталоны + default/trimaran (один раз, вес не влияет)"
    "$PY" scripts/sweep-series-config.py --variants default,trimaran --reps "$REPS" \
        --results-file results-sweep-ref.parquet > /tmp/sweep-ref.yaml || fail "конфиг ref"
    bench_clean
    ( cd harness && ../$PY run_experiment.py --config /tmp/sweep-ref.yaml --baseline ) \
        || fail "эталоны ref"
    ( cd harness && ../$PY run_experiment.py --config /tmp/sweep-ref.yaml --pressure ) \
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
    for w in $WEIGHTS; do
        set_weight "$w"
        say "SS-фаза: только A-sensitivityscore при weight=$w, reps=$REPS"
        "$PY" scripts/sweep-series-config.py --variants sensitivityscore --reps "$REPS" \
            --results-file "results-sweep-ss-w$w.parquet" > "/tmp/sweep-ss-w$w.yaml" \
            || fail "конфиг ss w$w"
        bench_clean
        ( cd harness && ../$PY run_experiment.py --config "/tmp/sweep-ss-w$w.yaml" --pressure ) \
            || fail "pressure ss w$w"
        ch_load "sweep-ss-w$w" "harness/results/results-sweep-ss-w$w.parquet"
    done
    restore_weight
}

phase_analyze() {
    say "анализ: measured-regret по весам (оракул B4)"
    # A-ss под каждым весом + эталоны/default из sweep-ref в один датафрейм.
    "$PY" scripts/sweep-analyze.py --weights "$WEIGHTS" --ch-host "$CH_HOST" --ch-port "$CH_PORT"
}

case "${1:-all}" in
    ref)     phase_ref ;;
    ss)      phase_ss ;;
    analyze) phase_analyze ;;
    all)     phase_ref; phase_ss; phase_analyze ;;
    *) echo "использование: $0 ref|ss|analyze|all"; exit 2 ;;
esac
