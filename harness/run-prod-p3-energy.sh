#!/bin/bash
# Фаза P3: полный цикл с гашением. Дизайн, плечи и конвейер после прогона
# — в шапке config-prod-p3-energy.yaml. Запуск:
#   STAND=prod make series SERIES=p3-energy                 # порог 480 с
#   SUSPEND_TIME=240 STAND=prod make series SERIES=p3-energy # точки свипа
#   SUSPEND_TIME=900 STAND=prod make series SERIES=p3-energy
# Свип порога — главная ось фазы: из него выходит кривая «экономия против
# добавленного ожидания», то есть само правило эксплуатации.
#
# ДВА ПРОХОДА ПОД ОДНОЙ МЕТКОЙ. Гашение — внешний контроллер с
# состоянием, а не вариант планировщика, поэтому включить его на два
# плеча из трёх внутри одного прогона харнесса нельзя.
#   проход 1: A-peaks                        контроллер НЕ запущен
#   проход 2: A-peaks-gash                   контроллер работает
# Проходы задаются ДВУМЯ конфигами (второй наследует первый и меняет два
# ключа), а не флагом: ограничивать набор плеч из командной строки харнесс
# не умеет, а results-файл переписывает с нуля — один файл на два прохода
# затёрся бы. Оба файла грузятся в ClickHouse под одной меткой.
# Внутри второго прохода плечи чередуются как обычно, поэтому вклад
# размещения при гашении сравнивается парно. Вклад самого гашения —
# сравнение блоками, и это честно сказано в §9 статьи.
set -x
cd "$(dirname "$0")" || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/prod} REDIS_ADDR=localhost:16379

SUSPEND_TIME=${SUSPEND_TIME:-480}   # 2·T по измерению 20.08 (T ≈ 4 мин)
IDRAC_MAP=${IDRAC_MAP:-wrk-b6=10.21.200.106,wrk-b7=10.21.200.107,wrk-b8=10.21.200.108}
RUN_LABEL=p3-energy
CTL_LOG=/tmp/p3-power-save.log

if pgrep -f "power-save.py" >/dev/null; then
    echo "ОСТАНОВЛЕНО: контроллер гашения уже работает — первый проход"
    echo "обязан идти БЕЗ него, иначе плечо A-peaks измерит не то."
    exit 1
fi

../scripts/run-series.sh page p3-energy || true

# Дозы жертв — те же, что в P2 и в калибровочной серии: иначе Дж/задача
# фаз несопоставимы между собой.
export HARNESS_OVERRIDE_ML_INFERENCE_CPU=28 \
       HARNESS_OVERRIDE_ML_INFERENCE_THREADS=28 \
       HARNESS_OVERRIDE_ML_INFERENCE_N_INFER=40000 \
       HARNESS_OVERRIDE_ML_INFERENCE_MEM_REQ=16Gi \
       HARNESS_OVERRIDE_ML_INFERENCE_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_CPU=28 HARNESS_OVERRIDE_HIGH_S_IO_THREADS=28 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=60000000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=16Gi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_HIGH_S_NET_CPU=28 HARNESS_OVERRIDE_HIGH_S_NET_THREADS=28 \
       HARNESS_OVERRIDE_HIGH_S_NET_PRIMARIES=60000000 \
       HARNESS_OVERRIDE_HIGH_S_NET_MEM_REQ=16Gi HARNESS_OVERRIDE_HIGH_S_NET_MEM_LIM=16Gi

echo "=== PRESSURE START $(date +%H:%M:%S) epoch=$(date +%s) сценарии=sparse ==="

# ---- проход 1: без гашения ------------------------------------------------
.venv/bin/python run_experiment.py --config config-prod-p3-energy.yaml \
    --pressure --scenarios sparse
rc1=$?

# ---- проход 2: контроллер гашения работает --------------------------------
# --once не годится: политика обязана жить весь проход. Порт-форвард к
# ClickHouse нужен контроллеру для окон перехода (цена цикла, Э3.3).
nohup kubectl -n sensitivityscore-system port-forward svc/clickhouse 8124:8123 \
    >/tmp/p3-pf-ch.log 2>&1 &
PF=$!
nohup kubectl -n sensitivityscore-monitoring port-forward svc/prometheus 19090:9090 \
    >/tmp/p3-pf-prom.log 2>&1 &
PFP=$!
sleep 6

nohup python3 -u ../scripts/power-save.py \
    --executor redfish --idrac-map "$IDRAC_MAP" \
    --suspend-time "$SUSPEND_TIME" --resume-timeout 600 --min-active 1 \
    --interval 30 --record-windows --run-label "$RUN_LABEL" \
    --prom http://localhost:19090 --ch-port 8124 \
    > "$CTL_LOG" 2>&1 &
CTL=$!
sleep 5
kill -0 $CTL 2>/dev/null || { echo "контроллер не поднялся, см. $CTL_LOG"; cat "$CTL_LOG"; exit 1; }
echo "контроллер гашения: pid $CTL, порог ${SUSPEND_TIME} c, лог $CTL_LOG"

.venv/bin/python run_experiment.py --config config-prod-p3-energy-gash.yaml \
    --pressure --scenarios sparse
rc2=$?

# Контроллер снимается ВСЕГДА, включая падение прохода: оставленный
# контроллер продолжит гасить узлы уже вне измерения.
kill $CTL 2>/dev/null; wait $CTL 2>/dev/null
kill $PF $PFP 2>/dev/null

# Ни один узел не должен остаться выключенным или закордоненным.
for n in $(kubectl get nodes -l node-role.kubernetes.io/bench -o name); do
    kubectl uncordon "${n#node/}" 2>/dev/null
done
kubectl get nodes -l node-role.kubernetes.io/bench

echo "=== PRESSURE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$((rc1 | rc2)) ==="
exit $((rc1 | rc2))
