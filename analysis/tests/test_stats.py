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
    compare_configs,
    paired_test,
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
