#!/usr/bin/env bash
# energy-windows-from-log.sh — энергия окон серии в ClickHouse одной командой.
#
#   ./scripts/energy-windows-from-log.sh harness/prod-mixed-calib.log prod prod-mixed-calib
#
# Парсит маркеры фаз из лога серии (их пишет run-<стенд>-<серия>.sh):
#   === BASELINE START ... epoch=NNN ===   /  === BASELINE DONE ... epoch=NNN ===
#   === PRESSURE START ... epoch=NNN ===   /  === PRESSURE DONE ... epoch=NNN ===
# и для каждого найденного окна снимает энергию узлов РАЗНОСТЬЮ накопительных
# RAPL-счётчиков (scripts/energy-window.py) в оба приёмника: in-cluster CH
# (:8124, make ch-forward) и домашний агрегатор (:8123 — на .72 это локальный
# CH). Источники: rapl-pkg (сумма package-доменов) и rapl-dram.
#
# PILOT-маркеры без epoch= пропускаются с предупреждением — пилот короткий и
# энергетике не интересен. Prometheus берётся из PROM_URL (по умолчанию
# localhost:19090 — разовый `kubectl port-forward svc/prometheus 19090:9090`
# в ns мониторинга).
#
# Требование методики: точка правды — разность регистра на границах, поэтому
# ретеншн Prometheus должен покрывать окно (на проде 365d — покрывает).
set -euo pipefail

LOG=${1:-}; STAND=${2:-}; RUN_LABEL=${3:-}
[ -n "$LOG" ] && [ -n "$STAND" ] && [ -n "$RUN_LABEL" ] || {
    echo "использование: $0 <лог серии> <стенд> <run_label>"; exit 2; }
[ -f "$LOG" ] || { echo "нет лога: $LOG"; exit 1; }

PROM_URL=${PROM_URL:-http://localhost:19090}
# DRY_RUN=1 — печатать строки, в ClickHouse не писать (прогон обвязки).
DRY=(); [ "${DRY_RUN:-0}" = "1" ] && DRY=(--dry-run)
HERE="$(cd "$(dirname "$0")" && pwd)"

curl -sf -m 5 "$PROM_URL/-/healthy" >/dev/null || {
    echo "Prometheus недоступен на $PROM_URL — поднять форвард:"
    echo "  kubectl -n sensitivityscore-monitoring port-forward svc/prometheus 19090:9090 &"
    exit 1; }

# Единственная пара START/DONE на фазу: харнесс пишет их по разу за сессию;
# при рестартах серии берём ПОСЛЕДНЮЮ пару (tail -1) и говорим об этом.
epoch_of() {  # epoch_of <BASELINE|PRESSURE> <START|DONE>
    grep -E "=== $1 $2 .*epoch=[0-9]+" "$LOG" | tail -1 \
        | sed -E "s/.*epoch=([0-9]+).*/\1/"
}

rc=0
windows=0
for PHASE in BASELINE PRESSURE; do
    T0=$(epoch_of "$PHASE" START || true)
    T1=$(epoch_of "$PHASE" DONE || true)
    WINDOW=$(echo "$PHASE" | tr "[:upper:]" "[:lower:]")
    if [ -z "$T0" ] || [ -z "$T1" ]; then
        echo "-- $PHASE: маркеров с epoch нет (PILOT?) — пропуск"
        continue
    fi
    echo "== окно $WINDOW: $T0 .. $T1 ($(( (T1-T0)/60 )) мин) =="
    windows=$((windows + 1))
    for SRC in rapl-pkg rapl-dram; do
        case "$SRC" in
            rapl-pkg)  METRIC='sum by (node) (ss_node_rapl_joules_total{domain=~"package-.*"})' ;;
            rapl-dram) METRIC='sum by (node) (ss_node_rapl_joules_total{domain="dram"})' ;;
        esac
        for PORT in 8124 8123; do
            python3 "$HERE/energy-window.py" \
                --prom "$PROM_URL" --metric "$METRIC" --node-label node \
                --t0 "$T0" --t1 "$T1" --factor 1 \
                --source "$SRC" --window "$WINDOW" \
                --stand "$STAND" --run-label "$RUN_LABEL" \
                --ch-host localhost --ch-port "$PORT" "${DRY[@]}" || rc=1
        done
    done
done
if [ "$windows" -eq 0 ]; then
    echo "ни одного окна с epoch-маркерами в $LOG — НИЧЕГО не записано"
    exit 1
fi
[ $rc -eq 0 ] && echo "готово: $windows окна(о) в оба приёмника (проверка: SELECT * FROM sensitivityscore.energy_windows WHERE run_label='$RUN_LABEL')"
exit $rc
