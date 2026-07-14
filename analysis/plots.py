"""plots.py — визуализация по docs/Технический_план_экспериментов.md §5.3.

Все подписи — по-русски и в терминах читателя со стороны (научный
руководитель): «планировщик», «время выполнения», «ошибка размещения»;
внутренние коды конфигураций (A-default) на графики не выводятся. Цвета
планировщиков фиксированы во всех графиках: SensitivityScore — синий,
default — серый, trimaran — оранжевый.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

# Код конфигурации -> подпись планировщика на графике.
ARM_LABEL = {
    "A-default": "default",
    "A-sensitivityscore": "SensitivityScore",
    "A-trimaran": "trimaran",
    "B-default": "default (KubeVirt)",
    "B-sensitivityscore": "SensitivityScore (KubeVirt)",
}
ARM_PALETTE = {
    "default": "#8a8f98",
    "SensitivityScore": "#1c7ed6",
    "trimaran": "#e8590c",
}
SCENARIO_RU = {
    "pressure:io": "фоновая дисковая нагрузка (IO)",
    "pressure:net": "фоновая сетевая нагрузка (Net)",
    "pressure:llc": "фоновая нагрузка на кэш (LLC)",
    "pressure:mixed3": "смешанная нагрузка (кэш+диск+сеть)",
    "batch": "пакетный план",
}


def arm_label(config: str) -> str:
    return ARM_LABEL.get(str(config), str(config))


def _arm_order(labels) -> list[str]:
    known = [a for a in ("default", "SensitivityScore", "trimaran") if a in set(labels)]
    return known + sorted(set(labels) - set(known))


def _scenario_ru(df: pd.DataFrame) -> str:
    scs = df["scenario"].dropna().unique() if "scenario" in df else []
    return SCENARIO_RU.get(scs[0], str(scs[0])) if len(scs) == 1 else ""


def plot_makespan_boxplot(
    df: pd.DataFrame, overcommit: float = 2.0, output_path: str | Path | None = None
):
    """Время выполнения по планировщикам × профилям при фиксированной
    переподписке (по умолчанию 2.0 — точка максимального расхождения, §5.3)."""
    subset = df[df["overcommit"] == overcommit].copy()
    if subset.empty:
        raise ValueError(f"no rows with overcommit={overcommit}")
    subset["планировщик"] = subset["config"].map(arm_label)

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.boxplot(
        data=subset, x="планировщик", y="makespan_s", hue="profile",
        order=_arm_order(subset["планировщик"]), ax=ax,
    )
    ax.set_title(f"Время выполнения по планировщикам (переподписка {overcommit})")
    ax.set_xlabel("")
    ax.set_ylabel("Время выполнения, с")
    ax.legend(title="профиль задачи")
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    return fig


def plot_metric_vs_makespan(
    df: pd.DataFrame,
    metric: str,
    metric_label: str,
    output_path: str | Path | None = None,
):
    """Давление одной оси чувствительности против времени выполнения — §5.3:
    проверка, что измеряемая ось действительно коррелирует с деградацией
    (это обосновывает её место в векторе чувствительности). В заголовке —
    ранговая корреляция Спирмена по точкам, где есть обе величины."""
    sub = df[df[metric].notna() & df["makespan_s"].notna()].copy()
    sub["планировщик"] = sub["config"].map(arm_label)

    rho_txt = ""
    if len(sub) >= 5 and sub[metric].nunique() > 1:
        try:
            from scipy.stats import spearmanr

            rho, _ = spearmanr(sub[metric], sub["makespan_s"])
            rho_txt = f" · ρ Спирмена = {rho:.2f}"
        except Exception:  # noqa: BLE001 — корреляция украшает, но не обязательна
            pass

    fig, ax = plt.subplots(figsize=(8, 6))
    sns.scatterplot(
        data=sub if len(sub) else df,
        x=metric,
        y="makespan_s",
        hue="profile",
        style="планировщик" if len(sub) else None,
        ax=ax,
        alpha=0.7,
    )
    ax.set_title(f"{metric_label} и время выполнения"
                 + (f"\n{rho_txt.strip(' ·')}" if rho_txt else ""), fontsize=11)
    ax.set_xlabel(metric_label)
    ax.set_ylabel("Время выполнения, с")
    if ax.get_legend():
        ax.get_legend().set_title("")
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    return fig


def plot_interference_vs_makespan(
    df: pd.DataFrame, output_path: str | Path | None = None
):
    """Интерференция выбранного узла (взвешенная давлением всех осей на момент
    решения) против времени выполнения — прямая проверка механизма: задачи,
    поставленные на нагруженный узел, работают дольше. Каждая точка — одна
    задача; цвет — планировщик: у interference-aware планировщика точки
    прижаты к нулю по X."""
    sub = df[df["interference_chosen"].notna() & df["makespan_s"].notna()].copy()
    if sub.empty:
        raise ValueError("no rows with interference_chosen")
    sub["планировщик"] = sub["config"].map(arm_label)

    rho_txt = ""
    if len(sub) >= 5 and sub["interference_chosen"].nunique() > 1:
        try:
            from scipy.stats import spearmanr

            rho, _ = spearmanr(sub["interference_chosen"], sub["makespan_s"])
            rho_txt = f" · ρ Спирмена = {rho:.2f}"
        except Exception:  # noqa: BLE001
            pass

    fig, ax = plt.subplots(figsize=(8, 6))
    order = _arm_order(sub["планировщик"])
    sns.scatterplot(
        data=sub, x="interference_chosen", y="makespan_s",
        hue="планировщик", hue_order=order,
        palette={a: ARM_PALETTE.get(a, "#74c0fc") for a in order},
        style="profile" if sub["profile"].nunique() > 1 else None,
        ax=ax, alpha=0.75,
    )
    sc = _scenario_ru(sub)
    subtitle = " · ".join(x for x in (sc, rho_txt.strip(" ·")) if x)
    ax.set_title("Интерференция выбранного узла и время выполнения"
                 + (f"\n{subtitle}" if subtitle else ""), fontsize=11)
    ax.set_xlabel("Интерференция узла на момент размещения (0..1)")
    ax.set_ylabel("Время выполнения, с")
    if ax.get_legend():
        ax.get_legend().set_title("")
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    return fig


def plot_regret_by_config(df: pd.DataFrame, output_path: str | Path | None = None):
    """Ошибка размещения по планировщикам (оттенок — интенсивность фоновой
    нагрузки, агрессоров на узел) — график МЕХАНИЗМА: планировщик, не видящий
    интерференции, ставит чувствительные задачи на перегруженный узел
    (ошибка > 0 и растёт с интенсивностью), interference-aware уводит их
    (ошибка ≈ 0). В отличие от времени выполнения, метрика решения почти
    не шумит."""
    subset = df[df["placement_regret"].notna()].copy()
    if subset.empty:
        raise ValueError("no rows with placement_regret")
    subset["планировщик"] = subset["config"].map(arm_label)

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.boxplot(
        data=subset, x="планировщик", y="placement_regret", hue="overcommit",
        order=_arm_order(subset["планировщик"]), ax=ax,
    )
    sc = _scenario_ru(subset)
    ax.set_title("Ошибка размещения по планировщикам"
                 + (f" — {sc}" if sc else ""))
    ax.set_xlabel("")
    ax.set_ylabel("Ошибка размещения (интерференция выбранного узла − лучшего)")
    ax.axhline(0.0, color="gray", linewidth=0.8, linestyle="--")
    ax.legend(title="агрессоров на узел")
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    return fig


def plot_cv_comparison(cv_summary: pd.DataFrame, output_path: str | Path | None = None):
    """Разброс времени выполнения (коэффициент вариации, %) по планировщикам —
    сравнение стабильности для H1 (§5.2/§5.3: CV, а не сырое σ, потому что
    средние времена у планировщиков разные). Столбцы подписаны именами
    планировщиков из сравнения, а не служебными cv_a/cv_b."""
    rows = []
    for r in cv_summary.itertuples():
        point = f"{r.profile} / переподписка {r.overcommit}"
        rows.append({"точка плана": point,
                     "планировщик": arm_label(r.config_a), "CV, %": 100 * r.cv_a})
        rows.append({"точка плана": point,
                     "планировщик": arm_label(r.config_b), "CV, %": 100 * r.cv_b})
    long = pd.DataFrame(rows).drop_duplicates()

    fig, ax = plt.subplots(figsize=(9, 5.5))
    order = _arm_order(long["планировщик"])
    sns.barplot(
        data=long, x="точка плана", y="CV, %", hue="планировщик",
        hue_order=order,
        palette={a: ARM_PALETTE.get(a, "#74c0fc") for a in order},
        ax=ax,
    )
    for c in ax.containers:
        ax.bar_label(c, fmt="%.0f%%", fontsize=9, padding=2)
    sc = _scenario_ru(cv_summary)
    ax.set_title("Разброс времени выполнения (меньше — стабильнее)"
                 + (f" — {sc}" if sc else ""))
    ax.set_xlabel("")
    ax.set_ylabel("Коэффициент вариации времени, %")
    ax.legend(title="")
    plt.xticks(rotation=15, ha="right")
    fig.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=150)
    return fig
