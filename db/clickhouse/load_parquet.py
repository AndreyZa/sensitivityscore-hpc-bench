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
]

# Колонки, которые обязаны быть в parquet (провенанс добавляем мы).
REQUIRED_PARQUET_COLUMNS = {
    "config", "profile", "scenario", "overcommit", "rep", "batch_size",
    "batch_index", "node", "makespan_s", "makespan_source", "submit_ts",
    "start_ts", "end_ts", "llc_miss_rate", "numa_remote_ratio", "net_bw",
    "net_pressure", "io_iops", "io_pressure", "interference_chosen",
    "placement_regret", "sensitivity_llc", "sensitivity_numa", "sensitivity_net",
    "sensitivity_io", "approximation",
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


def coerce_rows(df: pd.DataFrame, stand: str, run_label: str, source_file: str) -> list[list]:
    """DataFrame харнесса -> список строк в порядке INSERT_COLUMNS, с
    приведением типов и NaN->None для nullable-метрик."""
    missing = REQUIRED_PARQUET_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"{source_file}: в parquet нет колонок: {sorted(missing)}")

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
        ])
    return rows


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
    client = clickhouse_connect.get_client(
        host=args.host, port=args.port, username=args.user,
        password=args.password, database=args.database,
    )
    client.insert(table, rows, column_names=INSERT_COLUMNS)
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
