#!/usr/bin/env python3
"""analyze.py — runs the full §5/§6 analysis pipeline against results.parquet and
checks hypotheses H1-H4 (Программа_экспериментов §6).

Usage:
    python analyze.py --results ../harness/results/results.parquet --outdir report/
    # с бейзлайнами (slowdown + fingerprint):
    python analyze.py --results ... --baselines ../harness/results/baselines.parquet

Produces:
    report/comparisons.csv   — every Mann-Whitney/Cliff's delta/CV comparison run
                               (колонка metric: makespan_s | slowdown | placement_regret)
    report/summary.md        — human-readable H1-H4 verdicts for the advisor briefing
                               (+ fingerprint-таблица, если есть бейзлайны)
    report/fingerprint.csv   — заявленный vs измеренный S по соло-прогонам
    report/*.png             — boxplot, scatter по осям S, CV, placement regret

Метрики сравнения (каждая — своя Holm-семья в пределах сценария; по всем трём
«меньше = лучше»):
    makespan_s       — сырое время (всегда);
    slowdown         — makespan / makespan_isolated профиля (при --baselines):
                       безразмерная, профили разной длительности объединяемы;
    placement_regret — качество решения планировщика по снапшоту давления на
                       момент сабмита (см. harness/submit/node_pressure.py):
                       прямое свидетельство механизма, почти без шума исхода.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from fingerprint import fingerprint_table, to_markdown as fingerprint_md
from load import attach_slowdown, filter_valid, load_results
from plots import (
    plot_cv_comparison,
    plot_makespan_boxplot,
    plot_metric_vs_makespan,
    plot_regret_by_config,
)
from stats import run_all_comparisons

# Config pairs tested for each hypothesis (Программа_экспериментов §6).
# H1-trimaran — второй бейзлайн внутри той же инфраструктуры: доказывает, что
# выигрыш даёт именно S-вектор (interference-awareness), а не «любой учёт
# загрузки нод» (Trimaran/LoadVariationRiskBalancing видит CPU/memory
# utilization через metrics-server, но слеп к LLC/NUMA/IO-контенции).
HYPOTHESIS_PAIRS = {
    "H1": (
        "A-sensitivityscore",
        "A-default",
    ),  # SensitivityScore vs default, same infra (A)
    "H1-trimaran": (
        "A-sensitivityscore",
        "A-trimaran",
    ),  # SensitivityScore vs load-aware-но-не-interference-aware бейзлайн
    "H2": (
        "B-sensitivityscore",
        "B-default",
    ),  # same comparison, under KubeVirt overhead
    "H3": ("A-sensitivityscore", "C"),  # SensitivityScore vs Slurm ceiling
    "H4": ("A-sensitivityscore", "D"),  # SensitivityScore vs Slinky/slurm-bridge
}

# (колонка, подпись, формат значения) — по всем метрикам «меньше = лучше».
COMPARISON_METRICS = [
    ("makespan_s", "makespan", "{:.1f}s"),
    ("slowdown", "slowdown (makespan / isolated)", "{:.2f}x"),
    ("placement_regret", "placement regret", "{:.4f}"),
]


def summarize_hypothesis(
    comparisons: pd.DataFrame,
    name: str,
    config_a: str,
    config_b: str,
    value_fmt: str = "{:.1f}s",
    show_cv: bool = True,
) -> str:
    """Renders one hypothesis section from the already-computed comparison
    sweep. Significance verdicts use mw_p_holm (Holm-Bonferroni adjusted over
    the whole sweep) — with ~20 tests at alpha=0.05, an uncorrected verdict
    would produce about one spurious "significant" point by chance alone; the
    raw p is still shown for reference.

    show_cv=False drops the CV annotation: CV = σ/μ is a stability measure that
    only means something for a positive-magnitude metric (makespan, slowdown).
    For placement_regret the arm we care about sits at μ≈0, so CV explodes or
    is NaN — printing it is noise, not information."""
    rows = comparisons[
        (comparisons["config_a"] == config_a)
        & (comparisons["config_b"] == config_b)
        & (comparisons["mw_n_a"] > 0)
        & (comparisons["mw_n_b"] > 0)
    ]
    if rows.empty:
        # Пусто -> секцию не печатаем вовсе (раньше был "no data yet" на
        # каждую метрику; на A-only стенде это 9 мёртвых строк H2/H3/H4 —
        # чистый шум). Отсутствующие сравнения по-прежнему видны в
        # comparisons.csv (там пустые строки сохраняются для полноты).
        return ""
    lines = [f"### {name}: {config_a} vs {config_b}\n"]

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
        # По всем метрикам сравнения «меньше = лучше» (см. COMPARISON_METRICS).
        direction = "better" if result["mean_a"] < result["mean_b"] else "worse"
        cv_a = f" (CV={result['cv_a']:.3f})" if show_cv else ""
        cv_b = f" (CV={result['cv_b']:.3f})" if show_cv else ""
        mean_a = value_fmt.format(result["mean_a"])
        mean_b = value_fmt.format(result["mean_b"])
        lines.append(
            f"- profile={result['profile']}, overcommit={result['overcommit']}: "
            f"{config_a} mean={mean_a}{cv_a}, "
            f"{config_b} mean={mean_b}{cv_b} — "
            f"{config_a} {direction}, Mann-Whitney p={p:.4f}, "
            f"Holm-adjusted p={p_holm:.4f} ({sig}), "
            f"Cliff's delta={delta:.3f} ({mag}), "
            f"n={result['mw_n_a']}/{result['mw_n_b']} reps"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="../harness/results/results.parquet")
    parser.add_argument(
        "--baselines",
        default="../harness/results/baselines.parquet",
        help="Path to baselines.parquet (harness --baseline); if the file "
        "exists, slowdown comparisons and the fingerprint table are added",
    )
    parser.add_argument("--outdir", default="report")
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Keep synthetic-devbox rows (local pipeline testing ONLY, "
        "never for real results — see load.filter_valid)",
    )
    parser.add_argument(
        "--pmu-less-stand",
        action="store_true",
        help="Real stand without a usable PMU (e.g. STAGE cloud VMs): keep the "
        "rows but null LLC/NUMA (synthetic); io_pressure/makespan/regret are "
        "real. Use with io-only weights.",
    )
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df = load_results(args.results)
    valid = filter_valid(
        df,
        allow_synthetic=args.allow_synthetic,
        pmu_less_stand=args.pmu_less_stand,
    )

    # Бейзлайны опциональны: без них пайплайн работает как раньше (makespan +
    # regret), с ними добавляются slowdown-сравнения и fingerprint-таблица.
    baselines_valid = None
    baselines_path = Path(args.baselines)
    if baselines_path.exists():
        baselines_valid = filter_valid(
            load_results(baselines_path),
            allow_synthetic=args.allow_synthetic,
            pmu_less_stand=args.pmu_less_stand,
        )
        valid = attach_slowdown(valid, baselines_valid)
    else:
        print(
            f"[analyze] no baselines file at {baselines_path} — skipping "
            "slowdown and fingerprint (run harness --baseline to produce it)."
        )

    # Full comparison sweep (§5.2) across every config pair used by H1-H4 —
    # separately per scenario: "batch" (symmetric co-location matrix) and
    # each "pressure:<name>" (aggressors + victim stream) are different
    # experiments whose (profile, overcommit) axes mean different things
    # (overcommit = ratio vs = aggressors per pressured node), so they must
    # not be pooled into one comparison group. Each (scenario, metric) is its
    # own Holm family.
    pairs = list(HYPOTHESIS_PAIRS.values())
    scenario_frames = []
    summary_sections = []
    for scenario in sorted(valid["scenario"].dropna().unique()):
        subset = valid[valid["scenario"] == scenario]
        summary_sections.append(f"# Сценарий: {scenario}\n")
        for metric_col, metric_label, value_fmt in COMPARISON_METRICS:
            if metric_col not in subset.columns or subset[metric_col].notna().sum() == 0:
                continue
            # Пропускаем метрику без сигнала в этом сценарии: если по ВСЕМ
            # плечам значения ~0, сравнивать нечего. Так placement_regret
            # уходит из batch-сценария (снапшот берётся до того, как со-
            # размещённые члены создают давление -> regret ≈ 0 у всех), но
            # остаётся в pressure-сценариях, где default имеет regret > 0.
            if subset[metric_col].abs().max() < 1e-9:
                continue
            comparisons = run_all_comparisons(subset, pairs, value_col=metric_col)
            if comparisons.empty:
                continue
            comparisons.insert(0, "metric", metric_col)
            comparisons.insert(0, "scenario", scenario)
            scenario_frames.append(comparisons)

            show_cv = metric_col != "placement_regret"
            hyp_sections = [
                s
                for name, (a, b) in HYPOTHESIS_PAIRS.items()
                if (s := summarize_hypothesis(comparisons, name, a, b, value_fmt, show_cv))
            ]
            if hyp_sections:  # заголовок метрики только если под ним что-то есть
                summary_sections.append(f"## Метрика: {metric_label}\n")
                summary_sections.extend(hyp_sections)

    comparisons = (
        pd.concat(scenario_frames, ignore_index=True)
        if scenario_frames
        else pd.DataFrame()
    )
    comparisons.to_csv(outdir / "comparisons.csv", index=False)

    # Fingerprint «заявленный vs измеренный S» — из соло-бейзлайнов.
    if baselines_valid is not None and not baselines_valid.empty:
        fp = fingerprint_table(baselines_valid)
        fp.to_csv(outdir / "fingerprint.csv", index=False)
        summary_sections.append(fingerprint_md(fp))

    summary_md = (
        "# Проверка гипотез H1–H4\n\n" + "\n\n".join(summary_sections) + "\n"
    )
    (outdir / "summary.md").write_text(summary_md, encoding="utf-8")

    # Visualizations (§5.3) — per scenario, чтобы batch-строки (overcommit =
    # коэффициент) не смешивались с pressure-строками (overcommit =
    # агрессоров на ноду): boxplot at overcommit=2.0 для batch, scatter по
    # каждой измеряемой оси S, H1 stability (CV) comparison, regret по плечам.
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

    # Placement regret по плечам — на pressure-сценариях это главный график
    # механизма: default кладёт чувствительных жертв на придавленную ноду
    # (regret > 0), interference-aware планировщик уводит (regret ~ 0).
    for scenario in valid["scenario"].dropna().unique():
        sub = valid[valid["scenario"] == scenario]
        # Тот же порог, что для сравнений: строим regret-график только там, где
        # есть сигнал (pressure-сценарии), а не плоскую линию на нуле у batch.
        if sub["placement_regret"].abs().max() >= 1e-9:
            suffix = scenario.replace(":", "-")
            plot_regret_by_config(
                sub, output_path=outdir / f"placement_regret-{suffix}.png"
            )

    h1_a, h1_b = HYPOTHESIS_PAIRS["H1"]
    if not comparisons.empty:
        h1_all = comparisons[
            (comparisons["metric"] == "makespan_s")
            & (comparisons["config_a"] == h1_a)
            & (comparisons["config_b"] == h1_b)
            & (comparisons["mw_n_a"] > 0)
            & (comparisons["mw_n_b"] > 0)
        ]
        for scenario in h1_all["scenario"].unique():
            h1_rows = h1_all[h1_all["scenario"] == scenario]
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
