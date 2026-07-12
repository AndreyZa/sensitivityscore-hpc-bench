"""load.py — loads harness output (results/results.parquet, schema per §5.1) into
a pandas DataFrame with basic sanity checks."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

EXPECTED_COLUMNS = {
    "config",
    "profile",
    "overcommit",
    "rep",
    "node",
    "makespan_s",
    "makespan_source",
    "submit_ts",
    "start_ts",
    "end_ts",
    "llc_miss_rate",
    "numa_remote_ratio",
    "net_bw",
    "io_iops",
    "io_pressure",
    "approximation",
    "scenario",
    # Качество решения планировщика по снапшоту node:metrics на момент
    # сабмита (harness/submit/node_pressure.py).
    "interference_chosen",
    "placement_regret",
    # Заявленный S-вектор профиля — для fingerprint-таблицы (fingerprint()).
    "sensitivity_llc",
    "sensitivity_numa",
    "sensitivity_net",
    "sensitivity_io",
}


def load_results(path: str | Path) -> pd.DataFrame:
    df = pd.read_parquet(path)

    missing = EXPECTED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"results file {path} is missing expected columns: {missing}")

    n_missing_metrics = (df["approximation"] == "missing").sum()
    n_errors = df["approximation"].astype(str).str.startswith("error:").sum()
    if n_missing_metrics or n_errors:
        print(
            f"[load_results] warning: {n_missing_metrics} rows with no agent metrics, "
            f"{n_errors} rows with submission errors — check results before drawing conclusions."
        )

    return df


def filter_valid(df: pd.DataFrame, allow_synthetic: bool = False) -> pd.DataFrame:
    """Drops rows with submission errors or missing makespan — keeps rows with
    approximation="host-side" (config B), since that's a documented, expected
    approximation rather than a failure (docs §3.3).

    approximation="synthetic-devbox" rows (fake LLC values from the local-dev
    PMU fallback, see metrics-agent pkg/perf/synthetic.go) are dropped unless
    allow_synthetic=True: they must never blend into dissertation results.
    The flag exists only for exercising the analysis pipeline end-to-end on a
    dev box where ALL data is synthetic anyway."""
    valid = df[~df["approximation"].astype(str).str.startswith("error:")]
    valid = valid[valid["makespan_s"].notna()]

    synthetic = valid["approximation"] == "synthetic-devbox"
    n_synthetic = int(synthetic.sum())
    if n_synthetic and not allow_synthetic:
        print(
            f"[filter_valid] dropping {n_synthetic} synthetic-devbox rows "
            "(local-dev PMU fallback, not real measurements); pass "
            "--allow-synthetic to analyze.py to keep them for pipeline testing."
        )
        valid = valid[~synthetic]
    elif n_synthetic:
        print(
            f"[filter_valid] WARNING: keeping {n_synthetic} synthetic-devbox rows "
            "— pipeline-testing mode, NOT dissertation data."
        )
    return valid


def isolated_makespans(baselines: pd.DataFrame) -> pd.Series:
    """profile -> медианный соло-makespan из baselines.parquet (харнесс
    --baseline). Медиана, не среднее: один шумный соло-прогон (например,
    первый — с холодным image pull на ноде) не должен сдвигать знаменатель
    всех slowdown этого профиля."""
    solo = baselines[baselines["scenario"] == "baseline"]
    if solo.empty:
        raise ValueError(
            "baselines file has no scenario='baseline' rows — was it produced "
            "by run_experiment.py --baseline?"
        )
    return solo.groupby("profile")["makespan_s"].median()


def attach_slowdown(df: pd.DataFrame, baselines: pd.DataFrame) -> pd.DataFrame:
    """Добавляет makespan_isolated и slowdown = makespan_s / makespan_isolated.

    Slowdown — стандартная метрика литературы по интерференции (Bubble-Up,
    Paragon, Heracles): безразмерная, профили разной длительности становятся
    сравнимыми и объединяемыми, а «замедлился в 1.8x» читается напрямую.
    Профили без бейзлайна получают NaN (и предупреждение) — сравнения по
    slowdown их молча не потеряют: stats отбрасывает NaN с падением n.

    Оговорка: знаменатель — на профиль, но НЕ на инфраструктуру (бейзлайны
    гоняются одной конфигурацией, обычно bare-metal A-default). Для сравнений
    внутри одной инфраструктуры (H1: A vs A, H2: B vs B) делитель у обоих плеч
    общий, поэтому Mann-Whitney/Cliff's на slowdown идентичны тем же на сыром
    makespan — вывод не меняется. Для кросс-инфра ЧТЕНИЯ абсолютного slowdown
    у B/C/D оверхед инфраструктуры подмешивается к замедлению от интерференции;
    это надо держать в уме, интерпретируя абсолютные значения (не сравнения)."""
    iso = isolated_makespans(baselines)
    out = df.copy()
    out["makespan_isolated"] = out["profile"].map(iso)
    out["slowdown"] = out["makespan_s"] / out["makespan_isolated"]

    missing = sorted(set(out["profile"].dropna()) - set(iso.index))
    if missing:
        print(
            f"[attach_slowdown] warning: no baseline for profiles {missing} — "
            "their slowdown is NaN (run harness --baseline covering them)."
        )
    return out
