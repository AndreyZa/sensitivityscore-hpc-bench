#!/usr/bin/env python3
"""Экспортёр состояния load-watcher для Prometheus.

Зачем он есть. load-watcher — единственный источник утилизации узлов для трёх
плеч энерговетки (trimaran, peaks, packing; коммит 3ef51ac перевёл trimaran с
собственного metricProvider на него). Если он отдаёт пустоту или протухшие
данные, плечи не падают и не жалуются — они молча ставят всем узлам
одинаковый score, то есть ВЫРОЖДАЮТСЯ в «без различения». Сравнение плеч при
этом продолжает считаться и выглядит правдоподобно. До 19.08.2026 о его
состоянии знала только readinessProbe пода: она бьёт в /watcher и довольна
любым ответом, включая пустой NodeMetricsMap.

Почему экспортёр, а не push в pushgateway (как у iDRAC-поллера). Тот случай
вынужденный: iDRAC-сеть Prometheus'у недоступна. Здесь load-watcher — обычный
ClusterIP-сервис, Prometheus до него дотягивается; ему мешает только формат
(JSON вместо текста экспозиции). Значит, нужен переводчик, а не буфер: у
pushgateway последнее значение живёт вечно, и «экспортёр умер» выглядело бы
как «данные не меняются» — ровно та ошибка, от которой этот экспортёр и
защищает.

Данные тянутся НА КАЖДЫЙ СКРЕЙП, без фонового потока: если load-watcher
завис, скрейп честно провалится по таймауту, и цель станет DOWN. Отдельный
поток превратил бы зависание в «последние известные значения», то есть в ту
же ложь.
"""
import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

WATCHER_URL = os.environ.get(
    "LOADWATCHER_URL", "http://load-watcher.sensitivityscore-system.svc.cluster.local:2020/watcher")
TIMEOUT = float(os.environ.get("LOADWATCHER_TIMEOUT", "5"))
PORT = int(os.environ.get("EXPORTER_PORT", "9095"))


def esc(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def collect():
    """Ответ load-watcher -> строки экспозиции."""
    out = [
        "# HELP ss_loadwatcher_up доступен ли load-watcher и разобран ли его ответ",
        "# TYPE ss_loadwatcher_up gauge",
    ]
    started = time.time()
    try:
        with urllib.request.urlopen(WATCHER_URL, timeout=TIMEOUT) as resp:
            payload = json.load(resp)
    except Exception as exc:                       # сеть, таймаут, битый JSON
        out.append("ss_loadwatcher_up 0")
        out.append("# HELP ss_loadwatcher_error_info последняя ошибка опроса")
        out.append("# TYPE ss_loadwatcher_error_info gauge")
        out.append(f'ss_loadwatcher_error_info{{error="{esc(type(exc).__name__)}"}} 1')
        return "\n".join(out) + "\n"

    out.append("ss_loadwatcher_up 1")

    # Свежесть — по метке времени САМОГО load-watcher, а не по времени скрейпа:
    # он опрашивает metrics-server своим циклом и может застыть, отвечая при
    # этом мгновенно.
    ts = payload.get("timestamp")
    if isinstance(ts, (int, float)):
        out += ["# HELP ss_loadwatcher_timestamp_seconds метка времени последнего окна load-watcher",
                "# TYPE ss_loadwatcher_timestamp_seconds gauge",
                f"ss_loadwatcher_timestamp_seconds {ts}"]

    nodes = ((payload.get("data") or {}).get("NodeMetricsMap") or {})
    out += ["# HELP ss_loadwatcher_nodes сколько узлов есть в выдаче load-watcher",
            "# TYPE ss_loadwatcher_nodes gauge",
            f"ss_loadwatcher_nodes {len(nodes)}"]

    out += ["# HELP ss_loadwatcher_node_value утилизация узла глазами load-watcher (проценты)",
            "# TYPE ss_loadwatcher_node_value gauge"]
    for node, body in sorted(nodes.items()):
        metrics = (body or {}).get("metrics") or []
        for metric in metrics:
            kind = metric.get("type") or "unknown"
            value = metric.get("value")
            if isinstance(value, (int, float)):
                out.append(
                    f'ss_loadwatcher_node_value{{node="{esc(node)}",type="{esc(kind)}"}} {value}')
        # Узел без метрик — это не ноль, это отсутствие данных. Считаем
        # отдельно, чтобы «узел есть в списке» не путали с «по узлу есть чем
        # различать».
        out += [f'ss_loadwatcher_node_metrics{{node="{esc(node)}"}} {len(metrics)}']

    source = payload.get("source") or "unknown"
    out += ["# HELP ss_loadwatcher_source_info откуда load-watcher берёт утилизацию",
            "# TYPE ss_loadwatcher_source_info gauge",
            f'ss_loadwatcher_source_info{{source="{esc(source)}"}} 1']

    out += ["# HELP ss_loadwatcher_poll_duration_seconds сколько занял опрос load-watcher",
            "# TYPE ss_loadwatcher_poll_duration_seconds gauge",
            f"ss_loadwatcher_poll_duration_seconds {time.time() - started:.6f}"]
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/metrics"):
            body = collect().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/healthz"):
            # Живость СЛУЖБЫ, а не load-watcher: иначе kubelet перезапускал бы
            # экспортёр из-за чужой поломки, стирая единственного свидетеля.
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass                                        # скрейп раз в 30 с — лог не нужен


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
