#!/usr/bin/env python3
"""model_adequacy.py — адекватность модели мощности: остатки, классы моделей,
качество ранжирования размещений.

ЗАЧЕМ ОТДЕЛЬНЫМ СКРИПТОМ. Три блока чисел энергостатьи — сравнение классов
моделей на удаляемой точке (§5: экспонента 52,3 Вт против кусочно-линейной
23,1), решения Peaks против оптимума по измерению (таблица 5) и качество
ранжирования (145 пар, 32 % неверных) — считались разово и в репозитории
не воспроизводились ничем. Проверка figures/make_figures.py --check ловит
расхождение статьи с figdata.json, но не может поймать неверное число В
САМОМ figdata: она сличает копии, а не пересчитывает.

Здесь всё три блока пересчитываются из лестницы (analysis/p1-calib/) и
сверяются с опубликованными: `--check` возвращает ненулевой код при
расхождении.

  model_adequacy.py --csv analysis/p1-calib/calib-ipmi.csv
  model_adequacy.py --csv ... --check ../energy-hpc-paper/figures/figdata.json
  model_adequacy.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "fit_power_model", HERE / "fit_power_model.py")
fpm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fpm)

# Размеры пода в свипе ранжирования. Не «сколько попало»: 10–50 % ёмкости
# узла покрывают дозы жертв серий (28 ядер из 64 — это 44 %) с запасом в
# обе стороны. Пары, где под не влезает (u + pod > 100), в свип не входят.
POD_SIZES = (10, 20, 30, 40, 50)
LOW_UTIL_PCT = 50      # «оба узла загружены не более чем наполовину»
TABLE_PODS = (10, 20, 30)   # строки таблицы 5 статьи


def read_ladder(path, step: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    """Ступени лестницы: утилизация приводится к НОМИНАЛЬНОЙ сетке.

    В CSV лежит фактически достигнутая утилизация (0,15 вместо 0, 90,06
    вместо 90) — она и должна там лежать, это измерение. Но свип
    ранжирования спрашивает «какой из двух УРОВНЕЙ дешевле», а уровни
    задавались номинальные; отклонение в десятые доли процентного пункта
    — шум удержания нагрузки, а не отдельный уровень.

    Разница не косметическая: на фактических значениях верхний кандидат
    каждого размера пода отсекается условием «под влезает» (90,06 + 10 >
    100), и вместо 145 пар свип даёт 110 — то есть другое число в статье
    при тех же данных. Приведение к сетке проверяется: отклонение больше
    процентного пункта означает, что удержание нагрузки не сработало, и
    тогда молча округлять нельзя.
    """
    xs, ws = [], []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            xs.append(float(row["x"]))
            ws.append(float(row["watts"]))
    x = np.asarray(xs, float)
    nominal = np.round(x / step) * step
    drift = float(np.max(np.abs(x - nominal)))
    if drift > 1.0:
        raise ValueError(f"фактическая утилизация отклоняется от номинала на "
                         f"{drift:.2f} п.п. — приводить к сетке нельзя")
    order = np.argsort(nominal)
    return nominal[order], np.asarray(ws, float)[order]


def _rmse(a, b) -> float:
    return float(np.sqrt(np.mean((np.asarray(a) - np.asarray(b)) ** 2)))


def loo_compare(x: np.ndarray, w: np.ndarray, knee: float = 80.0) -> dict:
    """СКО на удаляемой точке для трёх классов модели.

    Каждая точка по очереди исключается, модель строится по остальным и
    предсказывает исключённую. Это и отвечает на вопрос «дело в шуме или в
    форме»: подгонка по всем точкам всегда льстит, проверка на удаляемой —
    нет.
    """
    err = {"exp": [], "lin": [], "piecewise": []}
    idx = np.arange(len(x))
    for i in idx:
        keep = idx != i
        xt, wt, xv, wv = x[keep], w[keep], x[i], w[i]

        k0, k1, k2 = fpm.fit_points(xt, wt)
        err["exp"].append(k0 + k1 * np.exp(k2 * xv) - wv)

        a, b = np.polyfit(xt, wt, 1)
        err["lin"].append(a * xv + b - wv)

        # Кусочно-линейная с изломом: две прямые, каждая по своей ветви.
        # Ветвь, оставшаяся с одной точкой, не определяет прямую — тогда
        # честнее интерполяция по оставшимся точкам, чем выдуманный наклон.
        lo, hi = xt <= knee, xt >= knee
        if lo.sum() >= 2 and hi.sum() >= 2:
            al, bl = np.polyfit(xt[lo], wt[lo], 1)
            ah, bh = np.polyfit(xt[hi], wt[hi], 1)
            pred = al * xv + bl if xv <= knee else ah * xv + bh
        else:
            pred = float(np.interp(xv, xt, wt))
        err["piecewise"].append(pred - wv)
    return {k: round(_rmse(v, np.zeros(len(v))), 1) for k, v in err.items()}


def placement_costs(x: np.ndarray, w: np.ndarray, fit: tuple,
                    pod: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Цена размещения пода на узле с утилизацией x: прирост мощности.

    Возвращает (утилизации-кандидаты, цена по модели, цена по измерению).
    Кандидат — ступень лестницы, на которую под ещё влезает.
    """
    k0, k1, k2 = fit
    pmod = lambda t: k0 + k1 * np.exp(k2 * t)
    cand = x[x + pod <= x.max() + 1e-9]
    model = np.array([pmod(u + pod) - pmod(u) for u in cand])
    meas = np.array([np.interp(u + pod, x, w) - np.interp(u, x, w) for u in cand])
    return cand, model, meas


def peaks_table(x, w, fit) -> dict:
    """Таблица 5: какой узел выбирает Peaks по модели и какой дешевле по
    измерению. Выбор модели — минимум её собственной цены; оптимум —
    минимум измеренной."""
    pods, model_cost, best_cost, over, node_model, node_best = [], [], [], [], [], []
    for pod in TABLE_PODS:
        cand, model, meas = placement_costs(x, w, fit, pod)
        i_model, i_best = int(np.argmin(model)), int(np.argmin(meas))
        pods.append(pod)
        node_model.append(round(float(cand[i_model])))
        node_best.append(round(float(cand[i_best])))
        model_cost.append(round(float(meas[i_model])))   # ЦЕНА ФАКТИЧЕСКАЯ
        best_cost.append(round(float(meas[i_best])))
        over.append(round(float(meas[i_model] - meas[i_best])))
    return {"pods_pct": pods, "node_by_model_pct": node_model,
            "node_best_pct": node_best, "model_cost_w": model_cost,
            "best_cost_w": best_cost, "overspend_w": over}


def ranking_quality(x, w, fit) -> dict:
    """Доля пар узлов, где модель ранжирует хуже измерения.

    Пара — два разных уровня утилизации; вопрос к модели один: на какой из
    двух узлов дешевле поставить под. Это НЕ то же, что точность
    предсказания мощности, и в этом весь смысл проверки.
    """
    pairs = wrong = 0
    excess = []
    low_pairs = low_wrong = 0
    for pod in POD_SIZES:
        cand, model, meas = placement_costs(x, w, fit, pod)
        for i in range(len(cand)):
            for j in range(i + 1, len(cand)):
                pairs += 1
                low = cand[i] <= LOW_UTIL_PCT and cand[j] <= LOW_UTIL_PCT
                low_pairs += low
                pick = i if model[i] <= model[j] else j
                best = i if meas[i] <= meas[j] else j
                if pick != best and abs(meas[i] - meas[j]) > 1e-9:
                    wrong += 1
                    low_wrong += low
                    excess.append(abs(meas[pick] - meas[best]))
    return {
        "pairs": pairs, "wrong": wrong,
        "wrong_pct": round(100.0 * wrong / pairs) if pairs else 0,
        "median_excess_w": round(float(np.median(excess))) if excess else 0,
        "max_excess_w": round(float(np.max(excess))) if excess else 0,
        "low_util_wrong_pct": round(100.0 * low_wrong / low_pairs) if low_pairs else 0,
    }


def compute(csv_path) -> dict:
    x, w = read_ladder(csv_path)
    fit = fpm.fit_points(x, w)
    loo = loo_compare(x, w)
    return {
        "modelfit": {"loo_exp_w": loo["exp"], "loo_lin_w": loo["lin"],
                     "loo_piecewise_w": loo["piecewise"]},
        "peaks_decisions": peaks_table(x, w, fit),
        "ranking": ranking_quality(x, w, fit),
    }


def check(computed: dict, figdata_path) -> int:
    """Сверка с опубликованным. Расхождение — ненулевой код."""
    pub = json.loads(pathlib.Path(figdata_path).read_text(encoding="utf-8"))
    bad = []
    for block, values in computed.items():
        for key, got in values.items():
            if key not in pub.get(block, {}):
                continue                      # в figdata этого поля нет — не наше дело
            want = pub[block][key]
            same = (list(got) == list(want) if isinstance(got, list)
                    else abs(float(got) - float(want)) < 0.51)
            if not same:
                bad.append(f"  {block}.{key}: в статье {want}, пересчёт {got}")
    if bad:
        print("ЧИСЛА АДЕКВАТНОСТИ МОДЕЛИ НЕ СХОДЯТСЯ С ОПУБЛИКОВАННЫМИ:",
              file=sys.stderr)
        print("\n".join(bad), file=sys.stderr)
        return 1
    print("адекватность модели: пересчитано из лестницы, сходится с figdata")
    return 0


def self_test() -> int:
    # Ровно экспоненциальная лестница: модель точна, значит ранжирует верно
    # всюду, а СКО на удаляемой точке у экспоненты близко к нулю.
    x = np.arange(0, 101, 10, dtype=float)
    w = 300.0 + 4.0 * np.exp(0.046 * x)
    fit = fpm.fit_points(x, w)
    rq = ranking_quality(x, w, fit)
    assert rq["pairs"] == 145, rq["pairs"]
    assert rq["wrong"] == 0, rq
    loo = loo_compare(x, w)
    assert loo["exp"] < 1.0, loo
    assert loo["lin"] > loo["exp"], loo

    # Лестница со скачком от холостого хода — та форма, из-за которой
    # модель и ошибается на реальном железе: первый же процент утилизации
    # стоит дорого, дальше прирост дёшев, поэтому САМЫЙ ДЕШЁВЫЙ узел не
    # пустой. Монотонная экспонента такого выразить не может и обязана
    # ранжировать неверно; кусочно-линейная с изломом — быть точнее.
    # (Первая версия теста брала выпуклую кусочно-линейную без скачка —
    # на ней приросты внутри нижней ветви равны, ошибок ранжирования нет
    # по построению, и тест был зелёным, ничего не проверяя.)
    w2 = np.array([260, 310, 320, 330, 340, 350, 360, 370, 380, 460, 540.])
    fit2 = fpm.fit_points(x, w2)
    rq2 = ranking_quality(x, w2, fit2)
    assert rq2["wrong"] == 25 and rq2["low_util_wrong_pct"] == 29, rq2
    loo2 = loo_compare(x, w2)
    assert loo2["piecewise"] < loo2["exp"], loo2

    # Таблица 5: перерасход неотрицателен по построению (оптимум — минимум).
    for over in peaks_table(x, w2, fit2)["overspend_w"]:
        assert over >= 0, over

    print("self-test: ок (145 пар в свипе, точная модель не ошибается, "
          "излом ловится кусочно-линейной, перерасход неотрицателен)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--csv", default=str(HERE / "p1-calib" / "calib-ipmi.csv"))
    ap.add_argument("--check", default="", help="figdata.json для сверки")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    result = compute(args.csv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return check(result, args.check) if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
