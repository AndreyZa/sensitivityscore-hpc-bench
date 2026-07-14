"""clickhouse_source.py — читать результаты из ClickHouse-агрегатора вместо
parquet, возвращая DataFrame той же формы, что load.load_results (§5.1).

Пишем в 2 места (локальный parquet + ClickHouse), но отчёты можно строить из
любого источника. Провенансные колонки (stand/run_label/...) в CH лишние для
анализа — не выбираем их; timestamps приводим к epoch float, как в parquet
(этого ждёт calibrate_axis_costs.py: start_ts/end_ts как float).

Доступ — по SSH-туннелю к ПК-агрегатору (см. `make ch-tunnel`), поэтому host
по умолчанию localhost.
"""

from __future__ import annotations

import pandas as pd

# Колонки схемы харнесса (§5.1); timestamps -> epoch float, как в parquet.
_SELECT = """
    config, profile, overcommit, rep, node, makespan_s, makespan_source,
    toUnixTimestamp64Milli(submit_ts) / 1000.0 AS submit_ts,
    toUnixTimestamp64Milli(start_ts)  / 1000.0 AS start_ts,
    toUnixTimestamp64Milli(end_ts)    / 1000.0 AS end_ts,
    llc_miss_rate, numa_remote_ratio, net_bw, net_pressure, io_iops, io_pressure,
    approximation, scenario, batch_size, batch_index,
    interference_chosen, placement_regret,
    sensitivity_llc, sensitivity_numa, sensitivity_net, sensitivity_io
"""

_FLOAT_COLS = [
    "overcommit", "makespan_s", "submit_ts", "start_ts", "end_ts",
    "llc_miss_rate", "numa_remote_ratio", "net_bw", "net_pressure",
    "io_iops", "io_pressure", "interference_chosen", "placement_regret",
]
_STR_COLS = [
    "config", "profile", "node", "makespan_source", "approximation", "scenario",
    "sensitivity_llc", "sensitivity_numa", "sensitivity_net", "sensitivity_io",
]


def load_from_clickhouse(
    table: str,
    *,
    host: str = "localhost",
    port: int = 8123,
    database: str = "sensitivityscore",
    user: str = "default",
    password: str = "",
    stand: str | None = None,
    run_labels: list[str] | None = None,
) -> pd.DataFrame:
    """SELECT из results/baselines с FINAL (схлопнуть версии ReplacingMergeTree)
    и опциональным фильтром по stand / run_label. Возвращает DataFrame,
    совместимый с load.load_results (те же колонки и dtypes, что у parquet)."""
    import clickhouse_connect

    if table not in ("results", "baselines"):
        raise ValueError(f"table должно быть results|baselines, не {table!r}")

    client = clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password, database=database,
    )
    where, params = [], {}
    if stand:
        where.append("stand = %(stand)s")
        params["stand"] = stand
    if run_labels:
        where.append("run_label IN %(labels)s")
        params["labels"] = run_labels
    sql = f"SELECT {_SELECT} FROM {table} FINAL"
    if where:
        sql += " WHERE " + " AND ".join(where)

    df = client.query_df(sql, parameters=params)

    # Привести dtypes к parquet-совместимым: числовые -> float с NaN (не pd.NA),
    # строковые -> object; иначе filter_valid/stats могут споткнуться на
    # nullable-расширениях clickhouse-connect.
    for c in _FLOAT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("rep", "batch_size", "batch_index"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    for c in _STR_COLS:
        df[c] = df[c].astype("object")
    return df
