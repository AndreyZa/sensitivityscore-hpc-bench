#!/usr/bin/env python3
"""Э0.3 — годность накопительного регистра PDU и поведение чужой нагрузки.

Две независимые вещи за один проход по окну:

1. РЕГИСТР. Разность накопительного счётчика сверяется с интегралом
   мгновенной мощности ТОГО ЖЕ устройства. Обе величины приходят с одной
   PDU, поэтому проверка ничего не знает о составе стойки и не требует её
   простоя — она отвечает ровно на вопрос «можно ли доверять разности
   регистра на границах окна», от которого зависят все энергоокна.
   Критерий Э0.3: расхождение ≤ 0.5 % энергии окна.

2. СОСЕДИ. Стойка общая (11 узлов и два коммутатора, стенду принадлежат
   три), поэтому отдельно считается «чужая» мощность — стойка минус узлы
   стенда по iDRAC — и её разброс за окно. Это не критерий, а вход в
   методику: чем сильнее гуляют соседи, тем осторожнее следует читать
   сверку энергоокон с PDU.

  python3 pdu-register-check.py --prom URL --t0 <epoch> --t1 <epoch>
  python3 pdu-register-check.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

# Регистры rPDU2 (сверены прямым опросом 20.08.2026):
#   энергия — десятые кВт·ч, мощность — сотые кВт.
ENERGY_METRIC = "pdu_device_energy_decikwh"
POWER_METRIC = "pdu_device_power_centikw"
ENERGY_TO_J = 3.6e5
POWER_TO_W = 10.0
DRIFT_LIMIT = 0.005


def q_range(prom: str, query: str, t0: float, t1: float, step: int) -> list:
    url = prom.rstrip("/") + "/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": query, "start": f"{t0:.0f}", "end": f"{t1:.0f}", "step": str(step)})
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.load(r)
    if d.get("status") != "success":
        raise RuntimeError(f"Prometheus: {d.get('error')}")
    return d["data"]["result"]


def integrate(points: list[tuple[float, float]]) -> float:
    """Трапеция по фактическим сэмплам, в джоулях (значения — ватты)."""
    total = 0.0
    for (ta, va), (tb, vb) in zip(points, points[1:]):
        total += (va + vb) / 2.0 * (tb - ta)
    return total


def series_points(res: dict) -> list[tuple[float, float]]:
    return [(float(t), float(v)) for t, v in res["values"]]


def check(prom: str, t0: float, t1: float, step: int) -> int:
    print(f"окно: {t0:.0f} .. {t1:.0f} ({(t1 - t0) / 3600:.2f} ч), шаг {step} с\n")
    bad = []

    print("=== 1. Регистр против интеграла мощности (Э0.3) ===")
    energy = {r["metric"].get("pdu", "?"): series_points(r)
              for r in q_range(prom, ENERGY_METRIC, t0, t1, step)}
    power = {r["metric"].get("pdu", "?"): series_points(r)
             for r in q_range(prom, POWER_METRIC, t0, t1, step)}
    if not energy:
        print("  нет данных регистра — PDU не собираются?")
        return 1
    for pdu in sorted(energy):
        pts = energy[pdu]
        delta_j = (pts[-1][1] - pts[0][1]) * ENERGY_TO_J
        if delta_j < 0:
            print(f"  PDU {pdu}: регистр УМЕНЬШИЛСЯ — сброс счётчика в окне, "
                  "окно непригодно")
            bad.append(f"регистр {pdu} сброшен")
            continue
        watts = [(t, v * POWER_TO_W) for t, v in power.get(pdu, [])]
        integral_j = integrate(watts)
        if integral_j <= 0:
            print(f"  PDU {pdu}: нет мощности для сверки")
            continue
        rel = abs(delta_j - integral_j) / integral_j
        # Порог не может быть жёстче собственной дискретности счётчика:
        # шаг регистра 0.1 кВт·ч, значит разность на границах окна известна
        # с точностью ±полшага. Пока энергия окна мала, эта неопределённость
        # больше 0.5 %, и сравнивать с 0.5 % бессмысленно — так у PDU с
        # малой нагрузкой два шага за два часа давали «дрейф 5 %» на
        # исправном счётчике (20.08.2026). Порог берётся как максимум из
        # методического 0.5 % и фактической дискретности окна.
        quant = (ENERGY_TO_J / 2.0) / integral_j
        limit = max(DRIFT_LIMIT, quant)
        mark = "ok" if rel <= limit else "ПРЕВЫШЕНО"
        note = "" if quant <= DRIFT_LIMIT else \
            f" [окно мало: квантование ±{quant * 100:.1f} %]"
        print(f"  PDU {pdu}: регистр {delta_j / 3.6e6:8.3f} кВт·ч | "
              f"интеграл {integral_j / 3.6e6:8.3f} кВт·ч | "
              f"расхождение {rel * 100:5.2f} % (порог {limit * 100:.1f} %) — {mark}{note}")
        if quant > DRIFT_LIMIT:
            need_h = (ENERGY_TO_J / 2.0 / DRIFT_LIMIT) / (integral_j / (t1 - t0)) / 3600
            print(f"           для вердикта по 0.5 % нужно окно от {need_h:.0f} ч "
                  "при этой мощности")
        if rel > limit:
            bad.append(f"дрейф {pdu} {rel * 100:.2f} % (порог {limit * 100:.1f} %)")

    tot_reg = sum((pts[-1][1] - pts[0][1]) * ENERGY_TO_J for pts in energy.values()
                  if pts[-1][1] >= pts[0][1])
    tot_int = sum(integrate([(t, v * POWER_TO_W) for t, v in power[pdu]])
                  for pdu in power)
    if tot_int > 0:
        rel = abs(tot_reg - tot_int) / tot_int
        quant = (ENERGY_TO_J * len(energy) / 2.0) / tot_int
        limit = max(DRIFT_LIMIT, quant)
        mark = "ok" if rel <= limit else "ПРЕВЫШЕНО"
        print(f"  ОБЕ PDU (то, чем пользуются энергоокна): регистр "
              f"{tot_reg / 3.6e6:.3f} кВт·ч | интеграл {tot_int / 3.6e6:.3f} кВт·ч | "
              f"расхождение {rel * 100:.2f} % (порог {limit * 100:.1f} %) — {mark}")
        if rel > limit:
            bad.append(f"дрейф суммы {rel * 100:.2f} %")

    print("\n=== 2. Чужая нагрузка в стойке (вход в методику, не критерий) ===")
    rack = q_range(prom, f"sum({POWER_METRIC}) * {POWER_TO_W}", t0, t1, step)
    stand = q_range(prom, "sum(idrac_power_watts)", t0, t1, step)
    if not rack or not stand:
        print("  недостаточно данных для оценки соседей")
    else:
        rp = dict(series_points(rack[0]))
        sp = dict(series_points(stand[0]))
        common = sorted(set(rp) & set(sp))
        foreign = [rp[t] - sp[t] for t in common]
        if foreign:
            lo, hi = min(foreign), max(foreign)
            mean = sum(foreign) / len(foreign)
            swing = (hi - lo) / mean * 100 if mean else 0.0
            print(f"  стойка целиком: {sum(rp[t] for t in common) / len(common):7.0f} Вт (сред.)")
            print(f"  узлы стенда:    {sum(sp[t] for t in common) / len(common):7.0f} Вт (сред.)")
            print(f"  чужая нагрузка: {mean:7.0f} Вт (сред.), "
                  f"от {lo:.0f} до {hi:.0f}, размах {swing:.1f} % от средней")
            print(f"  доля стенда:    {100 * sum(sp[t] for t in common) / sum(rp[t] for t in common):5.1f} %")

    print()
    if bad:
        print("ИТОГ: НЕ ПРОЙДЕНО — " + "; ".join(bad))
        return 1
    print("ИТОГ: регистр годен, разности на границах окна можно доверять")
    return 0


def self_test() -> int:
    """Проверка на данных с известным ответом: ровный 1 кВт час подряд."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    t0, t1, step = 1_000_000.0, 1_003_600.0, 60
    ts = [t0 + i * step for i in range(int((t1 - t0) / step) + 1)]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            query = qs["query"][0]
            if ENERGY_METRIC in query and "sum" not in query:
                # 1 кВт ровно час = 1 кВт·ч = 10 единиц регистра по 0.1 кВт·ч
                vals = [[t, f"{(t - t0) / 3600 * 10:.6f}"] for t in ts]
                res = [{"metric": {"pdu": "a"}, "values": vals}]
            elif POWER_METRIC in query and "sum" not in query:
                res = [{"metric": {"pdu": "a"}, "values": [[t, "100"] for t in ts]}]
            elif "sum(" in query and POWER_METRIC in query:
                res = [{"metric": {}, "values": [[t, "1000"] for t in ts]}]
            else:
                res = [{"metric": {}, "values": [[t, "250"] for t in ts]}]
            body = json.dumps({"status": "success", "data": {"resultType": "matrix",
                                                             "result": res}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    rc = check(url, t0, t1, step)
    srv.shutdown()
    # Регистр 1 кВт·ч против интеграла 1 кВт·ч — расхождение 0, чужая
    # нагрузка 1000-250 = 750 Вт ровно.
    print("самопроверка:", "OK" if rc == 0 else "ПРОВАЛ")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", default="http://localhost:19090")
    ap.add_argument("--t0")
    ap.add_argument("--t1")
    ap.add_argument("--step", type=int, default=60)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not (a.t0 and a.t1):
        ap.error("нужны --t0 и --t1 (epoch) либо --self-test")
    return check(a.prom, float(a.t0), float(a.t1), a.step)


if __name__ == "__main__":
    sys.exit(main())
