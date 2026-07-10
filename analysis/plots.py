"""plots.py — visualization per docs/Технический_план_экспериментов.md §5.3.

- Boxplot makespan по config x profile at overcommit=2.0 (H1's expected point of
  maximum divergence between A-default and A-sensitivityscore).
- Scatter LLC miss rate vs makespan, split by profile — sanity-checks that LLC
  pressure actually correlates with degradation (justifies LLC as an S dimension
  rather than a purely declarative choice).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def plot_makespan_boxplot(
    df: pd.DataFrame, overcommit: float = 2.0, output_path: str | Path | None = None
):
    """Boxplot of makespan_s by config, faceted by profile, at a fixed overcommit
    ratio (default 2.0, where interference is expected to be maximal — docs §5.3)."""
    subset = df[df["overcommit"] == overcommit]
    if subset.empty:
        raise ValueError(f"no rows with overcommit={overcommit}")

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.boxplot(data=subset, x="config", y="makespan_s", hue="profile", ax=ax)
    ax.set_title(f"Makespan by config x profile (overcommit={overcommit})")
    ax.set_xlabel("Configuration")
    ax.set_ylabel("Makespan (s)")
    plt.xticks(rotation=30, ha="right")
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    return fig


def plot_llc_vs_makespan(df: pd.DataFrame, output_path: str | Path | None = None):
    """Scatter of llc_miss_rate vs makespan_s, split by profile (low-s / high-s) —
    validates whether LLC pressure correlates with degradation, justifying the
    choice of LLC as an S dimension (docs §5.3)."""
    fig, ax = plt.subplots(figsize=(8, 6))
    sns.scatterplot(
        data=df,
        x="llc_miss_rate",
        y="makespan_s",
        hue="profile",
        style="config",
        ax=ax,
        alpha=0.7,
    )
    ax.set_title("LLC miss rate vs. makespan")
    ax.set_xlabel("LLC miss rate (normalized)")
    ax.set_ylabel("Makespan (s)")
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    return fig


def plot_cv_comparison(cv_summary: pd.DataFrame, output_path: str | Path | None = None):
    """Bar chart comparing coefficient of variation between two configs across
    plan points — the stability comparison for H1 (docs §5.2/§5.3: CV rather than
    raw stddev, since absolute makespans differ across configs)."""
    fig, ax = plt.subplots(figsize=(10, 6))
    melted = cv_summary.melt(
        id_vars=["profile", "overcommit"],
        value_vars=["cv_a", "cv_b"],
        var_name="side",
        value_name="cv",
    )
    melted["point"] = melted["profile"] + " / oc=" + melted["overcommit"].astype(str)
    sns.barplot(data=melted, x="point", y="cv", hue="side", ax=ax)
    ax.set_title("Coefficient of variation (stability) by plan point")
    ax.set_ylabel("CV = sigma / mu")
    plt.xticks(rotation=30, ha="right")
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    return fig
