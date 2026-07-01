"""stats.py — statistical analysis per docs/Технический_план_экспериментов.md §5.2.

Deliberately NOT using ANOVA/t-test by default: with only ~10 repetitions per plan
point there's no strong basis to assume normality, and HPC timings are often
right-skewed due to noisy-neighbor effects. Instead:

- Mann-Whitney U     — between two configs at the same plan point (same profile/overcommit)
- Cliff's delta      — effect size, more robust to outliers than Cohen's d for this data
- Coefficient of variation (CV = sigma/mu) — for comparing *stability* (H1) across
  configs with different absolute makespans, where comparing raw variance directly
  would be misleading.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu


def mann_whitney(sample_a: np.ndarray, sample_b: np.ndarray) -> dict:
    """Two-sided Mann-Whitney U test between two makespan samples."""
    sample_a = np.asarray(sample_a, dtype=float)
    sample_b = np.asarray(sample_b, dtype=float)
    sample_a = sample_a[~np.isnan(sample_a)]
    sample_b = sample_b[~np.isnan(sample_b)]

    if len(sample_a) < 2 or len(sample_b) < 2:
        return {"u_statistic": np.nan, "p_value": np.nan, "n_a": len(sample_a), "n_b": len(sample_b)}

    u_stat, p_value = mannwhitneyu(sample_a, sample_b, alternative="two-sided")
    return {"u_statistic": u_stat, "p_value": p_value, "n_a": len(sample_a), "n_b": len(sample_b)}


def cliffs_delta(sample_a: np.ndarray, sample_b: np.ndarray) -> dict:
    """Cliff's delta effect size: proportion of pairs where a > b minus proportion
    where a < b, in [-1, 1]. Chosen over Cohen's d for robustness to the
    outlier-heavy tails typical of HPC makespan measurements (§5.2).

    Interpretation (Romano et al. 2006 thresholds, commonly cited):
        |delta| < 0.147          negligible
        0.147 <= |delta| < 0.33  small
        0.33  <= |delta| < 0.474 medium
        |delta| >= 0.474         large
    """
    a = np.asarray(sample_a, dtype=float)
    b = np.asarray(sample_b, dtype=float)
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]

    if len(a) == 0 or len(b) == 0:
        return {"delta": np.nan, "magnitude": "n/a"}

    # O(n*m) pairwise comparison — fine at n,m ~= 10 (repetitions per plan point).
    greater = sum(1 for x in a for y in b if x > y)
    less = sum(1 for x in a for y in b if x < y)
    delta = (greater - less) / (len(a) * len(b))

    abs_delta = abs(delta)
    if abs_delta < 0.147:
        magnitude = "negligible"
    elif abs_delta < 0.33:
        magnitude = "small"
    elif abs_delta < 0.474:
        magnitude = "medium"
    else:
        magnitude = "large"

    return {"delta": delta, "magnitude": magnitude}


def coefficient_of_variation(sample: np.ndarray) -> float:
    """CV = sigma / mu. Used instead of raw stddev to compare *stability* between
    configs whose absolute makespan differs (docs §5.2: "иначе разные по
    абсолютному makespan конфигурации некорректно сравнивать по дисперсии
    напрямую")."""
    sample = np.asarray(sample, dtype=float)
    sample = sample[~np.isnan(sample)]
    if len(sample) < 2 or np.mean(sample) == 0:
        return float("nan")
    return float(np.std(sample, ddof=1) / np.mean(sample))


def compare_configs(df: pd.DataFrame, config_a: str, config_b: str,
                     profile: str, overcommit: float,
                     value_col: str = "makespan_s") -> dict:
    """Runs the full §5.2 comparison (Mann-Whitney + Cliff's delta + CV for both
    sides) for one (config_a vs config_b, profile, overcommit) plan point."""
    subset = df[(df["profile"] == profile) & (df["overcommit"] == overcommit)]
    sample_a = subset[subset["config"] == config_a][value_col].to_numpy()
    sample_b = subset[subset["config"] == config_b][value_col].to_numpy()

    result = {
        "config_a": config_a,
        "config_b": config_b,
        "profile": profile,
        "overcommit": overcommit,
        "mean_a": float(np.nanmean(sample_a)) if len(sample_a) else np.nan,
        "mean_b": float(np.nanmean(sample_b)) if len(sample_b) else np.nan,
        "cv_a": coefficient_of_variation(sample_a),
        "cv_b": coefficient_of_variation(sample_b),
    }
    result.update({f"mw_{k}": v for k, v in mann_whitney(sample_a, sample_b).items()})
    result.update({f"cliffs_{k}": v for k, v in cliffs_delta(sample_a, sample_b).items()})
    return result


def run_all_comparisons(df: pd.DataFrame, pairs: list[tuple[str, str]],
                         value_col: str = "makespan_s") -> pd.DataFrame:
    """Runs compare_configs for every (config_a, config_b) pair across every
    (profile, overcommit) combination present in df — the standard sweep for
    testing H1-H4."""
    rows = []
    for profile in sorted(df["profile"].dropna().unique()):
        for overcommit in sorted(df["overcommit"].dropna().unique()):
            for config_a, config_b in pairs:
                rows.append(compare_configs(df, config_a, config_b, profile, overcommit, value_col))
    return pd.DataFrame(rows)
