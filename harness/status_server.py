#!/usr/bin/env python3
"""status_server.py — локальный HTTP-эндпойнт прогресса прогонов харнесса.

Одна страница (авто-обновление каждые 10с) + /json для скриптов. Читает:
  - лог прогона (STATUS_LOG) — текущая фаза (baseline/pressure), последние
    события, ошибки;
  - results/baselines parquet — сколько строк уже записано, по плечам и нодам
    (харнесс пишет инкрементально после каждого job — это и есть прогресс);
  - kubectl (если доступен KUBECONFIG) — живые Job'ы жертв и агрессоры.

Запуск (с хоста, рядом с идущим прогоном):
    .venv/bin/python status_server.py \
        --log /path/to/stage_run.log \
        --results results/results-stage.parquet \
        --baselines results/baselines-stage.parquet \
        --port 8787
Затем открыть http://localhost:8787 (из WSL2 виден и в Windows-браузере).

Никакой записи, только чтение — безопасно держать запущенным всегда.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ARGS: argparse.Namespace  # заполняется в main()


def tail_lines(path: Path, n: int = 12) -> list[str]:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return [f"(лог {path} недоступен)"]


def run_phase(log_lines_all: list[str]) -> str:
    phase = "not started"
    for l in log_lines_all:
        if "BASELINE START" in l:
            phase = "baseline"
        elif "PRESSURE START" in l:
            phase = "pressure"
        elif "ALL DONE" in l or "PRESSURE DONE" in l:
            phase = "DONE"
    return phase


def parquet_summary(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    try:
        import pandas as pd

        df = pd.read_parquet(path)
        out = {
            "exists": True,
            "rows": len(df),
            "mtime": time.strftime("%H:%M:%S", time.localtime(path.stat().st_mtime)),
        }
        if "config" in df and len(df):
            out["by_config"] = df["config"].value_counts().to_dict()
            out["by_node"] = (
                df.groupby(["config", "node"]).size().unstack(fill_value=0).to_dict("index")
                if "node" in df
                else {}
            )
            if "makespan_s" in df:
                out["makespan_mean_by_config"] = (
                    df.groupby("config")["makespan_s"].mean().round(1).to_dict()
                )
            errors = df["approximation"].astype(str).str.startswith("error:")
            out["error_rows"] = int(errors.sum())
        return out
    except Exception as e:  # noqa: BLE001 — страница статуса не должна падать
        return {"exists": True, "error": str(e)}


def kubectl_snapshot() -> dict:
    """Живые Job'ы и агрессоры — best-effort, пустой dict если kubectl/доступ нет."""
    out: dict = {}
    env = os.environ.copy()
    for name, cmd in {
        "jobs": ["kubectl", "get", "jobs", "-n", "sensitivityscore-bench",
                 "--no-headers", "-o", "custom-columns=N:.metadata.name,S:.status.active"],
        "aggressors": ["kubectl", "get", "pods", "-n", "sensitivityscore-bench",
                       "-l", "app=ss-aggressor", "--no-headers",
                       "-o", "custom-columns=N:.metadata.name,NODE:.spec.nodeName,P:.status.phase"],
    }.items():
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=6, env=env)
            out[name] = r.stdout.strip().splitlines() if r.returncode == 0 else [r.stderr.strip()[:200]]
        except Exception as e:  # noqa: BLE001
            out[name] = [f"({e})"]
    return out


def collect() -> dict:
    log_path = Path(ARGS.log)
    all_lines = tail_lines(log_path, 4000)
    errors = [l for l in all_lines if re.search(r"ERROR|Traceback|failed", l)][-5:]
    return {
        "time": time.strftime("%H:%M:%S"),
        "phase": run_phase(all_lines),
        "log_tail": tail_lines(log_path, 10),
        "log_errors": errors,
        "results": parquet_summary(Path(ARGS.results)),
        "baselines": parquet_summary(Path(ARGS.baselines)),
        "cluster": kubectl_snapshot(),
    }


def render_html(d: dict) -> str:
    def pre(obj) -> str:
        return "<pre>" + html.escape(
            obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
        ) + "</pre>"

    phase = d["phase"]
    color = {"DONE": "#2e7d32", "pressure": "#e65100", "baseline": "#1565c0"}.get(phase, "#616161")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>harness status</title>
<style>body{{font-family:monospace;margin:2em;max-width:70em}}
pre{{background:#f5f5f5;padding:.7em;overflow-x:auto}}
h2{{border-bottom:1px solid #ccc}}</style></head><body>
<h1>Прогон харнесса — <span style="color:{color}">{phase}</span>
<small style="color:#999">(обновлено {d['time']}, автообновление 10с)</small></h1>
<h2>results ({html.escape(str(ARGS.results))})</h2>{pre(d['results'])}
<h2>baselines ({html.escape(str(ARGS.baselines))})</h2>{pre(d['baselines'])}
<h2>кластер (живые Job'ы / агрессоры)</h2>{pre(d['cluster'])}
<h2>хвост лога</h2>{pre(chr(10).join(d['log_tail']))}
<h2>ошибки в логе (последние)</h2>{pre(chr(10).join(d['log_errors']) or '—')}
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — API http.server
        d = collect()
        if self.path.startswith("/json"):
            body = json.dumps(d, ensure_ascii=False, indent=1).encode()
            ctype = "application/json; charset=utf-8"
        else:
            body = render_html(d).encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # тихий сервер — не спамить в консоль на каждый GET
        pass


def main():
    global ARGS
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log", default="../full_run.log")
    p.add_argument("--results", default="results/results.parquet")
    p.add_argument("--baselines", default="results/baselines.parquet")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--bind", default="127.0.0.1")
    ARGS = p.parse_args()
    srv = ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler)
    print(f"status: http://{ARGS.bind}:{ARGS.port}  (JSON: /json)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
