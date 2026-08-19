#!/usr/bin/env python3
"""fit_power_model.py — фаза P1: фит модели мощности P(x) = K0 + K1·e^(K2·x).

Вход — CSV со ступенями калибровки (scripts/p1-calibrate.sh): колонки
x,watts[,node]. x — утилизация CPU В ТЕХ ЖЕ ЕДИНИЦАХ, в которых Peaks
получает её от load-watcher (проценты 0–100); модель публикуется в
NodePowerModel как есть, поэтому конвенция единиц фиксируется входом и
никакого пересчёта внутри нет.

Протокол Э1.6 (предрегистрация, ЗАМОРОЖЕНО 18.08.2026): фит по ЧЁТНЫМ
ступеням (по возрастанию x), проверка на НЕЧЁТНЫХ; критерий — RMSE на
удержанных ступенях ≤ 5 % пиковой наблюдаемой мощности. Публикуемые
K0/K1/K2 после прохождения критерия фитятся по всем точкам.

Попутно репортится форма кривой (Э1.5): K1 < 0 и K2 < 0 — вогнутая,
насыщающаяся (Peaks уплотняет); иначе — выпуклая (Peaks разносит, меняется
интерпретация всей P2 — это не ошибка фита, а результат).

Выход — JSON с k0/k1/k2 по узлам, готовый для вставки в NodePowerModel
(k8s/scheduler-config/scheduler-config.yaml; после правки карты нужен
scheduler-redeploy — конфиг читается при старте процесса).

  python fit_power_model.py --csv calib.csv [--rmse-limit 0.05] [--out fit.json]
  python fit_power_model.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys

import numpy as np
from scipy.optimize import curve_fit


def model(x, k0, k1, k2):
    return k0 + k1 * np.exp(k2 * np.asarray(x, dtype=float))


def fit_points(xs, ws):
    """K0/K1/K2 по точкам. Стартуем с двух приближений — вогнутого
    (K1<0, K2<0: P(0)=K0+K1 — холостой ход, K0 — плато) и выпуклого
    (K1>0, K2>0) — и берём лучшее по остатку: старт одной формы на данных
    другой уводит curve_fit в вырожденную квазилинейную ветку (наблюдалось
    на self-test), а форма кривой — это Э1.5, её нельзя навязывать стартом."""
    xs, ws = np.asarray(xs, float), np.asarray(ws, float)
    span = float(ws.max() - ws.min()) or 1.0
    seeds = [
        [float(ws.max()), -span, -0.05],                       # вогнутая
        [float(ws.min()), span / math.exp(3.0), 0.03],         # выпуклая
    ]
    best = None
    for p0 in seeds:
        try:
            p, _ = curve_fit(model, xs, ws, p0=p0, maxfev=20000)
        except RuntimeError:
            continue
        sse = float(np.sum((model(xs, *p) - ws) ** 2))
        if best is None or sse < best[0]:
            best = (sse, p)
    if best is None:
        raise RuntimeError("фит не сошёлся ни с одного старта")
    return tuple(float(v) for v in best[1])


def fit_node(rows: list[tuple[float, float]], rmse_limit: float) -> dict:
    rows = sorted(rows)
    xs = [r[0] for r in rows]
    ws = [r[1] for r in rows]
    peak = max(ws)
    result: dict = {"points": len(rows), "peak_w": peak}

    if len(rows) >= 6:  # валидационный протокол осмыслен от ~6 ступеней
        train = rows[0::2]
        hold = rows[1::2]
        k = fit_points([r[0] for r in train], [r[1] for r in train])
        pred = model([r[0] for r in hold], *k)
        rmse = float(np.sqrt(np.mean((pred - np.array([r[1] for r in hold])) ** 2)))
        result["holdout_rmse_w"] = rmse
        result["holdout_rmse_share"] = rmse / peak
        result["e16_pass"] = rmse / peak <= rmse_limit
    else:
        result["e16_pass"] = None  # мало ступеней — критерий не применим

    k0, k1, k2 = fit_points(xs, ws)
    result.update({"k0": k0, "k1": k1, "k2": k2,
                   "idle_w": k0 + k1,           # P(0)
                   "idle_share": (k0 + k1) / peak,  # Э1.3
                   "concave": k1 < 0 and k2 < 0})   # Э1.5
    return result


def run(args) -> int:
    by_node: dict[str, list[tuple[float, float]]] = {}
    with open(args.csv, newline="") as f:
        for row in csv.DictReader(f):
            by_node.setdefault(row.get("node", "") or "", []).append(
                (float(row["x"]), float(row["watts"])))

    out, failed = {}, False
    for node, rows in sorted(by_node.items()):
        r = fit_node(rows, args.rmse_limit)
        out[node or "node"] = r
        name = node or "(узел не указан)"
        print(f"{name}: K0={r['k0']:.1f} K1={r['k1']:.1f} K2={r['k2']:.4f}  "
              f"idle {r['idle_w']:.0f} Вт ({r['idle_share']:.0%} пика {r['peak_w']:.0f} Вт)")
        if r["e16_pass"] is None:
            print(f"  Э1.6: ступеней {r['points']} < 6 — валидация не применима")
        else:
            print(f"  Э1.6: RMSE на удержанных {r['holdout_rmse_w']:.1f} Вт "
                  f"= {r['holdout_rmse_share']:.1%} пика (порог {args.rmse_limit:.0%}) — "
                  + ("ok" if r["e16_pass"] else "FAIL"))
            failed |= not r["e16_pass"]
        print("  Э1.5: " + ("кривая вогнутая (K1<0, K2<0) — Peaks уплотняет"
                            if r["concave"] else
                            "кривая НЕ вогнутая — Peaks будет разносить; "
                            "интерпретация P2 меняется (см. предрегистрацию)"))

    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"записано: {args.out}")
    return 1 if failed else 0


def self_test() -> int:
    rng = np.random.default_rng(7)
    true = (1000.0, -720.0, -0.03)  # K0, K1, K2: idle 280 Вт, плато 1000 Вт

    def make_csv(path, k, noise, nodes=("wrk-t",)):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["node", "x", "watts"])
            for n in nodes:
                for x in range(0, 101, 10):
                    w.writerow([n, x, model(x, *k) + rng.normal(0, noise)])

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        # 1. Вогнутая кривая с шумом 5 Вт: K восстановимы, Э1.6 проходит.
        make_csv(f"{d}/ok.csv", true, 5.0)
        args = parse_args(["--csv", f"{d}/ok.csv", "--out", f"{d}/fit.json"])
        assert run(args) == 0
        fit = json.load(open(f"{d}/fit.json"))["wrk-t"]
        assert abs(fit["k0"] - true[0]) < 50 and fit["concave"], fit
        assert abs(fit["idle_w"] - 280) < 25, fit
        assert fit["e16_pass"] and fit["holdout_rmse_share"] < 0.02, fit
        # 2. Выпуклая кривая (K1>0): фит честно репортит forme, не падает.
        make_csv(f"{d}/convex.csv", (250.0, 30.0, 0.035), 5.0)
        assert run(parse_args(["--csv", f"{d}/convex.csv"])) == 0
        # 3. Шум 30 % пика валит Э1.6 -> rc 1.
        make_csv(f"{d}/noisy.csv", true, 300.0)
        assert run(parse_args(["--csv", f"{d}/noisy.csv"])) == 1
    print("self-test: ок (восстановление K, idle/пик, вогнутость/выпуклость, "
          "порог Э1.6)")
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="ступени: x,watts[,node]")
    ap.add_argument("--rmse-limit", type=float, default=0.05,
                    help="порог Э1.6: доля пиковой мощности (default 0.05)")
    ap.add_argument("--out", default="", help="куда записать JSON фита")
    args = ap.parse_args(argv)
    if not args.csv:
        ap.error("--csv обязателен (кроме --self-test)")
    return args


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(run(parse_args(sys.argv[1:])))
