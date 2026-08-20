#!/bin/bash
# Фаза P2 энергостатьи: вклад размещения в Дж/задача при всех узлах
# включённых. Дизайн, плечи и конвейер после прогона — в шапке
# config-prod-p2-energy.yaml. Запуск:
#   STAND=prod make series SERIES=p2-energy                    # все уровни
#   SCENARIOS=feed-mid STAND=prod make series SERIES=p2-energy # один уровень
#
# ГАШЕНИЕ ЗДЕСЬ ВЫКЛЮЧЕНО НАМЕРЕННО: P2 изолирует размещение, вклад
# гашения меряется отдельно в P3. Контроллер scripts/power-save.py в этой
# серии НЕ запускается — если он остался работать с прошлого прогона,
# серия измерит смесь двух политик. Проверка ниже это ловит.
set -x
cd "$(dirname "$0")" || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/prod} REDIS_ADDR=localhost:16379

if pgrep -f "power-save.py" >/dev/null; then
    echo "ОСТАНОВЛЕНО: работает scripts/power-save.py — P2 меряет только"
    echo "размещение, гашение должно быть выключено (P3 — отдельная фаза)."
    exit 1
fi

../scripts/run-series.sh page p2-energy || true

# Дозы жертв — те же, что в калибровочной серии v2: профили сравнимы между
# фазами только при одинаковых дозах, иначе Дж/задача P2 не сопоставима с
# замедлением, измеренным там же.
export HARNESS_OVERRIDE_ML_INFERENCE_CPU=28 \
       HARNESS_OVERRIDE_ML_INFERENCE_THREADS=28 \
       HARNESS_OVERRIDE_ML_INFERENCE_N_INFER=40000 \
       HARNESS_OVERRIDE_ML_INFERENCE_MEM_REQ=16Gi \
       HARNESS_OVERRIDE_ML_INFERENCE_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_CPU=28 HARNESS_OVERRIDE_HIGH_S_IO_THREADS=28 \
       HARNESS_OVERRIDE_HIGH_S_IO_PRIMARIES=60000000 \
       HARNESS_OVERRIDE_HIGH_S_IO_MEM_REQ=16Gi HARNESS_OVERRIDE_HIGH_S_IO_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_HIGH_S_IO_OUTPUT_MODE=blocking \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_BURST_MB=256 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_INTERVAL_SECONDS=0 \
       HARNESS_OVERRIDE_HIGH_S_IO_IO_TOTAL_BURSTS=16 \
       HARNESS_OVERRIDE_HIGH_S_NET_CPU=28 HARNESS_OVERRIDE_HIGH_S_NET_THREADS=28 \
       HARNESS_OVERRIDE_HIGH_S_NET_PRIMARIES=60000000 \
       HARNESS_OVERRIDE_HIGH_S_NET_MEM_REQ=16Gi HARNESS_OVERRIDE_HIGH_S_NET_MEM_LIM=16Gi \
       HARNESS_OVERRIDE_HIGH_S_NET_OUTPUT_MODE=stream \
       HARNESS_OVERRIDE_HIGH_S_NET_NET_TOTAL_MB=8000

# Эталоны P2 не нужны: Дж/задача — абсолютная величина, нормировать её не
# на что, а замедление берётся из калибровочной серии. SKIP_BASELINE=0
# оставлен как ручка на случай, если понадобится сверка профилей.
if [ "${SKIP_BASELINE:-1}" != "1" ]; then
    echo "=== BASELINE START $(date +%H:%M:%S) epoch=$(date +%s) ==="
    .venv/bin/python run_experiment.py --config config-prod-p2-energy.yaml --baseline
    echo "=== BASELINE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$? ==="
fi

SCEN=${SCENARIOS:-feed-low,feed-mid,feed-high}
echo "=== PRESSURE START $(date +%H:%M:%S) epoch=$(date +%s) сценарии=$SCEN ==="
.venv/bin/python run_experiment.py --config config-prod-p2-energy.yaml \
    --pressure --scenarios "$SCEN"
rc=$?
echo "=== PRESSURE DONE $(date +%H:%M:%S) epoch=$(date +%s) rc=$rc ==="

# Окна энергии — сразу, пока Prometheus точно помнит период; расчёт
# отдельной командой (см. шапку конфига).
if [ "$rc" -eq 0 ]; then
    ../scripts/energy-windows-per-arm.py --stand prod --run-label p2-energy \
        --prom "${PROM_URL:-http://localhost:19090}" || \
        echo "ВНИМАНИЕ: окна не записаны — данные серии целы, повторить руками"
fi
exit $rc
