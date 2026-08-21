#!/usr/bin/env python3
"""energy-windows-per-arm.py — окна энергии ПО ПЛЕЧАМ И ПОВТОРЕНИЯМ.

Зачем отдельно от energy-windows-from-log.sh. Тот делает по одному окну на
ФАЗУ (`baseline`, `pressure`) из маркеров лога — этого хватало
интерференционной ветке, где энергия была справочной величиной. Для фазы
P2 энергостатьи нужно другое: Дж/задача считается на КАЖДОЕ плечо и
КАЖДОЕ повторение (иначе нет ни парного сравнения, ни интервалов), а
одно окно на всю фазу давления накрывает все плечи разом и для этого
непригодно.

Границы окон не парсятся из лога, а берутся из самих результатов:
у каждой задачи в `results` есть start_ts/end_ts, значит окно
(плечо, повторение) = [min(start_ts), max(end_ts)] его задач. Это и
точнее маркеров, и снимает требование к харнессу что-то печатать, и
делает покрытие окна задачами верным по построению — ровно ту проверку,
которая в analysis/energy_metrics.py стоит перед делением.

Что считается энергией плеча: потребление, пока шла его работа. Паузы
между повторениями (cooldown) в окно НЕ входят — их длительность одна и
та же у всех плеч и в разность не вносит вклада, а в Дж/задача внесла бы
шум простоя. Плата за выбор: узел, простаивающий между задачами ВНУТРИ
окна, в энергию входит — так и должно быть, это и есть цена решения
планировщика.

  energy-windows-per-arm.py --stand prod --run-label p2-prod-... \
      --prom http://localhost:19090 [--sources ipmi,rapl-pkg] [--dry-run]
  energy-windows-per-arm.py --self-test
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

_EW = pathlib.Path(__file__).with_name("energy-window.py")

# Те же аргументы, что у калибровки P1 (scripts/p1-calibrate.py): один
# источник — одно определение, иначе окна серий и окна лестницы окажутся
# посчитаны по-разному и станут несравнимы.
SOURCES = {
    "ipmi":      ["--metric", "idrac_power_watts", "--node-label", "node",
                  "--source", "ipmi", "--mode", "power"],
    "rapl-pkg":  ["--metric",
                  'sum by(node)(ss_node_rapl_joules_total{domain=~"package-.*"})',
                  "--node-label", "node", "--source", "rapl-pkg"],
    "rapl-dram": ["--metric",
                  'sum by(node)(ss_node_rapl_joules_total{domain="dram"})',
                  "--node-label", "node", "--source", "rapl-dram"],
}


def arm_windows(rows: list[dict], min_tasks: int = 1) -> list[dict]:
    """Строки результатов -> окна (плечо, повторение).

    Пропускаются: warmup-строки, строки без времён (ошибка размещения) и
    группы, где задач меньше min_tasks — окно из одной уцелевшей задачи
    даёт Дж/задача, не сопоставимую с полными повторениями."""
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        # Прогрев помечен в results полем approximation="warmup" — отдельной
        # колонки warmup в таблице нет и не было. Скрипт запрашивал именно
        # её, и на живой базе падал на UNKNOWN_IDENTIFIER; самотест этого не
        # ловил, потому что кормит функцию своими словарями и до SQL не
        # доходит. Признак взят из analysis/load.py:filter_valid — там же,
        # где его читает весь остальной анализ. (21.08.2026, до первого
        # расчёта P2.)
        if r.get("approximation") == "warmup":
            continue
        if r.get("start_ts") is None or r.get("end_ts") is None:
            continue
        groups.setdefault((r["config"], int(r["rep"])), []).append(r)
    out = []
    for (config, rep), tasks in sorted(groups.items()):
        if len(tasks) < min_tasks:
            continue
        out.append({
            "config": config, "rep": rep,
            "t0": int(min(t["start_ts"] for t in tasks)),
            "t1": int(max(t["end_ts"] for t in tasks)) + 1,  # правая граница включительно
            "tasks": len(tasks),
        })
    return out


def fetch_results(stand: str, run_label: str, host: str, port: int,
                  database: str = "sensitivityscore") -> list[dict]:
    import clickhouse_connect
    client = clickhouse_connect.get_client(host=host, port=port,
                                           username="default", database=database)
    df = client.query_df(
        "SELECT config, rep, "
        "toUnixTimestamp64Milli(start_ts)/1000.0 AS start_ts, "
        "toUnixTimestamp64Milli(end_ts)/1000.0 AS end_ts, approximation "
        "FROM results FINAL WHERE stand = %(stand)s AND run_label = %(label)s",
        parameters={"stand": stand, "label": run_label})
    return df.to_dict(orient="records")


def run(args) -> int:
    rows = fetch_results(args.stand, args.run_label, args.ch_host, args.ch_port)
    if not rows:
        print(f"результатов с меткой {args.run_label} нет", file=sys.stderr)
        return 1
    windows = arm_windows(rows, args.min_tasks)
    if not windows:
        print("ни одного пригодного окна (все строки warmup или без времён)",
              file=sys.stderr)
        return 1

    rc = 0
    for w in windows:
        span = w["t1"] - w["t0"]
        print(f"== {w['config']} rep{w['rep']}: {span // 60} мин, "
              f"{w['tasks']} задач ==")
        for src in args.sources.split(","):
            src = src.strip()
            if src not in SOURCES:
                print(f"неизвестный источник {src!r}", file=sys.stderr)
                return 2
            cmd = [sys.executable, str(_EW), "--prom", args.prom,
                   *SOURCES[src], "--factor", "1",
                   "--t0", str(w["t0"]), "--t1", str(w["t1"]),
                   "--window", f"arm-rep{w['rep']}",
                   "--config", w["config"],
                   "--stand", args.stand, "--run-label", args.run_label,
                   "--ch-host", args.ch_host, "--ch-port", str(args.ch_port)]
            if args.dry_run:
                cmd.append("--dry-run")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  {src}: НЕ ЗАПИСАНО — {r.stderr.strip()[:200]}",
                      file=sys.stderr)
                rc = 1
            else:
                print(f"  {src}: ok")
    print(f"окон: {len(windows)}; проверка расчёта — "
          f"analysis/energy_metrics.py --run-label {args.run_label}")
    return rc


def self_test() -> int:
    rows = [
        # два плеча по два повторения, по три задачи
        {"config": "A-peaks", "rep": 1, "start_ts": 100.0, "end_ts": 200.0, "approximation": ""},
        {"config": "A-peaks", "rep": 1, "start_ts": 110.0, "end_ts": 250.0, "approximation": ""},
        {"config": "A-peaks", "rep": 1, "start_ts": 105.0, "end_ts": 240.0, "approximation": ""},
        {"config": "A-peaks", "rep": 2, "start_ts": 400.0, "end_ts": 500.0, "approximation": ""},
        {"config": "A-default", "rep": 1, "start_ts": 300.0, "end_ts": 390.0, "approximation": ""},
        # warmup и строка без времён (ошибка размещения) — не должны попасть
        {"config": "A-peaks", "rep": 9, "start_ts": 1.0, "end_ts": 2.0, "approximation": "warmup"},
        {"config": "A-peaks", "rep": 8, "start_ts": None, "end_ts": None, "approximation": ""},
    ]
    w = arm_windows(rows)
    keys = [(x["config"], x["rep"]) for x in w]
    assert keys == [("A-default", 1), ("A-peaks", 1), ("A-peaks", 2)], keys

    peaks1 = next(x for x in w if x == w[1])
    assert peaks1["t0"] == 100 and peaks1["t1"] == 251, peaks1
    assert peaks1["tasks"] == 3, peaks1

    # warmup и строки без времён отброшены
    assert not any(x["rep"] in (8, 9) for x in w), w

    # min_tasks отсекает огрызки повторений
    w2 = arm_windows(rows, min_tasks=3)
    assert [(x["config"], x["rep"]) for x in w2] == [("A-peaks", 1)], w2

    # Границы окна ОБЯЗАНЫ покрывать все задачи группы — то, что потом
    # проверяет analysis/energy_metrics.py перед делением.
    for x in w:
        grp = [r for r in rows if r["config"] == x["config"]
               and r["rep"] == x["rep"] and r["start_ts"] is not None
               and r["approximation"] != "warmup"]
        assert x["t0"] <= min(g["start_ts"] for g in grp), x
        assert x["t1"] >= max(g["end_ts"] for g in grp), x

    print("self-test: ок (группировка по плечам и повторениям, отсев warmup "
          "и ошибок размещения, min-tasks, покрытие границ)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--stand")
    ap.add_argument("--run-label")
    ap.add_argument("--prom", default="http://localhost:19090")
    ap.add_argument("--sources", default="ipmi,rapl-pkg,rapl-dram")
    ap.add_argument("--min-tasks", type=int, default=1,
                    help="пропускать повторения с меньшим числом задач")
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=8123)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.stand or not args.run_label:
        ap.error("--stand и --run-label обязательны (кроме --self-test)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
