#!/usr/bin/env python3
"""placement_oracle.py — независимый оракул placement_regret (пункт B4 аудита).

ЗАЧЕМ. Штатный placement_regret (harness/submit/node_pressure.py) считается
через interference() — ТУ ЖЕ нормированную скор-функцию, что оптимизирует
плагин SensitivityScore. Для плеча A-sensitivityscore это тавтология: маленький
regret доказывает лишь, что плагин скомпилирован и считает свою же функцию, а
не что вектор S ведёт к физически лучшим размещениям. Оппонент возразит сразу.

ЧТО ДЕЛАЕТ ЭТОТ СКРИПТ. Строит НЕЗАВИСИМЫЙ метр качества размещения —
эмпирическую матрицу «профиль × узел → замедление», где замедление ИЗМЕРЕНО
(makespan под штормом / makespan в изоляции), а не выведено скор-функцией. Один
метр для всех плеч, к плагину отношения не имеющий.

    slowdown(job) = makespan(job) / median makespan_isolated(profile, node)
    M[profile][node] = median slowdown ПО ВСЕМ ПЛЕЧАМ (общий оракул)
    regret_measured(job) = M[profile][node_chosen] − min_node M[profile][node]

Идеальный планировщик ставит задачу на узел с наименьшим ИЗМЕРЕННЫМ
замедлением -> regret_measured ~ 0. Слепой к интерференции ставит и на дорогие
узлы -> regret_measured > 0. Матрица общая (из всех плеч), поэтому метр не
подыгрывает ни одному планировщику.

ДВА МЕТОДОЛОГИЧЕСКИХ ПРАВИЛА, как в twin_contrast:
  * агрегация на УРОВНЕ ПОВТОРОВ (не задач): задачи внутри повтора со-локированы
    намеренно и независимыми не являются (та же логика, что stats.rep_level);
  * min берётся по узлам, где профиль вообще НАБЛЮДАЛСЯ под штормом — цену
    остальных мы не измерили и придумывать её не станем.

ЗАПУСК:
  python placement_oracle.py --clickhouse --stand stage --run-label stage-llc
  python placement_oracle.py --results ../harness/results/results-stage.parquet
  python placement_oracle.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Минимум узлов с измеренным замедлением, чтобы regret вообще имел смысл: на
# одном узле «лучший узел» = «единственный», regret тождественно 0 и ничего не
# говорит.
MIN_NODES_FOR_REGRET = 2


def isolated_makespan(baselines: pd.DataFrame) -> dict[tuple[str, str], float]:
    """(profile, node) -> медиана makespan в изоляции. Знаменатель slowdown.

    Per-node намеренно: облачные «одинаковые» узлы бывают в разы разной
    скорости (урок STAGE), и общий знаменатель занизил бы slowdown на медленных
    узлах. Если для (profile, node) эталона нет, вызывающий откатывается на
    медиану по профилю — честно, но грубее (помечается)."""
    out: dict[tuple[str, str], float] = {}
    if baselines is None or baselines.empty:
        return out
    bl = baselines[baselines["makespan_s"].notna()]
    for (prof, node), g in bl.groupby(["profile", "node"]):
        out[(str(prof), str(node))] = float(g["makespan_s"].median())
    return out


def slowdown_column(df: pd.DataFrame, iso: dict[tuple[str, str], float]) -> pd.Series:
    """makespan / makespan_isolated(profile, node). NaN, если знаменателя нет
    ни для (profile, node), ни для профиля в целом."""
    prof_median: dict[str, float] = {}
    if iso:
        by_prof: dict[str, list[float]] = {}
        for (prof, _node), v in iso.items():
            by_prof.setdefault(prof, []).append(v)
        prof_median = {p: float(np.median(vs)) for p, vs in by_prof.items()}

    def _one(row) -> float:
        denom = iso.get((str(row["profile"]), str(row["node"])))
        if denom is None:
            denom = prof_median.get(str(row["profile"]))
        if not denom or denom <= 0 or pd.isna(row["makespan_s"]):
            return float("nan")
        return float(row["makespan_s"]) / denom

    return df.apply(_one, axis=1)


def empirical_slowdown_matrix(df: pd.DataFrame) -> dict[str, dict[str, float]]:
    """M[profile][node] = median slowdown ПО ВСЕМ ПЛЕЧАМ. Общий метр: строится
    из объединённых наблюдений, чтобы не подыгрывать ни одному планировщику
    (слепое плечо покрывает больше узлов, наш — меньше; вместе покрытие полнее)."""
    m: dict[str, dict[str, float]] = {}
    valid = df[df["slowdown"].notna()]
    for (prof, node), g in valid.groupby(["profile", "node"]):
        m.setdefault(str(prof), {})[str(node)] = float(g["slowdown"].median())
    return m


def hot_node(matrix: dict[str, dict[str, float]], profile: str) -> str | None:
    """Узел, где `profile` замедляется сильнее всего = штормовой узел, прочитан
    прямо из матрицы замедления. Самодостаточно: не требует колонки storm_nodes
    и автоматически верен при любом числе узлов (3 или 4). None, если профиля
    нет в матрице."""
    per_node = matrix.get(str(profile), {})
    if not per_node:
        return None
    return max(per_node, key=per_node.get)


def placement_share_on_node(df: pd.DataFrame, profile: str, node: str) -> float:
    """Доля жертв профиля `profile`, размещённых на `node`. Прямая «ступенька»
    размещения для свипа веса: с ростом веса плагин обязан уводить
    io-чувствительные жертвы С штормового узла, поэтому доля должна падать.
    NaN, если строк профиля нет."""
    rows = df[df["profile"].astype(str) == str(profile)]
    if rows.empty:
        return float("nan")
    return float((rows["node"].astype(str) == str(node)).mean())


def measured_regret(df: pd.DataFrame, matrix: dict[str, dict[str, float]]) -> pd.Series:
    """regret_measured(job) = M[profile][node] − min_node M[profile][node].

    NaN, если для профиля меньше MIN_NODES_FOR_REGRET узлов в матрице (не с чем
    сравнивать) или выбранный узел в матрице отсутствует."""
    def _one(row) -> float:
        per_node = matrix.get(str(row["profile"]), {})
        if len(per_node) < MIN_NODES_FOR_REGRET:
            return float("nan")
        chosen = per_node.get(str(row["node"]))
        if chosen is None:
            return float("nan")
        return chosen - min(per_node.values())

    return df.apply(_one, axis=1)


def regret_by_arm(df: pd.DataFrame) -> pd.DataFrame:
    """Измеренный regret по плечам, на УРОВНЕ ПОВТОРОВ.

    Сначала средний regret_measured внутри (config, rep) — задачи повтора
    со-локированы и независимыми не являются, — затем медиана/интервал по
    повторам. Рядом кладётся regret ПЛАГИНА (placement_regret из данных),
    усреднённый так же, — чтобы видеть, держится ли ранжирование плеч на
    независимом метре или только на скор-функции."""
    rows = []
    for config, g in df.groupby("config"):
        per_rep = g.groupby("rep").agg(
            regret_measured=("regret_measured", "mean"),
            regret_plugin=("placement_regret", "mean"),
        ).dropna(subset=["regret_measured"])
        if per_rep.empty:
            continue
        rm = per_rep["regret_measured"].to_numpy()
        rp = per_rep["regret_plugin"].dropna().to_numpy()
        rows.append({
            "config": str(config),
            "n_reps": int(len(per_rep)),
            "regret_measured_median": float(np.median(rm)),
            "regret_measured_iqr_lo": float(np.percentile(rm, 25)),
            "regret_measured_iqr_hi": float(np.percentile(rm, 75)),
            "regret_plugin_median": float(np.median(rp)) if len(rp) else float("nan"),
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("regret_measured_median").reset_index(drop=True)
    return out


def analyze(results: pd.DataFrame, baselines: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Полный расчёт: slowdown -> матрица -> measured_regret -> сводка по плечам."""
    df = results[results["scenario"].astype(str).str.startswith("pressure:")].copy()
    df = df[df["makespan_s"].notna()]
    iso = isolated_makespan(baselines)
    df["slowdown"] = slowdown_column(df, iso)
    matrix = empirical_slowdown_matrix(df)
    df["regret_measured"] = measured_regret(df, matrix)
    summary = regret_by_arm(df)
    return summary, matrix


def _print(summary: pd.DataFrame, matrix: dict) -> None:
    if summary.empty:
        print("нет данных для оракула (нет pressure-строк с измеренным slowdown)")
        return
    print("Эмпирическая матрица замедления M[профиль][узел] (median slowdown):")
    for prof in sorted(matrix):
        cells = "  ".join(f"{n.replace('worker-','w-')}={v:.2f}"
                          for n, v in sorted(matrix[prof].items()))
        print(f"  {prof:16} {cells}")
    print("\nИзмеренный regret размещения по плечам (меньше = лучше; "
          "уровень повторов):")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(summary.to_string(index=False))
    best = summary.iloc[0]
    print(f"\nЛучшее по ИЗМЕРЕННОМУ метру: {best['config']} "
          f"(regret_measured={best['regret_measured_median']:.3f}). "
          "Метр — эмпирическое замедление, к скор-функции плагина отношения не "
          "имеет: если ранжирование совпадает с regret_plugin, вывод «плагин "
          "размещает лучше» держится на независимом основании, а не на "
          "тавтологии.")


# --------------------------------------------------------------------------
# Самопроверка на данных с ЗАЛОЖЕННЫМ ответом.
# --------------------------------------------------------------------------
def _self_test() -> int:
    ok = True

    # Два узла: n_hot замедляет high-s вдвое, n_cold — нет. Эталон = 100 на
    # обоих. «Умное» плечо ставит всё на n_cold, «слепое» — половину на n_hot.
    def mk(config, placements):
        rows = []
        for rep, node in enumerate(placements):
            mk_iso = 100.0
            slow = 2.0 if node == "n_hot" else 1.0
            rows.append({
                "config": config, "profile": "high-s", "node": node,
                "scenario": "pressure:llc", "rep": rep,
                "makespan_s": mk_iso * slow, "placement_regret": np.nan,
            })
        return rows

    smart = mk("A-smart", ["n_cold"] * 10)               # всегда холодный
    blind = mk("A-blind", ["n_hot", "n_cold"] * 5)       # половина на горячий
    results = pd.DataFrame(smart + blind)
    baselines = pd.DataFrame([
        {"profile": "high-s", "node": n, "makespan_s": 100.0}
        for n in ("n_hot", "n_cold") for _ in range(3)
    ])

    summary, matrix = analyze(results, baselines)

    # 1. Матрица восстановила замедление: горячий ~2.0, холодный ~1.0.
    hot, cold = matrix["high-s"]["n_hot"], matrix["high-s"]["n_cold"]
    passed = abs(hot - 2.0) < 1e-9 and abs(cold - 1.0) < 1e-9
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} матрица замедления: "
          f"горячий={hot:.2f}, холодный={cold:.2f}")

    # 2. Умное плечо имеет regret ~0, слепое — заметно больше.
    s = summary.set_index("config")
    rm_smart = s.loc["A-smart", "regret_measured_median"]
    rm_blind = s.loc["A-blind", "regret_measured_median"]
    passed = abs(rm_smart) < 1e-9 and rm_blind > 0.4
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} regret: умное={rm_smart:.3f} ~0, "
          f"слепое={rm_blind:.3f} >0.4")

    # 3. Ранжирование: умное плечо первым (сортировка по regret_measured).
    passed = summary.iloc[0]["config"] == "A-smart"
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} ранжирование: лучшее = "
          f"{summary.iloc[0]['config']}")

    # 4. Метр НЕ использует скор-функцию: regret_plugin здесь весь NaN, но
    #    measured-вывод всё равно получен.
    passed = summary["regret_plugin_median"].isna().all() and not summary.empty
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} независимость: вывод есть без "
          "regret плагина")

    # 5. hot_node читает штормовой узел из матрицы (max slowdown = n_hot).
    passed = hot_node(matrix, "high-s") == "n_hot"
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} hot_node: штормовой = "
          f"{hot_node(matrix, 'high-s')}")

    # 6. Ступенька размещения: умное плечо 0% на шторме, слепое 50%.
    sh_smart = placement_share_on_node(results[results["config"] == "A-smart"],
                                       "high-s", "n_hot")
    sh_blind = placement_share_on_node(results[results["config"] == "A-blind"],
                                       "high-s", "n_hot")
    passed = abs(sh_smart) < 1e-9 and abs(sh_blind - 0.5) < 1e-9
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} ступенька: умное={sh_smart:.0%} на "
          f"шторме, слепое={sh_blind:.0%}")

    print("\nсамопроверка:", "пройдена" if ok else "ПРОВАЛЕНА")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path)
    p.add_argument("--clickhouse", action="store_true")
    p.add_argument("--stand"), p.add_argument("--run-label")
    p.add_argument("--ch-host", default="localhost")
    p.add_argument("--ch-port", type=int, default=8123)
    p.add_argument("--ch-database", default="sensitivityscore")
    p.add_argument("--ch-user", default="default"), p.add_argument("--ch-password", default="")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        return _self_test()

    if args.clickhouse:
        from clickhouse_source import load_from_clickhouse
        labels = [args.run_label] if args.run_label else None
        results = load_from_clickhouse(
            "results", host=args.ch_host, port=args.ch_port,
            database=args.ch_database, user=args.ch_user, password=args.ch_password,
            stand=args.stand, run_labels=labels)
        baselines = load_from_clickhouse(
            "baselines", host=args.ch_host, port=args.ch_port,
            database=args.ch_database, user=args.ch_user, password=args.ch_password,
            stand=args.stand, run_labels=labels)
    elif args.results:
        from load import load_results
        results = load_results(args.results)
        bl_path = Path(str(args.results).replace("results", "baselines"))
        baselines = load_results(bl_path) if bl_path.exists() else pd.DataFrame()
    else:
        p.error("укажи --results <файл> или --clickhouse")
        return 2

    summary, matrix = analyze(results, baselines)
    _print(summary, matrix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
