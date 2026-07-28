#!/usr/bin/env python3
"""verify_paper_numbers.py — сверка несущих чисел статьи с ClickHouse.

Точка правды — ClickHouse. Раньше числа статьи сверялись лишь с её собственным
текстом (make check: figdata <-> текст) и вручную с CH. Здесь сверка с CH стала
командой: несущие числа пересчитываются из ClickHouse и проверяется, что каждое
(а) совпадает с заявленным в статье в пределах округления и (б) присутствует в
тексте. Расхождение данные<->статья ловится до чтения человеком.

НЕ дублирует уже воспроизводимые пути:
  - контрасты близнецов ×1,73/×6,22/×4,20 -> canonical_numbers.py (--check/--recompute);
  - цены осей c⁰/cˢ/γ и их ДИ -> calibrate_axis_costs.py --clickhouse (make axis-costs).
Здесь — интеграл/размещение/p95 смешанной серии, медианы и зазор net-diff,
отрицательный контроль (плацебо).

Запуск: make paper-check (поверх make ch-tunnel; на .72 CH_HOST=localhost).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from clickhouse_source import load_from_clickhouse  # noqa: E402

# Статья вынесена отдельным репозиторием рядом.
ARTICLE = Path.home() / "phd" / "sensitivity-score-cloud-paper" / "Статья (черновик).md"
DISK_NODE = "worker-192.168.0.9"   # диск/сеть-шторм на STAGE (факт постановки)


def _load(label):
    return load_from_clickhouse("results", host=H, port=P, stand="stage", run_labels=[label])


def _press(df):
    return df[df["scenario"].astype(str).str.startswith("pressure:")]


def ru(x, nd=1):
    return f"{x:.{nd}f}".replace(".", ",")


def checks() -> list[tuple]:
    """[(описание, вычисленное, ожидаемое-в-статье, допуск, паттерн-в-тексте)]."""
    out = []
    mc = _press(_load("stage-mixed-calib"))
    arms = ("A-sensitivityscore", "A-default", "A-trimaran")
    exp_mean = {"A-sensitivityscore": 78.8, "A-default": 81.5, "A-trimaran": 85.2}
    exp_hot = {"A-sensitivityscore": 0, "A-default": 11, "A-trimaran": 35}
    # Паттерн для текста ВСЕГДА из ожидаемого (статейного) значения, не из
    # вычисленного: у 71,550 верное округление — 71,6 (статья), а ru(вычисл.)
    # дало бы 71,5 из-за float — ложный «нет в тексте».
    for a in arms:
        g = mc[mc["config"] == a]
        m = g["makespan_s"].mean()
        out.append((f"mixed-calib среднее makespan {a}", m, exp_mean[a], 0.1, ru(exp_mean[a])))
        hot = int((g["node"] == DISK_NODE).sum())
        out.append((f"mixed-calib на дорогом узле {a}", hot, exp_hot[a], 0, f"{exp_hot[a]} / 60"))
    p95 = mc[mc["config"] == "A-sensitivityscore"]["makespan_s"].quantile(0.95)
    out.append(("mixed-calib p95 SS", p95, 86.0, 0.5, "86 с"))

    pb = _press(_load("stage-placebo"))
    exp_pb = {"A-sensitivityscore": 71.6, "A-default": 71.2, "A-trimaran": 71.4}
    for a in arms:
        m = pb[pb["config"] == a]["makespan_s"].mean()
        out.append((f"плацебо среднее makespan {a}", m, exp_pb[a], 0.1, ru(exp_pb[a])))

    nv = _press(_load("stage-net-diff-v2"))
    exp_med = {"A-sensitivityscore": 53.0, "A-default": 59.0, "A-trimaran": 98.0}
    for a in arms:
        med = nv[(nv["config"] == a) & (nv["profile"] == "high-s-net")]["makespan_s"].median()
        out.append((f"net-diff-v2 медиана high-s-net {a}", med, exp_med[a], 0.5, f"{exp_med[a]:.0f}"))

    # Зазор размещения net-diff, пул v1+v2 (плечо SS): чувствительная vs близнец.
    nd = pd.concat([_press(_load("stage-net-diff")), nv], ignore_index=True)
    ss = nd[nd["config"] == "A-sensitivityscore"]
    for prof, exp, pat in (("high-s-net", 3, "3 из 60"), ("net-insensitive", 16, "16 из 60")):
        hot = int(((ss["profile"] == prof) & (ss["node"] == DISK_NODE)).sum())
        out.append((f"net-diff пул на шторме {prof}", hot, exp, 0, pat))
    return out


def main() -> int:
    global H, P
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=8123)
    args = ap.parse_args()
    H, P = args.ch_host, args.ch_port

    text = ARTICLE.read_text(encoding="utf-8") if ARTICLE.exists() else ""
    if not text:
        print(f"НЕ найден текст статьи: {ARTICLE}", file=sys.stderr)
        return 2

    ok = True
    print(f"{'число':44} {'из CH':>9} {'статья':>8} {'совпало':>8} {'в тексте':>9}")
    for desc, got, exp, tol, pat in checks():
        num_ok = abs(float(got) - float(exp)) <= tol
        txt_ok = pat in text
        ok = ok and num_ok and txt_ok
        gs = ru(got) if isinstance(got, float) else str(got)
        es = ru(exp) if isinstance(exp, float) else str(exp)
        print(f"{desc:44} {gs:>9} {es:>8} {'✓' if num_ok else '✗ РАСХОД':>8} "
              f"{'✓' if txt_ok else '✗ НЕТ':>9}")
    print("\nсверка с ClickHouse:", "пройдена" if ok else "ЕСТЬ РАСХОЖДЕНИЯ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
