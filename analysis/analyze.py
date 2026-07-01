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
from plots import plot_llc_vs_makespan, plot_makespan_boxplot
from stats import compare_configs, run_all_comparisons

# Config pairs tested for each hypothesis (Программа_экспериментов §6).
HYPOTHESIS_PAIRS = {
    "H1": ("A-sensitivityscore", "A-default"),   # SensitivityScore vs default, same infra (A)
    "H2": ("B-sensitivityscore", "B-default"),   # same comparison, under KubeVirt overhead
    "H3": ("A-sensitivityscore", "C"),           # SensitivityScore vs Slurm ceiling
    "H4": ("A-sensitivityscore", "D"),           # SensitivityScore vs Slinky/slurm-bridge
}


def summarize_hypothesis(df: pd.DataFrame, name: str, config_a: str, config_b: str) -> str:
    lines = [f"## {name}: {config_a} vs {config_b}\n"]
    any_data = False
    for profile in sorted(df["profile"].dropna().unique()):
        for overcommit in sorted(df["overcommit"].dropna().unique()):
            subset = df[(df["profile"] == profile) & (df["overcommit"] == overcommit)]
            if config_a not in subset["config"].values or config_b not in subset["config"].values:
                continue
            any_data = True
            result = compare_configs(df, config_a, config_b, profile, overcommit)
            p = result["mw_p_value"]
            delta = result["cliffs_delta"]
            mag = result["cliffs_magnitude"]
            sig = "significant (p<0.05)" if pd.notna(p) and p < 0.05 else "not significant"
            direction = "faster" if result["mean_a"] < result["mean_b"] else "slower"
            lines.append(
                f"- profile={profile}, overcommit={overcommit}: "
                f"{config_a} mean={result['mean_a']:.1f}s (CV={result['cv_a']:.3f}), "
                f"{config_b} mean={result['mean_b']:.1f}s (CV={result['cv_b']:.3f}) — "
                f"{config_a} {direction}, Mann-Whitney p={p:.4f} ({sig}), "
                f"Cliff's delta={delta:.3f} ({mag})"
            )
    if not any_data:
        lines.append("- no data yet for this comparison (run the missing configs first)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="../harness/results/results.parquet")
    parser.add_argument("--outdir", default="report")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df = load_results(args.results)
    valid = filter_valid(df)

    # Full comparison sweep (§5.2) across every config pair used by H1-H4.
    pairs = list(HYPOTHESIS_PAIRS.values())
    comparisons = run_all_comparisons(valid, pairs)
    comparisons.to_csv(outdir / "comparisons.csv", index=False)

    # Human-readable H1-H4 summary for the advisor briefing.
    summary_sections = [summarize_hypothesis(valid, name, a, b) for name, (a, b) in HYPOTHESIS_PAIRS.items()]
    summary_md = "# Проверка гипотез H1–H4\n\n" + "\n\n".join(summary_sections) + "\n"
    (outdir / "summary.md").write_text(summary_md, encoding="utf-8")

    # Visualizations (§5.3) — boxplot at overcommit=2.0 (max expected divergence),
    # scatter of LLC pressure vs makespan across all data.
    if 2.0 in valid["overcommit"].unique():
        plot_makespan_boxplot(valid, overcommit=2.0, output_path=outdir / "makespan_boxplot.png")
    plot_llc_vs_makespan(valid, output_path=outdir / "llc_vs_makespan.png")

    print(f"done — see {outdir}/summary.md, {outdir}/comparisons.csv, and the .png plots")


if __name__ == "__main__":
    main()
