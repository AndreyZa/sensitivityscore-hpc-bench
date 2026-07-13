#!/bin/bash
# Статус-страница STAGE-прогона: (пере)запускает сервер с актуальным кодом.
# Зависимости (pandas/yaml) берутся из venv харнесса; kubectl смотрит на
# STAGE через KUBECONFIG. Сервер только читает — безопасно перезапускать
# в любой момент, идущий прогон не затрагивается.
cd "$(dirname "$0")/.." || exit 1
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/timeweb-stage}
pkill -f "python -m statusserver" 2>/dev/null
sleep 0.5
nohup harness/.venv/bin/python -m statusserver \
    --log harness/stage-pressure.log \
    --config harness/config-stage.yaml \
    --results harness/results/results-stage.parquet \
    --baselines harness/results/baselines-stage.parquet \
    --report analysis/report-stage \
    --stand "STAGE (Timeweb k0s)" \
    --port 8787 > statusserver/server.out 2>&1 &
sleep 1
if curl -sf http://localhost:8787/ > /dev/null; then
    echo "OK: http://localhost:8787"
else
    echo "не поднялся — см. statusserver/server.out"
    tail -5 statusserver/server.out
    exit 1
fi
