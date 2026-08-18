#!/usr/bin/env python3
"""idrac-power-poller.py — мгновенная мощность узлов из iDRAC (Redfish) в Pushgateway.

Живёт на лабе (.72): iDRAC-сеть видна только из её WG-туннеля, а обратного
маршрута кластер→лаба нет — поэтому push через ss-forward@pushgateway
(kubectl port-forward на localhost:9091), а не скрейп. Смысл метрик — источник
ipmi энерговетки (кросс-сверка Э0.1/Э0.4 предрегистрации) и дашборд мощности:
накопительного счётчика энергии у iDRAC этой прошивки НЕТ (проверено
18.08.2026), так что энергия окна из этого источника — интегрирование опроса,
и точность честно ограничена его частотой; регистры — у RAPL и PDU.

Метрики (PUT заменяет группу целиком — узел, не ответивший на опрос, из
выдачи ИСЧЕЗАЕТ, что честнее протухшего значения):
  idrac_power_watts{node=...}             PowerControl.PowerConsumedWatts
  idrac_psu_input_watts{node=...,psu=...} вход каждого БП (карта розеток: PS2
                                          в горячем резерве ~5 Вт — тоже розетка)
  idrac_poll_timestamp_seconds{node=...}  свежесть опроса (pushgateway помнит
                                          последний push вечно — различать
                                          «мощность такая» и «poller молчит»)

Запуск (systemd-юнит scripts/ss-idrac-poller.service):
  IDRAC_MAP="wrk-b6=10.21.200.106,wrk-b7=10.21.200.107,wrk-b8=10.21.200.108" \\
  IDRAC_PASS_FILE=~/.idrac-pass.txt ./idrac-power-poller.py [--once]

Пароль читается из файла и в аргументы/окружение процессов не попадает.
TLS iDRAC самоподписанный — проверка сертификата отключена осознанно: сеть
доступна только из WG-туннеля, а альтернатива (таскать CA каждого BMC)
не стоит своей хрупкости. Только stdlib. --self-test: фальшивые Redfish и
Pushgateway на localhost, проверка тела push и выпадения молчащего узла.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

POWER_PATH = "/redfish/v1/Chassis/System.Embedded.1/Power"


def parse_map(s: str) -> dict[str, str]:
    out = {}
    for pair in s.split(","):
        pair = pair.strip()
        if not pair:
            continue
        node, _, host = pair.partition("=")
        if not node or not host:
            raise ValueError(f"IDRAC_MAP: ожидается node=host, получено {pair!r}")
        out[node.strip()] = host.strip()
    return out


def fetch_power(host: str, user: str, password: str, timeout: float = 10.0) -> dict:
    """{'watts': float, 'psu': {'PS1': float, ...}} одного iDRAC."""
    ctx = ssl._create_unverified_context()  # noqa: S323 — см. докстринг
    req = urllib.request.Request(f"https://{host}{POWER_PATH}")
    tok = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {tok}")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        d = json.load(resp)
    watts = float(d["PowerControl"][0]["PowerConsumedWatts"])
    psu = {}
    for i, p in enumerate(d.get("PowerSupplies", []), start=1):
        w = p.get("PowerInputWatts")
        if w is not None:
            psu[f"PS{i}"] = float(w)
    return {"watts": watts, "psu": psu}


def exposition(samples: dict[str, dict], now: float) -> str:
    """Тело push'а: только узлы, ответившие в ЭТОМ раунде."""
    lines = [
        "# TYPE idrac_power_watts gauge",
        "# HELP idrac_power_watts System power from iDRAC Redfish PowerControl (instantaneous).",
    ]
    for node, s in sorted(samples.items()):
        lines.append(f'idrac_power_watts{{node="{node}"}} {s["watts"]}')
    lines += [
        "# TYPE idrac_psu_input_watts gauge",
        "# HELP idrac_psu_input_watts Per-PSU input power; sum of both outlets is what a per-outlet PDU must see.",
    ]
    for node, s in sorted(samples.items()):
        for psu, w in sorted(s["psu"].items()):
            lines.append(f'idrac_psu_input_watts{{node="{node}",psu="{psu}"}} {w}')
    lines += [
        "# TYPE idrac_poll_timestamp_seconds gauge",
        "# HELP idrac_poll_timestamp_seconds Unix time of the poll that produced these values; alert on staleness.",
    ]
    for node in sorted(samples):
        lines.append(f'idrac_poll_timestamp_seconds{{node="{node}"}} {now:.3f}')
    return "\n".join(lines) + "\n"


def push(gateway: str, body: str, timeout: float = 10.0) -> None:
    # PUT (не POST): заменяет группу целиком, молчащие узлы исчезают.
    req = urllib.request.Request(
        f"{gateway.rstrip('/')}/metrics/job/idrac-power",
        data=body.encode(), method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def run(once: bool) -> int:
    idracs = parse_map(os.environ.get(
        "IDRAC_MAP",
        "wrk-b6=10.21.200.106,wrk-b7=10.21.200.107,wrk-b8=10.21.200.108"))
    user = os.environ.get("IDRAC_USER", "root")
    pass_file = os.path.expanduser(os.environ.get("IDRAC_PASS_FILE", "~/.idrac-pass.txt"))
    with open(pass_file) as f:
        password = f.read().strip()
    gateway = os.environ.get("PUSHGATEWAY", "http://127.0.0.1:9091")
    interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "10"))

    print(f"poller: {len(idracs)} iDRAC -> {gateway}, каждые {interval:g}с", flush=True)
    while True:
        samples: dict[str, dict] = {}
        for node, host in idracs.items():
            try:
                samples[node] = fetch_power(host, user, password)
            except Exception as e:  # noqa: BLE001 — один BMC не роняет опрос
                print(f"WARN {node} ({host}): {e}", flush=True)
        try:
            push(gateway, exposition(samples, time.time()))
        except Exception as e:  # noqa: BLE001 — push ретраится следующим раундом
            print(f"WARN push: {e}", flush=True)
        if once:
            return 0 if len(samples) == len(idracs) else 1
        time.sleep(interval)


def self_test() -> int:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # Redfish
            body = json.dumps({
                "PowerControl": [{"PowerConsumedWatts": 268}],
                "PowerSupplies": [{"PowerInputWatts": 259.5}, {"PowerInputWatts": 5.0}],
            }).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):  # Pushgateway
            captured["path"] = self.path
            captured["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
            self.send_response(200)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # опрос: один живой узел + один молчащий (порт без слушателя)
    s = {"ok-node": fetch_power_plain(f"127.0.0.1:{srv.server_port}")}
    body = exposition(s, 1000.0)
    push(f"http://127.0.0.1:{srv.server_port}", body)
    srv.shutdown()

    assert captured["path"] == "/metrics/job/idrac-power", captured
    assert 'idrac_power_watts{node="ok-node"} 268.0' in captured["body"], captured["body"]
    assert 'idrac_psu_input_watts{node="ok-node",psu="PS2"} 5.0' in captured["body"]
    assert 'idrac_poll_timestamp_seconds{node="ok-node"} 1000.000' in captured["body"]
    assert "silent-node" not in captured["body"]  # молчащий узел исчез, не протух
    print("self-test: ок (redfish-разбор, тело push, выпадение молчащего узла)")
    return 0


def fetch_power_plain(host: str) -> dict:
    """Как fetch_power, но http без авторизации — только для self-test."""
    with urllib.request.urlopen(f"http://{host}{POWER_PATH}", timeout=5) as resp:
        d = json.load(resp)
    watts = float(d["PowerControl"][0]["PowerConsumedWatts"])
    psu = {f"PS{i}": float(p["PowerInputWatts"])
           for i, p in enumerate(d.get("PowerSupplies", []), start=1)
           if p.get("PowerInputWatts") is not None}
    return {"watts": watts, "psu": psu}


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(run(once="--once" in sys.argv))
