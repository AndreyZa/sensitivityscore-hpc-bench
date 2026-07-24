#!/usr/bin/env python3
"""Сводит свип веса (C2): measured-regret плеча A-sensitivityscore как функция
веса плагина, плюс критерий плато. Запускается фазой analyze в weight-sweep.sh.

Матрица замедления M[профиль][узел] строится ОДНА, из ОБЪЕДИНЁННЫХ наблюдений
всех прогонов (ref + все веса): замедление узла под штормом — свойство физики
(шторм на w9), а не планировщика, и общая матрица даёт стабильный знаменатель.
regret_measured каждого веса — насколько размещения A-ss@вес близки к минимуму
этой общей матрицы. Метр независим от скор-функции плагина (см. B4).

Критерий (зафиксирован в weight-sweep.sh ДО прогона): минимальный вес, при
котором regret_measured выходит на плато — падение к следующему весу < 10%
полного размаха. Печатается колено; решение за человеком.
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
    isolated_makespan,
    measured_regret,
    slowdown_column,
)


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

    # regret_measured(A-ss) по весам, на уровне повторов.
    rows = []
    ss = press[(press["config"] == "A-sensitivityscore") & (press["weight"] >= 0)]
    for w, g in ss.groupby("weight"):
        per_rep = g.groupby("rep")["regret_measured"].mean().dropna()
        if per_rep.empty:
            continue
        rows.append({"weight": int(w), "n_reps": int(len(per_rep)),
                     "regret_measured": float(np.median(per_rep.to_numpy()))})
    curve = pd.DataFrame(rows).sort_values("weight").reset_index(drop=True)

    print("\nmeasured-regret плеча A-sensitivityscore по весу (меньше = лучше):")
    print(curve.to_string(index=False))

    # Критерий плато: минимальный вес, после которого падение regret к
    # следующему весу < 10% полного размаха кривой.
    if len(curve) >= 3:
        r = curve["regret_measured"].to_numpy()
        span = r.max() - r.min()
        knee = None
        if span > 0:
            for i in range(len(r) - 1):
                if (r[i] - r[i + 1]) < 0.10 * span:
                    knee = int(curve["weight"].iloc[i]); break
        if knee is not None:
            print(f"\nПЛАТО с веса {knee}: дальнейший рост веса меняет regret менее "
                  f"чем на 10% размаха. Текущий weight=5 "
                  f"{'НА плато' if knee <= 5 else 'НИЖЕ плато — сигнал ещё перекрыт'}.")
        else:
            print("\nПлато не достигнуто в этом диапазоне — regret падает до "
                  "последнего веса; расширить диапазон вверх.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
