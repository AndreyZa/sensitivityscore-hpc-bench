#!/bin/bash
# Статус-страница STAGE-прогона: (пере)запускает сервер с актуальным кодом.
# Зависимости (pandas/yaml) берутся из venv харнесса; kubectl смотрит на
# STAGE через KUBECONFIG. Сервер только читает — безопасно перезапускать
# в любой момент, идущий прогон не затрагивается.
#
# Все пути переопределяются переменными окружения — чтобы перенацелить
# страницу на другой прогон (добор эталонов, LLC-серия) одной строкой:
#   LOG=harness/stage-llc.log CONFIG=harness/config-stage-llc.yaml \
#   RESULTS=harness/results/results-stage-llc.parquet \
#   BASELINES=harness/results/baselines-stage-llc.parquet \
#   ./statusserver/run-stage.sh
cd "$(dirname "$0")/.." || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage}
LOG=${LOG:-harness/stage-pressure.log}
CONFIG=${CONFIG:-harness/config-stage.yaml}
RESULTS=${RESULTS:-harness/results/results-stage.parquet}
BASELINES=${BASELINES:-harness/results/baselines-stage.parquet}
REPORT=${REPORT:-analysis/report-stage}
SCOPE=${SCOPE:-full}
STAND=${STAND:-"STAGE (Timeweb k0s)"}
PORT=${PORT:-8787}
pkill -f "python -m statusserver" 2>/dev/null
sleep 0.5
nohup harness/.venv/bin/python -m statusserver \
    --log "$LOG" \
    --config "$CONFIG" \
    --results "$RESULTS" \
    --baselines "$BASELINES" \
    --report "$REPORT" \
    --scope "$SCOPE" \
    --stand "$STAND" \
    --port "$PORT" > statusserver/server.out 2>&1 &
# Ждём готовности циклом, а не фиксированной секундой. На прогретой машине
# сервер поднимается за ~0.5 с, но при ХОЛОДНОМ старте (свежий venv, pandas и
# pyarrow ещё не в page cache) импорт занимает несколько секунд — и проверка
# через sleep 1 объявляла «не поднялся», хотя сервер вставал сразу после.
# Врала она дважды: процесс запущен через nohup и продолжал жить, то есть
# страница по факту работала, а оператор видел WARN и шёл смотреть пустой
# server.out. Хуже всего это било в самом начале серии — когда машина как раз
# холоднее всего.
for _ in $(seq 1 40); do
    if curl -sf "http://localhost:$PORT/" > /dev/null 2>&1; then
        echo "OK: http://localhost:$PORT ($LOG)"
        exit 0
    fi
    sleep 0.5
done
echo "не поднялся за 20 с — см. statusserver/server.out"
tail -5 statusserver/server.out
exit 1
