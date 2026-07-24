#!/usr/bin/env python3
"""Сводит свип веса (C2): measured-regret плеча A-sensitivityscore как функция
веса плагина, плюс критерий плато. Запускается фазой analyze в weight-sweep.sh.

Матрица замедления M[профиль][узел] строится ОДНА, из ОБЪЕДИНЁННЫХ наблюдений
всех прогонов (ref + все веса): замедление узла под штормом — свойство физики
(шторм на w9), а не планировщика, и общая матрица даёт стабильный знаменатель.
regret_measured каждого веса — насколько размещения A-ss@вес близки к минимуму
этой общей матрицы. Метр независим от скор-функции плагина (см. B4).

Критерий (зафиксирован в weight-sweep.sh ДО прогона): минимальный вес, чей 95%
bootstrap-CI regret перекрывается с CI лучшего (минимального) веса — то есть
дальнейшее падение статистически незначимо. Шумоустойчивее прежнего «10%
размаха» (тот печатается справочно). Рядом — прямая ступенька размещения: доля
high-s-io на штормовом узле по весу. Печатается колено; решение за человеком.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ANALYSIS = Path(__file__).resolve().parent.parent / "analysis"
sys.path.insert(0, str(ANALYSIS))
from clickhouse_source import load_from_clickhouse  # noqa: E402
from placement_oracle import (  # noqa: E402
    empirical_slowdown_matrix,
    hot_node,
    isolated_makespan,
    measured_regret,
    placement_share_on_node,
    slowdown_column,
)
from stats import bootstrap_ci, plateau_onset  # noqa: E402

IO_SENSITIVE_PROFILE = "high-s-io"  # профиль-жертва, которую плагин должен уводить с шторма


def _load(label, host, port):
    return load_from_clickhouse("results", host=host, port=port,
                                stand="stage", run_labels=[label])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--weights", required=True, help="список весов через пробел")
    p.add_argument("--ch-host", default="localhost")
    p.add_argument("--ch-port", type=int, default=8123)
    args = p.parse_args()
    weights = [int(w) for w in args.weights.split()]

    baselines = load_from_clickhouse("baselines", host=args.ch_host, port=args.ch_port,
                                     stand="stage", run_labels=["sweep-ref"])

    # A-ss под каждым весом + ref (default/trimaran) — в один df с колонкой weight.
    frames = []
    ref = _load("sweep-ref", args.ch_host, args.ch_port)
    if not ref.empty:
        ref = ref.copy(); ref["weight"] = -1  # ref: вес неприменим
        frames.append(ref)
    for w in weights:
        d = _load(f"sweep-ss-w{w}", args.ch_host, args.ch_port)
        if d.empty:
            print(f"  (нет данных для веса {w})")
            continue
        d = d.copy(); d["weight"] = w
        frames.append(d)
    if not frames:
        print("нет данных свипа — сначала фазы ref и ss"); return 1

    allrows = pd.concat(frames, ignore_index=True)
    press = allrows[allrows["scenario"].astype(str).str.startswith("pressure:")].copy()
    press = press[press["makespan_s"].notna()]

    # ОБЩАЯ матрица замедления из всех наблюдений (физика узлов, не планировщик).
    iso = isolated_makespan(baselines)
    press["slowdown"] = slowdown_column(press, iso)
    matrix = empirical_slowdown_matrix(press)
    press["regret_measured"] = measured_regret(press, matrix)

    print("Общая матрица замедления M[профиль][узел] (median slowdown):")
    for prof in sorted(matrix):
        cells = "  ".join(f"{n.replace('worker-','w-')}={v:.2f}"
                          for n, v in sorted(matrix[prof].items()))
        print(f"  {prof:16} {cells}")

    # Штормовой узел — из матрицы (узел max slowdown high-s-io); ступенька
    # размещения строится относительно него, число узлов не хардкодится.
    hot = hot_node(matrix, IO_SENSITIVE_PROFILE)

    # regret_measured(A-ss) по весам, на уровне повторов + 95% bootstrap-CI и
    # прямая ступенька размещения (доля high-s-io на штормовом узле).
    rng = np.random.default_rng(12345)  # детерминированный bootstrap (воспроизводимость)
    rows = []
    ss = press[(press["config"] == "A-sensitivityscore") & (press["weight"] >= 0)]
    for w, g in ss.groupby("weight"):
        per_rep = g.groupby("rep")["regret_measured"].mean().dropna()
        if per_rep.empty:
            continue
        lo, point, hi = bootstrap_ci(per_rep.to_numpy(), statistic=np.median, rng=rng)
        share_hot = (placement_share_on_node(g, IO_SENSITIVE_PROFILE, hot)
                     if hot else float("nan"))
        rows.append({"weight": int(w), "n_reps": int(len(per_rep)),
                     "regret_measured": point, "ci_lo": lo, "ci_hi": hi,
                     "share_hot": share_hot})
    curve = pd.DataFrame(rows).sort_values("weight").reset_index(drop=True)

    show = curve.copy()
    show["regret [95% CI]"] = show.apply(
        lambda r: f"{r['regret_measured']:.3f} [{r['ci_lo']:.3f}, {r['ci_hi']:.3f}]", axis=1)
    show["high-s-io на шторме"] = show["share_hot"].map(
        lambda x: "—" if pd.isna(x) else f"{x:.0%}")
    print("\nmeasured-regret плеча A-sensitivityscore по весу (меньше = лучше):")
    print(show[["weight", "n_reps", "regret [95% CI]", "high-s-io на шторме"]]
          .to_string(index=False))
    if hot:
        print(f"\nШтормовой узел (max slowdown {IO_SENSITIVE_PROFILE}) = {hot}. "
              f"Доля high-s-io на нём — прямая ступенька размещения: с ростом "
              f"веса должна падать (плагин уводит жертву со шторма).")

    # Критерий плато (пред-регистрирован): минимальный вес, чей 95% CI regret
    # перекрывается с CI лучшего (минимального) веса — дальнейшее падение
    # статистически незначимо. Шумоустойчивее «10% размаха»: широкие CI дают
    # честное inconclusive, а не ложное колено.
    if len(curve) >= 2:
        knee = plateau_onset(curve["weight"].tolist(),
                             curve["regret_measured"].tolist(),
                             list(zip(curve["ci_lo"], curve["ci_hi"])))
        if knee is not None:
            verdict = "НА плато" if knee <= 5 else "НИЖЕ плато — сигнал ещё перекрыт"
            print(f"\nПЛАТО с веса {knee} (CI regret перекрывается с лучшим весом). "
                  f"Текущий weight=5 {verdict}.")
        # Справочно — прежний критерий «10% размаха», для сопоставимости.
        r = curve["regret_measured"].to_numpy()
        span = r.max() - r.min()
        old = None
        if span > 0:
            old = next((int(curve["weight"].iloc[i]) for i in range(len(r) - 1)
                        if (r[i] - r[i + 1]) < 0.10 * span), None)
        print(f"(справочно, прежний критерий 10% размаха: колено = {old})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
