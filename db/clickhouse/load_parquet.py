#!/usr/bin/env python3
"""load_parquet.py — залить results.parquet / baselines.parquet в ClickHouse.

Batch-load: харнесс пишет parquet как прежде, этот скрипт заливает файлы в
центральный ClickHouse (ПК-агрегатор), добавляя провенанс — какой стенд, какая
серия, из какого файла, когда залито. Прогон от доступности ClickHouse не
зависит; заливать можно с харнесс-хоста (если ПК доступен по сети) или локально
на самом ПК (скопировав туда parquet).

Схема таблиц — db/clickhouse/schema.sql (применить один раз, `make ch-schema`).

Примеры:
  # проверить парсинг без ClickHouse:
  python load_parquet.py --results ../../harness/results/results.parquet \\
      --stand stage --run-label 2026-07-14-io --dry-run

  # залить results + baselines:
  python load_parquet.py --host 192.168.1.50 --stand stage \\
      --run-label 2026-07-14-io \\
      --results ../../harness/results/results.parquet \\
      --baselines ../../harness/results/baselines.parquet
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Порядок колонок вставки = schema.sql минус ingested_at (у неё DEFAULT now()).
INSERT_COLUMNS = [
    "stand", "run_label", "config", "profile", "scenario", "overcommit", "rep",
    "batch_size", "batch_index", "node", "makespan_s", "makespan_source",
    "submit_ts", "start_ts", "end_ts", "llc_miss_rate", "numa_remote_ratio",
    "net_bw", "net_pressure", "io_iops", "io_pressure", "interference_chosen",
    "placement_regret", "sensitivity_llc", "sensitivity_numa", "sensitivity_net",
    "sensitivity_io", "approximation", "source_file",
    # Провенанс (harness/provenance.py); в parquet до его введения этих
    # колонок нет — добираются пустыми через _BACKFILL_DEFAULTS.
    "harness_commit", "config_sha256", "workload_image", "calibration", "score_weights",
    "profile_overrides", "storm_nodes",
]

# Колонки, которые обязаны быть в parquet (провенанс добавляем мы).
REQUIRED_PARQUET_COLUMNS = {
    "config", "profile", "scenario", "overcommit", "rep", "batch_size",
    "batch_index", "node", "makespan_s", "makespan_source", "submit_ts",
    "start_ts", "end_ts", "llc_miss_rate", "numa_remote_ratio", "net_bw",
    "net_pressure", "io_iops", "io_pressure", "interference_chosen",
    "placement_regret", "sensitivity_llc", "sensitivity_numa", "sensitivity_net",
    "sensitivity_io", "approximation",
    "harness_commit", "config_sha256", "workload_image", "calibration", "score_weights",
    "profile_overrides", "storm_nodes",
}

def _is_missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _f(x):
    """Nullable float: NaN/None -> None (чтобы avg() не считал неизмеренное)."""
    return None if _is_missing(x) else float(x)


def _s(x) -> str:
    """Строка, None/NaN -> '' (ключевые/диагностические поля не бывают NULL)."""
    return "" if _is_missing(x) else str(x)


def _ts(x):
    """Unix epoch float -> naive UTC datetime (ClickHouse DateTime64 = UTC)."""
    return None if _is_missing(x) else datetime.fromtimestamp(float(x), tz=timezone.utc).replace(tzinfo=None)


def _int(x, default: int) -> int:
    return default if _is_missing(x) else int(x)


# Дефолты для колонок, добавленных в схему позже (старые parquet их не имеют).
# Backfill вместо ошибки — чтобы залить весь исторический бэклог разных поколений
# схемы. float('nan') -> в coerce станет NULL; '' для строковых меток.
_BACKFILL_DEFAULTS = {
    "net_pressure": float("nan"),
    "interference_chosen": float("nan"),
    "placement_regret": float("nan"),
    "sensitivity_llc": "", "sensitivity_numa": "", "sensitivity_net": "", "sensitivity_io": "",
    "scenario": "",
    "batch_size": 1, "batch_index": 0,
    # Пустой провенанс = «серия снята до его введения, восстановить нельзя».
    "harness_commit": "", "config_sha256": "", "workload_image": "",
    "calibration": "", "score_weights": "", "profile_overrides": "", "storm_nodes": "",
}


def coerce_rows(df: pd.DataFrame, stand: str, run_label: str, source_file: str) -> list[list]:
    """DataFrame харнесса -> список строк в порядке INSERT_COLUMNS, с
    приведением типов и NaN->None для nullable-метрик. Недостающие колонки
    старых поколений схемы добираются дефолтами (backfill)."""
    missing = REQUIRED_PARQUET_COLUMNS - set(df.columns)
    if missing:
        unfillable = missing - _BACKFILL_DEFAULTS.keys()
        if unfillable:
            raise ValueError(f"{source_file}: нет колонок без дефолта: {sorted(unfillable)}")
        df = df.copy()
        for col in missing:
            df[col] = _BACKFILL_DEFAULTS[col]
        print(f"  backfill старой схемы: {sorted(missing)}")

    rows: list[list] = []
    for r in df.to_dict("records"):
        rows.append([
            stand,
            run_label,
            _s(r["config"]),
            _s(r["profile"]),
            _s(r["scenario"]),
            float(r["overcommit"]) if not _is_missing(r["overcommit"]) else 0.0,
            _int(r["rep"], 0),
            _int(r["batch_size"], 0),
            _int(r["batch_index"], -1),
            _s(r["node"]),
            _f(r["makespan_s"]),
            _s(r["makespan_source"]),
            _ts(r["submit_ts"]),
            _ts(r["start_ts"]),
            _ts(r["end_ts"]),
            _f(r["llc_miss_rate"]),
            _f(r["numa_remote_ratio"]),
            _f(r["net_bw"]),
            _f(r["net_pressure"]),
            _f(r["io_iops"]),
            _f(r["io_pressure"]),
            _f(r["interference_chosen"]),
            _f(r["placement_regret"]),
            _s(r["sensitivity_llc"]),
            _s(r["sensitivity_numa"]),
            _s(r["sensitivity_net"]),
            _s(r["sensitivity_io"]),
            _s(r["approximation"]),
            source_file,
            _s(r["harness_commit"]),
            _s(r["config_sha256"]),
            _s(r["workload_image"]),
            _s(r["calibration"]),
            _s(r["score_weights"]),
            _s(r["profile_overrides"]),
            _s(r["storm_nodes"]),
        ])
    return rows


def _guard_run_label(client, table: str, source_file: str, n_rows: int, args) -> None:
    """Не дать одной серии молча затереть другую под тем же run_label.

    ReplacingMergeTree схлопывает по ключу сортировки, а не по серии: если
    прогон перезалить с МЕНЬШИМ числом повторов под той же меткой, лишние
    строки прошлого прогона останутся и смешаются с новыми — SELECT ... FINAL
    вернёт химеру из двух серий, и по данным это не отличить.

    Повторная заливка того же файла с тем же числом строк безвредна (так
    работает долив второго приёмника после сбоя, make ch-load-all) — её
    пропускаем молча. Расхождение — это либо переиспользованная метка, либо
    перегнанная серия: останавливаемся и требуем решения человека.
    """
    if args.allow_existing:
        return
    try:
        rs = client.query(
            f"SELECT source_file, count() FROM {args.database}.{table} FINAL "
            "WHERE stand = {s:String} AND run_label = {l:String} GROUP BY source_file",
            parameters={"s": args.stand, "l": args.run_label},
        ).result_rows
    except Exception as e:
        # Таблицы ещё нет (первая заливка) — это норма. Но так же выглядела бы
        # и опечатка в самом запросе, а тихо отключившаяся защита хуже её
        # отсутствия: печатаем, чтобы поломка была видна.
        print(f"  ВНИМАНИЕ: проверка занятости run_label не выполнена ({e}) — заливаю без неё")
        return
    existing = {str(r[0]): int(r[1]) for r in rs}
    if not existing:
        return
    prev = existing.get(source_file)
    if prev == n_rows:
        return  # тот же файл того же размера — идемпотентный долив
    what = (f"тот же файл, но было {prev} строк, а заливается {n_rows}"
            if prev is not None
            else f"под этой меткой уже лежат другие файлы: {', '.join(sorted(existing))}")
    raise SystemExit(
        f"ERROR: stand={args.stand} run_label={args.run_label} в {table} уже занят — {what}.\n"
        f"       Перезаливка под той же меткой оставит строки прошлого прогона.\n"
        f"       Взять новую метку (--run-label), либо, если это осознанная\n"
        f"       перезаливка, сначала удалить старое:\n"
        f"         ALTER TABLE {args.database}.{table} DELETE "
        f"WHERE stand='{args.stand}' AND run_label='{args.run_label}'\n"
        f"       и повторить с --allow-existing."
    )


def _load_one(path: Path, table: str, args) -> int:
    df = pd.read_parquet(path)
    rows = coerce_rows(df, args.stand, args.run_label, path.name)
    n_missing = sum(1 for row in rows if str(row[27]).startswith(("missing", "error:")))
    print(f"[{path.name}] {len(rows)} строк -> {table} "
          f"(stand={args.stand} run_label={args.run_label}; {n_missing} без метрик/с ошибкой)")

    if args.dry_run:
        if rows:
            sample = dict(zip(INSERT_COLUMNS, rows[0]))
            print(f"  пример строки: config={sample['config']} scenario={sample['scenario']} "
                  f"node={sample['node']!r} makespan_s={sample['makespan_s']} "
                  f"llc={sample['llc_miss_rate']} submit_ts={sample['submit_ts']}")
        return len(rows)

    import clickhouse_connect  # импорт здесь: dry-run не требует клиента
    # Недоступный приёмник — штатная ситуация (ПК-агрегатор выключен, прод-порт
    # не проброшен), а не баг загрузчика: parquet на диске остаётся источником
    # истины, залить можно позже. Поэтому одна внятная строка вместо трейсбека.
    # OperationalError — уровень соединения (порт закрыт, туннель не поднят);
    # clickhouse_connect заворачивает в него в т.ч. ConnectionRefusedError,
    # поэтому ловим его, а не OSError.
    from clickhouse_connect.driver.exceptions import OperationalError
    try:
        client = clickhouse_connect.get_client(
            host=args.host, port=args.port, username=args.user,
            password=args.password, database=args.database,
        )
        _guard_run_label(client, table, path.name, len(rows), args)
        client.insert(table, rows, column_names=INSERT_COLUMNS)
    except OperationalError as e:
        raise SystemExit(f"ERROR: нет связи с ClickHouse {args.host}:{args.port} — {e}")
    except Exception as e:  # ошибки схемы/типов: приёмник ответил, но вставку не принял
        raise SystemExit(f"ERROR: ClickHouse {args.host}:{args.port} не принял вставку в {table} — {e}")
    print(f"  залито в {args.database}.{table}")
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path, help="results.parquet -> таблица results")
    p.add_argument("--baselines", type=Path, help="baselines.parquet -> таблица baselines")
    p.add_argument("--stand", required=True, help="метка стенда (stage / prod / ...)")
    p.add_argument("--run-label", required=True, help="метка серии (напр. 2026-07-14-llc)")
    p.add_argument("--host", default="localhost", help="ClickHouse host (default localhost)")
    p.add_argument("--port", type=int, default=8123, help="HTTP-порт ClickHouse (default 8123)")
    p.add_argument("--user", default="default")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="sensitivityscore")
    p.add_argument("--dry-run", action="store_true", help="прочитать+привести, не подключаясь к ClickHouse")
    p.add_argument("--allow-existing", action="store_true",
                   help="разрешить заливку поверх занятой пары (stand, run_label)")
    args = p.parse_args()

    if not args.results and not args.baselines:
        p.error("укажи хотя бы --results или --baselines")

    total = 0
    for path, table in ((args.results, "results"), (args.baselines, "baselines")):
        if not path:
            continue
        if not path.exists():
            print(f"WARNING: файл не найден, пропуск: {path}", file=sys.stderr)
            continue
        total += _load_one(path, table, args)

    print(f"итого строк: {total}{' (dry-run, ничего не залито)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
