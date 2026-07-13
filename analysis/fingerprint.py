"""fingerprint.py — таблица «заявленный vs измеренный S» из соло-бейзлайнов.

Защита методики от вопроса «аннотации sensitivity-* произвольны, вы сами себе
нарисовали вход»: по baselines.parquet (харнесс --baseline, каждый профиль
соло на пустом кластере) сверяем декларацию (sensitivity_* колонки, едут из
harness/profiles.py в самих данных) с фактическим поведением, измеренным
агентом без чужой интерференции. Плюс проверка монотонности: профиль с
заявленным high по оси обязан измеряться выше профиля с low по той же оси —
нарушение печатается в summary как ⚠️, это сигнал пересмотреть либо профиль,
либо декларацию ДО боевых прогонов.

Замечание к осям: llc/numa/io измеряются в честных [0,1]-шкалах (miss ratio,
remote ratio, PSI stall share); net для fingerprint берётся сырыми bytes/s —
сравнение внутри оси относительное, и сырая метрика не зависит от того,
откалиброван ли NET_REFERENCE_MBPS на стенде (в score при этом идёт
нормированный net_pressure). Известная честная оговорка: профиль high-s-net
ДЕКЛАРИРУЕТ net=high, но сетевого трафика сам пока не генерирует (см.
harness/profiles.py) — монотонность по оси net ожидаемо флагается, это
осознанный компромисс до появления сетевого OUTPUT_MODE у воркера.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# (ось, измеряемая колонка, подпись в таблице)
AXES = [
    ("llc", "llc_miss_rate", "LLC miss ratio"),
    ("numa", "numa_remote_ratio", "NUMA remote ratio"),
    ("io", "io_pressure", "IO pressure (PSI)"),
    ("net", "net_bw", "Net bytes/s (raw)"),
]

_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}


def fingerprint_table(baselines: pd.DataFrame) -> pd.DataFrame:
    """-> строки (profile, axis, declared, measured_mean, measured_std, n)
    по валидным соло-строкам. n — прогоны с непустой метрикой (агент мог не
    писать ось, например NUMA-события недоступны на стенде)."""
    solo = baselines[baselines["scenario"] == "baseline"]
    rows = []
    for profile, group in solo.groupby("profile"):
        for axis, metric_col, _label in AXES:
            declared_vals = group[f"sensitivity_{axis}"].dropna().unique()
            declared = declared_vals[0] if len(declared_vals) else None
            measured = group[metric_col].dropna()
            rows.append(
                {
                    "profile": profile,
                    "axis": axis,
                    "declared": declared,
                    "measured_mean": measured.mean() if len(measured) else np.nan,
                    "measured_std": measured.std(ddof=1) if len(measured) > 1 else np.nan,
                    "n": len(measured),
                }
            )
    return pd.DataFrame(rows)


def monotonicity_violations(table: pd.DataFrame) -> list[str]:
    """Для каждой оси: средняя измеренная величина по уровню декларации должна
    расти с уровнем (low < medium < high, по уровням, реально встречающимся в
    данных). Возвращает список человекочитаемых нарушений."""
    violations = []
    for axis, _metric_col, label in AXES:
        sub = table[(table["axis"] == axis) & table["measured_mean"].notna()]
        sub = sub[sub["declared"].isin(_LEVEL_ORDER)]
        if sub.empty:
            continue
        by_level = sub.groupby("declared")["measured_mean"].mean()
        levels = sorted(by_level.index, key=_LEVEL_ORDER.get)
        for lo, hi in zip(levels, levels[1:]):
            if by_level[hi] <= by_level[lo]:
                violations.append(
                    f"ось {axis} ({label}): declared '{hi}' измеряется "
                    f"{by_level[hi]:.4g} <= declared '{lo}' {by_level[lo]:.4g} — "
                    "декларация не подтверждается соло-прогонами"
                )
    return violations


def to_markdown(table: pd.DataFrame) -> str:
    """Markdown-секция для summary.md: таблица профиль x ось + вердикт
    монотонности."""
    lines = [
        "# Fingerprint: заявленный vs измеренный S (соло-бейзлайны)\n",
        "| profile | ось | заявлено | измерено (mean ± std) | n |",
        "|---|---|---|---|---|",
    ]
    for r in table.to_dict("records"):
        # Оси без единого сэмпла на стенде (например LLC/NUMA на STAGE, где
        # PMU занулён) — не строка данных, а шум: пропускаем. csv (fingerprint.csv)
        # сохраняет все оси; сюда, в человекочитаемую сводку, они не нужны.
        if r["n"] == 0:
            continue
        if pd.isna(r["measured_mean"]):
            measured = "—"
        elif pd.isna(r["measured_std"]):
            measured = f"{r['measured_mean']:.4g}"
        else:
            measured = f"{r['measured_mean']:.4g} ± {r['measured_std']:.2g}"
        lines.append(
            f"| {r['profile']} | {r['axis']} | {r['declared'] or '—'} "
            f"| {measured} | {r['n']} |"
        )

    violations = monotonicity_violations(table)
    if violations:
        lines.append("\n**Проверка монотонности: НАРУШЕНИЯ**\n")
        lines.extend(f"- ⚠️ {v}" for v in violations)
    else:
        lines.append(
            "\nПроверка монотонности пройдена: по каждой оси измеренные "
            "величины упорядочены согласно заявленным уровням."
        )
    return "\n".join(lines)
