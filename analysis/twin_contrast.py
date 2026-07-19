#!/usr/bin/env python3
"""twin_contrast.py — контраст двойников на штормовом узле (пункт B1 аудита).

ЗАЧЕМ. Ключевые числа блока 3 сводки (замедление чувствительного профиля
против его двойника, ×1.70 / ×6.11, и зазор по размещению) не производились
НИ ОДНОЙ строкой кода — они посчитаны вручную и живут только текстом. Ни
проверить их через год, ни показать оппоненту нечем. Этот скрипт делает их
воспроизводимыми и, попутно, снимает два методологических возражения.

ЧТО ТАКОЕ ДВОЙНИК. Профиль с ТЕМ ЖЕ compute и теми же ресурсами, но НЕ
заявляющий чувствительность к оси (high-s-net против net-insensitive,
high-s-io против io-insensitive). Пара устраняет конфаунд упаковки: если
чувствительный медленнее двойника на одном и том же штормовом узле, разница
не объясняется ни размером задачи, ни её requests.

ДВА ВОЗРАЖЕНИЯ, КОТОРЫЕ СКРИПТ ОБЯЗАН СНЯТЬ.

1. Псевдорепликация. Наблюдение — ПОВТОР, а не задача: жертвы внутри
   повторения со-локированы намеренно и независимыми не являются. Считать
   n=30 вместо n=10 значит завышать значимость втрое (та же логика, что в
   stats.rep_level_sample).

2. Отбор по пост-трактментной переменной. «Взять строки, попавшие на
   штормовой узел» — это обусловливание на исходе, который выбрал САМ
   планировщик: в плече sensitivityscore туда попадают не случайные задачи,
   а те, которые плагин туда пустил. Поэтому контраст считается ОТДЕЛЬНО ПО
   ПЛЕЧАМ, а плечо default выделено особо: там размещение слепо к вектору S,
   и попадание на шторм ближе всего к случайному.

ЧТО СЧИТАЕТСЯ.
  * отношение медиан makespan (чувствительный / двойник) на штормовом узле,
    на уровне повторений, с бутстрап-интервалом — интервал здесь важнее
    p-значения: он показывает, насколько эффект вообще определён при n=10;
  * критерий Уилкоксона по парам повторений, когда пары есть (дизайн парный),
    иначе Манна-Уитни;
  * зазор по размещению: доля повторений, в которых чувствительный попал на
    шторм, против той же доли у двойника — на уровне повторений, а не задач.

ЗАПУСК:
  python twin_contrast.py --results ../harness/results/results-stage.parquet \\
      --pair high-s-net:net-insensitive
  python twin_contrast.py --clickhouse --stand stage --run-label stage-net-diff \\
      --pair high-s-net:net-insensitive
  python twin_contrast.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu, wilcoxon

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Ось -> колонка измеренного давления. Нужна только когда штормовой узел
# приходится ВЫВОДИТЬ (серии до появления колонки storm_nodes).
AXIS_PRESSURE = {
    "net": "net_pressure",
    "io": "io_pressure",
    "llc": "llc_miss_rate",
    "numa": "numa_remote_ratio",
}

# Известные пары. Двойник объявлен в harness/profiles.py как «тот же compute,
# те же ресурсы, но без заявленной чувствительности».
DEFAULT_PAIRS = {
    "high-s-net": ("net-insensitive", "net"),
    "high-s-io": ("io-insensitive", "io"),
}


def storm_nodes_for(df: pd.DataFrame, axis: str,
                    explicit: str | None = None) -> pd.Series:
    """Штормовой узел на каждую (rep) — из данных, а не из догадки.

    Приоритет: --storm-node (оператор берёт узел из конфига серии, storms[].node)
    -> колонка storm_nodes (факт постановки, пишется с 19.07) -> вывод по
    максимуму ИЗМЕРЕННОГО давления оси.

    Вывод по давлению ОПАСЕН на оси net: чувствительная жертва сама льёт
    трафик (профиль high-s-net шлёт 2 ГБ наружу) и поднимает net_pressure
    СВОЕГО узла выше штормового. Тогда «штормовым» объявляется узел, где
    жертва и работала, контраст вырождается в разницу профилей на чистом
    узле (их эталоны 49 и 28 с -> ×1.7), а настоящий шторм в выборку не
    попадает. Признак вырождения — storm_share_sensitive == 1.0 во всех
    плечах; он проверяется в main() и печатается предупреждением.
    """
    if explicit:
        return pd.Series(explicit, index=sorted(df["rep"].dropna().unique()), dtype=object)
    if "storm_nodes" in df.columns and df["storm_nodes"].astype(str).str.len().gt(0).any():
        # Берём первый узел списка: сценарии стенда штормят один узел.
        return (df.groupby("rep")["storm_nodes"].agg(
            lambda s: next((str(v).split(";")[0] for v in s if str(v)), "")))

    col = AXIS_PRESSURE[axis]
    if col not in df.columns:
        return pd.Series(dtype=str)
    per_node = df.groupby(["rep", "node"])[col].mean().reset_index()
    idx = per_node.groupby("rep")[col].idxmax()
    return per_node.loc[idx].set_index("rep")["node"]


def bootstrap_ratio_ci(a: np.ndarray, b: np.ndarray, n_boot: int = 10000,
                       seed: int = 0, alpha: float = 0.05) -> tuple[float, float, float]:
    """Отношение медиан a/b и его перцентильный бутстрап-интервал.

    Интервал, а не только точечная оценка: при n=10 «×6.11» без границ ничего
    не говорит о том, насколько число устойчиво.
    """
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 2 or len(b) < 2 or np.median(b) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    point = float(np.median(a) / np.median(b))
    boots = np.empty(n_boot)
    for i in range(n_boot):
        ra = rng.choice(a, size=len(a), replace=True)
        rb = rng.choice(b, size=len(b), replace=True)
        mb = np.median(rb)
        boots[i] = np.median(ra) / mb if mb else np.nan
    boots = boots[~np.isnan(boots)]
    lo, hi = np.percentile(boots, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return point, float(lo), float(hi)


def contrast_one_arm(df: pd.DataFrame, sensitive: str, twin: str, axis: str,
                     value_col: str = "makespan_s",
                     storm_node: str | None = None) -> dict:
    """Контраст на штормовом узле внутри ОДНОГО плеча, на уровне повторений."""
    storm = storm_nodes_for(df, axis, explicit=storm_node)
    inferred = not (storm_node or ("storm_nodes" in df.columns
                    and df["storm_nodes"].astype(str).str.len().gt(0).any()))
    out = {
        "n_reps_sensitive": 0, "n_reps_twin": 0,
        "ratio": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan"),
        "test": "", "p": float("nan"),
        "storm_share_sensitive": float("nan"), "storm_share_twin": float("nan"),
        "storm_node_inferred": inferred,
    }
    if storm.empty:
        return out

    on_storm = df[df.apply(lambda r: r["node"] == storm.get(r["rep"], None), axis=1)]

    def rep_vals(profile: str) -> pd.Series:
        sub = on_storm[on_storm["profile"] == profile]
        return sub.groupby("rep")[value_col].median().sort_index()

    a, b = rep_vals(sensitive), rep_vals(twin)
    out["n_reps_sensitive"], out["n_reps_twin"] = len(a), len(b)
    if len(a) < 2 or len(b) < 2:
        return out

    out["ratio"], out["ci_lo"], out["ci_hi"] = bootstrap_ratio_ci(a.to_numpy(), b.to_numpy())

    # Дизайн парный: тот же повтор — те же условия стенда. Если пары есть,
    # Уилкоксон снимает межповторную вариацию; иначе честно откатываемся.
    common = a.index.intersection(b.index)
    if len(common) >= 5:
        out["test"] = f"wilcoxon(n={len(common)})"
        out["p"] = float(wilcoxon(a.loc[common].to_numpy(), b.loc[common].to_numpy()).pvalue)
    else:
        out["test"] = f"mannwhitney(n={len(a)},{len(b)})"
        out["p"] = float(mannwhitneyu(a.to_numpy(), b.to_numpy(), alternative="two-sided").pvalue)

    # Зазор по размещению — на уровне повторений: доля повторов, в которых
    # профиль вообще попал на штормовой узел.
    for tag, profile in (("sensitive", sensitive), ("twin", twin)):
        sub = df[df["profile"] == profile]
        if sub.empty:
            continue
        hit = sub.apply(lambda r: r["node"] == storm.get(r["rep"], None), axis=1)
        out[f"storm_share_{tag}"] = float(hit.groupby(sub["rep"]).any().mean())
    return out


def twin_contrast(df: pd.DataFrame, sensitive: str, twin: str, axis: str,
                  value_col: str = "makespan_s",
                  storm_node: str | None = None) -> pd.DataFrame:
    """По плечу на строку. Плечи НЕ объединяются: попадание на штормовой узел
    в плече sensitivityscore выбирал сам планировщик."""
    rows = []
    for config, arm in df.groupby("config", dropna=False):
        res = contrast_one_arm(arm, sensitive, twin, axis, value_col, storm_node)
        rows.append({"config": config, **res})
    return pd.DataFrame(rows).sort_values("config").reset_index(drop=True)


def _self_test() -> int:
    """Данные с ИЗВЕСТНЫМ ответом: иначе «эффекта нет» неотличимо от
    «скрипт не умеет его находить»."""
    rng = np.random.default_rng(0)
    ok = True

    def frame(effect: float, n_reps: int = 10, storm="w1", nodes=("w1", "w2", "w3")):
        rows = []
        for rep in range(n_reps):
            for profile, mult in (("high-s-net", effect), ("net-insensitive", 1.0)):
                for i, node in enumerate(nodes):
                    base = 100.0 * (mult if node == storm else 1.0)
                    rows.append({
                        "config": "A-default", "profile": profile, "rep": rep,
                        "node": node, "batch_index": i,
                        "makespan_s": base + rng.normal(0, 2),
                        "net_pressure": 0.9 if node == storm else 0.1,
                        "storm_nodes": storm, "approximation": "",
                    })
        return pd.DataFrame(rows)

    # 1. Заложен эффект ×1.7 -> должен восстановиться, CI не должен накрывать 1.
    r = twin_contrast(frame(1.7), "high-s-net", "net-insensitive", "net").iloc[0]
    passed = bool(1.5 < r["ratio"] < 1.9 and r["ci_lo"] > 1.0 and r["p"] < 0.05)
    print(f"  {'OK ' if passed else 'НЕТ'} эффект ×1.7 восстановлен: ×{r['ratio']:.2f} "
          f"[{r['ci_lo']:.2f}; {r['ci_hi']:.2f}], p={r['p']:.4g}, {r['test']}")
    ok &= passed

    # 2. Эффекта нет -> CI обязан накрывать 1, значимости быть не должно.
    r = twin_contrast(frame(1.0), "high-s-net", "net-insensitive", "net").iloc[0]
    passed = bool(r["ci_lo"] <= 1.0 <= r["ci_hi"] and r["p"] >= 0.05)
    print(f"  {'OK ' if passed else 'НЕТ'} эффекта нет: ×{r['ratio']:.2f} "
          f"[{r['ci_lo']:.2f}; {r['ci_hi']:.2f}], p={r['p']:.3g}")
    ok &= passed

    # 3. n=10 повторов, а не n=30 задач — псевдорепликации быть не должно.
    r = twin_contrast(frame(1.7), "high-s-net", "net-insensitive", "net").iloc[0]
    passed = bool(r["n_reps_sensitive"] == 10 and "n=10" in r["test"])
    print(f"  {'OK ' if passed else 'НЕТ'} наблюдение = повтор: n={r['n_reps_sensitive']}, {r['test']}")
    ok &= passed

    # 4. Без колонки storm_nodes узел выводится по давлению — и тот же ответ.
    df = frame(1.7).drop(columns=["storm_nodes"])
    r = twin_contrast(df, "high-s-net", "net-insensitive", "net").iloc[0]
    passed = bool(1.5 < r["ratio"] < 1.9 and r["storm_node_inferred"])
    print(f"  {'OK ' if passed else 'НЕТ'} без storm_nodes: вывод по давлению, ×{r['ratio']:.2f}, "
          f"помечен как выведенный={r['storm_node_inferred']}")
    ok &= passed

    # 4б. Самодавление жертвы: чувствительная сама поднимает давление своей оси
    # на СВОЁМ узле выше штормового (реальный случай оси net — 2 ГБ egress).
    # Вывод по давлению тогда указывает на узел жертвы и вырождает контраст;
    # явный --storm-node обязан вернуть правильный ответ.
    dfs = frame(1.7).drop(columns=["storm_nodes"]).copy()
    victim_self = (dfs["profile"] == "high-s-net")
    dfs.loc[dfs["node"] == "w1", "net_pressure"] = 0.5   # шторм умеренный по счётчику
    dfs.loc[victim_self, "net_pressure"] = 0.99          # жертва «шумит» громче шторма
    dfs = dfs[~(victim_self & (dfs["node"] == "w1"))]    # на штормовой узел не попала ни разу
    r_bad = twin_contrast(dfs, "high-s-net", "net-insensitive", "net").iloc[0]
    r_ok = twin_contrast(dfs, "high-s-net", "net-insensitive", "net", storm_node="w1").iloc[0]
    passed = bool(r_bad["storm_share_sensitive"] == 1.0 and np.isnan(r_ok["ratio"]))
    print(f"  {'OK ' if passed else 'НЕТ'} самодавление жертвы: вывод по давлению вырождается "
          f"(доля «на шторме»={r_bad['storm_share_sensitive']:.2f}), "
          f"--storm-node честно говорит «данных нет» (ratio={r_ok['ratio']})")
    ok &= passed

    # 5. Плечи не смешиваются.
    two = pd.concat([frame(1.7), frame(1.0).assign(config="A-sensitivityscore")])
    r = twin_contrast(two, "high-s-net", "net-insensitive", "net")
    passed = bool(len(r) == 2 and r.set_index("config").loc["A-default", "ratio"] > 1.4
                  and abs(r.set_index("config").loc["A-sensitivityscore", "ratio"] - 1.0) < 0.15)
    print(f"  {'OK ' if passed else 'НЕТ'} плечи раздельно: "
          f"{dict(zip(r['config'], r['ratio'].round(2)))}")
    ok &= passed

    print("\nсамопроверка:", "пройдена" if ok else "ПРОВАЛЕНА")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path)
    p.add_argument("--clickhouse", action="store_true")
    p.add_argument("--stand"), p.add_argument("--run-label")
    p.add_argument("--ch-host", default="localhost"), p.add_argument("--ch-port", type=int, default=8123)
    p.add_argument("--ch-database", default="sensitivityscore")
    p.add_argument("--ch-user", default="default"), p.add_argument("--ch-password", default="")
    p.add_argument("--pair", help="чувствительный:двойник, напр. high-s-net:net-insensitive")
    p.add_argument("--axis", help="ось шторма (net|io|llc|numa); по умолчанию — из известных пар")
    p.add_argument("--storm-node", help="узел шторма из конфига серии (storms[].node) — "
                                        "факт постановки; сильнее вывода по давлению")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        return _self_test()

    if args.pair:
        sensitive, _, twin = args.pair.partition(":")
        axis = args.axis or DEFAULT_PAIRS.get(sensitive, (None, "net"))[1]
    else:
        p.error("укажи --pair <чувствительный>:<двойник> (известные: "
                + ", ".join(f"{k}:{v[0]}" for k, v in DEFAULT_PAIRS.items()) + ")")

    if args.clickhouse:
        from clickhouse_source import load_from_clickhouse
        df = load_from_clickhouse(
            "results",
            host=args.ch_host, port=args.ch_port, database=args.ch_database,
            user=args.ch_user, password=args.ch_password,
            stand=args.stand,
            run_labels=[args.run_label] if args.run_label else None,
        )
    elif args.results:
        from load import load_results
        df = load_results(args.results)
    else:
        p.error("укажи --results <файл> или --clickhouse")

    df = df[~df["approximation"].astype(str).str.startswith(("error:", "missing"))]
    res = twin_contrast(df, sensitive, twin, axis, storm_node=args.storm_node)
    if res.empty:
        print("нет пригодных строк")
        return 1

    print(f"контраст {sensitive} против {twin} на штормовом узле (ось {axis})\n")
    print(res.to_string(index=False, float_format=lambda v: f"{v:.4g}"))
    if res["storm_node_inferred"].any():
        print("\nВНИМАНИЕ: у части плеч штормовой узел ВЫВЕДЕН по максимуму измеренного\n"
              "давления (колонки storm_nodes в этих данных нет) — это слабее, чем факт\n"
              "постановки: шторм мог не состояться, и тогда узел выбран по шуму.")
        # Вырождение вывода: жертва сама создаёт давление своей оси (net —
        # 2 ГБ egress, io — запись файла), и «штормовым» становится её
        # собственный узел. Признак — чувствительный профиль «на шторме» в
        # КАЖДОМ повторе КАЖДОГО плеча, включая плечо, которое его оттуда
        # уводит. Тогда сравниваются профили на чистом узле, а не под штормом.
        shares = res["storm_share_sensitive"].dropna()
        if len(shares) and (shares >= 0.999).all():
            print("\nВЫРОЖДЕНИЕ ВЫВОДА: чувствительный профиль оказался «на шторме» в 100%\n"
                  "повторов ВСЕХ плеч — так не бывает даже у слепого планировщика. Скорее\n"
                  "всего узел выведен по СОБСТВЕННОМУ давлению жертвы, и контраст измеряет\n"
                  "разницу профилей, а не эффект шторма. Перезапусти с --storm-node <узел>\n"
                  "из конфига серии (storms[].node).")
    default_arm = res[res["config"].astype(str).str.endswith("default")]
    if not default_arm.empty:
        r = default_arm.iloc[0]
        print(f"\nПлечо default (размещение слепо к S, отбор наименее смещён): "
              f"×{r['ratio']:.2f} [{r['ci_lo']:.2f}; {r['ci_hi']:.2f}], p={r['p']:.3g}, {r['test']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
