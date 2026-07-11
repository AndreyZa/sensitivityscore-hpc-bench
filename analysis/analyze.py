#!/usr/bin/env python3
"""analyze.py — runs the full §5/§6 analysis pipeline against results.parquet and
checks hypotheses H1-H4 (Программа_экспериментов §6).

Usage:
    python analyze.py --results ../harness/results/results.parquet --outdir report/

Produces:
    report/comparisons.csv   — every Mann-Whitney/Cliff's delta/CV comparison run
    report/summary.md        — human-readable H1-H4 verdicts for the advisor briefing
    report/makespan_boxplot.png
    report/llc_vs_makespan.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from load import filter_valid, load_results
from plots import plot_cv_comparison, plot_makespan_boxplot, plot_metric_vs_makespan
from stats import run_all_comparisons

# Config pairs tested for each hypothesis (Программа_экспериментов §6).
HYPOTHESIS_PAIRS = {
    "H1": (
        "A-sensitivityscore",
        "A-default",
    ),  # SensitivityScore vs default, same infra (A)
    "H2": (
        "B-sensitivityscore",
        "B-default",
    ),  # same comparison, under KubeVirt overhead
    "H3": ("A-sensitivityscore", "C"),  # SensitivityScore vs Slurm ceiling
    "H4": ("A-sensitivityscore", "D"),  # SensitivityScore vs Slinky/slurm-bridge
}


def summarize_hypothesis(
    comparisons: pd.DataFrame, name: str, config_a: str, config_b: str
) -> str:
    """Renders one hypothesis section from the already-computed comparison
    sweep. Significance verdicts use mw_p_holm (Holm-Bonferroni adjusted over
    the whole sweep) — with ~20 tests at alpha=0.05, an uncorrected verdict
    would produce about one spurious "significant" point by chance alone; the
    raw p is still shown for reference."""
    lines = [f"## {name}: {config_a} vs {config_b}\n"]
    rows = comparisons[
        (comparisons["config_a"] == config_a)
        & (comparisons["config_b"] == config_b)
        & (comparisons["mw_n_a"] > 0)
        & (comparisons["mw_n_b"] > 0)
    ]
    if rows.empty:
        lines.append(
            "- no data yet for this comparison (run the missing configs first)"
        )
        return "\n".join(lines)

    for result in rows.to_dict("records"):
        p = result["mw_p_value"]
        p_holm = result["mw_p_holm"]
        delta = result["cliffs_delta"]
        mag = result["cliffs_magnitude"]
        sig = (
            "significant (Holm-adjusted p<0.05)"
            if pd.notna(p_holm) and p_holm < 0.05
            else "not significant after Holm correction"
        )
        direction = "faster" if result["mean_a"] < result["mean_b"] else "slower"
        lines.append(
            f"- profile={result['profile']}, overcommit={result['overcommit']}: "
            f"{config_a} mean={result['mean_a']:.1f}s (CV={result['cv_a']:.3f}), "
            f"{config_b} mean={result['mean_b']:.1f}s (CV={result['cv_b']:.3f}) — "
            f"{config_a} {direction}, Mann-Whitney p={p:.4f}, "
            f"Holm-adjusted p={p_holm:.4f} ({sig}), "
            f"Cliff's delta={delta:.3f} ({mag}), "
            f"n={result['mw_n_a']}/{result['mw_n_b']} reps"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="../harness/results/results.parquet")
    parser.add_argument("--outdir", default="report")
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Keep synthetic-devbox rows (local pipeline testing ONLY, "
        "never for real results — see load.filter_valid)",
    )
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df = load_results(args.results)
    valid = filter_valid(df, allow_synthetic=args.allow_synthetic)

    # Full comparison sweep (§5.2) across every config pair used by H1-H4 —
    # separately per scenario: "batch" (symmetric co-location matrix) and
    # each "pressure:<name>" (aggressors + victim stream) are different
    # experiments whose (profile, overcommit) axes mean different things
    # (overcommit = ratio vs = aggressors per pressured node), so they must
    # not be pooled into one comparison group. Each scenario is its own
    # Holm family.
    pairs = list(HYPOTHESIS_PAIRS.values())
    scenario_frames = []
    summary_sections = []
    for scenario in sorted(valid["scenario"].dropna().unique()):
        subset = valid[valid["scenario"] == scenario]
        comparisons = run_all_comparisons(subset, pairs)
        if comparisons.empty:
            continue
        comparisons.insert(0, "scenario", scenario)
        scenario_frames.append(comparisons)

        summary_sections.append(f"# Сценарий: {scenario}\n")
        summary_sections.extend(
            summarize_hypothesis(comparisons, name, a, b)
            for name, (a, b) in HYPOTHESIS_PAIRS.items()
        )

    comparisons = (
        pd.concat(scenario_frames, ignore_index=True)
        if scenario_frames
        else pd.DataFrame()
    )
    comparisons.to_csv(outdir / "comparisons.csv", index=False)

    summary_md = (
        "# Проверка гипотез H1–H4\n\n" + "\n\n".join(summary_sections) + "\n"
    )
    (outdir / "summary.md").write_text(summary_md, encoding="utf-8")

    # Visualizations (§5.3) — per scenario, чтобы batch-строки (overcommit =
    # коэффициент) не смешивались с pressure-строками (overcommit =
    # агрессоров на ноду): boxplot at overcommit=2.0 для batch, scatter по
    # каждой измеряемой оси S, H1 stability (CV) comparison.
    batch = valid[valid["scenario"] == "batch"]
    if not batch.empty and 2.0 in batch["overcommit"].unique():
        plot_makespan_boxplot(
            batch, overcommit=2.0, output_path=outdir / "makespan_boxplot.png"
        )
    for metric, label, fname in [
        ("llc_miss_rate", "LLC miss rate (normalized)", "llc_vs_makespan.png"),
        ("io_pressure", "IO pressure (PSI stall share)", "io_pressure_vs_makespan.png"),
        ("numa_remote_ratio", "NUMA remote read ratio", "numa_vs_makespan.png"),
    ]:
        if valid[metric].notna().any():
            plot_metric_vs_makespan(valid, metric, label, output_path=outdir / fname)

    h1_a, h1_b = HYPOTHESIS_PAIRS["H1"]
    if not comparisons.empty:
        for scenario in comparisons["scenario"].unique():
            h1_rows = comparisons[
                (comparisons["scenario"] == scenario)
                & (comparisons["config_a"] == h1_a)
                & (comparisons["config_b"] == h1_b)
                & (comparisons["mw_n_a"] > 0)
                & (comparisons["mw_n_b"] > 0)
            ]
            if not h1_rows.empty:
                suffix = scenario.replace(":", "-")
                plot_cv_comparison(
                    h1_rows, output_path=outdir / f"cv_comparison-{suffix}.png"
                )

    print(
        f"done — see {outdir}/summary.md, {outdir}/comparisons.csv, and the .png plots"
    )


if __name__ == "__main__":
    main()
