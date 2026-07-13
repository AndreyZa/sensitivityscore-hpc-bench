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


# Columns whose values come from the PMU (LLC + NUMA). On a stand without a
# usable PMU the agent fills these from the synthetic host-CPU fallback — real
# only in the io_pressure / makespan sense, never in the LLC/NUMA sense.
_PMU_COLUMNS = ["llc_miss_rate", "numa_remote_ratio"]


def filter_valid(
    df: pd.DataFrame,
    allow_synthetic: bool = False,
    pmu_less_stand: bool = False,
) -> pd.DataFrame:
    """Drops rows with submission errors or missing makespan — keeps rows with
    approximation="host-side" (config B), since that's a documented, expected
    approximation rather than a failure (docs §3.3).

    approximation="synthetic-devbox" rows carry fake LLC/NUMA values from the
    PMU fallback (metrics-agent pkg/perf/synthetic.go). Handling, in order:

    - pmu_less_stand=True: this is a REAL stand that simply has no usable PMU
      (e.g. cloud VMs where perf_event_open returns EINVAL — the STAGE Timeweb
      cluster). io_pressure / makespan / placement_regret ARE real there; only
      LLC/NUMA are synthetic. Keep the rows but NULL the PMU columns so the
      synthetic host-CPU numbers can't be misread as real cache/NUMA data.
      Score on io only (weights.json) and read the IO-axis results.
    - allow_synthetic=True: keep everything as-is — for exercising the pipeline
      on a fully-synthetic dev box; NOT dissertation data.
    - default: drop synthetic-devbox rows entirely (they must never blend into
      results silently)."""
    valid = df[~df["approximation"].astype(str).str.startswith("error:")]
    valid = valid[valid["makespan_s"].notna()]

    synthetic = valid["approximation"] == "synthetic-devbox"
    n_synthetic = int(synthetic.sum())
    if not n_synthetic:
        return valid

    if pmu_less_stand:
        print(
            f"[filter_valid] PMU-less stand: keeping {n_synthetic} rows but "
            "nulling LLC/NUMA (synthetic without a PMU); io_pressure/makespan "
            "are real. Score/analyze the IO axis only."
        )
        valid = valid.copy()
        present = [c for c in _PMU_COLUMNS if c in valid.columns]
        valid.loc[synthetic, present] = float("nan")
    elif allow_synthetic:
        print(
            f"[filter_valid] WARNING: keeping {n_synthetic} synthetic-devbox rows "
            "— pipeline-testing mode, NOT dissertation data."
        )
    else:
        print(
            f"[filter_valid] dropping {n_synthetic} synthetic-devbox rows "
            "(PMU fallback, not real measurements); pass --allow-synthetic "
            "(pipeline testing) or --pmu-less-stand (real stand w/o PMU) to keep."
        )
        valid = valid[~synthetic]
    return valid


def isolated_makespans(baselines: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """-> (per_node, per_profile): медианные соло-makespan из baselines.parquet
    (харнесс --baseline). per_node индексирован (profile, node) — основной
    знаменатель slowdown; per_profile — fallback для строк на нодах без своего
    бейзлайна. Медиана, не среднее: один шумный соло-прогон (например, первый —
    с холодным image pull на ноде) не должен сдвигать знаменатель."""
    solo = baselines[baselines["scenario"] == "baseline"]
    solo = solo[solo["makespan_s"].notna()]
    if solo.empty:
        raise ValueError(
            "baselines file has no scenario='baseline' rows — was it produced "
            "by run_experiment.py --baseline?"
        )
    per_node = solo.groupby(["profile", "node"])["makespan_s"].median()
    per_profile = solo.groupby("profile")["makespan_s"].median()
    return per_node, per_profile


def attach_slowdown(df: pd.DataFrame, baselines: pd.DataFrame) -> pd.DataFrame:
    """Добавляет makespan_isolated, slowdown = makespan_s / makespan_isolated и
    slowdown_basis ("node" | "profile").

    Slowdown — стандартная метрика литературы по интерференции (Bubble-Up,
    Paragon, Heracles): безразмерная, профили разной длительности становятся
    сравнимыми и объединяемыми, а «замедлился в 1.8x» читается напрямую.

    Знаменатель — PER (profile, node), из пиновых per-node бейзлайнов
    (--baseline). Урок STAGE: «одинаковые» облачные ноды бывают в ~1.9x разной
    реальной скорости, и общий на все ноды знаменатель приписывал разницу
    железа интерференции. С per-node нормировкой slowdown измеряет только то,
    что сделала ко-локация, а «планировщик увёл job с медленной ноды» честно
    остаётся в makespan-метрике, не в slowdown. Строки на нодах без своего
    бейзлайна получают fallback-знаменатель профиля (slowdown_basis="profile",
    с предупреждением) — хуже, но лучше молчаливого NaN.

    Оговорка (как раньше): знаменатель на инфраструктуру не делится (бейзлайны
    гоняются одной конфигурацией, обычно A-default). Для сравнений внутри одной
    инфраструктуры это не влияет; кросс-инфра абсолютные значения slowdown
    подмешивают оверхед инфраструктуры."""
    per_node, per_profile = isolated_makespans(baselines)
    out = df.copy()

    keys = pd.MultiIndex.from_arrays([out["profile"], out["node"]])
    iso_node = pd.Series(
        [per_node.get(k, float("nan")) for k in keys], index=out.index, dtype=float
    )
    iso_profile = out["profile"].map(per_profile)

    out["makespan_isolated"] = iso_node.fillna(iso_profile)
    out["slowdown_basis"] = "node"
    out.loc[iso_node.isna() & iso_profile.notna(), "slowdown_basis"] = "profile"
    out.loc[out["makespan_isolated"].isna(), "slowdown_basis"] = None
    out["slowdown"] = out["makespan_s"] / out["makespan_isolated"]

    n_fallback = int((out["slowdown_basis"] == "profile").sum())
    if n_fallback:
        pairs = sorted(
            set(
                zip(
                    out.loc[out["slowdown_basis"] == "profile", "profile"],
                    out.loc[out["slowdown_basis"] == "profile", "node"].astype(str),
                )
            )
        )
        print(
            f"[attach_slowdown] warning: {n_fallback} rows have no per-node "
            f"baseline for {pairs} — using the profile-wide median (nodes of "
            "unequal speed then leak hardware differences into slowdown; rerun "
            "harness --baseline with per_node pinning)."
        )
    missing = sorted(set(out["profile"].dropna()) - set(per_profile.index))
    if missing:
        print(
            f"[attach_slowdown] warning: no baseline at all for profiles "
            f"{missing} — their slowdown is NaN (run harness --baseline "
            "covering them)."
        )
    return out
