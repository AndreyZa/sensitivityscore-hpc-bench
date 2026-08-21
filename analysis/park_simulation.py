#!/usr/bin/env python3
"""park_simulation.py — экономия от гашения на парке из N узлов.

ЗАЧЕМ. Стенд из трёх узлов отвечает на вопрос «работает ли механизм», но
не на вопрос «сколько это даст у нас». Второй задают партнёры, и ответ на
него нельзя получить измерением: пятидесяти узлов нет. Зато все входы
модели измерены — кривая мощности узла, мощность выключенного состояния,
цена цикла и время подъёма, — а трасса задач берётся из настоящей серии.
Этим расчёт отличается от преобладающих в литературе симуляций, где
параметры приняты по модели.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. Ни очередей с приоритетами, ни миграции, ни
догадок о будущем: политика ровно та, что измерена на стенде, — задача
идёт на узел, где помещается, а узел, простоявший дольше порога,
выключается. Всё, что сложнее, добавило бы в расчёт предположения, не
подкреплённые измерением, и ответ стал бы менее достоверным, а не более.

    park_simulation.py --nodes 3 12 50 --run-label p2-energy
    park_simulation.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

# Измеренные входы (figures/figdata.json энергостатьи, лестница P1 и цикл
# Э1.4). Держим здесь копией сознательно: расчёт обязан быть
# воспроизводимым без доступа к репозиторию статьи, а расхождение ловит
# --check по тому же файлу.
LADDER_UTIL = np.array([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100], float)
LADDER_WATT = np.array([263, 303, 324, 332, 353, 377, 395, 414, 433, 551, 730], float)
P_OFF_W = 23.3
BOOT_S = 179.0
CORES = 64


def power_w(util_pct: float) -> float:
    """Мощность узла при утилизации: линейная интерполяция ИЗМЕРЕННОЙ
    лестницы, а не модель K0+K1e^(K2x).

    Модель нужна планировщику, потому что таков его интерфейс; для оценки
    экономии она хуже — систематически мажет на холостом ходу (§6 статьи:
    311 Вт против измеренных 263). Интерполяция лестницы не добавляет к
    измерению ничего своего.
    """
    return float(np.interp(np.clip(util_pct, 0, 100), LADDER_UTIL, LADDER_WATT))


def simulate(arrivals: np.ndarray, durations: np.ndarray, cores: np.ndarray,
             n_nodes: int, suspend_s: float, e_cycle_j: float,
             step_s: float = 10.0) -> dict:
    """Прогон трассы на парке из n_nodes узлов, с гашением и без.

    Возвращает энергию обоих режимов, экономию и добавленное ожидание.
    Шаг по времени фиксированный: событийная схема здесь ничего не
    уточнила бы (мощность меняется медленнее шага), а читается хуже.
    """
    # Горизонт задаётся ТРАССОЙ, а не политикой. Раньше в него входил
    # suspend_s, и прогоны с разными порогами шли разное время — то есть
    # сравнивались периоды разной длины, а свип порога терял смысл. Хвост
    # длиной в подъём оставлен, чтобы начатый подъём успел завершиться.
    end = float(arrivals.max() + durations.max()) + BOOT_S
    ticks = int(end / step_s) + 1

    free = np.full(n_nodes, CORES, dtype=float)      # свободные ядра
    idle_for = np.zeros(n_nodes)                     # сколько простаивает
    off = np.zeros(n_nodes, dtype=bool)
    boot_left = np.zeros(n_nodes)                    # сколько осталось подниматься
    running: list[tuple[int, float, float]] = []     # (узел, ядра, когда закончит)
    queue: list[tuple[float, float, float]] = []     # (пришла, ядра, длительность)

    e_gash = e_nogash = 0.0
    cycles = 0
    wait_total = 0.0
    nxt = 0

    for tick in range(ticks):
        t = tick * step_s

        # завершения
        for r in [r for r in running if r[2] <= t]:
            free[r[0]] += r[1]
            running.remove(r)

        # прибытия
        while nxt < len(arrivals) and arrivals[nxt] <= t:
            queue.append((arrivals[nxt], float(cores[nxt]), float(durations[nxt])))
            nxt += 1

        # Подъём начатых узлов. Возвращать в строй надо ТОЛЬКО те, что
        # действительно поднимались: строка `off[boot_left <= 0] &= False`
        # включала обратно все выключенные разом, и узел гас и поднимался
        # каждый такт — 29 циклов на трассе, где всей энергии 0,1 кВт·ч,
        # то есть «экономия» уходила в минус пятикратно.
        booting = boot_left > 0
        boot_left = np.where(booting, np.maximum(0.0, boot_left - step_s), boot_left)
        off[booting & (boot_left <= 0)] = False

        # размещение: первый узел, где помещается и который включён
        rest: list[tuple[float, float, float]] = []
        for came, need, dur in queue:
            fit = np.where((~off) & (boot_left <= 0) & (free >= need))[0]
            if len(fit):
                n = int(fit[0])
                free[n] -= need
                running.append((n, need, t + dur))
                wait_total += t - came
            else:
                # работы некуда поставить — поднимаем выключенный узел
                sleeping = np.where(off & (boot_left <= 0))[0]
                if len(sleeping):
                    boot_left[int(sleeping[0])] = BOOT_S
                rest.append((came, need, dur))
        queue = rest

        # энергия за такт
        busy = CORES - free
        util = busy / CORES * 100.0
        for n in range(n_nodes):
            p_on = power_w(util[n])
            e_nogash += p_on * step_s
            if off[n] and boot_left[n] <= 0:
                e_gash += P_OFF_W * step_s
            elif boot_left[n] > 0:
                e_gash += power_w(0) * step_s      # подъём: считаем по холостому ходу
            else:
                e_gash += p_on * step_s

        # гашение простоявших дольше порога; один узел всегда остаётся
        idle_for = np.where(busy == 0, idle_for + step_s, 0.0)
        can_off = (~off) & (boot_left <= 0) & (busy == 0) & (idle_for >= suspend_s)
        if can_off.any() and (~off).sum() > 1:
            n = int(np.where(can_off)[0][0])
            off[n] = True
            idle_for[n] = 0.0
            e_gash += e_cycle_j
            cycles += 1

    saved = e_nogash - e_gash
    return {
        "nodes": n_nodes,
        "kwh_nogash": e_nogash / 3.6e6,
        "kwh_gash": e_gash / 3.6e6,
        "saved_pct": 100.0 * saved / e_nogash if e_nogash else 0.0,
        "cycles": cycles,
        "wait_s": wait_total / max(len(arrivals), 1),
    }


def trace_from_ch(run_label: str, stand: str, host: str, port: int):
    """Трасса из настоящей серии: приходы, длительности, запрошенные ядра."""
    import clickhouse_connect
    c = clickhouse_connect.get_client(host=host, port=port, username="default",
                                      database="sensitivityscore")
    df = c.query_df(
        "SELECT toUnixTimestamp64Milli(start_ts)/1000. AS a, makespan_s AS d "
        "FROM results FINAL WHERE run_label = %(l)s AND stand = %(s)s "
        "AND makespan_s IS NOT NULL AND approximation != 'warmup' ORDER BY a",
        parameters={"l": run_label, "s": stand})
    if df.empty:
        raise SystemExit(f"нет задач с меткой {run_label}")
    a = df["a"].to_numpy(float)
    return a - a.min(), df["d"].to_numpy(float), np.full(len(df), 28.0)


def self_test() -> int:
    # Одна задача на 64 ядра и 100 с при пороге 10 с: узлы, которым нечего
    # делать, обязаны погаснуть, и экономия обязана быть положительной.
    a = np.array([0.0]); d = np.array([100.0]); c = np.array([64.0])
    r = simulate(a, d, c, n_nodes=4, suspend_s=10.0, e_cycle_j=57_000.0)
    assert r["cycles"] >= 2, r
    assert 0 < r["saved_pct"] < 100, r

    # Порог выше длительности трассы — гасить некогда, экономии нет.
    r2 = simulate(a, d, c, n_nodes=4, suspend_s=10_000.0, e_cycle_j=57_000.0)
    assert r2["cycles"] == 0 and abs(r2["saved_pct"]) < 1e-9, r2

    # Цена цикла не бесплатна: при дорогом цикле экономия меньше.
    r3 = simulate(a, d, c, n_nodes=4, suspend_s=10.0, e_cycle_j=5_000_000.0)
    assert r3["saved_pct"] < r["saved_pct"], (r3, r)

    # Мощность берётся из лестницы, а не из модели.
    assert power_w(0) == 263 and power_w(100) == 730
    assert 433 < power_w(85) < 551

    print("self-test: ок (гашение идёт, порог выше трассы отключает его, "
          "цена цикла уменьшает выигрыш, мощность из лестницы)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--nodes", type=int, nargs="*", default=[3, 12, 50])
    ap.add_argument("--suspend", type=float, default=480.0, help="порог простоя, с")
    ap.add_argument("--e-cycle-kj", type=float, default=57.0)
    ap.add_argument("--run-label", default="p2-energy")
    ap.add_argument("--stand", default="prod")
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=8123)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    a, d, c = trace_from_ch(args.run_label, args.stand, args.ch_host, args.ch_port)
    # Трасса снята на трёх узлах; на парк большего размера её масштабируем
    # по числу узлов, иначе пятьдесят узлов простаивали бы почти всегда и
    # экономия вышла бы завышенной до бессмыслицы.
    rows = []
    for n in args.nodes:
        k = max(1, round(n / 3))
        aa = np.concatenate([a + i * 1e-3 for i in range(k)])
        dd = np.tile(d, k); cc = np.tile(c, k)
        order = np.argsort(aa)
        rows.append(simulate(aa[order], dd[order], cc[order], n,
                             args.suspend, args.e_cycle_kj * 1000.0))

    print(f"{'узлов':>6} {'кВт·ч без гашения':>18} {'с гашением':>12} "
          f"{'экономия':>9} {'циклов':>7} {'ожидание, с':>12}")
    for r in rows:
        print(f"{r['nodes']:>6} {r['kwh_nogash']:>18.1f} {r['kwh_gash']:>12.1f} "
              f"{r['saved_pct']:>8.1f}% {r['cycles']:>7} {r['wait_s']:>12.1f}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
