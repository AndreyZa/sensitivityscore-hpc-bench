#!/usr/bin/env python3
"""drift_check.py — есть ли внутрисессионный дрейф стенда (пункт B2 аудита).

ЗАЧЕМ. Порядок плеч во всех девяти сериях жёстко фиксирован: trimaran всегда
последний. Значит позиция в прогоне слита с личностью планировщика, и любой
монотонный тренд внутри повторения — остаточный page cache после дискового
шторма, прогрев, соседи по гипервизору — бьёт по плечам систематически и в
одну сторону.

Это ГИПОТЕЗА, а не установленный факт, и проверяется она на уже собранных
данных. Исход важен обоими концами:

  * тренда нет  -> рандомизация порядка плеч не нужна, пункт закрыт, а в
                   разделе об угрозах валидности появляется ЧИСЛО вместо
                   оговорки «порядок мог влиять»;
  * тренд есть  -> для будущих серий порядок надо рандомизировать, а девять
                   уже снятых требуют явной оговорки в тексте.

КАК. Внутри одного плеча (config × profile × overcommit) смотрим связь между
индексом повтора и makespan:

  * Спирмен (ранговый, монотонный тренд) — не требует линейности и устойчив
    к выбросам, в отличие от Пирсона;
  * наклон линейной регрессии в % за повтор — чтобы видеть РАЗМЕР эффекта,
    а не только значимость: при n=10 «не значимо» слишком часто означает
    «мало данных», и одно p-значение тут вводит в заблуждение;
  * сравнение первых и последних повторов — прямая, читаемая величина.

Наблюдение = ПОВТОР, а не задача: участники батча со-локированы намеренно и
независимыми наблюдениями не являются (та же логика, что в stats.rep_level_sample).

Поправка на множественность обязательна: плеч в серии до десятка, и при
alpha=0.05 без коррекции одно «значимое» плечо ожидается просто по случайности
— то есть без Холма мы бы почти гарантированно «нашли» дрейф в любых данных.

ЗАПУСК:
  python drift_check.py --results ../harness/results/results-stage.parquet
  python drift_check.py --clickhouse --stand stage --run-label stage-llc
  python drift_check.py --self-test        # проверка самого скрипта
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stats import holm_bonferroni  # noqa: E402

ARM_KEYS = ["config", "profile", "overcommit"]


def rep_series(df: pd.DataFrame, value_col: str = "makespan_s") -> pd.Series:
    """Один повтор -> одно значение (среднее по батчу). Индекс — номер повтора."""
    return df.groupby("rep")[value_col].mean().sort_index()


def arm_drift(sample: pd.Series) -> dict:
    """Тренд внутри одного плеча. NaN-поля, если данных на вывод не хватает."""
    reps = sample.index.to_numpy(dtype=float)
    vals = sample.to_numpy(dtype=float)
    ok = ~np.isnan(vals)
    reps, vals = reps[ok], vals[ok]
    out = {
        "n_reps": int(len(vals)),
        "rho": float("nan"),
        "p": float("nan"),
        "slope_pct_per_rep": float("nan"),
        "first_half_mean": float("nan"),
        "second_half_mean": float("nan"),
        "delta_pct": float("nan"),
    }
    if len(vals) < 4:
        # Спирмен на трёх точках выдаёт |rho|=1 почти всегда — считать его
        # здесь значит производить уверенность из ничего.
        return out

    rho, p = spearmanr(reps, vals)
    out["rho"], out["p"] = float(rho), float(p)

    mean = float(np.mean(vals))
    if mean:
        slope = float(np.polyfit(reps, vals, 1)[0])
        out["slope_pct_per_rep"] = 100.0 * slope / mean

    half = len(vals) // 2
    first, second = float(np.mean(vals[:half])), float(np.mean(vals[-half:]))
    out["first_half_mean"], out["second_half_mean"] = first, second
    if first:
        out["delta_pct"] = 100.0 * (second - first) / first
    return out


def check_drift(df: pd.DataFrame, value_col: str = "makespan_s") -> pd.DataFrame:
    """По плечу на строку + p с поправкой Холма по всему семейству плеч."""
    rows = []
    for keys, arm in df.groupby(ARM_KEYS, dropna=False):
        res = arm_drift(rep_series(arm, value_col))
        rows.append({**dict(zip(ARM_KEYS, keys)), **res})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["p_holm"] = holm_bonferroni(out["p"].to_numpy())
    return out.sort_values("p", na_position="last").reset_index(drop=True)


def verdict(res: pd.DataFrame, alpha: float = 0.05) -> str:
    """Читаемый вывод — именно он отвечает на вопрос «делать ли B2»."""
    if res.empty:
        return "нет данных"
    sig = res[res["p_holm"] < alpha]
    worst = res["slope_pct_per_rep"].abs().max()
    if sig.empty:
        return (
            f"ДРЕЙФА НЕ НАЙДЕНО: ни одно из {len(res)} плеч не значимо после Холма "
            f"(alpha={alpha}); максимальный наклон {worst:.2f}% за повтор.\n"
            "  -> рандомизация порядка плеч не требуется; в раздел об угрозах\n"
            "     валидности идёт это число, а не оговорка."
        )
    return (
        f"ДРЕЙФ ЕСТЬ: {len(sig)} из {len(res)} плеч значимы после Холма "
        f"(alpha={alpha}); максимальный наклон {worst:.2f}% за повтор.\n"
        "  -> порядок плеч в будущих сериях рандомизировать; для девяти уже\n"
        "     снятых — явная оговорка в тексте (позиция слита с планировщиком)."
    )


def _self_test() -> int:
    """Проверка на данных с ИЗВЕСТНЫМ ответом.

    Без неё «дрейфа не найдено» неотличимо от «скрипт ничего не умеет
    находить» — а это ровно тот класс ошибки, который здесь дороже всего.
    """
    rng = np.random.default_rng(0)
    ok = True

    def frame(makespans_by_rep, config="A-default"):
        rows = []
        for rep, vals in enumerate(makespans_by_rep):
            for i, v in enumerate(vals):
                rows.append({"config": config, "profile": "high-s", "overcommit": 2.0,
                             "rep": rep, "batch_index": i, "makespan_s": v})
        return pd.DataFrame(rows)

    # 1. Чистый шум -> тренда быть не должно.
    noise = frame([[100 + rng.normal(0, 3), 100 + rng.normal(0, 3)] for _ in range(10)])
    r = check_drift(noise)
    passed = bool(r["p_holm"].iloc[0] >= 0.05)
    print(f"  {'OK ' if passed else 'НЕТ'} шум: тренд не найден (p_holm={r['p_holm'].iloc[0]:.3f})")
    ok &= passed

    # 2. Явный монотонный рост 2% за повтор -> обязан найтись.
    trend = frame([[100 * (1 + 0.02 * rep) + rng.normal(0, 1),
                    100 * (1 + 0.02 * rep) + rng.normal(0, 1)] for rep in range(10)])
    r = check_drift(trend)
    passed = bool(r["p_holm"].iloc[0] < 0.05 and r["slope_pct_per_rep"].iloc[0] > 1.0)
    print(f"  {'OK ' if passed else 'НЕТ'} рост 2%/повтор: найден "
          f"(p_holm={r['p_holm'].iloc[0]:.4f}, наклон={r['slope_pct_per_rep'].iloc[0]:.2f}%)")
    ok &= passed

    # 3. Три повтора -> отказ считать, а не выдумывание |rho|=1.
    short = frame([[100.0], [110.0], [120.0]])
    r = check_drift(short)
    passed = bool(np.isnan(r["rho"].iloc[0]))
    print(f"  {'OK ' if passed else 'НЕТ'} 3 повтора: считать отказался (rho={r['rho'].iloc[0]})")
    ok &= passed

    # 4. Поправка Холма применяется по семейству плеч.
    many = pd.concat([frame([[100 + rng.normal(0, 3)] for _ in range(10)], config=f"arm{i}")
                      for i in range(8)])
    r = check_drift(many)
    passed = bool((r["p_holm"] >= r["p"]).all())
    print(f"  {'OK ' if passed else 'НЕТ'} Холм: p_holm >= p по всем {len(r)} плечам")
    ok &= passed

    print("\nсамопроверка:", "пройдена" if ok else "ПРОВАЛЕНА")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path, help="results.parquet")
    p.add_argument("--clickhouse", action="store_true", help="читать из ClickHouse")
    p.add_argument("--stand"), p.add_argument("--run-label")
    p.add_argument("--ch-host", default="localhost"), p.add_argument("--ch-port", type=int, default=8123)
    p.add_argument("--ch-database", default="sensitivityscore")
    p.add_argument("--ch-user", default="default"), p.add_argument("--ch-password", default="")
    p.add_argument("--value-col", default="makespan_s")
    p.add_argument("--self-test", action="store_true", help="проверить скрипт на данных с известным ответом")
    args = p.parse_args()

    if args.self_test:
        return _self_test()

    if args.clickhouse:
        from clickhouse_source import load_from_clickhouse
        df, _ = load_from_clickhouse(
            host=args.ch_host, port=args.ch_port, database=args.ch_database,
            user=args.ch_user, password=args.ch_password,
            stand=args.stand, run_label=args.run_label,
        )
    elif args.results:
        from load import load_results
        df = load_results(args.results)
    else:
        p.error("укажи --results <файл> или --clickhouse (или --self-test)")

    df = df[df["approximation"].astype(str).eq("") | df["approximation"].isna()
            | ~df["approximation"].astype(str).str.startswith(("error:", "missing"))]
    res = check_drift(df, args.value_col)
    if res.empty:
        print("нет пригодных строк")
        return 1

    cols = ["config", "profile", "overcommit", "n_reps", "rho", "p", "p_holm",
            "slope_pct_per_rep", "delta_pct"]
    print(res[cols].to_string(index=False, float_format=lambda v: f"{v:.4g}"))
    print()
    print(verdict(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
