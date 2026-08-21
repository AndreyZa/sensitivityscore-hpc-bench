"""stats.py — statistical analysis per docs/Технический_план_экспериментов.md §5.2.

Deliberately NOT using ANOVA/t-test by default: with only ~10 repetitions per plan
point there's no strong basis to assume normality, and HPC timings are often
right-skewed due to noisy-neighbor effects. Instead:

- Mann-Whitney U     — between two configs at the same plan point (same profile/overcommit)
- Cliff's delta      — effect size, more robust to outliers than Cohen's d for this data
- Coefficient of variation (CV = sigma/mu) — for comparing *stability* (H1) across
  configs with different absolute makespans, where comparing raw variance directly
  would be misleading.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu, wilcoxon

# Порог пар для парного теста. Ниже — Уилкоксон при n<~5-6 почти не имеет
# мощности (минимальный достижимый p ограничен снизу числом пар), и падать в
# него смысла нет: честнее отработать непарным Манна-Уитни, чем показать
# «p=0.06» там, где парный тест просто не мог дать меньше.
MIN_PAIRS_FOR_WILCOXON = 6


def mann_whitney(sample_a: np.ndarray, sample_b: np.ndarray) -> dict:
    """Two-sided Mann-Whitney U test between two makespan samples."""
    sample_a = np.asarray(sample_a, dtype=float)
    sample_b = np.asarray(sample_b, dtype=float)
    sample_a = sample_a[~np.isnan(sample_a)]
    sample_b = sample_b[~np.isnan(sample_b)]

    if len(sample_a) < 2 or len(sample_b) < 2:
        return {
            "u_statistic": np.nan,
            "p_value": np.nan,
            "n_a": len(sample_a),
            "n_b": len(sample_b),
        }

    u_stat, p_value = mannwhitneyu(sample_a, sample_b, alternative="two-sided")
    return {
        "u_statistic": u_stat,
        "p_value": p_value,
        "n_a": len(sample_a),
        "n_b": len(sample_b),
    }


def paired_test(series_a: pd.Series, series_b: pd.Series) -> dict:
    """Парный тест между плечами, выровненными ПО НОМЕРУ ПОВТОРА (B3).

    Дизайн парный: повтор №k обоих плеч снят в одной сессии, при одних и тех
    же условиях стенда (соседи по гипервизору, прогрев, остаточный кэш). Тест
    для НЕзависимых выборок (Манна-Уитни) держит всю межповторную вариацию в
    шуме, хотя дизайн её физически исключает. Критерий Уилкоксона по парам её
    снимает — и при n=10 это разница между «значимо» и «нет» на одном эффекте.

    Выравниваем по общему множеству rep (плечо могло потерять повтор из-за
    ошибки задачи), отбрасываем пары с NaN. Если пар < MIN_PAIRS_FOR_WILCOXON
    или все разности нулевые — честно откатываемся на Манна-Уитни, помечая
    paired=False, чтобы в отчёте было видно, что тест не парный.
    """
    a = series_a.dropna()
    b = series_b.dropna()
    common = a.index.intersection(b.index)
    pa, pb = a.loc[common].to_numpy(dtype=float), b.loc[common].to_numpy(dtype=float)
    diffs = pa - pb

    if len(common) >= MIN_PAIRS_FOR_WILCOXON and np.any(diffs != 0):
        stat, p_value = wilcoxon(pa, pb)  # two-sided по умолчанию
        return {
            "test": "wilcoxon", "paired": True, "p_value": float(p_value),
            "statistic": float(stat), "n_pairs": int(len(common)),
        }
    # Fallback: пар мало или нет разброса — непарный тест по всем значениям.
    mw = mann_whitney(a.to_numpy(), b.to_numpy())
    return {
        "test": "mannwhitney", "paired": False, "p_value": mw["p_value"],
        "statistic": mw["u_statistic"], "n_pairs": int(len(common)),
    }


def cliffs_delta(sample_a: np.ndarray, sample_b: np.ndarray) -> dict:
    """Cliff's delta effect size: proportion of pairs where a > b minus proportion
    where a < b, in [-1, 1]. Chosen over Cohen's d for robustness to the
    outlier-heavy tails typical of HPC makespan measurements (§5.2).

    Interpretation (Romano et al. 2006 thresholds, commonly cited):
        |delta| < 0.147          negligible
        0.147 <= |delta| < 0.33  small
        0.33  <= |delta| < 0.474 medium
        |delta| >= 0.474         large
    """
    a = np.asarray(sample_a, dtype=float)
    b = np.asarray(sample_b, dtype=float)
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]

    if len(a) == 0 or len(b) == 0:
        return {"delta": np.nan, "magnitude": "n/a"}

    # O(n*m) pairwise comparison — fine at n,m ~= 10 (repetitions per plan point).
    greater = sum(1 for x in a for y in b if x > y)
    less = sum(1 for x in a for y in b if x < y)
    delta = (greater - less) / (len(a) * len(b))

    abs_delta = abs(delta)
    if abs_delta < 0.147:
        magnitude = "negligible"
    elif abs_delta < 0.33:
        magnitude = "small"
    elif abs_delta < 0.474:
        magnitude = "medium"
    else:
        magnitude = "large"

    return {"delta": delta, "magnitude": magnitude}


def coefficient_of_variation(sample: np.ndarray) -> float:
    """CV = sigma / mu. Used instead of raw stddev to compare *stability* between
    configs whose absolute makespan differs (docs §5.2: "иначе разные по
    абсолютному makespan конфигурации некорректно сравнивать по дисперсии
    напрямую")."""
    sample = np.asarray(sample, dtype=float)
    sample = sample[~np.isnan(sample)]
    if len(sample) < 2 or np.mean(sample) == 0:
        return float("nan")
    return float(np.std(sample, ddof=1) / np.mean(sample))


def rep_level_sample(
    df: pd.DataFrame, config: str, value_col: str = "makespan_s"
) -> np.ndarray:
    """Collapses concurrent batch members to one value (their mean) per rep.

    Members of one batch run co-located ON PURPOSE — they interfere with each
    other, so they are not independent observations. Treating each member as a
    separate sample inflates n by the batch size (2-4x at overcommit >= 1.0)
    and makes Mann-Whitney's p-values overconfident. One repetition = one
    independent observation; its value is the mean makespan across the batch.
    """
    return rep_level_series(df, config, value_col).to_numpy()


def rep_level_series(
    df: pd.DataFrame, config: str, value_col: str = "makespan_s"
) -> pd.Series:
    """Как rep_level_sample, но с СОХРАНЁННЫМ индексом rep — нужно парному
    тесту (paired_test), чтобы сопоставить повтор №k одного плеча с тем же
    повтором другого. Голый массив rep_level_sample этот индекс теряет."""
    rows = df[df["config"] == config]
    if rows.empty:
        return pd.Series(dtype=float)
    return rows.groupby("rep")[value_col].mean()


def holm_bonferroni(p_values) -> np.ndarray:
    """Holm-Bonferroni step-down adjustment across a family of comparisons.

    The H1-H4 sweep runs one Mann-Whitney test per (pair, profile, overcommit)
    — up to ~20 tests; at alpha=0.05 uncorrected, one spurious "significant"
    point is expected by chance alone. NaN p-values (empty comparisons) are
    passed through and don't count toward the family size m.
    """
    p = np.asarray(p_values, dtype=float)
    adjusted = np.full(p.shape, np.nan)
    mask = ~np.isnan(p)
    m = int(mask.sum())
    if m == 0:
        return adjusted
    order = np.argsort(np.where(mask, p, np.inf))
    running_max = 0.0
    for rank, i in enumerate(order[:m]):
        running_max = max(running_max, (m - rank) * p[i])
        adjusted[i] = min(1.0, running_max)
    return adjusted


def bootstrap_ci(
    values, statistic=np.median, n_boot: int = 2000, alpha: float = 0.05, rng=None
) -> tuple[float, float, float]:
    """Percentile-bootstrap CI вокруг `statistic` по rep-уровневым значениям.

    Возвращает (lo, point, hi) на уровне (1-alpha). Нужен свипу веса (C2), чтобы
    судить о плато по ПЕРЕКРЫТИЮ CI, а не по голому «падение < 10% размаха»: при
    ~10 повторах и дискретном пространстве размещений (3-4 узла) кривая regret
    шумная, и перекрытие CI даёт честный тест «не значимо ниже» — широкие CI
    дают inconclusive, а не ложное колено.

    `statistic` обязан принимать axis (np.median/np.mean подходят): бутстрап
    векторизован через (n_boot, n)-матрицу индексов.
    """
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return (float("nan"), float("nan"), float("nan"))
    point = float(statistic(v))
    if len(v) == 1:
        return (point, point, point)
    rng = rng if rng is not None else np.random.default_rng()
    idx = rng.integers(0, len(v), size=(n_boot, len(v)))
    boots = statistic(v[idx], axis=1)
    lo = float(np.percentile(boots, 100 * alpha / 2))
    hi = float(np.percentile(boots, 100 * (1 - alpha / 2)))
    return (lo, point, hi)


def plateau_onset(levels, points, cis):
    """Минимальный уровень, чей CI перекрывается с CI лучшего (минимального по
    point) — начало плато для кривой «меньше = лучше» (свип веса C2).

    levels — ось x (веса), points — точечная оценка на уровень, cis — список
    (lo, hi). Уровень «на плато», если его CI перекрывает CI лучшего уровня
    (не значимо хуже достижимого минимума). Пред-регистрированный критерий;
    возвращает уровень-колено или None, если валидных точек нет. Лучший уровень
    перекрывает сам себя, поэтому при непустом входе всегда что-то вернётся.
    """
    lv, pts, ci = list(levels), list(points), list(cis)
    valid = [i for i in range(len(pts)) if not np.isnan(pts[i])]
    if not valid:
        return None
    best = min(valid, key=lambda i: pts[i])
    blo, bhi = ci[best]
    for i in sorted(valid, key=lambda i: lv[i]):
        lo, hi = ci[i]
        if lo <= bhi and blo <= hi:  # перекрытие CI с лучшим уровнем
            return lv[i]
    return lv[best]


def paired_diff_ci(a, b, n_boot=2000, alpha=0.05, rng=None):
    """Бутстрап-CI медианы ПАРНОЙ разности (a − b), сопоставление по ключу.

    a, b — отображения «повторение -> величина» (Series с индексом rep или
    dict). Пары берутся по общим ключам, как в paired_test: сопоставление по
    номеру повтора, а не по позиции. Возвращает (lo, point, hi) или NaN, если
    общих пар нет.
    """
    sa = a if isinstance(a, pd.Series) else pd.Series(a, dtype=float)
    sb = b if isinstance(b, pd.Series) else pd.Series(b, dtype=float)
    common = sa.index.intersection(sb.index)
    if len(common) == 0:
        return float("nan"), float("nan"), float("nan")
    diff = (sa.loc[common] - sb.loc[common]).dropna().to_numpy(dtype=float)
    if diff.size == 0:
        return float("nan"), float("nan"), float("nan")
    return bootstrap_ci(diff, statistic=np.median, n_boot=n_boot, alpha=alpha, rng=rng)


PLATEAU_MARGIN_FRACTION = 0.10   # доля размаха кривой, признаваемая несущественной


def plateau_onset_paired(levels, series, margin=None, n_boot=2000, alpha=0.05,
                         rng=None, return_margin=False):
    """Минимальный уровень, который НЕ ХУЖЕ лучшего более чем на margin:
    верхняя граница 95% CI парной разности (уровень − лучший) ниже margin.

    levels — ось x (веса); series — {уровень: повторение -> величина};
    margin — граница практической несущественности в тех же единицах, что и
    величина. По умолчанию 10% размаха медиан по уровням (см.
    PLATEAU_MARGIN_FRACTION) — та же шкала, что у прежнего эвристического
    критерия «10% размаха», но теперь с доверительным утверждением.

    Почему именно так — два отвергнутых варианта.

    (1) Перекрытие двух МАРГИНАЛЬНЫХ CI (plateau_onset) — слишком слабо.
    Интервалы могут перекрываться, тогда как разность значима, и на десяти
    повторениях это происходит систематически: на синтетике уровень с regret
    0,317 против лучшего 0,022 объявлялся «плато» только потому, что нижняя
    граница его интервала заходила под верхнюю границу лучшего.

    (2) «CI парной разности накрывает 0» — слишком строго, и в этом суть.
    Дизайн парный (один поток через все веса), поэтому мощность высока, и
    устойчивая разница в 1% отвергает плато. Но вопрос свипа не «есть ли хоть
    какая-то разница», а «мал ли проигрыш настолько, что весом можно не
    рисковать». Это утверждение о неменьшей эффективности, и его нельзя
    сформулировать без границы: «практически эквивалентно» требует шкалы.

    Отсюда правило: плато с первого уровня, про который можно уверенно сказать
    «хуже лучшего меньше чем на margin». Широкие интервалы дают честное
    отсутствие плато (вернётся лучший уровень), а не ложное колено.
    """
    lv = list(levels)
    pts = {}
    for level in lv:
        s = series.get(level)
        s = s if isinstance(s, pd.Series) else pd.Series(s or {}, dtype=float)
        s = s.dropna()
        pts[level] = float(np.median(s.to_numpy())) if len(s) else float("nan")
    valid = [level for level in lv if not np.isnan(pts[level])]
    if not valid:
        return (None, float("nan")) if return_margin else None
    best = min(valid, key=lambda level: pts[level])

    if margin is None:
        span = max(pts[level] for level in valid) - pts[best]
        margin = PLATEAU_MARGIN_FRACTION * span

    answer = best
    for level in sorted(valid, key=lambda x: lv.index(x)):
        if level == best:
            break
        _, _, hi = paired_diff_ci(series[level], series[best],
                                  n_boot=n_boot, alpha=alpha, rng=rng)
        if np.isnan(hi):
            continue
        if hi <= margin:       # уверенно не хуже лучшего более чем на margin
            answer = level
            break
    return (answer, float(margin)) if return_margin else answer


def compare_configs(
    df: pd.DataFrame,
    config_a: str,
    config_b: str,
    profile: str,
    overcommit: float,
    value_col: str = "makespan_s",
) -> dict:
    """Runs the full §5.2 comparison (Mann-Whitney + Cliff's delta + CV for both
    sides) for one (config_a vs config_b, profile, overcommit) plan point.
    Samples are rep-level (see rep_level_sample): n = number of repetitions,
    not repetitions x batch members."""
    subset = df[(df["profile"] == profile) & (df["overcommit"] == overcommit)]
    series_a = rep_level_series(subset, config_a, value_col)
    series_b = rep_level_series(subset, config_b, value_col)
    sample_a, sample_b = series_a.to_numpy(), series_b.to_numpy()

    result = {
        "config_a": config_a,
        "config_b": config_b,
        "profile": profile,
        "overcommit": overcommit,
        "mean_a": float(np.nanmean(sample_a)) if len(sample_a) else np.nan,
        "mean_b": float(np.nanmean(sample_b)) if len(sample_b) else np.nan,
        "cv_a": coefficient_of_variation(sample_a),
        "cv_b": coefficient_of_variation(sample_b),
    }
    # Манна-Уитни оставлен рядом (mw_*) намеренно: вердикт теперь по ПАРНОМУ
    # тесту (wsr_*), но видеть, что даёт непарный, полезно — расхождение и есть
    # цена дизайна (парный снимает межповторную вариацию, непарный держит её в
    # шуме).
    result.update({f"mw_{k}": v for k, v in mann_whitney(sample_a, sample_b).items()})
    result.update({f"wsr_{k}": v for k, v in paired_test(series_a, series_b).items()})
    result.update(
        {f"cliffs_{k}": v for k, v in cliffs_delta(sample_a, sample_b).items()}
    )
    return result


def run_all_comparisons(
    df: pd.DataFrame, pairs: list[tuple[str, str]], value_col: str = "makespan_s"
) -> pd.DataFrame:
    """Runs compare_configs for every (config_a, config_b) pair across every
    (profile, overcommit) combination present in df — the standard sweep for
    testing H1-H4. Adds wsr_p_holm (ПАРНЫЙ тест, Holm-скорректированный по
    всему свипу как одной семье) — именно из него читаются вердикты H1-H4
    (B3). mw_p_holm (непарный) оставлен рядом для сравнения."""
    rows = []
    for profile in sorted(df["profile"].dropna().unique()):
        for overcommit in sorted(df["overcommit"].dropna().unique()):
            for config_a, config_b in pairs:
                rows.append(
                    compare_configs(
                        df, config_a, config_b, profile, overcommit, value_col
                    )
                )
    out = pd.DataFrame(rows)
    if not out.empty:
        out["mw_p_holm"] = holm_bonferroni(out["mw_p_value"])
        out["wsr_p_holm"] = holm_bonferroni(out["wsr_p_value"])
    return out


# ---------------------------------------------------------------- самопроверка

def self_test() -> int:
    """Оценки проверяются на данных с ИЗВЕСТНЫМ ответом.

    Модуль до сих пор был без самотеста, хотя из него приходит каждый
    интервал в обеих статьях: bootstrap_ci и paired_diff_ci дают числа,
    которые печатаются в таблицах. Ошибка здесь не выглядит как ошибка —
    интервал просто оказывается не тем, и это неотличимо на глаз.
    """
    rng = np.random.default_rng(0)

    # 1. Точечная оценка — ровно медиана, интервал её накрывает.
    v = np.arange(1.0, 100.0)
    lo, point, hi = bootstrap_ci(v, rng=np.random.default_rng(1))
    assert point == 50.0, point
    assert lo < 50.0 < hi, (lo, hi)

    # 2. Одинаковое зерно — одинаковый интервал (числа статьи обязаны
    #    воспроизводиться командой, а не «примерно»).
    a1 = bootstrap_ci(v, rng=np.random.default_rng(7))
    a2 = bootstrap_ci(v, rng=np.random.default_rng(7))
    assert a1 == a2, (a1, a2)

    # 3. Покрытие. Главное свойство интервала: 95% интервалов накрывают
    #    истинную медиану. Проверяем прямым перебором — на 300 выборках
    #    доля обязана лежать около 0,95; широкий допуск взят под шум самой
    #    оценки доли, узкий превратил бы тест в ложно падающий.
    hits = 0
    trials = 300
    for _ in range(trials):
        s = rng.normal(loc=10.0, scale=2.0, size=25)
        l, _, h = bootstrap_ci(s, n_boot=400, rng=rng)
        hits += l <= 10.0 <= h
    cover = hits / trials
    assert 0.86 <= cover <= 1.0, f"покрытие {cover:.2f} вместо ~0,95"

    # 4. Парная разность: сопоставление ПО КЛЮЧУ, а не по позиции. Плечо
    #    может потерять повторение, и позиционное выравнивание тогда молча
    #    сравнит разные прогоны — интервал получится не тот, а сообщения
    #    об ошибке не будет.
    base = pd.Series({r: 100.0 + r for r in range(10)})
    arm = pd.Series({r: 110.0 + r for r in range(10)})
    shuffled = arm.sample(frac=1.0, random_state=3)      # тот же индекс, другой порядок
    lo, point, hi = paired_diff_ci(shuffled, base, rng=np.random.default_rng(2))
    assert abs(point - 10.0) < 1e-9, point
    assert abs(lo - 10.0) < 1e-9 and abs(hi - 10.0) < 1e-9, (lo, hi)

    # ...и пары берутся только по ОБЩИМ ключам.
    partial = arm.drop(index=[0, 1, 2])
    _, point2, _ = paired_diff_ci(partial, base, rng=np.random.default_rng(2))
    assert abs(point2 - 10.0) < 1e-9, point2
    assert np.isnan(paired_diff_ci(pd.Series({99: 1.0}), base)[1])

    # 5. Cliff's delta на разделённых выборках — ровно ±1.
    assert cliffs_delta([10, 11, 12], [1, 2, 3])["delta"] == 1.0
    assert cliffs_delta([1, 2, 3], [10, 11, 12])["delta"] == -1.0
    assert cliffs_delta([1, 2, 3], [1, 2, 3])["magnitude"] == "negligible"

    # 6. Holm: наименьшее p умножается на m, наибольшее не трогается,
    #    монотонность сохраняется, NaN не считается в семью.
    adj = holm_bonferroni([0.01, 0.04, 0.03, np.nan])
    assert abs(adj[0] - 0.03) < 1e-12, adj
    assert np.isnan(adj[3])
    assert adj[1] >= adj[2] >= adj[0], adj

    # 7. Повторение — одно наблюдение: члены пачки схлопываются средним.
    df = pd.DataFrame({"config": ["A"] * 4, "rep": [0, 0, 1, 1],
                       "makespan_s": [10.0, 20.0, 30.0, 50.0]})
    s = rep_level_series(df, "A")
    assert list(s) == [15.0, 40.0], list(s)

    # 8. Вариация — безразмерная и сравнима между плечами разного масштаба.
    assert abs(coefficient_of_variation([10, 12, 14]) -
               coefficient_of_variation([100, 120, 140])) < 1e-12

    print("self-test: ок (медиана и её интервал, воспроизводимость по зерну, "
          "покрытие 95%, парность по ключу, Cliff's delta, Holm, "
          "схлопывание пачки, CV)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
