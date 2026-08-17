#!/usr/bin/env python3
"""energy-window.py — энергия окна серии из накопительных счётчиков.

Опрашивает Prometheus мгновенным запросом на двух границах окна и пишет в
ClickHouse (sensitivityscore.energy_windows, миграция 003) по строке на узел:
energy_j = (счётчик(t1) − счётчик(t0)) × factor. Точка правды — накопительный
регистр прибора (кВт·ч PDU, джоули RAPL, счётчик IPMI), а не интеграл
мгновенной мощности: так точность задаётся классом прибора, не частотой
опроса (см. docs/Энергоэффективность (план, прод).md, §2).

Примеры:
  RAPL: --metric node_rapl_package_joules_total --source rapl-pkg --factor 1
  PDU : --metric pdu_outlet_energy_kwh_total   --source pdu --factor 3.6e6

Метрика обязана иметь метку с именем узла (--node-label, по умолчанию
instance); маппинг instance→узел при необходимости делается relabel'ом на
стороне Prometheus, не здесь.

Отрицательная разность (сброс/переполнение счётчика — угроза из плана §8)
не маскируется: строка узла пропускается, код возврата ненулевой.

--self-test поднимает фальшивые Prometheus и ClickHouse на localhost и
проверяет счёт, фактор, среднюю мощность и защиту от сброса. Только stdlib.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TABLE = "sensitivityscore.energy_windows"


def _ts(s: str) -> float:
    """ISO-8601 (с зоной или UTC по умолчанию) либо unix-секунды."""
    try:
        return float(s)
    except ValueError:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


def prom_instant(base: str, query: str, ts: float, node_label: str) -> dict[str, float]:
    """{узел: значение счётчика} на момент ts."""
    url = f"{base.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode(
        {"query": query, "time": f"{ts:.3f}"})
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus: {payload}")
    out: dict[str, float] = {}
    for r in payload["data"]["result"]:
        node = r["metric"].get(node_label, "")
        if node:
            out[node] = float(r["value"][1])
    return out


def ch_insert(host: str, port: int, rows: list[dict], user: str, password: str) -> None:
    q = f"INSERT INTO {TABLE} FORMAT JSONEachRow"
    url = f"http://{host}:{port}/?" + urllib.parse.urlencode(
        {"query": q, "user": user, "password": password})
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def run(args: argparse.Namespace) -> int:
    t0, t1 = _ts(args.t0), _ts(args.t1)
    if t1 <= t0:
        print(f"пустое окно: t1 ({args.t1}) <= t0 ({args.t0})", file=sys.stderr)
        return 2
    v0 = prom_instant(args.prom, args.metric, t0, args.node_label)
    v1 = prom_instant(args.prom, args.metric, t1, args.node_label)
    nodes = sorted(set(v0) & set(v1))
    if not nodes:
        print("нет узлов, присутствующих на обеих границах окна", file=sys.stderr)
        return 2

    rows, resets = [], []
    dur = t1 - t0
    iso = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # noqa: E731
    for n in nodes:
        delta = v1[n] - v0[n]
        if delta < 0:
            resets.append(n)  # сброс счётчика: не маскировать, не вписывать
            continue
        energy_j = delta * args.factor
        rows.append({
            "stand": args.stand, "run_label": args.run_label,
            "config": args.config, "window": args.window,
            "node": n, "source": args.source,
            "ts_start": iso(t0), "ts_end": iso(t1),
            "energy_j": energy_j, "avg_power_w": energy_j / dur,
            "meta": json.dumps({"metric": args.metric}, ensure_ascii=False),
            "harness_commit": args.harness_commit,
        })
    for n in resets:
        print(f"СБРОС СЧЁТЧИКА на {n}: {v0[n]} -> {v1[n]}, строка не записана",
              file=sys.stderr)
    lost = sorted((set(v0) | set(v1)) - set(nodes))
    if lost:
        print(f"узлы без обеих границ (пропущены): {', '.join(lost)}", file=sys.stderr)

    if args.dry_run:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
    elif rows:
        ch_insert(args.ch_host, args.ch_port, rows, args.ch_user, args.ch_password)
    print(f"окно {args.window} [{args.t0} .. {args.t1}]: узлов {len(rows)}, "
          f"источник {args.source}" + (", ЕСТЬ СБРОСЫ" if resets else ""))
    return 1 if resets else 0


def self_test() -> int:
    """Фальшивые Prometheus и ClickHouse в одном процессе; сеть — localhost."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    # t=1000: узлы a=100, b=200, c=500; t=1100: a=460 (+360), b=150 (сброс), c нет
    prom_data = {
        "1000.000": {"a": 100.0, "b": 200.0, "c": 500.0},
        "1100.000": {"a": 460.0, "b": 150.0},
    }
    captured: dict[str, bytes] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # Prometheus
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            vals = prom_data.get(qs["time"][0], {})
            result = [{"metric": {"node": n}, "value": [0, str(v)]}
                      for n, v in vals.items()]
            body = json.dumps({"status": "success",
                               "data": {"result": result}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # ClickHouse
            captured["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    argv = ["--prom", base, "--metric", "fake_energy_kwh_total",
            "--node-label", "node", "--t0", "1000", "--t1", "1100",
            "--factor", "3.6e6", "--source", "pdu", "--window", "pressure",
            "--stand", "prod", "--run-label", "selftest",
            "--ch-host", "127.0.0.1", "--ch-port", str(srv.server_port)]
    rc = run(parse_args(argv))
    srv.shutdown()

    assert rc == 1, f"сброс счётчика на b обязан дать rc=1, получено {rc}"
    rows = [json.loads(line) for line in captured["body"].decode().splitlines()]
    assert [r["node"] for r in rows] == ["a"], rows  # b — сброс, c — нет границы
    r = rows[0]
    assert r["energy_j"] == (460.0 - 100.0) * 3.6e6, r
    assert abs(r["avg_power_w"] - r["energy_j"] / 100) < 1e-9, r
    assert r["window"] == "pressure" and r["source"] == "pdu", r
    assert json.loads(r["meta"])["metric"] == "fake_energy_kwh_total", r
    print("self-test: ок (дельта×фактор, средняя мощность, сброс, потеря узла)")
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prom", required=True, help="базовый URL Prometheus")
    ap.add_argument("--metric", required=True,
                    help="PromQL-селектор накопительного счётчика")
    ap.add_argument("--node-label", default="instance",
                    help="метка с именем узла (default: instance)")
    ap.add_argument("--t0", required=True, help="начало окна: ISO-8601 или unix")
    ap.add_argument("--t1", required=True, help="конец окна")
    ap.add_argument("--factor", type=float, default=1.0,
                    help="множитель в джоули (кВт·ч: 3.6e6; Дж: 1)")
    ap.add_argument("--source", required=True,
                    choices=["pdu", "rapl-pkg", "rapl-dram", "ipmi"])
    ap.add_argument("--window", required=True,
                    help="pressure | idle | calib-step-<n>")
    ap.add_argument("--stand", required=True)
    ap.add_argument("--run-label", required=True)
    ap.add_argument("--config", default="", help="плечо (A-peaks, ...)")
    ap.add_argument("--harness-commit", default="")
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=8123)
    ap.add_argument("--ch-user", default="default")
    ap.add_argument("--ch-password", default="")
    ap.add_argument("--dry-run", action="store_true",
                    help="печатать строки, в ClickHouse не писать")
    return ap.parse_args(argv)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(run(parse_args(sys.argv[1:])))
