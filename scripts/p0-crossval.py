#!/usr/bin/env python3
"""p0-crossval.py — фаза P0: кросс-валидация источников энергии одним прогоном.

Считает энергию окна [t0, t1] по всем источникам и применяет критерии
предрегистрации (docs/Энергопрогноз (предрегистрация).md, ЗАМОРОЖЕНО
18.08.2026). Нарушение критерия = недоверие инструменту: rc=1, «стоп до
выяснения» — как записано в прогнозе.

Источники и их устройство:
  * RAPL      — накопительные счётчики агента ss_node_rapl_joules_total
                (domain: package-0/package-1/dram/psys), разность на границах;
  * IPMI      — интеграл мгновенной idrac_power_watts (регистра энергии у
                iDRAC нет), трапеция по фактическим сэмплам;
  * PDU       — разность накопительного регистра кВт·ч. ВАЖНО: AP8853 —
                Metered, per-outlet учёта НЕТ, поэтому PDU — АГРЕГАТ стойки
                (сумма обеих PDU A/B-питания), не пер-узловой источник.

Критерии (поузловые выполняются всегда, агрегатные — при наличии PDU):
  Э0.4а  Σ RAPL(package+dram) ≤ psys        — на каждом узле (строго);
  Э0.4б  Σ psys(узлы) ≤ Σ PDU               — агрегат (грязные банки, т.е.
         чужие потребители на тех же банках, делают неравенство только
         сильнее — направление проверки безопасно до Д3);
  Э0.1   |Σ IPMI − Σ PDU| / Σ PDU ≤ 8 %     — агрегат; ЧЕСТНА только на
         чистых банках (Д3), до тех пор — репортится без вердикта;
  Э0.2   Σ RAPL / PDU в заданной полосе     — только при --e02-band lo:hi
         (полоса зависит от уровня нагрузки окна: 0.50:0.85 полная,
         0.35:0.65 холостой ход — задаёт оператор по типу окна).

Э0.3 (дрейф регистра PDU на выключенной нагрузке) — это тот же прогон по
окну простоя: смотреть накопление PDU при погашенных узлах, отдельного кода
не требует.

Отношения без критериев (rapl/psys, psys/ipmi, ipmi поузлово) печатаются
как измеренные — они уходят в §3 статьи как есть.

Примеры:
  # до PDU (доступна поузловая часть):
  p0-crossval.py --prom http://localhost:19090 --t0 <unix> --t1 <unix>
  # после Д2:
  p0-crossval.py --prom ... --t0 ... --t1 ... \\
      --pdu-metric 'pdu_energy_kwh' --e02-band 0.50:0.85

Только stdlib; --self-test поднимает фальшивый Prometheus и проверяет
цепочку, полосу и вердикты.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import urllib.parse
import urllib.request

# Инструментарий запросов и интегрирования — из energy-window.py (дефис в
# имени не даёт импортировать модулем).
_EW = pathlib.Path(__file__).with_name("energy-window.py")
_spec = importlib.util.spec_from_file_location("energy_window", _EW)
_ew = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ew)

RAPL_METRIC = "ss_node_rapl_joules_total"
IPMI_METRIC = "idrac_power_watts"


def counter_delta(prom: str, selector: str, t0: float, t1: float,
                  label: str) -> dict[str, float]:
    """{ключ: дельта счётчика}; отрицательная дельта (рестарт источника) —
    исключение: P0 требует чистого окна, маскировать нечего."""
    v0 = _ew.prom_instant(prom, selector, t0, label)
    v1 = _ew.prom_instant(prom, selector, t1, label)
    out = {}
    for k in sorted(set(v0) & set(v1)):
        d = v1[k] - v0[k]
        if d < 0:
            raise RuntimeError(f"сброс счётчика {selector} на {k}: {v0[k]} -> {v1[k]}")
        out[k] = d
    return out


def rapl_zones(prom: str, sel: str, ts: float) -> dict[tuple[str, str, str], float]:
    """{(node, domain, zone): счётчик} на момент ts. Ключ сознательно без
    pod/instance: рестарт агента меняет их, но сам по себе окно не портит —
    порчу (сброс счётчика) ловит по-зонная проверка дельты."""
    url = f"{prom.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode(
        {"query": sel, "time": f"{ts:.3f}"})
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus: {payload}")
    out: dict[tuple[str, str, str], float] = {}
    for r in payload["data"]["result"]:
        m = r["metric"]
        out[(m.get("node", ""), m.get("domain", ""), m.get("zone", ""))] = \
            float(r["value"][1])
    return out


def rapl_energy(prom: str, t0: float, t1: float) -> tuple[dict[str, float], dict[str, float]]:
    """({узел: Дж pkg+dram}, {узел: Дж psys}) — сумма по зонам считается
    здесь, а не sum by(node) в PromQL: в готовой сумме сброс одной зоны
    (рестарт агента) был бы неотличим от честной дельты."""
    pkgdram: dict[str, float] = {}
    psys: dict[str, float] = {}
    for dom_re, acc in (("package-.*|dram", pkgdram), ("psys", psys)):
        sel = f'{RAPL_METRIC}{{domain=~"{dom_re}"}}'
        v0 = rapl_zones(prom, sel, t0)
        v1 = rapl_zones(prom, sel, t1)
        for key in sorted(set(v0) & set(v1)):
            d = v1[key] - v0[key]
            if d < 0:
                raise RuntimeError(f"сброс RAPL-счётчика {key}: {v0[key]} -> {v1[key]}")
            acc[key[0]] = acc.get(key[0], 0.0) + d
    return pkgdram, psys


def ipmi_energy(prom: str, t0: float, t1: float, gap_limit: float) -> dict[str, float]:
    samples = _ew.prom_range_samples(prom, IPMI_METRIC, t0, t1, "node", gap_limit)
    out = {}
    for n in sorted(samples):
        e, note = _ew.integrate_power(samples[n], t0, t1, gap_limit)
        if e is None:
            raise RuntimeError(f"IPMI {n}: {note}")
        out[n] = e
    return out


def run(args: argparse.Namespace) -> int:
    t0, t1 = _ew._ts(args.t0), _ew._ts(args.t1)
    if t1 <= t0:
        print("пустое окно", file=sys.stderr)
        return 2
    dur = t1 - t0

    pkgdram, psys = rapl_energy(args.prom, t0, t1)
    ipmi = ipmi_energy(args.prom, t0, t1, args.gap_limit)
    nodes = sorted(set(pkgdram) & set(ipmi))
    if not nodes:
        print("нет узлов с RAPL и IPMI одновременно", file=sys.stderr)
        return 2

    pdu_j = None
    if args.pdu_metric:
        per_pdu = counter_delta(args.prom, args.pdu_metric, t0, t1, args.pdu_label)
        if not per_pdu:
            print("PDU-метрика задана, но пуста", file=sys.stderr)
            return 2
        pdu_j = sum(per_pdu.values()) * args.pdu_factor

    failures: list[str] = []
    W = lambda j: j / dur  # noqa: E731

    print(f"окно [{args.t0} .. {args.t1}] ({dur:.0f}с), узлов {len(nodes)}"
          + (f", PDU {W(pdu_j):.0f} Вт" if pdu_j is not None else ", PDU: нет (до Д2)"))
    print(f"{'узел':<10} {'rapl Вт':>9} {'psys Вт':>9} {'ipmi Вт':>9} "
          f"{'rapl/psys':>9} {'psys/ipmi':>9}  Э0.4а")
    for n in nodes:
        has_psys = n in psys and psys[n] > 0
        chain_ok = (pkgdram[n] <= psys[n]) if has_psys else None
        if chain_ok is False:
            failures.append(f"Э0.4а нарушена на {n}: ΣRAPL {W(pkgdram[n]):.0f} Вт "
                            f"> psys {W(psys[n]):.0f} Вт")
        print(f"{n:<10} {W(pkgdram[n]):>9.1f} "
              f"{W(psys[n]) if has_psys else float('nan'):>9.1f} {W(ipmi[n]):>9.1f} "
              f"{pkgdram[n]/psys[n] if has_psys else float('nan'):>9.3f} "
              f"{psys[n]/ipmi[n] if has_psys else float('nan'):>9.3f}  "
              + ("ok" if chain_ok else "нет psys" if chain_ok is None else "FAIL"))

    sum_psys = sum(psys[n] for n in nodes if n in psys)
    sum_ipmi = sum(ipmi[n] for n in nodes)
    print(f"{'Σ узлов':<10} {W(sum(pkgdram[n] for n in nodes)):>9.1f} "
          f"{W(sum_psys):>9.1f} {W(sum_ipmi):>9.1f}")

    if pdu_j is not None:
        if sum_psys > pdu_j:
            failures.append(f"Э0.4б нарушена: Σpsys {W(sum_psys):.0f} Вт "
                            f"> PDU {W(pdu_j):.0f} Вт")
        else:
            print(f"Э0.4б: Σpsys ≤ PDU — ok ({W(sum_psys):.0f} ≤ {W(pdu_j):.0f} Вт)")
        dev = abs(sum_ipmi - pdu_j) / pdu_j
        verdict = ("ok" if dev <= args.e01_limit else "FAIL") if args.clean_banks \
            else "без вердикта (банки не подтверждены чистыми, Д3)"
        print(f"Э0.1: |ΣIPMI − PDU|/PDU = {dev:.1%} (порог {args.e01_limit:.0%}) — {verdict}")
        if args.clean_banks and dev > args.e01_limit:
            failures.append(f"Э0.1 нарушена: расхождение IPMI↔PDU {dev:.1%}")
        if args.e02_band:
            lo, hi = (float(x) for x in args.e02_band.split(":"))
            share = sum(pkgdram[n] for n in nodes) / pdu_j
            ok = lo <= share <= hi
            print(f"Э0.2: ΣRAPL/PDU = {share:.2f} (полоса {lo}..{hi}) — "
                  + ("ok" if ok else "FAIL"))
            if not ok:
                failures.append(f"Э0.2 вне полосы: {share:.2f} не в [{lo}, {hi}]")

    if failures:
        print("\nСТОП ДО ВЫЯСНЕНИЯ (недоверие инструменту):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("\nP0-проверки окна пройдены"
          + ("" if pdu_j is not None else " (поузловая часть; агрегатная ждёт PDU)"))
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prom", required=True)
    ap.add_argument("--t0", required=True)
    ap.add_argument("--t1", required=True)
    ap.add_argument("--gap-limit", type=float, default=60.0)
    ap.add_argument("--pdu-metric", default="",
                    help="PromQL-селектор накопительного регистра PDU "
                         "(пусто до Д2 — агрегатные проверки пропускаются)")
    ap.add_argument("--pdu-label", default="pdu",
                    help="метка, различающая PDU A/B (суммируются)")
    ap.add_argument("--pdu-factor", type=float, default=3.6e6,
                    help="множитель регистра в джоули (кВт·ч: 3.6e6)")
    ap.add_argument("--e01-limit", type=float, default=0.08)
    ap.add_argument("--e02-band", default="",
                    help="lo:hi для ΣRAPL/PDU (0.50:0.85 полная нагрузка, "
                         "0.35:0.65 холостой ход); пусто — не проверять")
    ap.add_argument("--clean-banks", action="store_true",
                    help="банки подтверждены чистыми (Д3) — Э0.1 получает вердикт")
    return ap.parse_args(argv)


def self_test() -> int:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    # Узел n1: rapl pkg0+pkg1+dram = (100+90+30)Дж, psys 300Дж, ipmi 400Вт×100с.
    # PDU: две штуки, в сумме 0.03 кВт·ч = 108000 Дж (1080 Вт средних).
    counters = {
        "1000.000": {("n1", "package-0"): 1000.0, ("n1", "package-1"): 2000.0,
                     ("n1", "dram"): 500.0, ("n1", "psys"): 9000.0},
        "1100.000": {("n1", "package-0"): 1100.0, ("n1", "package-1"): 2090.0,
                     ("n1", "dram"): 530.0, ("n1", "psys"): 9300.0},
    }
    pdu = {"1000.000": {"a": 100.00, "b": 200.00},
           "1100.000": {"a": 100.02, "b": 200.01}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q, t = qs["query"][0], qs.get("time", ["1100.000"])[0]
            if "[" in q:  # ipmi range: ровные 400 Вт каждые 10с
                result = [{"metric": {"node": "n1"},
                           "values": [[ts, "400.0"] for ts in range(1000, 1101, 10)]}]
            elif "pdu_energy" in q:
                result = [{"metric": {"pdu": p}, "value": [0, str(v)]}
                          for p, v in pdu[t].items()]
            else:
                want_psys = "psys" in q  # селектор различает домены — зеркалим
                result = [{"metric": {"node": n, "domain": d}, "value": [0, str(v)]}
                          for (n, d), v in counters[t].items()
                          if (d == "psys") == want_psys]
            body = json.dumps({"status": "success",
                               "data": {"result": result}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    common = ["--prom", base, "--t0", "1000", "--t1", "1100"]

    # 1. Без PDU: цепочка Э0.4а (220 ≤ 300) — rc 0.
    assert run(parse_args(common)) == 0
    # 2. С PDU: Σpsys 300Дж=3Вт ≤ 1080Вт; Э0.1 без --clean-banks — без
    #    вердикта, rc 0 несмотря на дикое расхождение.
    assert run(parse_args(common + ["--pdu-metric", "pdu_energy_kwh"])) == 0
    # 3. --clean-banks: |40000−108000|/108000 = 63% > 8% — rc 1.
    assert run(parse_args(common + ["--pdu-metric", "pdu_energy_kwh",
                                    "--clean-banks"])) == 1
    # 4. Полоса Э0.2: ΣRAPL/PDU = 220/108000 — вне 0.5:0.85 — rc 1.
    assert run(parse_args(common + ["--pdu-metric", "pdu_energy_kwh",
                                    "--e02-band", "0.50:0.85"])) == 1
    srv.shutdown()
    print("self-test: ок (Э0.4а, Э0.4б, Э0.1 с/без чистых банков, Э0.2)")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(run(parse_args(sys.argv[1:])))
