#!/usr/bin/env python3
"""status_server.py — локальный HTTP-эндпойнт прогресса прогонов харнесса.

Одна страница (авто-обновление каждые 10с) + /json для скриптов. Показывает:
  - стенд: кластер (API-сервер, ноды с версиями) — из kubectl, кэшируется;
  - шапку прогресса: % всего прогона, % фазы, ETA (ожидаемый объём считается
    из config.yaml той же логикой, что run_experiment.py);
  - таблицы по results/baselines parquet (строки по плечам и нодам, средний
    makespan) — харнесс пишет инкрементально, это живой прогресс;
  - живые Job'ы жертв и агрессоры (kubectl);
  - секцию «Анализ», когда в --report каталоге появляется summary.md после
    `analyze.py` — рендерит markdown и встраивает все PNG-графики оттуда
    (сервер отдаёт их по /report/<файл>);
  - хвост лога и последние ошибки.

Запуск (с хоста, рядом с идущим прогоном):
    .venv/bin/python status_server.py \
        --log /path/to/run.log --config config-stage.yaml \
        --results results/results-stage.parquet \
        --baselines results/baselines-stage.parquet \
        --report ../analysis/report-stage --stand STAGE
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

_STAND_CACHE: dict = {"ts": 0.0, "data": {}}
STAND_TTL_SECONDS = 300  # топология кластера меняется редко


def esc(s) -> str:
    return html.escape(str(s))


def tail_lines(path: Path, n: int = 12) -> list[str]:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return [f"(лог {path} недоступен)"]


# ---------------------------------------------------------------- стенд ----


def stand_info() -> dict:
    """Кластер, на который смотрит kubectl этого процесса (KUBECONFIG env):
    API-сервер + ноды. Кэш на STAND_TTL_SECONDS — не дёргать API каждые 10с."""
    now = time.time()
    if now - _STAND_CACHE["ts"] < STAND_TTL_SECONDS and _STAND_CACHE["data"]:
        return _STAND_CACHE["data"]
    data: dict = {"label": ARGS.stand or ""}
    try:
        r = subprocess.run(
            ["kubectl", "config", "view", "--minify",
             "-o", "jsonpath={.clusters[0].cluster.server}"],
            capture_output=True, text=True, timeout=6,
        )
        data["server"] = r.stdout.strip() or "(kubectl недоступен)"
        r = subprocess.run(
            ["kubectl", "get", "nodes", "--no-headers", "-o",
             "custom-columns=N:.metadata.name,V:.status.nodeInfo.kubeletVersion,"
             "K:.status.nodeInfo.kernelVersion,CPU:.status.allocatable.cpu,"
             "MEM:.status.allocatable.memory"],
            capture_output=True, text=True, timeout=6,
        )
        data["nodes"] = [l.split() for l in r.stdout.strip().splitlines()] if r.returncode == 0 else []
    except Exception as e:  # noqa: BLE001 — страница статуса не должна падать
        data.setdefault("server", f"({e})")
        data.setdefault("nodes", [])
    _STAND_CACHE.update(ts=now, data=data)
    return data


def worker_node_count() -> int:
    """Число worker-нод (без control-plane) для расчёта ожидаемых per-node
    бейзлайнов. Через кэш stand_info; managed control-plane (STAGE) в списке
    нод и так не отображается, on-prem кластеру цифра чуть завысит ожидание —
    прогресс тогда консервативен, не сломан."""
    return len(stand_info().get("nodes", [])) or 1


# ------------------------------------------------------------- прогресс ----


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
    # per-node бейзлайны (дефолт run_experiment): множитель = число worker-нод.
    if cfg.get("baseline", {}).get("per_node", True):
        baseline_exp *= max(worker_node_count(), 1)

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


# --------------------------------------------------------------- данные ----


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
            if "node" in df:
                out["by_node"] = (
                    df.groupby(["config", "node"]).size().unstack(fill_value=0)
                    .to_dict("index")
                )
            if "makespan_s" in df:
                out["makespan_mean_by_config"] = (
                    df.groupby("config")["makespan_s"].mean().round(1).to_dict()
                )
            errors = df["approximation"].astype(str).str.startswith("error:")
            out["error_rows"] = int(errors.sum())
        return out
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": str(e)}


def kubectl_snapshot() -> dict:
    """Живые Job'ы и агрессоры — best-effort, пустые списки если kubectl нет."""
    out: dict = {}
    for name, cmd in {
        "jobs": ["kubectl", "get", "jobs", "-n", "sensitivityscore-bench",
                 "--no-headers", "-o",
                 "custom-columns=N:.metadata.name,ACTIVE:.status.active"],
        "aggressors": ["kubectl", "get", "pods", "-n", "sensitivityscore-bench",
                       "-l", "app=ss-aggressor", "--no-headers", "-o",
                       "custom-columns=N:.metadata.name,NODE:.spec.nodeName,P:.status.phase"],
    }.items():
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            out[name] = (
                [l.split() for l in r.stdout.strip().splitlines()]
                if r.returncode == 0
                else [[r.stderr.strip()[:120]]]
            )
        except Exception as e:  # noqa: BLE001
            out[name] = [[f"({e})"]]
    return out


def collect() -> dict:
    log_path = Path(ARGS.log)
    all_lines = tail_lines(log_path, 4000)
    errors = [l for l in all_lines if re.search(r"ERROR|Traceback|failed", l)][-5:]
    phase, starts = run_phase(all_lines)
    results = parquet_summary(Path(ARGS.results))
    baselines = parquet_summary(Path(ARGS.baselines))
    exp = expected_rows(Path(ARGS.config))
    report_dir = Path(ARGS.report)
    return {
        "time": time.strftime("%H:%M:%S"),
        "stand": stand_info(),
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
        "report": {
            "exists": (report_dir / "summary.md").exists(),
            "dir": str(report_dir),
            "plots": sorted(p.name for p in report_dir.glob("*.png"))
            if report_dir.is_dir()
            else [],
        },
    }


# ---------------------------------------------------------------- рендер ----


def table(headers: list[str], rows: list[list], caption: str = "") -> str:
    if not rows:
        return "<p class='dim'>— пусто —</p>"
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    trs = "".join(
        "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return f"<table>{cap}<tr>{th}</tr>{trs}</table>"


def parquet_section(d: dict) -> str:
    if not d.get("exists"):
        return "<p class='dim'>файла ещё нет</p>"
    if "error" in d:
        return f"<p class='err'>{esc(d['error'])}</p>"
    head = (
        f"<p><b>{d['rows']}</b> строк · обновлён {esc(d.get('mtime','?'))}"
        + (f" · <span class='err'>{d['error_rows']} error-строк</span>"
           if d.get("error_rows") else "")
        + "</p>"
    )
    parts = [head]
    by_node = d.get("by_node") or {}
    if by_node:
        nodes = sorted({n for row in by_node.values() for n in row})
        mk = d.get("makespan_mean_by_config", {})
        rows = [
            [cfg] + [by_node[cfg].get(n, 0) for n in nodes] + [mk.get(cfg, "")]
            for cfg in sorted(by_node)
        ]
        parts.append(table(
            ["плечо"] + [n.replace("worker-", "w-") for n in nodes] + ["makespan, с (ср.)"],
            rows, "строки по плечам × нодам",
        ))
    elif d.get("by_config"):
        parts.append(table(
            ["плечо", "строк"], [[k, v] for k, v in sorted(d["by_config"].items())]
        ))
    return "".join(parts)


def md_to_html(md: str) -> str:
    """Мини-рендер markdown для summary.md: заголовки, таблицы, списки,
    **bold**, `code`. Не общий парсер — ровно то, что генерирует analyze.py."""
    out: list[str] = []
    in_table = False
    in_list = False
    for line in md.splitlines():
        s = line.rstrip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue  # разделительная строка таблицы
            tag = "th" if not in_table else "td"
            if not in_table:
                out.append("<table>")
                in_table = True
            out.append(
                "<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>"
            )
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if s.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(s[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            lvl = min(len(m.group(1)) + 1, 5)  # h1 занят шапкой страницы
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
        elif s:
            out.append(f"<p>{inline(s)}</p>")
    if in_table:
        out.append("</table>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def inline(s: str) -> str:
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def report_section(rep: dict) -> str:
    if not rep["exists"]:
        return ("<p class='dim'>появится после прогона: "
                f"<code>analyze.py ... --outdir {esc(rep['dir'])}</code></p>")
    parts = []
    try:
        md = (Path(rep["dir"]) / "summary.md").read_text(encoding="utf-8")
        parts.append(md_to_html(md))
    except OSError as e:
        parts.append(f"<p class='err'>{esc(e)}</p>")
    for png in rep["plots"]:
        parts.append(
            f"<figure><img src='/report/{esc(png)}' alt='{esc(png)}'>"
            f"<figcaption>{esc(png)}</figcaption></figure>"
        )
    return "".join(parts)


def render_html(d: dict) -> str:
    phase = d["phase"]
    color = {"DONE": "#2e7d32", "pressure": "#e65100", "baseline": "#1565c0"}.get(
        phase, "#616161"
    )
    prog = d.get("progress", {})
    pct = prog.get("overall_pct")
    bar = ""
    if pct is not None:
        eta = (
            f" · ETA ~{prog['eta']} (осталось ~{prog['eta_minutes']} мин)"
            if "eta" in prog
            else ""
        )
        phase_pct = f" (фаза {phase}: {prog['phase_pct']}%)" if "phase_pct" in prog else ""
        bar = f"""<div class="prog">
<div class="progtext"><b>{pct}%</b> всего прогона{phase_pct}{eta}</div>
<div class="barbg"><div class="bar" style="background:{color};width:{pct}%"></div></div></div>"""

    st = d["stand"]
    stand_label = f"<b>{esc(st['label'])}</b> · " if st.get("label") else ""
    stand_html = (
        f"<p class='stand'>{stand_label}{esc(st.get('server',''))}</p>"
        + table(["нода", "kubelet", "ядро", "cpu", "mem"], st.get("nodes", []))
    )

    cl = d["cluster"]
    cluster_html = (
        "<h3>Job'ы жертв</h3>" + table(["job", "active"], cl.get("jobs", []))
        + "<h3>Агрессоры</h3>" + table(["под", "нода", "фаза"], cl.get("aggressors", []))
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>harness · {esc(st.get('label') or 'status')}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2em auto;max-width:72em;padding:0 1em;color:#222}}
h1 small{{color:#999;font-weight:normal;font-size:.55em}}
h2{{border-bottom:2px solid #eee;padding-bottom:.2em;margin-top:1.6em}}
table{{border-collapse:collapse;margin:.5em 0;font-size:.92em}}
caption{{text-align:left;color:#888;font-size:.85em;padding-bottom:.2em}}
th,td{{border:1px solid #ddd;padding:.35em .7em;text-align:left}}
th{{background:#f7f7f7}}
tr:nth-child(even) td{{background:#fafafa}}
pre{{background:#f5f5f5;padding:.7em;overflow-x:auto;font-size:.85em;border-radius:6px}}
code{{background:#f0f0f0;padding:.1em .3em;border-radius:3px}}
.dim{{color:#999}} .err{{color:#c62828}}
.stand{{color:#555;margin:.2em 0}}
.prog{{margin:.6em 0 1.2em}} .progtext{{font-size:1.15em;margin-bottom:.3em}}
.barbg{{background:#e0e0e0;height:14px;border-radius:7px;max-width:44em}}
.bar{{height:14px;border-radius:7px;transition:width .5s}}
figure{{margin:1em 0;border:1px solid #eee;border-radius:6px;padding:.5em;display:inline-block}}
figure img{{max-width:100%;height:auto}}
figcaption{{color:#888;font-size:.8em;text-align:center}}
</style></head><body>
<h1>Прогон харнесса — <span style="color:{color}">{phase}</span>
<small>обновлено {d['time']} · автообновление 10с · <a href="/json">/json</a></small></h1>
{stand_html}
{bar}
<h2>Результаты прогона <span class='dim'>({esc(str(ARGS.results))})</span></h2>
{parquet_section(d['results'])}
<h2>Бейзлайны <span class='dim'>({esc(str(ARGS.baselines))})</span></h2>
{parquet_section(d['baselines'])}
<h2>Кластер сейчас</h2>
{cluster_html}
<h2>Анализ</h2>
{report_section(d['report'])}
<h2>Хвост лога</h2>
<pre>{esc(chr(10).join(d['log_tail']))}</pre>
<h2>Ошибки в логе (последние)</h2>
<pre>{esc(chr(10).join(d['log_errors']) or '—')}</pre>
</body></html>"""


# ---------------------------------------------------------------- сервер ----


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
        self.send_response(code)
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
    p.add_argument(
        "--report",
        default="../analysis/report",
        help="каталог с выходом analyze.py — когда там появляется summary.md, "
        "страница показывает секцию «Анализ» с графиками",
    )
    p.add_argument("--stand", default="", help="человекочитаемое имя стенда для шапки")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--bind", default="127.0.0.1")
    ARGS = p.parse_args()
    srv = ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler)
    print(f"status: http://{ARGS.bind}:{ARGS.port}  (JSON: /json)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
