"""Регрессионные тесты statistics — прежде всего парный тест (B3).

Запуск: analysis/.venv/bin/python -m pytest analysis/tests/ -q
(или make test-analysis).

Данные синтетические, с ЗАЛОЖЕННЫМ ответом: без этого «парный тест значим»
неотличимо от «тест сломан и всегда значим». Тот же принцип, что у
--self-test в twin_contrast/drift_check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stats import (  # noqa: E402
    MIN_PAIRS_FOR_WILCOXON,
    bootstrap_ci,
    compare_configs,
    paired_diff_ci,
    paired_test,
    plateau_onset,
    plateau_onset_paired,
    rep_level_series,
)


def _series(values: dict[int, float]) -> pd.Series:
    """rep -> value как Series с индексом rep (как rep_level_series)."""
    return pd.Series(values, dtype=float)


def test_paired_uses_wilcoxon_with_enough_pairs():
    # Плечо B всегда медленнее A на ~5 -> знаковый ранговый тест обязан
    # увидеть согласованный сдвиг и быть значимым.
    a = _series({i: 100 + i for i in range(10)})
    b = _series({i: 105 + i for i in range(10)})
    r = paired_test(a, b)
    assert r["test"] == "wilcoxon"
    assert r["paired"] is True
    assert r["n_pairs"] == 10
    assert r["p_value"] < 0.05


def test_paired_falls_back_below_min_pairs():
    # Пар меньше порога -> честный откат на непарный Манна-Уитни, помеченный.
    n = MIN_PAIRS_FOR_WILCOXON - 1
    a = _series({i: 100 + i for i in range(n)})
    b = _series({i: 105 + i for i in range(n)})
    r = paired_test(a, b)
    assert r["test"] == "mannwhitney"
    assert r["paired"] is False


def test_paired_aligns_on_rep_not_position():
    # Ключевое свойство B3: сопоставление ПО НОМЕРУ ПОВТОРА. Плечо B потеряло
    # rep 0 и 1 (ошибки задач). Пары должны быть только по общим rep 2..9 (8
    # штук), а НЕ по позиции (иначе rep2 B сравнился бы с rep0 A — мусор).
    a = _series({i: 100 + i for i in range(10)})
    b = _series({i: 105 + i for i in range(2, 10)})
    r = paired_test(a, b)
    assert r["n_pairs"] == 8
    assert r["paired"] is True
    # Разность строго +5 на каждой паре -> значимо и в одну сторону.
    assert r["p_value"] < 0.05


def test_paired_no_variance_falls_back():
    # Все разности нулевые (плечи идентичны) -> Уилкоксон неопределён;
    # обязан откатиться, а не упасть.
    a = _series({i: 100.0 for i in range(10)})
    b = _series({i: 100.0 for i in range(10)})
    r = paired_test(a, b)
    assert r["paired"] is False
    # Идентичные плечи не должны выглядеть значимо различными.
    assert not (r["p_value"] < 0.05)


def test_paired_wilcoxon_discrete_floor():
    # При 10 парах минимально достижимый двусторонний p Уилкоксона = 2/2^10.
    # Это не баг, а свойство теста; фиксируем, чтобы «слишком большой p» на
    # явном эффекте не приняли за ошибку.
    a = _series({i: 100.0 for i in range(10)})
    b = _series({i: 200.0 for i in range(10)})  # B медленнее ВСЕГДА
    r = paired_test(a, b)
    assert r["p_value"] == pytest.approx(2 / 2**10, rel=1e-6)


def test_compare_configs_exposes_both_tests():
    # compare_configs должен отдать и парный (wsr_*), и непарный (mw_*).
    reps = list(range(10))
    rows = []
    for rep in reps:
        rows.append({"config": "A-x", "profile": "p", "overcommit": 2.0,
                     "rep": rep, "makespan_s": 100 + rep})
        rows.append({"config": "A-y", "profile": "p", "overcommit": 2.0,
                     "rep": rep, "makespan_s": 108 + rep})
    df = pd.DataFrame(rows)
    res = compare_configs(df, "A-x", "A-y", "p", 2.0)
    assert res["wsr_test"] == "wilcoxon"
    assert res["wsr_paired"] is True
    assert res["wsr_p_value"] < 0.05
    assert "mw_p_value" in res  # непарный рядом для сравнения


def test_bootstrap_ci_brackets_point_and_stays_in_range():
    # Bootstrap-CI медианы: точка внутри [lo, hi], а сами границы — внутри
    # диапазона данных (percentile-бутстрап не может выйти за наблюдения).
    rng = np.random.default_rng(0)
    lo, point, hi = bootstrap_ci([1, 2, 3, 4, 5], statistic=np.median, rng=rng)
    assert point == 3.0
    assert lo <= point <= hi
    assert lo >= 1.0 and hi <= 5.0
    assert lo < hi  # есть разброс -> ненулевая ширина


def test_bootstrap_ci_no_variance_collapses():
    # Нет разброса -> CI схлопывается в точку, а не падает.
    lo, point, hi = bootstrap_ci([7.0, 7.0, 7.0, 7.0])
    assert lo == point == hi == 7.0


def test_bootstrap_ci_empty_is_nan():
    lo, point, hi = bootstrap_ci([])
    assert np.isnan(lo) and np.isnan(point) and np.isnan(hi)


def test_plateau_onset_finds_knee_by_ci_overlap():
    # Кривая «меньше = лучше»: высокий regret на весах 0,1, плато с веса 3.
    # CI веса 3 перекрывает CI лучшего (вес 10), CI весов 0/1 — нет.
    weights = [0, 1, 3, 5, 10, 20]
    points = [1.00, 0.90, 0.20, 0.18, 0.17, 0.17]
    cis = [(0.95, 1.05), (0.85, 0.95), (0.15, 0.25),
           (0.13, 0.23), (0.12, 0.22), (0.12, 0.22)]
    assert plateau_onset(weights, points, cis) == 3


def test_plateau_onset_all_overlap_returns_smallest():
    # Плоская шумная кривая: все CI перекрывают лучший -> плато с самого
    # маленького веса (робастность тривиально высокая, отклик — нет).
    weights = [0, 1, 5]
    points = [0.20, 0.19, 0.18]
    cis = [(0.10, 0.30), (0.09, 0.29), (0.08, 0.28)]
    assert plateau_onset(weights, points, cis) == 0


def test_plateau_onset_all_nan_returns_none():
    assert plateau_onset([0, 1], [float("nan"), float("nan")],
                         [(np.nan, np.nan), (np.nan, np.nan)]) is None


def test_paired_diff_ci_aligns_on_rep_not_position():
    # То же свойство, что у paired_test: сопоставление ПО НОМЕРУ ПОВТОРА.
    # Плечо b потеряло rep 0 и 1; разность обязана считаться по общим rep 2..9,
    # где она строго +5, а не по позиции (иначе получился бы мусор).
    a = _series({i: 100 + i for i in range(10)})
    b = _series({i: 95 + i for i in range(2, 10)})
    lo, point, hi = paired_diff_ci(a, b, rng=np.random.default_rng(0))
    assert point == pytest.approx(5.0)
    assert lo <= 5.0 <= hi
    assert lo > 0  # разность уверенно положительна


def test_paired_diff_ci_no_common_reps_is_nan():
    a = _series({0: 1.0, 1: 2.0})
    b = _series({5: 1.0, 6: 2.0})
    lo, point, hi = paired_diff_ci(a, b)
    assert np.isnan(lo) and np.isnan(point) and np.isnan(hi)


def test_plateau_onset_paired_ignores_marginal_ci_overlap():
    # Ключевое отличие от plateau_onset. Уровень 1 много хуже лучшего, но его
    # маргинальный CI перекрывает CI лучшего из-за большого разброса ВНУТРИ
    # уровня. Парная разность при этом устойчиво положительна на каждой паре,
    # поэтому плато обязано начаться позже.
    rng = np.random.default_rng(3)
    base = np.array([0.10, 0.30, 0.50, 0.70, 0.90, 0.11, 0.31, 0.51, 0.71, 0.91])
    series = {
        0: pd.Series(base + 0.60),   # заметно хуже
        1: pd.Series(base + 0.30),   # хуже, но CI перекрывает лучший
        3: pd.Series(base + 0.01),   # уже неотличим
        5: pd.Series(base),          # лучший
    }
    levels = [0, 1, 3, 5]
    points = [float(np.median(series[l])) for l in levels]
    cis = [bootstrap_ci(series[l].to_numpy(), rng=np.random.default_rng(1))[::2]
           for l in levels]
    # Прежний критерий обманывается разбросом внутри уровня...
    assert plateau_onset(levels, points, [tuple(c) for c in cis]) <= 1
    # ...парный — нет.
    assert plateau_onset_paired(levels, series, rng=rng) == 3


def test_plateau_onset_paired_flat_curve_starts_at_smallest():
    # Все уровни одинаковы -> плато с самого малого: критерий не должен
    # выдумывать колено там, где кривая плоская.
    rng = np.random.default_rng(5)
    vals = np.linspace(0.1, 0.9, 10)
    series = {w: pd.Series(vals) for w in (0, 1, 5)}
    assert plateau_onset_paired([0, 1, 5], series, rng=rng) == 0


def test_plateau_onset_paired_all_empty_returns_none():
    assert plateau_onset_paired([0, 1], {0: pd.Series(dtype=float),
                                         1: pd.Series(dtype=float)}) is None


def test_rep_level_series_keeps_rep_index():
    # Батч-члены схлопываются в один на повтор, индекс rep сохраняется.
    df = pd.DataFrame([
        {"config": "A-x", "rep": 0, "makespan_s": 10},
        {"config": "A-x", "rep": 0, "makespan_s": 20},  # тот же rep -> среднее 15
        {"config": "A-x", "rep": 1, "makespan_s": 30},
    ])
    s = rep_level_series(df, "A-x")
    assert list(s.index) == [0, 1]
    assert s.loc[0] == 15
    assert s.loc[1] == 30
