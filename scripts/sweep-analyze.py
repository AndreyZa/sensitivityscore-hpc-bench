#!/usr/bin/env python3
"""Сводит свип веса (C2): measured-regret плеча A-sensitivityscore как функция
веса плагина, плюс критерий плато. Запускается фазой analyze в weight-sweep.sh.

Матрица замедления M[профиль][узел] строится ОДНА, из ОБЪЕДИНЁННЫХ наблюдений
всех прогонов (ref + все веса): замедление узла под штормом — свойство физики
(шторм на w9), а не планировщика, и общая матрица даёт стабильный знаменатель.
regret_measured каждого веса — насколько размещения A-ss@вес близки к минимуму
этой общей матрицы. Метр независим от скор-функции плагина (см. B4).

Критерий (зафиксирован в weight-sweep.sh ДО прогона): минимальный вес, чья
ПАРНАЯ разность regret с лучшим весом уверенно ниже границы практической
несущественности (верх 95% CI разности < margin, margin = 10% размаха).
Прежние редакции — перекрытие маргинальных CI и голое «10% размаха» —
печатаются рядом справочно; почему отвергнуты, см. stats.plateau_onset_paired.
Рядом — прямая ступенька размещения: доля high-s-io на штормовом узле по весу.
Печатается колено; решение за человеком.

Счёт вынесен в analyze_sweep() и покрыт `--self-test` на синтетике с
ЗАЛОЖЕННЫМ ответом. Причина та же, что у twin_contrast и drift_check: сам свип
идёт часами, и узнавать об опечатке в разборе результатов после него — дорого.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ANALYSIS = Path(__file__).resolve().parent.parent / "analysis"
sys.path.insert(0, str(ANALYSIS))
from placement_oracle import (  # noqa: E402
    empirical_slowdown_matrix,
    hot_node,
    isolated_makespan,
    measured_regret,
    placement_share_on_node,
    slowdown_column,
)
from stats import bootstrap_ci, plateau_onset, plateau_onset_paired  # noqa: E402

IO_SENSITIVE_PROFILE = "high-s-io"  # профиль-жертва, которую плагин должен уводить с шторма
SS_ARM = "A-sensitivityscore"


def _load(label, host, port):
    from clickhouse_source import load_from_clickhouse
    return load_from_clickhouse("results", host=host, port=port,
                                stand="stage", run_labels=[label])


def analyze_sweep(allrows: pd.DataFrame, baselines: pd.DataFrame, *,
                  profile: str = IO_SENSITIVE_PROFILE, seed: int = 12345):
    """Весь счёт свипа: (press, matrix, hot, curve, knee, knee_marginal).

    Отделён от загрузки и печати, чтобы его можно было прогнать на синтетике
    до того, как появятся настоящие данные.
    """
    press = allrows[allrows["scenario"].astype(str).str.startswith("pressure:")].copy()
    press = press[press["makespan_s"].notna()]

    # ОБЩАЯ матрица замедления из всех наблюдений (физика узлов, не планировщик).
    iso = isolated_makespan(baselines)
    press["slowdown"] = slowdown_column(press, iso)
    matrix = empirical_slowdown_matrix(press)
    press["regret_measured"] = measured_regret(press, matrix)

    # Штормовой узел — из матрицы (узел max slowdown профиля-жертвы); ступенька
    # размещения строится относительно него, число узлов не хардкодится.
    hot = hot_node(matrix, profile)

    # regret_measured(A-ss) по весам, на уровне повторов + 95% bootstrap-CI и
    # прямая ступенька размещения (доля жертвы на штормовом узле).
    rng = np.random.default_rng(seed)  # детерминированный bootstrap (воспроизводимость)
    rows = []
    by_weight = {}          # вес -> (повторение -> regret): для парного критерия
    ss = press[(press["config"] == SS_ARM) & (press["weight"] >= 0)]
    for w, g in ss.groupby("weight"):
        per_rep = g.groupby("rep")["regret_measured"].mean().dropna()
        if per_rep.empty:
            continue
        by_weight[int(w)] = per_rep
        lo, point, hi = bootstrap_ci(per_rep.to_numpy(), statistic=np.median, rng=rng)
        share_hot = (placement_share_on_node(g, profile, hot)
                     if hot else float("nan"))
        rows.append({"weight": int(w), "n_reps": int(len(per_rep)),
                     "regret_measured": point, "ci_lo": lo, "ci_hi": hi,
                     "share_hot": share_hot})
    curve = (pd.DataFrame(rows, columns=["weight", "n_reps", "regret_measured",
                                         "ci_lo", "ci_hi", "share_hot"])
             .sort_values("weight").reset_index(drop=True))

    knee = knee_marginal = None
    margin = float("nan")
    if len(curve) >= 2:
        levels = curve["weight"].tolist()
        # Основной критерий — парная разность с лучшим весом.
        knee, margin = plateau_onset_paired(
            levels, by_weight, rng=np.random.default_rng(seed + 1),
            return_margin=True)
        # Прежний (перекрытие маргинальных CI) — рядом, для сопоставимости.
        knee_marginal = plateau_onset(levels, curve["regret_measured"].tolist(),
                                      list(zip(curve["ci_lo"], curve["ci_hi"])))
    return press, matrix, hot, curve, knee, knee_marginal, margin


def _print_report(matrix, hot, curve, knee, knee_marginal, margin,
                  profile=IO_SENSITIVE_PROFILE) -> None:
    print("Общая матрица замедления M[профиль][узел] (median slowdown):")
    for prof in sorted(matrix):
        cells = "  ".join(f"{n.replace('worker-','w-')}={v:.2f}"
                          for n, v in sorted(matrix[prof].items()))
        print(f"  {prof:16} {cells}")

    show = curve.copy()
    show["regret [95% CI]"] = show.apply(
        lambda r: f"{r['regret_measured']:.3f} [{r['ci_lo']:.3f}, {r['ci_hi']:.3f}]", axis=1)
    show["жертва на шторме"] = show["share_hot"].map(
        lambda x: "—" if pd.isna(x) else f"{x:.0%}")
    print(f"\nmeasured-regret плеча {SS_ARM} по весу (меньше = лучше):")
    print(show[["weight", "n_reps", "regret [95% CI]", "жертва на шторме"]]
          .to_string(index=False))
    if hot:
        print(f"\nШтормовой узел (max slowdown {profile}) = {hot}. "
              f"Доля {profile} на нём — прямая ступенька размещения: с ростом "
              f"веса должна падать (плагин уводит жертву со шторма).")

    # Критерий плато (пред-регистрирован): минимальный вес, парная разность
    # regret которого с лучшим весом неотличима от нуля. Прежние редакции
    # печатаются рядом — видно, насколько они расходятся на этих данных.
    if len(curve) >= 2:
        if knee is not None:
            verdict = "НА плато" if knee <= 5 else "НИЖЕ плато — сигнал ещё перекрыт"
            print(f"\nПЛАТО с веса {knee}: парная разность regret с лучшим "
                  f"весом уверенно ниже границы margin = {margin:.4f} "
                  f"(10% размаха). Текущий weight=5 {verdict}.")
        if knee_marginal is not None and knee_marginal != knee:
            print(f"(прежний критерий — перекрытие маргинальных CI — дал бы "
                  f"{knee_marginal}; он слабее, см. stats.plateau_onset_paired)")
        # Справочно — прежний критерий «10% размаха», для сопоставимости.
        r = curve["regret_measured"].to_numpy()
        span = r.max() - r.min()
        old = None
        if span > 0:
            old = next((int(curve["weight"].iloc[i]) for i in range(len(r) - 1)
                        if (r[i] - r[i + 1]) < 0.10 * span), None)
        print(f"(справочно, прежний критерий 10% размаха: колено = {old})")


# --------------------------------------------------------------------------
# Синтетика с заложенным ответом
# --------------------------------------------------------------------------

HOT, COLD_A, COLD_B = "n_hot", "n_cold_a", "n_cold_b"
# Доля жертвы на штормовом узле по весу: падает и выходит на полку с веса 3.
# Заложенный ответ: плато не раньше 3 и не позже 5, ступенька монотонна.
PLANTED_SHARE = {0: 0.6, 1: 0.5, 2: 0.3, 3: 0.1, 5: 0.1, 10: 0.1, 20: 0.1, 40: 0.1}
PLANTED_PLATEAU = (3, 5)


def _synthetic(reps: int = 10, per_rep: int = 20, seed: int = 7):
    """Стенд-игрушка: жертва на штормовом узле медленнее вдвое, нечувствительная
    задача его почти не замечает. Планировщик с ростом веса уводит жертву."""
    rng = np.random.default_rng(seed)
    slow = {(IO_SENSITIVE_PROFILE, HOT): 2.0, ("low-s", HOT): 1.1}

    def makespan(profile, node):
        # Шум обязателен: без разброса bootstrap-CI схлопывается в точку и
        # критерий плато становится тривиальным — проверять было бы нечего.
        return 100.0 * slow.get((profile, node), 1.0) * float(rng.normal(1.0, 0.03))

    bl = [{"profile": p, "node": n, "makespan_s": 100.0}
          for p in (IO_SENSITIVE_PROFILE, "low-s") for n in (HOT, COLD_A, COLD_B)]

    rows = []
    n_victims = per_rep // 2
    # regret_measured зависит ТОЛЬКО от размещения: замедление берётся из общей
    # матрицы, а не из времени конкретной задачи. Значит, разброс по
    # повторениям создаёт исключительно разброс размещений — при одинаковом
    # размещении во всех повторах bootstrap-CI схлопывается в точку и критерий
    # плато проверять нечем. Отсюда джиттер с нулевой суммой: доля по серии
    # остаётся ровно заложенной, а повторения различаются, как на стенде.
    jitter = [-1, 0, 1, 0, -1, 1, 0, 0, 1, -1]
    assert sum(jitter) == 0
    for weight, share in PLANTED_SHARE.items():
        for rep in range(reps):
            on_hot = int(round(share * n_victims)) + jitter[rep % len(jitter)]
            on_hot = max(0, min(n_victims, on_hot))
            for i in range(per_rep):
                victim = i % 2 == 0
                profile = IO_SENSITIVE_PROFILE if victim else "low-s"
                if victim:
                    k = i // 2
                    node = HOT if k < on_hot else (COLD_A, COLD_B)[k % 2]
                else:
                    node = (HOT, COLD_A, COLD_B)[(i // 2) % 3]
                rows.append({"config": SS_ARM, "profile": profile, "node": node,
                             "rep": rep, "weight": weight, "scenario": "pressure:io",
                             "makespan_s": makespan(profile, node)})
    return pd.DataFrame(rows), pd.DataFrame(bl)


def _self_test() -> int:
    ok = True

    def check(cond, msg):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'OK ' if cond else 'НЕТ'} {msg}")

    res, bl = _synthetic()
    press, matrix, hot, curve, knee, knee_marg, _m = analyze_sweep(res, bl)

    check(hot == HOT, f"штормовой узел найден по матрице: {hot}")
    check(len(curve) == len(PLANTED_SHARE),
          f"кривая построена по всем весам: {len(curve)} из {len(PLANTED_SHARE)}")
    check(curve["n_reps"].min() == 10, "все веса на 10 повторениях")

    first, last = curve.iloc[0], curve.iloc[-1]
    check(first["regret_measured"] > last["regret_measured"],
          f"regret падает с весом: {first['regret_measured']:.3f} -> "
          f"{last['regret_measured']:.3f}")
    check(first["share_hot"] > last["share_hot"] + 0.2,
          f"ступенька размещения видна: {first['share_hot']:.0%} -> "
          f"{last['share_hot']:.0%}")
    check(knee is not None and PLANTED_PLATEAU[0] <= knee <= PLANTED_PLATEAU[1],
          f"плато найдено в заложенном месте: {knee} (ожидалось "
          f"{PLANTED_PLATEAU[0]}..{PLANTED_PLATEAU[1]})")
    # Расхождение двух критериев проверяется отдельно, на данных, построенных
    # ровно под него (test_plateau_onset_paired_ignores_marginal_ci_overlap):
    # на чистой синтетике они совпадают, и требовать здесь расхождения нельзя.
    check(knee_marg is not None, f"прежний критерий тоже посчитан: {knee_marg}")
    check((curve["ci_lo"] < curve["ci_hi"]).all(),
          "доверительные интервалы ненулевой ширины")

    # Отрицательный контроль: если плагин ничего не меняет, колена быть не
    # должно раньше самого маленького веса — иначе критерий рисует эффект там,
    # где его нет.
    flat_share = dict.fromkeys(PLANTED_SHARE, 0.4)
    saved = dict(PLANTED_SHARE)
    try:
        PLANTED_SHARE.clear(); PLANTED_SHARE.update(flat_share)
        res2, bl2 = _synthetic(seed=11)
        _, _, _, curve2, knee2, _, _ = analyze_sweep(res2, bl2)
        check(knee2 == curve2["weight"].iloc[0],
              f"на плоской кривой плато с самого малого веса: {knee2}")
    finally:
        PLANTED_SHARE.clear(); PLANTED_SHARE.update(saved)

    # Пропуск веса (фаза не отработала) не должен ронять разбор.
    res3 = res[res["weight"] != 3]
    _, _, _, curve3, _, _, _ = analyze_sweep(res3, bl)
    check(3 not in curve3["weight"].tolist() and len(curve3) == len(curve) - 1,
          "пропущенный вес просто выпадает из кривой")

    # Пустые данные — не исключение, а пустая кривая.
    empty = res.iloc[0:0]
    _, _, _, curve4, knee4, _, _ = analyze_sweep(empty, bl)
    check(curve4.empty and knee4 is None, "пустой вход даёт пустую кривую")

    print("\nсамопроверка:", "пройдена" if ok else "ПРОВАЛЕНА")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--weights", help="список весов через пробел")
    p.add_argument("--ch-host", default="localhost")
    p.add_argument("--ch-port", type=int, default=8123)
    p.add_argument("--self-test", action="store_true",
                   help="прогнать счёт на синтетике с заложенным ответом")
    args = p.parse_args()
    if args.self_test:
        return _self_test()
    if not args.weights:
        p.error("нужен --weights (или --self-test)")
    weights = [int(w) for w in args.weights.split()]

    from clickhouse_source import load_from_clickhouse
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
    _, matrix, hot, curve, knee, knee_marginal, margin = analyze_sweep(
        allrows, baselines)
    if curve.empty:
        print(f"нет наблюдений плеча {SS_ARM} под давлением — нечего сводить")
        return 1
    _print_report(matrix, hot, curve, knee, knee_marginal, margin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
