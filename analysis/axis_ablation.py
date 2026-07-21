#!/usr/bin/env python3
"""axis_ablation.py — абляция схемы весов скоринга (пункт C3 аудита).

ВОПРОС. Новизна заявлена как «пер-задачный вектор чувствительности S против
одномерных load-aware метрик». Trimaran закрывает лишь половину возражения —
что скалярной загрузки CPU мало. Не показано главное: что выигрыш даёт именно
вектор, а не одна правильно оценённая ось. Пока это не показано, метод
неотличим от «а давайте считать диск дорогим».

ЧТО СРАВНИВАЕТСЯ. На STAGE один и тот же смешанный сценарий (три шторма на
трёх узлах, шесть жертв трёх профилей, десять повторений) прогонялся при
РАЗНЫХ схемах весов плагина. Это и есть абляция — не переписывание истории,
а сравнение уже снятых режимов:

  stage-mixed        weights {llc 1, numa 0, net 1, io 1}: три РАВНЫЕ оси,
                     весь вклад идёт через чувствительность задачи
                     (base отсутствует, score = Σ p_a · s_a);
  stage-mixed-calib  base {io 1.0, net 0.09}, sensitivity = 0: цены осей
                     откалиброваны (calibrate_axis_costs.py), вектор S в
                     решении НЕ участвует вовсе (score = Σ p_a · c_a);
  stage-final        то же, что calib — регрессионный повтор 20.07, служит
                     проверкой воспроизводимости вывода.

МЕТРИКИ ВЫБРАНЫ НЕЗАВИСИМЫМИ ОТ ВЕСОВ. `placement_regret` считается той же
скор-функцией, которой принимал решение плагин, поэтому МЕЖДУ режимами он
несравним (каждый режим оценивает себя своей же линейкой — сравнение было бы
тавтологией вдвойне). Сравниваются:

  * выигрыш SS над default по времени ВНУТРИ серии, парно по повторениям —
    межсессионный дрейф стенда (до 23%) при этом сокращается;
  * доля задач на ДОРОГОМ узле — том, чей шторм по калибровке платный
    (диск). Это факт постановки, а не оценка модели;
  * разбивка по профилям — видно, какой именно профиль режим уводит не туда.

ЗАПУСК:
  python axis_ablation.py --clickhouse --stand stage
  python axis_ablation.py --results stage-mixed=../harness/results/results-stage-mixed.parquet \\
                          --results stage-final=../harness/results/results-stage-final.parquet
  python axis_ablation.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))

# Узел дорогой оси на STAGE: дисковый шторм. Ось io — единственная с ненулевой
# базовой ценой по калибровке (io 1.0 против net 0.09 и llc 0.0), поэтому
# попадание именно сюда платное для ЛЮБОЙ задачи узла. Узлы кэша и сети
# занимать не ошибка — счётчик «в шторм вообще» тут ничего не различает.
EXPENSIVE_NODE = "worker-192.168.0.9"

HERO, BASE = "A-sensitivityscore", "A-default"

# Режимы весов в порядке возрастания «калиброванности». Подписи короткие:
# они уезжают в таблицу и на слайд.
REGIMES = [
    ("stage-mixed", "равные оси, решает вектор S"),
    ("stage-ablation", "калиброванные цены В РЕЖИМЕ sensitivity (base = 0)"),
    ("stage-mixed-calib", "калиброванные цены, S выключен"),
    ("stage-final", "то же, регрессионный повтор"),
]

BOOTSTRAP = 20000


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Только строки нагрузочной фазы с измеренным временем."""
    d = df[df["scenario"].astype(str).str.startswith("pressure:")]
    bad = d["approximation"].astype(str).str.startswith("error:")
    return d[~bad & d["makespan_s"].notna()]


def paired_gain(df: pd.DataFrame, rng: np.random.Generator) -> dict:
    """Выигрыш SS над default, парно ПО ПОВТОРЕНИЯМ.

    Наблюдение — повторение, а не задача: шесть жертв одного повторения
    со-локированы намеренно и независимыми наблюдениями не являются
    (та же логика, что в stats.rep_level_sample). Плечи внутри повторения
    видят один и тот же паттерн прибытия, поэтому сравнение парное."""
    piv = (df[df["config"].isin([HERO, BASE])]
           .groupby(["rep", "config"])["makespan_s"].mean().unstack().dropna())
    if piv.empty or len(piv.columns) < 2:
        return {}
    delta = piv[BASE] - piv[HERO]
    boot = np.percentile(
        [np.mean(rng.choice(delta, len(delta), replace=True)) for _ in range(BOOTSTRAP)],
        [2.5, 97.5])
    return {
        "reps": len(delta),
        "gain_s": float(delta.mean()),
        "ci_lo": float(boot[0]),
        "ci_hi": float(boot[1]),
        "p_wilcoxon": float(stats.wilcoxon(piv[HERO], piv[BASE]).pvalue),
        "wins": int((delta > 0).sum()),
    }


def regime_row(df: pd.DataFrame, label: str, note: str, node: str,
               rng: np.random.Generator) -> dict:
    hero = df[df["config"] == HERO]
    base = df[df["config"] == BASE]
    row = {
        "режим": note,
        "серия": label,
        "SS, с": round(hero["makespan_s"].mean(), 1),
        "default, с": round(base["makespan_s"].mean(), 1),
        "SS на дорогой узел": f"{int((hero['node'] == node).sum())}/{len(hero)}",
        "default на дорогой узел": f"{int((base['node'] == node).sum())}/{len(base)}",
    }
    row.update(paired_gain(df, rng))
    return row


def profile_breakdown(df: pd.DataFrame, node: str) -> pd.DataFrame:
    """Где именно режим ошибается: время и число попаданий на дорогой узел
    по профилям. Средние по плечу могут совпасть при разной внутренней
    картине — например, выигрыш на одном профиле гасит проигрыш на другом."""
    out = []
    for prof, g in df.groupby("profile"):
        hero, base = g[g["config"] == HERO], g[g["config"] == BASE]
        out.append({
            "профиль": prof,
            "SS, с": round(hero["makespan_s"].mean(), 1),
            "default, с": round(base["makespan_s"].mean(), 1),
            "SS на дорогой узел": int((hero["node"] == node).sum()),
        })
    return pd.DataFrame(out)


def report(frames: dict[str, pd.DataFrame], node: str, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows, breakdowns = [], {}
    for label, note in REGIMES:
        if label not in frames:
            continue
        d = clean(frames[label])
        rows.append(regime_row(d, label, note, node, rng))
        breakdowns[label] = profile_breakdown(d, node)

    table = pd.DataFrame(rows)
    print(f"дорогой узел (платная ось по калибровке): {node}\n")
    for r in rows:
        print(f"=== {r['режим']}  [{r['серия']}] ===")
        print(f"  время   SS {r['SS, с']} с против default {r['default, с']} с")
        print(f"  на дорогой узел   SS {r['SS на дорогой узел']}, "
              f"default {r['default на дорогой узел']}")
        if "gain_s" in r:
            print(f"  выигрыш SS  {r['gain_s']:+.2f} с   "
                  f"95% ДИ [{r['ci_lo']:+.2f}; {r['ci_hi']:+.2f}]   "
                  f"Уилкоксон p={r['p_wilcoxon']:.4f}   "
                  f"{r['wins']}/{r['reps']} повторений")
        print(breakdowns[r["серия"]].to_string(index=False))
        print()

    _verdict(rows)
    return table


def _verdict(rows: list[dict]) -> None:
    """Вывод формулируется знаком выигрыша, а не желаемым результатом.

    Три режима образуют разложение по двум факторам: ЦЕНЫ осей (равные ->
    калиброванные) и СПОСОБ вклада (через чувствительность задачи -> через
    базовую цену узла). Меняя по одному, видно, какой из них несущий."""
    by = {r["серия"]: r for r in rows if "gain_s" in r}
    equal = by.get("stage-mixed")            # равные цены, режим sensitivity
    sens = by.get("stage-ablation")          # калиброванные цены, режим sensitivity
    base = [by[k] for k in ("stage-mixed-calib", "stage-final") if k in by]
    if not equal or not base:
        print("вывод: нужны обе крайние точки — равные оси и калиброванные цены")
        return
    best = max(base, key=lambda r: r["gain_s"])

    def line(tag: str, r: dict) -> str:
        return (f"  {tag:52} {r['gain_s']:+.2f} с, ДИ [{r['ci_lo']:+.2f}; "
                f"{r['ci_hi']:+.2f}], p={r['p_wilcoxon']:.3f}, "
                f"на дорогой узел {r['SS на дорогой узел']}")

    print("ВЫВОД")
    print(line("равные цены, вклад через чувствительность", equal))
    if sens:
        print(line("калиброванные цены, вклад через чувствительность", sens))
    print(line("калиброванные цены, вклад через базовую цену", best))

    if not sens:
        print("\n  ОГРАНИЧЕНИЕ. Две снятые точки отличаются ДВУМЯ факторами сразу.\n"
              "  Чтобы развести цены и способ вклада, нужен третий прогон —\n"
              "  калиброванные цены в режиме sensitivity (base = 0).")
        return

    print("\n  РАЗЛОЖЕНИЕ ПО ФАКТОРАМ")
    print(f"  цены (равные -> калиброванные, режим тот же): "
          f"{equal['gain_s']:+.2f} -> {sens['gain_s']:+.2f} с")
    print(f"  способ (чувствительность -> базовая цена, цены те же): "
          f"{sens['gain_s']:+.2f} -> {best['gain_s']:+.2f} с")
    if sens["p_wilcoxon"] >= 0.05 <= 1 and best["p_wilcoxon"] < 0.05:
        print("\n  НЕСУЩИЙ ФАКТОР — СПОСОБ ВКЛАДА, а не цены и не размерность\n"
              "  вектора. С правильными ценами, но вкладом через чувствительность\n"
              "  выигрыш статистически неотличим от нуля, а уход с дорогого узла\n"
              "  теряется полностью. Причина прямая: цена оси умножается на\n"
              "  чувствительность задачи, поэтому задача, не объявившая эту ось,\n"
              "  видит дорогой узел БЕСПЛАТНЫМ — сколько ни калибруй. Работает\n"
              "  только базовая цена, которую платит каждый на узле.")
    else:
        print("\n  Разложение не даёт однозначного несущего фактора — смотреть\n"
              "  на доверительные интервалы и разбивку по профилям выше.")


def _self_test() -> int:
    """Данные с ЗАЛОЖЕННЫМ ответом: без такой проверки вывод «эффект есть»
    неотличим от «скрипт умеет печатать эффект»."""
    ok = True
    rng = np.random.default_rng(7)

    def frame(hero_offset: float, hero_on_expensive: int) -> pd.DataFrame:
        rows = []
        for rep in range(10):
            for i in range(6):
                for cfg, off in ((HERO, hero_offset), (BASE, 0.0)):
                    on_exp = (cfg == HERO and i < hero_on_expensive) or \
                             (cfg == BASE and i < 2)
                    rows.append({
                        "scenario": "pressure:mixed3", "config": cfg,
                        "profile": ["high-s", "high-s-io", "high-s-net"][i % 3],
                        "rep": rep, "node": EXPENSIVE_NODE if on_exp else "w-other",
                        "makespan_s": 100 + off + rng.normal(0, 1),
                        "approximation": "",
                    })
        return pd.DataFrame(rows)

    # Заложено: SS быстрее на 10 с -> выигрыш положительный и значимый.
    r = paired_gain(clean(frame(-10.0, 0)), rng)
    good = r["gain_s"] > 5 and r["p_wilcoxon"] < 0.05 and r["wins"] == 10
    print(f"  {'OK ' if good else 'НЕТ'} выигрыш найден там, где он заложен: "
          f"{r['gain_s']:+.2f} с, p={r['p_wilcoxon']:.4f}")
    ok &= good

    # Заложено: SS медленнее -> знак ОТРИЦАТЕЛЬНЫЙ. Скрипт обязан это показать,
    # а не «не найти разницы».
    r = paired_gain(clean(frame(+10.0, 4)), rng)
    good = r["gain_s"] < -5 and r["wins"] == 0
    print(f"  {'OK ' if good else 'НЕТ'} проигрыш показан со своим знаком: "
          f"{r['gain_s']:+.2f} с, побед {r['wins']}/10")
    ok &= good

    # Счётчик дорогого узла считает именно дорогой узел.
    d = clean(frame(0.0, 3))
    hero = d[d["config"] == HERO]
    got = int((hero["node"] == EXPENSIVE_NODE).sum())
    good = got == 30  # 3 жертвы x 10 повторений
    print(f"  {'OK ' if good else 'НЕТ'} счётчик дорогого узла: {got}, ожидалось 30")
    ok &= good

    print("\nсамопроверка:", "пройдена" if ok else "ПРОВАЛЕНА")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", action="append", default=[],
                   metavar="МЕТКА=ФАЙЛ", help="parquet одной серии (можно несколько раз)")
    p.add_argument("--clickhouse", action="store_true", help="читать из ClickHouse")
    p.add_argument("--stand", default="stage")
    p.add_argument("--ch-host", default="localhost"), p.add_argument("--ch-port", type=int, default=8123)
    p.add_argument("--ch-database", default="sensitivityscore")
    p.add_argument("--ch-user", default="default"), p.add_argument("--ch-password", default="")
    p.add_argument("--expensive-node", default=EXPENSIVE_NODE,
                   help="узел платной оси (по калибровке — дисковый шторм)")
    p.add_argument("--out", type=Path, help="куда сложить таблицу CSV")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        return _self_test()

    frames: dict[str, pd.DataFrame] = {}
    if args.clickhouse:
        from clickhouse_source import load_from_clickhouse
        for label, _ in REGIMES:
            d = load_from_clickhouse(
                "results", host=args.ch_host, port=args.ch_port,
                database=args.ch_database, user=args.ch_user,
                password=args.ch_password, stand=args.stand, run_labels=[label])
            if len(d):
                frames[label] = d
    for spec in args.results:
        if "=" not in spec:
            p.error(f"--results ждёт МЕТКА=ФАЙЛ, получено {spec!r}")
        label, path = spec.split("=", 1)
        from load import load_results
        frames[label] = load_results(Path(path))

    if not frames:
        p.error("нечего сравнивать: укажи --clickhouse или --results МЕТКА=ФАЙЛ")

    table = report(frames, args.expensive_node)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(args.out, index=False)
        print(f"таблица -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
