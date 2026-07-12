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


def run_phase(log_lines_all: list[str]) -> tuple[str, dict[str, float]]:
    """-> (фаза, {фаза: unix-время старта}) по маркерам '=== X START HH:MM:SS ==='."""
    phase = "not started"
    starts: dict[str, float] = {}
    today = time.strftime("%Y-%m-%d")
    for l in log_lines_all:
        m = re.search(r"=== (BASELINE|PRESSURE) START (\d\d:\d\d:\d\d)", l)
        if m:
            phase = m.group(1).lower()
            try:
                starts[phase] = time.mktime(
                    time.strptime(f"{today} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
                )
            except ValueError:
                pass
        elif "ALL DONE" in l or "PRESSURE DONE" in l:
            phase = "DONE"
    return phase, starts


def expected_rows(config_path: Path) -> dict[str, int]:
    """Ожидаемое число строк по фазам из config.yaml харнесса — чтобы считать
    процент готовности. Логика зеркалит run_experiment.py (baseline_profiles /
    expand_configs / run_pressure_scenario)."""
    try:
        import yaml

        cfg = yaml.safe_load(config_path.read_text())
    except Exception:  # noqa: BLE001
        return {}

    profiles = list(cfg.get("profiles", []))
    for sc in cfg.get("pressure_scenarios", []):
        v = sc.get("victim_profile", "high-s")
        if v not in profiles:
            profiles.append(v)
    baseline_exp = len(profiles) * cfg.get("baseline", {}).get("repetitions", 5)

    variants = cfg.get("scheduler_variants", ["default", "sensitivityscore"])
    arms = 0
    for c in cfg.get("configs", []):
        arms += len(variants) if c in ("A", "B") else 1

    pressure_exp = 0
    for sc in cfg.get("pressure_scenarios", []):
        pressure_exp += (
            arms
            * len(sc.get("aggressors_per_node", [1]))
            * sc.get("repetitions", cfg.get("repetitions", 10))
            * sc.get("victim_count", 6)
        )
    return {"baseline": baseline_exp, "pressure": pressure_exp}


def progress(phase: str, starts: dict, b_rows: int, p_rows: int, exp: dict) -> dict:
    """Процент (текущей фазы и всего прогона) + ETA по скорости текущей фазы."""
    out: dict = {}
    b_exp, p_exp = exp.get("baseline", 0), exp.get("pressure", 0)
    total_exp = b_exp + p_exp
    if not total_exp:
        return out
    done_overall = min(b_rows, b_exp) + min(p_rows, p_exp)
    if phase == "pressure":  # baseline к этому моменту завершён
        done_overall = b_exp + min(p_rows, p_exp)
    out["overall_pct"] = round(100 * done_overall / total_exp)

    cur_done, cur_exp = (b_rows, b_exp) if phase == "baseline" else (p_rows, p_exp)
    if phase in ("baseline", "pressure") and cur_exp:
        out["phase_pct"] = round(100 * min(cur_done, cur_exp) / cur_exp)
        start = starts.get(phase)
        if start and cur_done > 0:
            elapsed = time.time() - start
            rate = cur_done / elapsed  # строк/сек в текущей фазе
            remaining_cur = max(cur_exp - cur_done, 0) / rate
            # После baseline остаётся pressure — грубо тем же темпом на строку
            # (честнее занизить, чем молчать; pressure-строки обычно дольше).
            remaining = remaining_cur + (
                (p_exp / rate) if phase == "baseline" and p_exp else 0
            )
            out["eta"] = time.strftime("%H:%M", time.localtime(time.time() + remaining))
            out["eta_minutes"] = round(remaining / 60)
    elif phase == "DONE":
        out["overall_pct"] = 100
        out["phase_pct"] = 100
    return out


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
    phase, starts = run_phase(all_lines)
    results = parquet_summary(Path(ARGS.results))
    baselines = parquet_summary(Path(ARGS.baselines))
    exp = expected_rows(Path(ARGS.config))
    return {
        "time": time.strftime("%H:%M:%S"),
        "phase": phase,
        "progress": progress(
            phase, starts, baselines.get("rows", 0), results.get("rows", 0), exp
        ),
        "expected_rows": exp,
        "log_tail": tail_lines(log_path, 10),
        "log_errors": errors,
        "results": results,
        "baselines": baselines,
        "cluster": kubectl_snapshot(),
    }


def render_html(d: dict) -> str:
    def pre(obj) -> str:
        return "<pre>" + html.escape(
            obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
        ) + "</pre>"

    phase = d["phase"]
    color = {"DONE": "#2e7d32", "pressure": "#e65100", "baseline": "#1565c0"}.get(phase, "#616161")
    prog = d.get("progress", {})
    pct = prog.get("overall_pct")
    bar = ""
    if pct is not None:
        eta = (
            f" · ETA ~{prog['eta']} (осталось ~{prog['eta_minutes']} мин)"
            if "eta" in prog
            else ""
        )
        phase_pct = (
            f" (фаза {phase}: {prog['phase_pct']}%)" if "phase_pct" in prog else ""
        )
        bar = f"""<div style="margin:.6em 0 1.2em">
<div style="font-size:1.15em;margin-bottom:.3em"><b>{pct}%</b> всего прогона{phase_pct}{eta}</div>
<div style="background:#e0e0e0;height:14px;border-radius:7px;max-width:40em">
<div style="background:{color};width:{pct}%;height:14px;border-radius:7px"></div></div></div>"""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>harness status</title>
<style>body{{font-family:monospace;margin:2em;max-width:70em}}
pre{{background:#f5f5f5;padding:.7em;overflow-x:auto}}
h2{{border-bottom:1px solid #ccc}}</style></head><body>
<h1>Прогон харнесса — <span style="color:{color}">{phase}</span>
<small style="color:#999">(обновлено {d['time']}, автообновление 10с)</small></h1>
{bar}
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
    p.add_argument(
        "--config",
        default="config.yaml",
        help="config.yaml харнесса — из него считается ожидаемое число строк "
        "(для процента готовности и ETA в шапке)",
    )
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--bind", default="127.0.0.1")
    ARGS = p.parse_args()
    srv = ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler)
    print(f"status: http://{ARGS.bind}:{ARGS.port}  (JSON: /json)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
