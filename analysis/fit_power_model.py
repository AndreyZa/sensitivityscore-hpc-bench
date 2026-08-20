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


# Сетка K2 для сканирования. Диапазон покрывает обе ветви (вогнутую K2<0 и
# выпуклую K2>0); окрестность нуля исключена — там exp(K2·x) вырождается в
# константу, K0 и K1 перестают различаться, и МНК-система сингулярна.
K2_GRID = np.concatenate([
    -np.geomspace(0.2, 1e-3, 400),   # вогнутая ветвь
    np.geomspace(1e-3, 0.2, 400),    # выпуклая ветвь
])


def _k01_for_k2(xs, ws, k2):
    """При ФИКСИРОВАННОМ K2 модель линейна по K0 и K1 — обычный МНК."""
    a = np.column_stack([np.ones_like(xs), np.exp(k2 * xs)])
    (k0, k1), *_ = np.linalg.lstsq(a, ws, rcond=None)
    return float(k0), float(k1)


def fit_points(xs, ws):
    """K0/K1/K2 по точкам: сканирование K2 по сетке, K0 и K1 — МНК при
    фиксированном K2, затем уточнение локальным методом из найденной точки.

    Почему не curve_fit с несколькими стартами (так было до 20.08.2026):
    задача трёхпараметрическая на ~11 ступенях и плохо обусловлена — в
    пределах +5 % от минимума RMSE K1 гуляет впятеро. Локальный метод из
    произвольного старта уходит в ближайший овраг: старт вогнутой формы на
    выпуклых данных давал вырожденную квазилинейную ветку. Сканирование по
    единственному нелинейному параметру находит глобальный минимум по
    построению и делает фит воспроизводимым (результат не зависит от
    стартов), а форма кривой остаётся исходом (Э1.5), а не допущением:
    сетка симметрично покрывает обе ветви."""
    xs, ws = np.asarray(xs, float), np.asarray(ws, float)
    best = None
    for k2 in K2_GRID:
        k0, k1 = _k01_for_k2(xs, ws, k2)
        sse = float(np.sum((model(xs, k0, k1, k2) - ws) ** 2))
        if best is None or sse < best[0]:
            best = (sse, (k0, k1, float(k2)))
    if best is None:
        raise RuntimeError("сканирование по сетке K2 не дало решения")
    try:  # уточнение: сетка даёт K2 с шагом сетки, локальный метод — точнее
        p, _ = curve_fit(model, xs, ws, p0=list(best[1]), maxfev=20000)
        if float(np.sum((model(xs, *p) - ws) ** 2)) <= best[0]:
            best = (float(np.sum((model(xs, *p) - ws) ** 2)),
                    tuple(float(v) for v in p))
    except RuntimeError:
        pass
    return tuple(float(v) for v in best[1])


def k2_interval(xs, ws, tol=0.05):
    """Диапазон K2, дающий RMSE не хуже (1+tol) от минимума, и размах K1
    внутри него — мера обусловленности задачи (в статью, к коэффициентам)."""
    xs, ws = np.asarray(xs, float), np.asarray(ws, float)
    sses = []
    for k2 in K2_GRID:
        k0, k1 = _k01_for_k2(xs, ws, k2)
        sses.append((float(np.sum((model(xs, k0, k1, k2) - ws) ** 2)), k2, k1))
    best_sse = min(s for s, _, _ in sses)
    # RMSE = sqrt(SSE/n): порог по RMSE в (1+tol) раз — это SSE в (1+tol)^2
    ok = [(k2, k1) for s, k2, k1 in sses if s <= best_sse * (1.0 + tol) ** 2]
    k2s = [k for k, _ in ok]
    k1s = [k for _, k in ok]
    return (min(k2s), max(k2s)), (min(k1s), max(k1s))


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
    # RMSE ПУБЛИКУЕМЫХ коэффициентов на всех точках — не то же самое, что
    # RMSE валидационного фита на удержанных ступенях (разные фиты!).
    # Смешение этих двух чисел — замечание рецензии 20.08.2026.
    rmse_all = float(np.sqrt(np.mean((model(xs, k0, k1, k2) - np.array(ws)) ** 2)))
    (k2lo, k2hi), (k1lo, k1hi) = k2_interval(xs, ws)
    result.update({"k0": k0, "k1": k1, "k2": k2,
                   "rmse_all_w": rmse_all,
                   "rmse_all_share": rmse_all / peak,
                   "k2_range_5pct": [k2lo, k2hi],
                   "k1_range_5pct": [k1lo, k1hi],
                   "resid_at_zero_w": float(model(min(xs), k0, k1, k2) - ws[0]),
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
        print(f"{name}: K0={r['k0']:.1f} K1={r['k1']:.2f} K2={r['k2']:.4f}  "
              f"idle {r['idle_w']:.0f} Вт ({r['idle_share']:.0%} пика {r['peak_w']:.0f} Вт)")
        print(f"  фит по всем точкам: RMSE {r['rmse_all_w']:.1f} Вт "
              f"= {r['rmse_all_share']:.1%} пика; остаток на холостом ходу "
              f"{r['resid_at_zero_w']:+.1f} Вт")
        print(f"  обусловленность (+5% RMSE): K2 ∈ [{r['k2_range_5pct'][0]:.4f}, "
              f"{r['k2_range_5pct'][1]:.4f}], K1 ∈ [{r['k1_range_5pct'][0]:.2f}, "
              f"{r['k1_range_5pct'][1]:.2f}]")
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
