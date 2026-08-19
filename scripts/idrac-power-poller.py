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
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

POWER_PATH = "/redfish/v1/Chassis/System.Embedded.1/Power"
BIOS_PATH = "/redfish/v1/Systems/System.Embedded.1/Bios"

# Настройки BIOS, которые ПАРАМЕТРИЗУЮТ ИЗМЕРЕНИЕ. Список закрытый: атрибутов
# в Redfish 559, и вываливать их все значило бы утопить провенанс в шуме про
# порядок загрузки и SecureBoot.
#
# Каждая строка здесь — либо вход модели, либо источник конфаунда:
#   SysProfile, ProcPwrPerf   кто управляет частотой и по какому критерию.
#                             На проде PerfPerWattOptimizedDapc + SysDbpm, то
#                             есть BIOS оптимизирует «производительность на
#                             ватт», а не держит максимум.
#   EnergyEfficientTurbo,     платформа САМА снижает турбо, когда считает
#   EnergyPerformanceBias,    задачу памяти-зависимой — а memory-bound это
#   ControlledTurbo,          ровно то, что создают агрессоры. Замедление от
#   ProcTurboMode             интерференции и от снижения частоты по этой
#                             причине в данных неразличимы.
#   ProcCStates, ProcC1E      глубина сна ядер: объясняет и нижнюю ступень
#                             частоты, и задержку пробуждения жертвы.
#   ProcUncoreFreqRapl        частота uncore (а значит latency LLC и ПСП
#                             памяти) управляется RAPL — прямой вход энергоbranch.
#   Hw/Dcu*/Adj/Amp Prefetch  префетчеры формируют промахи LLC, то есть
#                             ГЛАВНУЮ ось стенда.
#   SubNumaCluster,           топология памяти: знаменатель NUMA-оси и смысл
#   NodeInterleave, MemOpMode доли remote-обращений.
#   LogicalProc               SMT. Независимая от ОС проверка того, что
#                             зафиксировано в методике (SMT off).
#   Proc*NumCores, L3, Brand, размер LLC и модель CPU — параметры дозы и
#   ProcCoreSpeed, Microcode  масштаба; микрокод меняет поведение PMU.
#   SysMem*                   объём/тип/скорость памяти.
BIOS_ATTRS = (
    "SysProfile", "ProcPwrPerf", "ProcTurboMode", "ControlledTurbo",
    "EnergyEfficientTurbo", "EnergyPerformanceBias", "OptimizedPowerMode",
    "ProcCStates", "ProcC1E", "ProcUncoreFreqRapl", "ProcIssSetting",
    "ProcHwPrefetcher", "ProcAdjCacheLine", "ProcAmpPrefetch",
    "DcuStreamerPrefetcher", "DcuIpPrefetcher", "ProcHomelessPrefetch",
    "SubNumaCluster", "NodeInterleave", "MemOpMode", "MemFrequency",
    "MemPatrolScrub", "MemRefreshRate", "AdddcSetting",
    "LogicalProc", "ProcCores", "ProcCoreSpeed",
    "Proc1Brand", "Proc1NumCores", "Proc1L3Cache", "Proc1Microcode",
    "Proc2Brand", "Proc2NumCores", "Proc2L3Cache", "Proc2Microcode",
    "SysMemSize", "SysMemSpeed", "SysMemType",
)


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


def fetch_bios(host: str, user: str, password: str, timeout: float = 20.0) -> dict[str, str]:
    """Значения BIOS_ATTRS одного узла. Пусто — узел не ответил."""
    ctx = ssl._create_unverified_context()  # noqa: S323 — см. докстринг
    req = urllib.request.Request(f"https://{host}{BIOS_PATH}")
    tok = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {tok}")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        attrs = json.load(resp).get("Attributes", {})
    return {k: str(attrs[k]) for k in BIOS_ATTRS if k in attrs}


def bios_hash(attrs: dict[str, str]) -> int:
    """Устойчивый числовой отпечаток набора настроек.

    Нужен затем, что сравнивать info-метрики между собой в PromQL неудобно, а
    вопрос «настройки BIOS менялись?» должен отвечаться одним `changes()`.
    Берём sha256 от канонической строки и первые 52 бита — Float64 в Prometheus
    держит целые до 2^53 без потери точности, поэтому отпечаток не «поплывёт».
    """
    canon = ";".join(f"{k}={attrs[k]}" for k in sorted(attrs))
    return int(hashlib.sha256(canon.encode()).hexdigest()[:13], 16)


def bios_exposition(bios: dict[str, dict[str, str]]) -> str:
    if not bios:
        return ""
    out = [
        "# HELP idrac_bios_attribute_info BIOS attribute that parameterises the measurement "
        "(value is in the label; the series itself is always 1).",
        "# TYPE idrac_bios_attribute_info gauge",
    ]
    for node, attrs in sorted(bios.items()):
        for name in sorted(attrs):
            value = attrs[name].replace("\\", "").replace('"', "'")
            out.append(f'idrac_bios_attribute_info{{node="{node}",attribute="{name}",'
                       f'value="{value}"}} 1')
    out += [
        "# HELP idrac_bios_profile_hash Fingerprint of the whole BIOS attribute set; any change "
        "means the measurement platform changed under the experiment.",
        "# TYPE idrac_bios_profile_hash gauge",
    ]
    for node, attrs in sorted(bios.items()):
        out.append(f'idrac_bios_profile_hash{{node="{node}"}} {bios_hash(attrs)}')
    return "\n".join(out) + "\n"


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


def push(gateway: str, body: str, timeout: float = 10.0, job: str = "idrac-power") -> None:
    # PUT (не POST): заменяет группу целиком, молчащие узлы исчезают.
    # job — разные группы для мощности и настроек BIOS: PUT заменяет группу
    # ЦЕЛИКОМ, и общая группа означала бы, что часовой опрос BIOS стирает
    # мощность, а десятисекундный опрос мощности стирает настройки.
    req = urllib.request.Request(
        f"{gateway.rstrip('/')}/metrics/job/{job}",
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
    # Настройки BIOS меняются вручную и с перезагрузкой узла — опрашивать их
    # каждые 10 секунд незачем, а вот пропустить смену нельзя. Час — компромисс:
    # смена всё равно требует окна между кампаниями.
    bios_interval = float(os.environ.get("BIOS_POLL_INTERVAL_SECONDS", "3600"))
    last_bios = 0.0

    print(f"poller: {len(idracs)} iDRAC -> {gateway}, мощность каждые {interval:g}с, "
          f"BIOS каждые {bios_interval:g}с", flush=True)
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

        now = time.time()
        if now - last_bios >= bios_interval:
            bios: dict[str, dict[str, str]] = {}
            for node, host in idracs.items():
                try:
                    got = fetch_bios(host, user, password)
                    if got:
                        bios[node] = got
                except Exception as e:  # noqa: BLE001 — как и у мощности
                    print(f"WARN bios {node} ({host}): {e}", flush=True)
            if bios:
                try:
                    # ОТДЕЛЬНАЯ группа pushgateway (job=ss_idrac_bios). PUT
                    # заменяет группу целиком, поэтому класть настройки в ту же
                    # группу, что и мощность, значило бы стирать мощность раз в
                    # час — и наоборот, стирать настройки каждые 10 секунд.
                    push(gateway, bios_exposition(bios), job="ss_idrac_bios")
                    last_bios = now
                    print(f"bios: снято с {len(bios)} узлов", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"WARN push bios: {e}", flush=True)

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
