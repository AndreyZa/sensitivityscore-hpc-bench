# -*- coding: utf-8 -*-
"""ДИ цен осей бутстрепом ПО ПОВТОРЕНИЯМ + скорректированный R² (рецензия v2).

Та же модель и те же данные, что calibrate_axis_costs.py (NNLS, серии
stage-mixed + stage-llc, давления из обращения скор-функции), но:

  1. данные берутся из ClickHouse, а не parquet;
  2. в строках сохраняется номер ПОВТОРЕНИЯ — единица бутстрепа. Задачи
     одного повторения размещаются совместно и независимыми наблюдениями не
     являются (раздел 5.3 статьи); ресемплировать задачи значило бы занизить
     интервалы той же псевдорепликацией, которая исключена из тестов.
     Кластерный бутстреп: с возвращением выбираются ПОВТОРЕНИЯ внутри каждой
     серии, все задачи выбранного повторения входят целиком;
  3. печатается скорректированный R²: у модели >=8 свободных параметров на
     ~360 наблюдений, сырой R² льстит.

Запуск: analysis/.venv/bin/python calibrate_ci.py [--ch-host ... --n-boot N]
        analysis/.venv/bin/python calibrate_ci.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calibrate_axis_costs import (  # noqa: E402
    AXES,
    SENSITIVITY,
    baseline_medians,
    fit,
    neighbours,
    node_pressures_llc,
    node_pressures_mixed,
)

SERIES_SPEC = {
    "stage-mixed": node_pressures_mixed,
    "stage-llc": node_pressures_llc,
}
BASELINE_LABELS = {"stage-mixed": "stage-mixed", "stage-llc": "stage-llc"}
REPORT_COEFS = ["alpha_io", "alpha_llc", "alpha_net",
                "beta_io", "beta_llc", "beta_net", "gamma"]


def build_rows_with_rep(df: pd.DataFrame, pressures: pd.DataFrame,
                        base: dict, series: str) -> pd.DataFrame:
    """Как calibrate_axis_costs.build_rows, но с колонкой rep."""
    rows = []
    nb = neighbours(df)
    for idx, r in df.iterrows():
        b = base.get((r["profile"], r["node"]))
        p = pressures.loc[r["node"]] if r["node"] in pressures.index else None
        if not b or p is None or not np.isfinite(r["makespan_s"]):
            continue
        s = SENSITIVITY.get(r["profile"])
        if s is None:
            continue
        row = {"series": series, "rep": int(r["rep"]),
               "y": r["makespan_s"] / b - 1.0, "nb": nb[idx]}
        for a in AXES:
            row[f"p_{a}"] = p[a]
            row[f"ps_{a}"] = p[a] * s[a]
        rows.append(row)
    return pd.DataFrame(rows)


def load_rows(ch_host: str, ch_port: int) -> pd.DataFrame:
    from clickhouse_source import load_from_clickhouse
    parts = []
    for label, pressures_fn in SERIES_SPEC.items():
        df = load_from_clickhouse("results", host=ch_host, port=ch_port,
                                  stand="stage", run_labels=[label])
        df = df[~df["approximation"].astype(str).str.startswith(("error:", "missing"))]
        df = df[df["scenario"].astype(str).str.startswith("pressure:")]
        bl = load_from_clickhouse("baselines", host=ch_host, port=ch_port,
                                  stand="stage",
                                  run_labels=[BASELINE_LABELS[label]])
        base = baseline_medians(bl)
        parts.append(build_rows_with_rep(df, pressures_fn(df), base, label))
    return pd.concat(parts, ignore_index=True)


def adjusted_r2(r2: float, n: int, n_params: int) -> float:
    if n - n_params - 1 <= 0:
        return float("nan")
    return 1.0 - (1.0 - r2) * (n - 1) / (n - n_params - 1)


def n_params_of(data: pd.DataFrame) -> int:
    return 2 * len(AXES) + 1 + data["series"].nunique()


def bootstrap_by_rep(data: pd.DataFrame, n_boot: int = 2000,
                     seed: int = 12345) -> pd.DataFrame:
    """Кластерный бутстреп: ресемплируются повторения внутри серий."""
    rng = np.random.default_rng(seed)
    groups = {key: g for key, g in data.groupby(["series", "rep"])}
    by_series: dict[str, list] = {}
    for series, rep in groups:
        by_series.setdefault(series, []).append((series, rep))
    out = []
    for _ in range(n_boot):
        chunks = []
        for series, keys in by_series.items():
            take = rng.integers(0, len(keys), size=len(keys))
            chunks.extend(groups[keys[i]] for i in take)
        sample = pd.concat(chunks, ignore_index=True)
        try:
            out.append({c: fit(sample)[c] for c in REPORT_COEFS})
        except Exception:
            continue
    return pd.DataFrame(out)


def summarize(point: dict, boots: pd.DataFrame, n: int, n_params: int) -> str:
    lines = [f"наблюдений: {n}, параметров: {n_params}, "
             f"R² = {point['r2']:.3f}, скорректированный R² = "
             f"{adjusted_r2(point['r2'], n, n_params):.3f}",
             f"бутстреп по повторениям: {len(boots)} успешных из заданных, "
             f"95% перцентильные интервалы:", ""]
    for c in REPORT_COEFS:
        lo, hi = np.percentile(boots[c], [2.5, 97.5])
        star = "  <-- отделён от нуля" if lo > 0 else ""
        lines.append(f"  {c:10} = {point[c]:.3f}  [{lo:.3f}; {hi:.3f}]{star}")
    return "\n".join(lines)


def _self_test() -> int:
    """Два сценария с заложенным ответом.

    А. Все цены ВНУТРИ области (не на границе NNLS) — проверяется покрытие
       интервалами заложенных значений. На границе покрытие проверять нельзя:
       отсечение отрицательных координат смещает соседние коэффициенты, и
       перцентильный интервал систематически промахивается (наблюдалось
       [0.544; 0.597] против заложенного 0.600 при истинных нулях рядом).
    Б. Часть цен — честные нули (граница): проверяется только то, что
       нулевые цены не выдумываются, а ненулевые отделены от нуля.
    """
    ok = True

    def check(cond, msg):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'OK ' if cond else 'НЕТ'} {msg}")

    # Пять узлов с РАЗНЫМИ суммами давлений. На трёх узлах со штормом по
    # генератору на узел суммы почти равны (1.05/1.10/1.05), и базовые цены
    # коллинеарны посерийному свободному члену — модель обнуляет одну alpha,
    # рассовывая её по соседям. Это свойство реального плана (статья, §7,
    # «Обусловленность калибровочной задачи»), и покрытие на таком плане
    # проверять бессмысленно; здесь план обусловлен намеренно.
    pressures = {"n0": {"llc": 0.05, "io": 0.05, "net": 0.05},
                 "n1": {"llc": 0.9, "io": 0.05, "net": 0.1},
                 "n2": {"llc": 0.1, "io": 0.9, "net": 0.1},
                 "n3": {"llc": 0.1, "io": 0.05, "net": 0.9},
                 "n4": {"llc": 0.5, "io": 0.5, "net": 0.5}}

    def synth(true, seed, noise=0.05):
        # 30 повторов: у перцентильного бутстрепа по 10 кластерам покрытие
        # ~90% вместо 95%, и однозёрнная проверка на границе мигает.
        rng = np.random.default_rng(seed)
        rows = []
        for rep in range(30):
            for profile, s in SENSITIVITY.items():
                for node, p in pressures.items():
                    y = sum(p[a] * (true["alpha"][a] + true["beta"][a] * s[a])
                            for a in AXES) + rng.normal(0, noise)
                    row = {"series": "synt", "rep": rep, "y": y, "nb": 0.0}
                    for a in AXES:
                        row[f"p_{a}"] = p[a]
                        row[f"ps_{a}"] = p[a] * s[a]
                    rows.append(row)
        return pd.DataFrame(rows)

    # --- А: внутренняя истина, покрытие ---
    TRUE_A = {"alpha": {"llc": 0.10, "io": 0.25, "net": 0.15},
              "beta": {"llc": 0.20, "io": 0.60, "net": 0.30}}
    data = synth(TRUE_A, seed=7)
    point = fit(data.copy())
    boots = bootstrap_by_rep(data, n_boot=200, seed=1)
    for coef, true_v in (("alpha_io", 0.25), ("beta_io", 0.60),
                         ("alpha_llc", 0.10), ("beta_net", 0.30)):
        lo, hi = np.percentile(boots[coef], [2.5, 97.5])
        check(lo <= true_v <= hi,
              f"А: ДИ {coef} накрывает заложенное {true_v}: [{lo:.3f}; {hi:.3f}]")
    lo, _ = np.percentile(boots["alpha_io"], [2.5, 97.5])
    check(lo > 0, "А: ДИ alpha_io отделён от нуля")
    adj = adjusted_r2(point["r2"], len(data), n_params_of(data))
    check(adj > 0.9, f"А: модель объясняет чистую синтетику: adj R² = {adj:.3f}")

    # --- Б: граничная истина, нули не выдумываются ---
    TRUE_B = {"alpha": {"llc": 0.0, "io": 0.25, "net": 0.0},
              "beta": {"llc": 0.0, "io": 0.6, "net": 0.0}}
    data = synth(TRUE_B, seed=11)
    boots = bootstrap_by_rep(data, n_boot=200, seed=2)
    _, hi = np.percentile(boots["alpha_llc"], [2.5, 97.5])
    check(hi < 0.05, f"Б: нулевая alpha_llc не выдумана: hi = {hi:.3f}")
    lo, _ = np.percentile(boots["alpha_io"], [2.5, 97.5])
    check(lo > 0.1, f"Б: alpha_io отделена от нуля и на границе: lo = {lo:.3f}")

    print("\nсамопроверка:", "пройдена" if ok else "ПРОВАЛЕНА")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=8123)
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    data = load_rows(args.ch_host, args.ch_port)
    point = fit(data.copy())
    boots = bootstrap_by_rep(data, n_boot=args.n_boot)
    print(summarize(point, boots, len(data), n_params_of(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
