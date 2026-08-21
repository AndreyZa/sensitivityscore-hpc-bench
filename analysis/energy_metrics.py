#!/usr/bin/env python3
"""energy_metrics.py — энергетические метрики плеч: Дж/задача, EDP, ED²P,
цена цикла гашения и порог окупаемости T.

Зачем отдельный модуль. Вся энергетика до 20.08.2026 жила в `scripts/`,
то есть на стороне ИЗМЕРЕНИЯ: снять окно, записать в ClickHouse. Считать
по этим окнам было нечем — серия P2 отработала бы две ночи, а обработать
её было бы не на чем (инвентаризация в «План расчётов.md» статьи).

Модель данных. Энергия лежит в `energy_windows` (окно × узел × источник),
исходы задач — в `results`. Общий ключ — (stand, run_label, config);
повторение кодируется ИМЕНЕМ ОКНА, потому что колонки rep в
energy_windows нет:

    window = "<что-это>-rep<N>"      напр. "arm-rep3", "cycle-boot-rep7"

Разбор имени — единственное место, где эта конвенция зашита (RE_REP).

ГЛАВНАЯ ПРОВЕРКА — покрытие. Дж/задача есть отношение энергии окна к
числу задач, и если окно не покрывает задачи целиком (сдвиг границ,
перепутанный rep, задача, пережившая окно), обе величины останутся
правдоподобными, а отношение будет молча неверным. Поэтому `coverage`
сверяет [start_ts, end_ts] каждой задачи с границами её окна и по
умолчанию ВАЛИТ расчёт, а не печатает предупреждение.

  energy_metrics.py --run-label p2-prod-... --stand prod
  energy_metrics.py --run-label ... --cycle --p-off 23.3
  energy_metrics.py --self-test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:  # stats — часть анализа; при запуске из корня репозитория пути разные
    from analysis.stats import paired_diff_ci, bootstrap_ci
except ImportError:  # pragma: no cover
    from stats import paired_diff_ci, bootstrap_ci

RE_REP = re.compile(r"^(?P<kind>.+?)-rep(?P<rep>\d+)$")

# Источник по умолчанию — BMC: он покрывает розетку УЗЛА целиком, тогда
# как RAPL видит 40–67 % её (фаза P0), а PDU в разделяемой стойке несёт
# чужую нагрузку. Тот же выбор, что для фита кривой мощности.
DEFAULT_SOURCE = "ipmi"


def parse_window(name: str) -> tuple[str, int | None]:
    """'arm-rep3' -> ('arm', 3); 'idle' -> ('idle', None)."""
    m = RE_REP.match(name)
    return (m.group("kind"), int(m.group("rep"))) if m else (name, None)


def window_energy(windows: pd.DataFrame, source: str = DEFAULT_SOURCE) -> pd.DataFrame:
    """Энергия окна = сумма по УЗЛАМ (плечо занимает несколько узлов).

    Границы окна берутся как min(ts_start)/max(ts_end) по узлам: узлы
    пишутся независимо и расходятся на доли секунды, а покрытие задач
    надо проверять против внешних границ, иначе проверка сама создаёт
    ложные срабатывания."""
    w = windows[windows["source"] == source].copy()
    if w.empty:
        return pd.DataFrame(columns=["config", "rep", "kind", "energy_j",
                                     "ts_start", "ts_end", "duration_s", "nodes"])
    parsed = w["window"].map(parse_window)
    w["kind"] = [p[0] for p in parsed]
    w["rep"] = [p[1] for p in parsed]
    g = w.groupby(["config", "kind", "rep"], dropna=False)
    out = g.agg(energy_j=("energy_j", "sum"),
                ts_start=("ts_start", "min"),
                ts_end=("ts_end", "max"),
                nodes=("node", "nunique"),
                rows=("energy_j", "size")).reset_index()
    # Одно плечо одного повторения — по одному окну на узел. Больше значит,
    # что под меткой серии лежит НЕСКОЛЬКО сценариев: нумерация повторений
    # в каждом начинается с нуля, и arm-rep0 уровня подачи feed-mid здесь
    # сложился бы с arm-rep0 уровня feed-low. Молча это даёт удвоенную
    # энергию и вдвое заниженную Дж/задача, поэтому — отказ с указанием,
    # чего не хватает вызывающему.
    doubled = out[out["rows"] > out["nodes"]]
    if not doubled.empty:
        r = doubled.iloc[0]
        raise ValueError(
            f"окно {r['config']}/{r['kind']}-rep{r['rep']} встречается "
            f"{int(r['rows'])} раз на {int(r['nodes'])} узлах — под меткой "
            f"серии лежит несколько сценариев; укажи --scenario")
    out["duration_s"] = out["ts_end"] - out["ts_start"]
    return out.drop(columns=["rows"])


def coverage(energy: pd.DataFrame, results: pd.DataFrame,
             tolerance_s: float = 5.0) -> list[str]:
    """Задачи каждого (config, rep) обязаны лежать внутри своего окна.

    Допуск — на разъезд часов узла и агрегатора; он односторонним быть не
    может, поэтому применяется к обеим границам."""
    problems = []
    for _, row in energy.iterrows():
        if pd.isna(row["rep"]):
            continue
        tasks = results[(results["config"] == row["config"])
                        & (results["rep"] == row["rep"])]
        if tasks.empty:
            problems.append(f"{row['config']} rep{int(row['rep'])}: "
                            f"окно есть, задач нет")
            continue
        late = tasks["end_ts"].max() - row["ts_end"]
        early = row["ts_start"] - tasks["start_ts"].min()
        if late > tolerance_s:
            problems.append(
                f"{row['config']} rep{int(row['rep'])}: задачи кончились на "
                f"{late:.1f} c ПОЗЖЕ окна — энергия посчитана не за весь прогон")
        if early > tolerance_s:
            problems.append(
                f"{row['config']} rep{int(row['rep'])}: задачи начались на "
                f"{early:.1f} c РАНЬШЕ окна — часть работы вне окна")
    return problems


def per_task_metrics(energy: pd.DataFrame, results: pd.DataFrame,
                     kind: str = "arm") -> pd.DataFrame:
    """Дж/задача, EDP, ED²P по (плечо, повторение).

    Задачей считается ЗАВЕРШЁННАЯ задача (makespan_s не NULL): делить
    энергию на число отправленных, среди которых есть упавшие, значит
    вознаграждать плечо за собственные отказы."""
    e = energy[energy["kind"] == kind].dropna(subset=["rep"])
    rows = []
    for _, row in e.iterrows():
        tasks = results[(results["config"] == row["config"])
                        & (results["rep"] == row["rep"])]
        done = tasks[tasks["makespan_s"].notna()]
        n = len(done)
        if n == 0:
            continue
        j_task = row["energy_j"] / n
        med = float(done["makespan_s"].median())
        rows.append({
            "config": row["config"], "rep": int(row["rep"]),
            "tasks": n, "energy_j": row["energy_j"],
            "j_per_task": j_task,
            "median_makespan_s": med,
            "edp": j_task * med,
            "ed2p": j_task * med * med,
            "duration_s": row["duration_s"],
        })
    return pd.DataFrame(rows)


def compare_arms(metrics: pd.DataFrame, baseline: str, metric: str = "j_per_task",
                 n_boot: int = 2000, rng=None) -> pd.DataFrame:
    """Парное сравнение плеч с базовым, сопоставление по номеру повторения.

    Парность здесь не украшение: повторения идут в рандомизированном
    порядке внутри одной ночи, и общий дрейф стенда (температура, чужая
    нагрузка в стойке) входит в оба плеча повторения одинаково — в
    разности он сокращается, в независимых выборках нет."""
    base = metrics[metrics["config"] == baseline].set_index("rep")[metric]
    rows = []
    for cfg in sorted(set(metrics["config"]) - {baseline}):
        arm = metrics[metrics["config"] == cfg].set_index("rep")[metric]
        lo, point, hi = paired_diff_ci(arm, base, n_boot=n_boot, rng=rng)
        rel = point / float(base.median()) * 100.0 if len(base) else float("nan")
        rows.append({"config": cfg, "vs": baseline, "metric": metric,
                     "diff": point, "lo": lo, "hi": hi, "rel_pct": rel,
                     "significant": bool(lo * hi > 0) if np.isfinite(lo * hi) else False})
    return pd.DataFrame(rows)


def cycle_cost(energy: pd.DataFrame, p_off_w: float, p_idle_w: float,
               n_boot: int = 2000, rng=None) -> dict:
    """Цена цикла и порог окупаемости T.

    E_цикла — энергия переходов СВЕРХ того, что узел потратил бы,
    оставаясь выключенным: интеграл (P(t) − P_выкл) по окнам гашения и
    подъёма. Именно эта величина стоит в числителе порога
    T = E_цикла/(P_простоя − P_выкл), и вычитание P_выкл здесь не
    косметика: без него в цену цикла попадает дежурное потребление,
    которое политика и так платит.
    """
    tr = energy[energy["kind"].isin(("cycle-off", "cycle-boot"))].dropna(subset=["rep"])
    if tr.empty:
        return {"reps": 0}
    # Цена цикла — свойство ОДНОГО узла, того, который гасили. Если в окне
    # оказалось несколько узлов, суммировать их нельзя: соседи в этот
    # момент стоят на холостом ходу, и их энергия превращает цену цикла в
    # цену стенда. Так и вышло 21.08.2026 — 248 кДж вместо 32, потому что
    # окна писались по всем узлам сразу. Отказываем громко: тихо взять
    # один узел из нескольких значило бы гадать, какой именно гасили.
    per_rep = {}
    for rep, grp in tr.groupby("rep"):
        many = grp["nodes"].max()
        if many is not None and many > 1:
            raise ValueError(
                f"окно цикла rep{int(rep)} собрано по {int(many)} узлам — "
                f"цена цикла считается по ОДНОМУ гасимому узлу, соседи в "
                f"этот момент стоят на холостом ходу и превращают её в цену "
                f"стенда; перезапиши окна свежим scripts/power-save.py, он "
                f"ограничивает метрику узлом")
        extra = float((grp["energy_j"] - p_off_w * grp["duration_s"]).sum())
        per_rep[int(rep)] = extra
    vals = np.array(list(per_rep.values()), dtype=float)
    lo, point, hi = bootstrap_ci(vals, statistic=np.median, n_boot=n_boot, rng=rng)
    denom = p_idle_w - p_off_w
    to_t = lambda e: e / denom / 60.0 if denom > 0 else float("nan")
    return {
        "reps": len(vals),
        "e_cycle_j": point, "e_cycle_lo": lo, "e_cycle_hi": hi,
        "delta_w": denom,
        "t_min": to_t(point), "t_lo_min": to_t(lo), "t_hi_min": to_t(hi),
        "per_rep_j": per_rep,
    }


# ------------------------------------------------------------------ ввод

def figdata_block(metrics: pd.DataFrame, baseline: str) -> dict:
    """Числа таблицы политик в том виде, в каком они попадут в статью.

    ЗАЧЕМ. Числа §6 до сих пор переносились в статью руками: analysis
    печатала Дж/задача, я перепечатывал их в таблицу, и сверять их было
    не с чем — make check сличает статью с figdata.json, а блока P2 там
    не было вовсе. Значит, опечатка в таблице не ловилась ничем.
    Округление здесь не косметика: make check ищет число в тексте как
    строку, поэтому в figdata кладём ровно то, что будет напечатано —
    кДж/задача с одним знаком, а не джоули с пробелом-разделителем.
    """
    med = metrics.groupby("config")[["j_per_task", "edp", "median_makespan_s"]].median()
    out = {}
    for cfg, row in med.iterrows():
        out[cfg] = {"kj_per_task": round(row["j_per_task"] / 1000.0, 1),
                    "edp_mln": round(row["edp"] / 1e6, 2),
                    "makespan_s": round(row["median_makespan_s"], 1)}
    if baseline in med.index:
        cmp = compare_arms(metrics, baseline)
        for _, r in cmp.iterrows():
            out[r["config"]]["diff_pct"] = round(r["rel_pct"], 2)
            out[r["config"]]["lo_pct"] = round(r["lo"] / med.loc[baseline, "j_per_task"] * 100, 2)
            out[r["config"]]["hi_pct"] = round(r["hi"] / med.loc[baseline, "j_per_task"] * 100, 2)
    return out


def load_from_ch(run_label: str, stand: str, host: str, port: int,
                 database: str = "sensitivityscore",
                 scenario: str = "") -> tuple[pd.DataFrame, pd.DataFrame]:
    import clickhouse_connect
    client = clickhouse_connect.get_client(host=host, port=port,
                                           username="default", database=database)
    params = {"label": run_label, "stand": stand, "scenario": scenario}
    windows = client.query_df(
        "SELECT config, window, node, source, energy_j, avg_power_w, "
        "toUnixTimestamp64Milli(ts_start)/1000.0 AS ts_start, "
        "toUnixTimestamp64Milli(ts_end)/1000.0 AS ts_end "
        "FROM energy_windows FINAL "
        "WHERE run_label = %(label)s AND stand = %(stand)s", parameters=params)
    results = client.query_df(
        "SELECT config, rep, node, makespan_s, "
        "toUnixTimestamp64Milli(start_ts)/1000.0 AS start_ts, "
        "toUnixTimestamp64Milli(end_ts)/1000.0 AS end_ts "
        "FROM results FINAL "
        # Прогрев отсеивается ТЕМ ЖЕ признаком, что и в построителе окон
        # (scripts/energy-windows-per-arm.py) и в analysis/load.py. Иначе
        # знаменатель Дж/задача считал бы задачи, которых нет в числителе:
        # окно плеча их не покрывает, а в счёт они идут. Сейчас в P2 строк
        # прогрева нет (эталоны пропускаются, и rep у прогрева отрицательный),
        # поэтому это защита от будущего конфига, а не исправление ошибки.
        "WHERE run_label = %(label)s AND stand = %(stand)s "
        "AND approximation != 'warmup'"
        + (" AND scenario = %(scenario)s" if scenario else ""),
        parameters=params)
    if scenario and not results.empty:
        # У окон энергии поля сценария нет, зато сценарии идут подряд:
        # отбираем окна по времени самого сценария. Запас в 10 минут — на
        # окно, открытое до первой задачи и закрытое после последней.
        lo = results["start_ts"].min() - 600.0
        hi = results["end_ts"].max() + 600.0
        windows = windows[(windows["ts_start"] >= lo) & (windows["ts_end"] <= hi)]
    return windows, results


# --------------------------------------------------------------- самотест

def _fake_data(n_reps: int = 8, seed: int = 3):
    """Синтетика с ИЗВЕСТНЫМ ответом: плечо B тратит на 10 % меньше энергии
    при том же числе задач и том же makespan."""
    rng = np.random.default_rng(seed)
    win, res = [], []
    t0 = 1_700_000_000.0
    for rep in range(1, n_reps + 1):
        for cfg, e_node in (("A", 500_000.0), ("B", 450_000.0)):
            start = t0 + rep * 10_000
            dur = 600.0
            for node in ("wrk-b6", "wrk-b7"):
                win.append({"config": cfg, "window": f"arm-rep{rep}", "node": node,
                            "source": "ipmi",
                            "energy_j": e_node * (1 + rng.normal(0, 0.01)),
                            "ts_start": start, "ts_end": start + dur})
            for i in range(10):
                res.append({"config": cfg, "rep": rep, "node": "wrk-b6",
                            "makespan_s": 200.0 + rng.normal(0, 5),
                            "start_ts": start + 5 + i,
                            "end_ts": start + 300 + i})
    return pd.DataFrame(win), pd.DataFrame(res)


def self_test() -> int:
    win, res = _fake_data()
    energy = window_energy(win)
    assert set(energy["kind"]) == {"arm"}, energy["kind"].unique()
    assert energy["nodes"].eq(2).all(), "энергия должна суммироваться по узлам"

    assert coverage(energy, res) == [], "чистые данные не должны давать проблем"

    m = per_task_metrics(energy, res)
    assert len(m) == 16, len(m)
    assert m["tasks"].eq(10).all(), m["tasks"].unique()
    # A: 2 узла × 500 кДж / 10 задач = 100 кДж/задача
    a = m[m["config"] == "A"]["j_per_task"].median()
    assert abs(a - 100_000) < 3_000, a

    cmp = compare_arms(m, baseline="A", n_boot=500,
                       rng=np.random.default_rng(1))
    row = cmp.iloc[0]
    assert row["config"] == "B" and row["diff"] < 0, row.to_dict()
    assert abs(row["rel_pct"] + 10.0) < 2.0, row["rel_pct"]
    assert row["significant"], "10 % разница на 8 повторениях обязана ловиться"

    # Покрытие ловит сдвиг окна: задачи кончаются позже границы. Сдвиг
    # берётся заведомо больше запаса между концом задач (start+309) и
    # концом окна (start+600) — иначе тест зелёный на сломанных данных.
    bad = win.copy()
    bad.loc[bad["window"] == "arm-rep1", "ts_end"] -= 400
    probs = coverage(window_energy(bad), res)
    assert probs and "ПОЗЖЕ" in probs[0], probs

    # ...и перепутанный rep: окно есть, задач нет.
    bad2 = win.copy()
    bad2["window"] = bad2["window"].replace({"arm-rep1": "arm-rep99"})
    probs2 = coverage(window_energy(bad2), res)
    assert any("задач нет" in p for p in probs2), probs2

    # Цена цикла: 300 c гашения и подъёма со средней 200 Вт при P_выкл 20 Вт
    # => (200−20)·300 = 54 кДж на окно, два окна = 108 кДж.
    cyc = []
    for rep in range(1, 6):
        for kind in ("cycle-off", "cycle-boot"):
            cyc.append({"config": "P3", "window": f"{kind}-rep{rep}",
                        "node": "wrk-b8", "source": "ipmi",
                        "energy_j": 200.0 * 300.0, "ts_start": 0.0, "ts_end": 300.0})
    # Окно, собранное по нескольким узлам, обязано ОТКАЗАТЬ, а не
    # посчитаться: соседи на холостом ходу давали 248 кДж вместо 32.
    multi = [dict(r, node=n) for r in cyc for n in ("wrk-b6", "wrk-b7", "wrk-b8")]
    try:
        cycle_cost(window_energy(pd.DataFrame(multi)), p_off_w=20.0, p_idle_w=260.0)
        raise AssertionError("цена цикла посчиталась по трём узлам — страж не сработал")
    except ValueError as exc:
        assert "гасимому узлу" in str(exc), exc

    c = cycle_cost(window_energy(pd.DataFrame(cyc)), p_off_w=20.0, p_idle_w=260.0)
    assert abs(c["e_cycle_j"] - 108_000) < 1.0, c["e_cycle_j"]
    # T = 108 кДж / (260−20) Вт = 450 c = 7,5 мин
    assert abs(c["t_min"] - 7.5) < 0.01, c["t_min"]

    # Два сценария под одной меткой: повторения нумеруются заново, окна
    # arm-rep0 совпадают по имени. Складывать их нельзя — страж обязан
    # сработать, иначе Дж/задача занижается вдвое молча.
    second = win.copy()
    second["ts_start"] += 100_000.0
    second["ts_end"] += 100_000.0
    try:
        window_energy(pd.concat([win, second], ignore_index=True))
        raise AssertionError("два сценария сложились в одно окно — страж молчит")
    except ValueError as exc:
        assert "--scenario" in str(exc), exc

    # Пустой источник — не падение, а пустой результат (метка ещё не залита).
    assert window_energy(win, source="pdu").empty

    print("self-test: ок (сумма по узлам, Дж/задача, парное сравнение, "
          "покрытие окна, перепутанный rep, два сценария под меткой, "
          "цена цикла и порог T)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--run-label")
    ap.add_argument("--stand", default="prod")
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=8123)
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help=f"источник энергии (default {DEFAULT_SOURCE})")
    ap.add_argument("--scenario", default="",
                    help="сценарий (уровень подачи), если под меткой их несколько")
    ap.add_argument("--baseline", default="default",
                    help="плечо сравнения (default: default)")
    ap.add_argument("--cycle", action="store_true",
                    help="считать цену цикла гашения вместо метрик плеч")
    ap.add_argument("--p-off", type=float, help="мощность выключенного узла, Вт")
    ap.add_argument("--p-idle", type=float, help="мощность простоя, Вт")
    ap.add_argument("--allow-coverage-gaps", action="store_true",
                    help="не валить расчёт при разрыве покрытия (по умолчанию валит)")
    ap.add_argument("--out", default="", help="куда записать JSON")
    ap.add_argument("--figdata", default="",
                    help="дописать блок p2.<уровень> в figdata.json статьи")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.run_label:
        ap.error("--run-label обязателен (кроме --self-test)")

    windows, results = load_from_ch(args.run_label, args.stand,
                                    args.ch_host, args.ch_port,
                                    scenario=args.scenario)
    if windows.empty:
        print(f"окон с меткой {args.run_label} нет", file=sys.stderr)
        return 1
    energy = window_energy(windows, args.source)

    if args.cycle:
        if args.p_off is None or args.p_idle is None:
            ap.error("--cycle требует --p-off и --p-idle (оба измеряются)")
        c = cycle_cost(energy, args.p_off, args.p_idle)
        if not c["reps"]:
            print("окон cycle-off/cycle-boot не найдено", file=sys.stderr)
            return 1
        print(f"цена цикла: {c['e_cycle_j']/1000:.1f} кДж "
              f"[{c['e_cycle_lo']/1000:.1f}; {c['e_cycle_hi']/1000:.1f}] "
              f"по {c['reps']} повторениям")
        print(f"порог окупаемости T = {c['t_min']:.1f} мин "
              f"[{c['t_lo_min']:.1f}; {c['t_hi_min']:.1f}] "
              f"при разности {c['delta_w']:.1f} Вт")
        out = c
    else:
        probs = coverage(energy, results)
        if probs:
            print("ПОКРЫТИЕ ОКОН НАРУШЕНО:", file=sys.stderr)
            for p in probs:
                print("  " + p, file=sys.stderr)
            if not args.allow_coverage_gaps:
                print("Дж/задача считать нельзя: энергия и задачи относятся к "
                      "разным интервалам (обход: --allow-coverage-gaps)",
                      file=sys.stderr)
                return 1
        m = per_task_metrics(energy, results)
        if m.empty:
            print("плеч с завершёнными задачами не найдено", file=sys.stderr)
            return 1
        print(m.groupby("config")[["j_per_task", "edp", "median_makespan_s"]]
              .median().to_string(float_format=lambda v: f"{v:,.1f}"))
        if args.baseline in set(m["config"]):
            print()
            for metric in ("j_per_task", "edp"):
                cmp = compare_arms(m, args.baseline, metric=metric)
                print(cmp.to_string(index=False,
                                    float_format=lambda v: f"{v:,.2f}"))
        out = {"per_rep": m.to_dict(orient="records")}
        if args.figdata:
            key = args.scenario.split(":")[-1] or args.run_label
            fd = json.loads(Path(args.figdata).read_text(encoding="utf-8"))
            p2 = fd.setdefault("p2", {})
            p2["_comment"] = ("политики размещения по уровням подачи; "
                              "медианы по повторениям, ДИ парного бутстрепа")
            p2[key] = figdata_block(m, args.baseline)
            Path(args.figdata).write_text(
                json.dumps(fd, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            print(f"figdata обновлён: p2.{key} в {args.figdata}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=float)
        print(f"записано: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
