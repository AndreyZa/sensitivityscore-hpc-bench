"""Сборка полного снимка состояния (collect) + HTTP-сервер."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .cluster import kubectl_snapshot, stand_info
from .data import analysis_digest, baseline_summary, pressure_results
from .labels import profile_scenario_map
from .progress import current_activity, expected_rows, progress, run_phase, run_plan
from .render import render_html

ARGS: argparse.Namespace  # заполняется в main()

# Пути по умолчанию — от корня репозитория, чтобы сервер можно было
# запускать из любого каталога.
ROOT = Path(__file__).resolve().parents[1]


def tail_lines(path: Path, n: int = 12) -> list[str]:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return [f"(лог {path} недоступен)"]


def load_cfg(path: Path) -> dict:
    try:
        # Через загрузчик харнесса: конфиг серии — слой поверх родителя
        # (extends), и плоский safe_load показал бы страницу без унаследованных
        # значений, то есть тихо соврал бы.
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
        from config_loader import load_config

        return load_config(path) or {}
    except Exception:  # noqa: BLE001 — страница не должна падать из-за конфига
        return {}


def collect() -> dict:
    log_path = Path(ARGS.log)
    all_lines = tail_lines(log_path, 4000)
    errors = [l for l in all_lines if re.search(r"ERROR|Traceback|failed", l)][-5:]
    phase, starts, ends = run_phase(all_lines)
    # Прогон-добор (--scope baseline): маркер BASELINE DONE завершает прогон —
    # основной серии в нём не будет, иначе страница вечно висела бы в фазе
    # «эталонные прогоны 100%».
    if ARGS.scope == "baseline" and phase == "baseline" and "baseline" in ends:
        phase = "DONE"
    cfg = load_cfg(Path(ARGS.config))
    results = pressure_results(Path(ARGS.results), cfg)
    baselines = baseline_summary(Path(ARGS.baselines))
    exp = expected_rows(cfg)
    report_dir = Path(ARGS.report)
    report_info = {
        "exists": (report_dir / "summary.md").exists(),
        "dir": str(report_dir),
        "plots": sorted(p.name for p in report_dir.glob("*.png"))
        if report_dir.is_dir()
        else [],
        "digest": analysis_digest(report_dir),
    }
    return {
        "time": time.strftime("%H:%M:%S"),
        "stand": stand_info(ARGS.stand),
        "phase": phase,
        "activity": current_activity(all_lines, profile_scenario_map(cfg)),
        "progress": progress(
            phase, starts, ends, baselines.get("rows", 0), results.get("rows", 0),
            exp, ARGS.scope,
        ),
        "expected_rows": exp,
        "reps": {
            "baseline": cfg.get("baseline", {}).get("repetitions"),
            "pressure": {
                sc["name"]: sc.get("repetitions", cfg.get("repetitions"))
                for sc in cfg.get("pressure_scenarios", [])
            },
        },
        "log_tail": tail_lines(log_path, 10),
        "log_errors": errors,
        "results": results,
        "baselines": baselines,
        "cluster": kubectl_snapshot(),
        "report": report_info,
        "plan": run_plan(cfg, phase, baselines, results, report_info),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — API http.server
        if self.path.startswith("/report/"):
            return self._serve_report_file()
        d = collect()
        if self.path.startswith("/json"):
            body = json.dumps(d, ensure_ascii=False, indent=1).encode()
            ctype = "application/json; charset=utf-8"
        else:
            body = render_html(d).encode()
            ctype = "text/html; charset=utf-8"
        self._respond(200, ctype, body)

    def _serve_report_file(self):
        """Отдаёт файлы ТОЛЬКО из --report каталога, только известные типы —
        без directory traversal (resolve + проверка родителя)."""
        name = os.path.basename(self.path[len("/report/"):])
        suffix_types = {".png": "image/png", ".csv": "text/csv",
                        ".md": "text/plain; charset=utf-8"}
        ctype = suffix_types.get(Path(name).suffix.lower())
        report_dir = Path(ARGS.report).resolve()
        target = (report_dir / name).resolve()
        if not ctype or target.parent != report_dir or not target.is_file():
            return self._respond(404, "text/plain", b"not found")
        self._respond(200, ctype, target.read_bytes())

    def _respond(self, code: int, ctype: str, body: bytes):
        # Браузер может оборвать соединение посреди ответа (закрытая вкладка,
        # авто-обновление) — это не повод сыпать traceback'и в консоль.
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):  # тихий сервер — не спамить в консоль на каждый GET
        pass


def main():
    global ARGS
    p = argparse.ArgumentParser(
        description="локальная HTTP-страница прогресса прогонов харнесса"
    )
    p.add_argument("--log", default=str(ROOT / "full_run.log"))
    p.add_argument("--results", default=str(ROOT / "harness/results/results.parquet"))
    p.add_argument("--baselines", default=str(ROOT / "harness/results/baselines.parquet"))
    p.add_argument(
        "--config",
        default=str(ROOT / "harness/config.yaml"),
        help="config.yaml харнесса — из него считается ожидаемое число строк "
        "(для процента готовности и ETA в шапке) и перегружаемые узлы сценариев",
    )
    p.add_argument(
        "--report",
        default=str(ROOT / "analysis/report"),
        help="каталог с выходом analyze.py — когда там появляется summary.md, "
        "страница показывает секцию «Анализ» с графиками",
    )
    p.add_argument(
        "--scope",
        choices=["full", "baseline"],
        default="full",
        help="baseline — прогон только эталонный (добор): общий процент "
        "не включает основную серию, BASELINE DONE завершает прогон",
    )
    p.add_argument("--stand", default="", help="человекочитаемое имя стенда для шапки")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--bind", default="127.0.0.1")
    ARGS = p.parse_args()
    srv = ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler)
    print(f"status: http://{ARGS.bind}:{ARGS.port}  (JSON: /json)")
    srv.serve_forever()
