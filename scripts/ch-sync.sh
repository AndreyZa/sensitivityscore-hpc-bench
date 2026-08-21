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

# Кампанийные таблицы сверяются ПО МЕТКАМ СЕРИЙ, а не по времени вставки.
# Время вставки здесь негодный признак: в приёмник историю грузили пачками
# и в другом порядке, поэтому его max перепрыгивает через строки прода, и
# «к переносу 0» означало бы не совпадение, а слепоту.
for tbl in results baselines energy_windows; do
    n_src=$(src -q "SELECT count() FROM $DB.$tbl" 2>/dev/null | tr -d '\r')
    n_dst=$(dst "$POD" -- clickhouse-client -q "SELECT count() FROM $DB.$tbl" 2>/dev/null | tr -d '\r')
    # метки, где в приёмнике строк меньше, чем в источнике
    missing=$(src -q "SELECT DISTINCT concat(stand, '\t', run_label) FROM $DB.$tbl FORMAT TSV" 2>/dev/null)
    to_copy=""
    while IFS=$'\t' read -r st lb; do
        [ -n "${st:-}" ] || continue
        a=$(src -q "SELECT count() FROM $DB.$tbl WHERE stand='$st' AND run_label='$lb'" | tr -d '\r')
        b=$(dst "$POD" -- clickhouse-client -q "SELECT count() FROM $DB.$tbl FINAL WHERE stand='$st' AND run_label='$lb'" 2>/dev/null | tr -d '\r')
        [ "${a:-0}" -gt "${b:-0}" ] 2>/dev/null && to_copy="$to_copy $st/$lb"
    done <<< "$missing"

    printf '%-16s источник %-8s приёмник %-8s расходятся:%s\n' \
        "$tbl" "${n_src:-?}" "${n_dst:-?}" "${to_copy:- нет}"
    [ "$DRY_RUN" = "1" ] && continue
    for pair in $to_copy; do
        st=${pair%%/*}; lb=${pair##*/}
        if src -q "SELECT * FROM $DB.$tbl WHERE stand='$st' AND run_label='$lb' FORMAT Native" \
            | dst -i "$POD" -- clickhouse-client -q "INSERT INTO $DB.$tbl FORMAT Native"; then
            echo "    перенесено: $st/$lb"
        else
            echo "    ОШИБКА переноса $tbl $st/$lb" >&2; rc=1
        fi
    done
done

# Ряды метрик — по ВРЕМЕНИ ДАННЫХ (ts), а не вставки: это поток, у него
# нет меток серий, и догружать надо ровно то, чего в приёмнике ещё нет.
for st in $(src -q "SELECT DISTINCT stand FROM $DB.metrics_samples FORMAT TSV" | tr -d '\r'); do
    mark=$(dst "$POD" -- clickhouse-client -q \
        "SELECT ifNull(toString(max(ts)), '1970-01-01 00:00:00') FROM $DB.metrics_samples WHERE stand='$st'" 2>/dev/null | tr -d '\r')
    pending=$(src -q "SELECT count() FROM $DB.metrics_samples WHERE stand='$st' AND ts > '$mark'" | tr -d '\r')
    printf '%-16s стенд %-6s приёмник до %s, к переносу %s\n' "metrics_samples" "$st" "$mark" "${pending:-?}"
    [ "$DRY_RUN" = "1" ] && continue
    [ "${pending:-0}" -gt 0 ] 2>/dev/null || continue
    if src -q "SELECT * FROM $DB.metrics_samples WHERE stand='$st' AND ts > '$mark' FORMAT Native" \
        | dst -i "$POD" -- clickhouse-client -q "INSERT INTO $DB.metrics_samples FORMAT Native"; then
        echo "    перенесено: $pending"
    else
        echo "    ОШИБКА переноса metrics_samples $st" >&2; rc=1
    fi
done

exit $rc
