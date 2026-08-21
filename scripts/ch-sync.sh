#!/bin/bash
# Зеркалирование таблиц ClickHouse: прод -> агрегатор (лаба/дом).
#
# ЗАЧЕМ ОТДЕЛЬНЫМ ШАГОМ. У результатов многоприёмниковый путь есть —
# ch-load-all умеет CH_SINKS="prod home". У ОКОН ЭНЕРГИИ его нет вовсе:
# energy-window.py пишет туда, куда указали --ch-host, и всё. Поэтому
# 21.08.2026 в агрегаторе лежали 42 окна против 411 в проде — вся
# лестница P1, окна плеч P2 и циклы гашения существовали в одном
# экземпляре, то есть потеря прод-базы стоила бы их целиком.
#
# Перенос инкрементальный: берём строки, которые новее последней
# записанной в приёмнике. Таблицы ReplacingMergeTree, поэтому повторный
# прогон безвреден — дубликаты схлопнутся при слиянии, — но без водяного
# знака мы бы каждый раз гоняли всю историю.
#
#   scripts/ch-sync.sh                 # прод -> лаба (умолчание)
#   DRY_RUN=1 scripts/ch-sync.sh       # только показать расхождение
set -u
SRC_KUBECONFIG=${SRC_KUBECONFIG:-$HOME/.kube/configs/prod}
DST_KUBECONFIG=${DST_KUBECONFIG:-$HOME/.kube/configs/local72.yaml}
NS=${NS:-sensitivityscore-system}
POD=${POD:-clickhouse-0}
DB=${DB:-sensitivityscore}
DRY_RUN=${DRY_RUN:-0}

src() { kubectl --kubeconfig "$SRC_KUBECONFIG" -n "$NS" exec "$POD" -- clickhouse-client "$@"; }
dst() { kubectl --kubeconfig "$DST_KUBECONFIG" -n "$NS" exec "$@"; }

rc=0
for pair in "results:ingested_at" "baselines:ingested_at" \
            "energy_windows:inserted_at" "metrics_samples:inserted_at"; do
    tbl=${pair%%:*}; col=${pair##*:}
    n_src=$(src -q "SELECT count() FROM $DB.$tbl" 2>/dev/null | tr -d '\r')
    n_dst=$(dst "$POD" -- clickhouse-client -q "SELECT count() FROM $DB.$tbl" 2>/dev/null | tr -d '\r')
    mark=$(dst "$POD" -- clickhouse-client -q \
        "SELECT ifNull(toString(max($col)), '1970-01-01 00:00:00') FROM $DB.$tbl" 2>/dev/null | tr -d '\r')
    pending=$(src -q "SELECT count() FROM $DB.$tbl WHERE $col > '$mark'" 2>/dev/null | tr -d '\r')

    printf '%-16s источник %-8s приёмник %-8s к переносу %s\n' \
        "$tbl" "${n_src:-?}" "${n_dst:-?}" "${pending:-?}"
    [ "$DRY_RUN" = "1" ] && continue
    [ "${pending:-0}" -gt 0 ] 2>/dev/null || continue

    if src -q "SELECT * FROM $DB.$tbl WHERE $col > '$mark' FORMAT Native" \
        | dst -i "$POD" -- clickhouse-client -q "INSERT INTO $DB.$tbl FORMAT Native"; then
        echo "    перенесено: $pending"
    else
        echo "    ОШИБКА переноса $tbl" >&2
        rc=1
    fi
done
exit $rc
