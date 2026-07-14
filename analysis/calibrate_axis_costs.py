"""calibrate_axis_costs.py — калибровка цены осей интерференции по уже
собранным сериям STAGE (LLC + смешанная), без новых прогонов.

Мотивация (см. docs/«Сводка результатов STAGE (июль 2026).md»): скоринг
считает все оси равноценными, а эмпирически единица давления диска стоит
на порядок дороже единицы давления кэша. Модель калибровки:

    замедление(задача t на узле n) − 1 ≈
        γ · соседей(t, n) + Σ_a p_a(n) · (α_a + β_a · s_a(t))

где p_a — давление оси на узле [0,1], s_a — декларированная чувствительность
задачи (high=1, medium=0.5, low=0), α_a — базовая цена оси (платят ВСЕ задачи
узла: iowait дискового шторма бьёт по узлу целиком), β_a — надбавка
чувствительной задачи, γ — цена соседства (замедление на одного co-located
соседа-жертву; первая оценка для слагаемого скученности в скоринге).
Все коэффициенты ≥ 0 (NNLS).

Плюс интерсепт на СЕРИЮ: эталоны и серия могут идти в разные часы, а на
облачных vCPU есть суточный steal (урок v3: ночные эталоны против дневной
серии дают ложный «пол» замедления на всех узлах сразу) — посерийная
константа впитывает этот дрейф, чтобы он не записался в цену оси. На выбор
узла внутри серии константа не влияет.

Откуда данные:
  - замедление = makespan / медиана эталона (профиль, узел) — пер-узловые
    эталонные прогоны обязательны (гетерогенность облака);
  - давления узлов восстанавливаются из interference_chosen (снапшот на
    момент решения) обращением скор-функции: в LLC-серии weights={llc:1} и
    s_llc(high-s)=1, т.е. interference = p_llc; в смешанной weights
    {llc,net,io}=1/3: high-s -> 3i = p_llc, high-s-net -> 3i = p_net,
    high-s-io -> 3i = p_llc + p_io;
  - соседи — по перекрытию интервалов [start_ts, end_ts] жертв одного
    (плеча, повторения) на одном узле.

Выход: таблица α/β по осям, два варианта внедрения (веса как есть /
расширение скоринга базовой ценой) и проверка на матрице токсичности —
какой узел выбрал бы каждый профиль при каждой схеме.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import nnls

AXES = ("llc", "io", "net")  # numa выключена честно: один домен на узел

# Зеркало harness/profiles.py (high=1, medium=0.5, low=0) по осям AXES.
SENSITIVITY = {
    "high-s": {"llc": 1.0, "io": 0.0, "net": 0.0},
    "high-s-io": {"llc": 1.0, "io": 1.0, "net": 0.0},
    "high-s-net": {"llc": 0.0, "io": 0.0, "net": 1.0},
    "low-s": {"llc": 0.0, "io": 0.0, "net": 0.0},
}

NODE_SHORT = {
    "worker-192.168.0.8": "w8",
    "worker-192.168.0.9": "w9",
    "worker-192.168.0.10": "w10",
}


def baseline_medians(baselines: pd.DataFrame) -> dict[tuple[str, str], float]:
    med = baselines.groupby(["profile", "node"])["makespan_s"].median()
    return med.to_dict()


def node_pressures_llc(df: pd.DataFrame) -> pd.DataFrame:
    """LLC-серия: weights={llc:1}, жертвы только high-s (s_llc=1) =>
    interference_chosen = p_llc выбранного узла. Остальные оси без штормов
    и без генерации у жертв — 0."""
    p = df.groupby("node")["interference_chosen"].median().rename("llc").to_frame()
    p["io"] = 0.0
    p["net"] = 0.0
    return p


def node_pressures_mixed(df: pd.DataFrame) -> pd.DataFrame:
    """Смешанная серия: weights {llc,net,io}=1, знаменатель 3 => 3i по
    профилю с единственной (для net) или известной (для io: llc+io)
    комбинацией осей. Медианы по всем плечам: default/trimaran ставят
    задачи на все узлы, так что каждая ячейка населена."""
    i3 = (3 * df["interference_chosen"]).clip(0, 3)
    by = pd.DataFrame({"node": df["node"], "profile": df["profile"], "i3": i3})
    med = by.groupby(["profile", "node"])["i3"].median().unstack(0)
    p = pd.DataFrame(index=med.index)
    p["llc"] = med["high-s"]
    p["net"] = med["high-s-net"]
    p["io"] = (med["high-s-io"] - med["high-s"]).clip(lower=0.0)
    return p.clip(0.0, 1.0)


def neighbours(df: pd.DataFrame) -> pd.Series:
    """Среднее число других жертв, работавших одновременно на том же узле:
    Σ длительностей перекрытий / собственная длительность, в рамках одного
    (плеча, повторения) — разные повторения разнесены во времени."""
    out = pd.Series(0.0, index=df.index)
    for _, g in df.groupby(["config", "rep", "node"]):
        if len(g) < 2:
            continue
        s, e = g["start_ts"].to_numpy(float), g["end_ts"].to_numpy(float)
        for idx, (si, ei) in zip(g.index, zip(s, e)):
            if not ei > si:
                continue
            ov = np.minimum(ei, e) - np.maximum(si, s)
            out[idx] = (np.clip(ov, 0, None).sum() - (ei - si)) / (ei - si)
    return out


def build_rows(df: pd.DataFrame, pressures: pd.DataFrame,
               base: dict, series: str) -> pd.DataFrame:
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
        row = {
            "series": series, "profile": r["profile"],
            "node": NODE_SHORT.get(r["node"], r["node"]),
            "y": r["makespan_s"] / b - 1.0, "nb": nb[idx],
        }
        for a in AXES:
            row[f"p_{a}"] = p[a]
            row[f"ps_{a}"] = p[a] * s[a]
        rows.append(row)
    return pd.DataFrame(rows)


def fit(data: pd.DataFrame) -> dict:
    cols = [f"p_{a}" for a in AXES] + [f"ps_{a}" for a in AXES] + ["nb"]
    series = sorted(data["series"].unique())
    X = np.column_stack(
        [data[cols].to_numpy(float)]
        + [(data["series"] == s).to_numpy(float) for s in series]
    )
    y = data["y"].to_numpy(float)
    coef, resid = nnls(X, y)
    out = {"gamma": coef[2 * len(AXES)]}
    for i, a in enumerate(AXES):
        out[f"alpha_{a}"] = coef[i]
        out[f"beta_{a}"] = coef[len(AXES) + i]
    for j, s in enumerate(series):
        out[f"intercept_{s}"] = coef[2 * len(AXES) + 1 + j]
    pred = X @ coef
    data["pred"] = pred
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    out["r2"] = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return out


def score_table(pressures: pd.DataFrame, coefs: dict, scheme: str) -> pd.DataFrame:
    """Скор узлов для каждого профиля: uniform — текущие единичные веса
    (Σ s·p); weights — веса ∝ (α+β), скоринг плагина как есть (Σ w·s·p);
    base — расширенный скоринг Σ p·(α + β·s). Меньше — лучше."""
    rows = {}
    for prof, s in SENSITIVITY.items():
        if prof == "low-s":
            continue
        scores = {}
        for node, p in pressures.iterrows():
            if scheme == "uniform":
                v = sum(s[a] * p[a] for a in AXES)
            elif scheme == "weights":
                w = {a: coefs[f"alpha_{a}"] + coefs[f"beta_{a}"] for a in AXES}
                v = sum(w[a] * s[a] * p[a] for a in AXES)
            else:
                v = sum(p[a] * (coefs[f"alpha_{a}"] + coefs[f"beta_{a}"] * s[a])
                        for a in AXES)
            scores[NODE_SHORT.get(node, node)] = v
        rows[prof] = scores
    return pd.DataFrame(rows).T


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results-mixed",
                    default="../harness/results/results-stage-mixed.parquet")
    ap.add_argument("--results-llc",
                    default="../harness/results/results-stage-llc.parquet")
    ap.add_argument("--baselines",
                    default="../harness/results/baselines-stage-mixed.parquet")
    ap.add_argument("--out-json", default=None,
                    help="куда сохранить коэффициенты и рекомендованные веса")
    args = ap.parse_args()

    base = baseline_medians(pd.read_parquet(args.baselines))
    mixed = pd.read_parquet(args.results_mixed)
    llc = pd.read_parquet(args.results_llc)

    p_mixed = node_pressures_mixed(mixed)
    p_llc = node_pressures_llc(llc)
    print("=== Восстановленные давления узлов (медианы на момент решения) ===")
    print("смешанная серия:\n", p_mixed.round(3).rename(index=NODE_SHORT))
    print("LLC-серия:\n", p_llc.round(3).rename(index=NODE_SHORT))

    data = pd.concat([
        build_rows(mixed, p_mixed, base, "mixed3"),
        build_rows(llc, p_llc, base, "llc"),
    ], ignore_index=True)
    print(f"\nстрок в фите: {len(data)} "
          f"(mixed3 {sum(data.series == 'mixed3')}, llc {sum(data.series == 'llc')})")

    coefs = fit(data)
    print("\n=== Цены осей (замедление на единицу давления) ===")
    print(f"{'ось':>5} | {'α базовая':>10} | {'β чувствит.':>11} | {'α+β':>6}")
    for a in AXES:
        al, be = coefs[f"alpha_{a}"], coefs[f"beta_{a}"]
        print(f"{a:>5} | {al:10.3f} | {be:11.3f} | {al + be:6.3f}")
    print(f"цена соседства γ = {coefs['gamma']:.3f} на одного co-located соседа")
    for k, v in coefs.items():
        if k.startswith("intercept_"):
            print(f"дрейф сессии {k.removeprefix('intercept_')}: +{v:.3f} "
                  "(эталоны vs серия в разные часы — в цену осей не входит)")
    print(f"R² модели (по строкам): {coefs['r2']:.3f}")
    cell = data.groupby(["series", "profile", "node"]).agg(
        y=("y", "mean"), pred=("pred", "mean"), n=("y", "size"))
    ss = 1 - ((cell.y - cell.pred) ** 2).sum() / ((cell.y - cell.y.mean()) ** 2).sum()
    print(f"R² по ячейкам (профиль × узел): {ss:.3f}")
    print("наблюдение vs модель по ячейкам:\n", cell.round(3))

    print("\n=== Проверка на матрице токсичности (смешанная серия) ===")
    emp = (data[data.series == "mixed3"]
           .groupby(["profile", "node"])["y"].mean().unstack())
    print("эмпирическое замедление−1 (профиль × узел):\n", emp.round(3))
    for scheme, label in [("uniform", "текущие единичные веса"),
                          ("weights", "калиброванные веса (без изменения кода)"),
                          ("base", "расширенный скоринг α + β·s")]:
        t = score_table(p_mixed, coefs, scheme)
        picks = t.idxmin(axis=1)
        ok = {p: picks[p] == emp.loc[p].idxmin() for p in picks.index}
        print(f"\n-- {label}: выбор узла по профилям --")
        print(t.round(3))
        print("выбор:", dict(picks), "| совпал с эмпирически лучшим:", ok)

    if args.out_json:
        w = {a: coefs[f"alpha_{a}"] + coefs[f"beta_{a}"] for a in AXES}
        top = max(w.values()) or 1.0
        payload = {
            "coefficients": {k: round(v, 4) for k, v in coefs.items()},
            "weights_only": {a: round(w[a] / top, 2) for a in AXES} | {"numa": 0.0},
            "base_plus_sensitivity": {
                a: {"base": round(coefs[f"alpha_{a}"], 3),
                    "sensitivity": round(coefs[f"beta_{a}"], 3)} for a in AXES},
        }
        Path(args.out_json).write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nкоэффициенты сохранены: {args.out_json}")


if __name__ == "__main__":
    main()
