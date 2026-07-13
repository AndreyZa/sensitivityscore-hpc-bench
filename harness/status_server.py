#!/usr/bin/env python3
"""status_server.py — локальный HTTP-эндпойнт прогресса прогонов харнесса.

Одна страница (авто-обновление 10с) + /json для скриптов. Терминология на
странице рассчитана на читателя со стороны (научный руководитель), а не на
жаргон проекта: «планировщик» вместо «плечо», «повторение» вместо «реп»,
«эталонные прогоны» вместо «бейзлайны», «перегруженный узел» вместо «шторм».
Секции:
  - шапка «сейчас»: этап, сценарий, планировщик, номер повторения; крупный
    прогресс-бар всего прогона + ожидаемое время завершения; краткий план
    эксперимента с объёмами каждого этапа;
  - ключевая таблица: по каждому сценарию, по каждому планировщику — какая
    доля задач размещена на перегруженный узел (прямой показатель качества
    решений планировщика), среднее время выполнения и ошибка размещения;
  - текущее состояние кластера: генераторы фоновой нагрузки и выполняющиеся
    задачи;
  - эталонные прогоны матрицей профиль × узел (видна неоднородность узлов);
  - секция «Анализ», когда в --report появляется summary.md после analyze.py
    (рендерит markdown + PNG-графики, сервер отдаёт их по /report/<файл>);
  - хвост лога и последние ошибки.

Запуск (с хоста, рядом с идущим прогоном):
    .venv/bin/python status_server.py \
        --log stage-pressure.log --config config-stage.yaml \
        --results results/results-stage.parquet \
        --baselines results/baselines-stage.parquet \
        --report ../analysis/report-stage --stand "STAGE (Timeweb k0s)"
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

# Порядок и подписи сравниваемых планировщиков. Внутренние имена конфигураций
# ("A-default") на страницу не выводятся — читателю важен планировщик, а не
# код инфраструктурной конфигурации.
ARM_ORDER = ["A-default", "A-sensitivityscore", "A-trimaran"]
ARM_LABEL = {
    "A-default": "default",
    "A-sensitivityscore": "SensitivityScore",
    "A-trimaran": "trimaran",
}
# Исследуемый планировщик (его строка в таблицах подсвечивается).
HERO_ARM = "A-sensitivityscore"

SCENARIO_LABEL = {
    "pressure:io": "Диск (IO): фоновая дисковая нагрузка",
    "pressure:net": "Сеть (Net): фоновая сетевая нагрузка",
    "pressure:llc": "Кэш (LLC): фоновая нагрузка на кэш и память",
}
# Профиль задачи -> сценарий, к которому она относится (для шапки «сейчас»).
PROFILE_SCENARIO = {
    "high-s-io": "pressure:io",
    "high-s-net": "pressure:net",
    "high-s": "pressure:llc",
}


def esc(s) -> str:
    return html.escape(str(s))


def arm_label(cfg_name: str) -> str:
    return ARM_LABEL.get(cfg_name, str(cfg_name).replace("A-", ""))


def scenario_label(s: str) -> str:
    return SCENARIO_LABEL.get(s, str(s).replace("pressure:", ""))


def tail_lines(path: Path, n: int = 12) -> list[str]:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return [f"(лог {path} недоступен)"]


def load_cfg() -> dict:
    try:
        import yaml

        return yaml.safe_load(Path(ARGS.config).read_text()) or {}
    except Exception:  # noqa: BLE001 — страница не должна падать из-за конфига
        return {}


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


def worker_node_count(cfg: dict | None = None) -> int:
    """Число worker-узлов, участвующих в прогоне, для расчёта ожидаемых
    per-node эталонных прогонов: все узлы кластера минус exclude_nodes
    конфига (исключённые узлы харнесс обходит и в эталонах, и в матрице).
    Через кэш stand_info; managed control-plane (STAGE) в списке узлов и так
    не отображается."""
    names = {row[0] for row in stand_info().get("nodes", []) if row}
    excluded = set((cfg or {}).get("exclude_nodes", []))
    return len(names - excluded) or 1


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
                ts = time.mktime(
                    time.strptime(f"{today} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
                )
                # В маркере только время суток, без даты. Если момент вышел
                # в будущем — фаза стартовала до полуночи: минус сутки, иначе
                # elapsed < 0 и ETA уезжает в прошлое.
                if ts > time.time() + 60:
                    ts -= 86400.0
                starts[phase] = ts
            except ValueError:
                pass
        elif "ALL DONE" in l or "PRESSURE DONE" in l:
            phase = "DONE"
    return phase, starts


def current_activity(log_lines_all: list[str]) -> dict:
    """Что харнесс делает прямо сейчас — из последней строки 'submit: job_id=...'
    (в ней явные config/profile/rep, а из job_id хвоста -vN — номер жертвы)."""
    act: dict = {}
    for l in log_lines_all:
        m = re.search(
            r"submit: job_id=(\S+) config=(\S+) profile=(\S+) overcommit=\S+ rep=(\d+)",
            l,
        )
        if m:
            job_id, arm, profile, rep = m.groups()
            act = {"job_id": job_id, "arm": arm, "profile": profile, "rep": int(rep)}
    if act:
        v = re.search(r"-v(\d+)$", act["job_id"])
        act["victim"] = int(v.group(1)) if v else None
        act["scenario"] = PROFILE_SCENARIO.get(act["profile"])
    return act


def expected_by_scenario(cfg: dict) -> dict[str, int]:
    """Ожидаемое число pressure-строк по каждому сценарию (arms × интенсивности
    × повторы × жертвы) — для per-сценарного прогресса."""
    variants = cfg.get("scheduler_variants", ["default", "sensitivityscore"])
    arms = sum(len(variants) if c in ("A", "B") else 1 for c in cfg.get("configs", []))
    out = {}
    for sc in cfg.get("pressure_scenarios", []):
        out[f"pressure:{sc['name']}"] = (
            arms
            * len(sc.get("aggressors_per_node", [1]))
            * sc.get("repetitions", cfg.get("repetitions", 10))
            * sc.get("victim_count", 6)
        )
    return out


def expected_rows(cfg: dict) -> dict[str, int]:
    """Ожидаемое число строк по фазам — зеркалит run_experiment.py."""
    profiles = list(cfg.get("profiles", []))
    for sc in cfg.get("pressure_scenarios", []):
        v = sc.get("victim_profile", "high-s")
        if v not in profiles:
            profiles.append(v)
    baseline_exp = len(profiles) * cfg.get("baseline", {}).get("repetitions", 5)
    if cfg.get("baseline", {}).get("per_node", True):
        baseline_exp *= max(worker_node_count(cfg), 1)
    pressure_exp = sum(expected_by_scenario(cfg).values())
    return {"baseline": baseline_exp, "pressure": pressure_exp}


def progress(phase: str, starts: dict, b_rows: int, p_rows: int, exp: dict) -> dict:
    """Процент (текущей фазы и всего прогона) + ETA по скорости текущей фазы."""
    out: dict = {}
    b_exp, p_exp = exp.get("baseline", 0), exp.get("pressure", 0)
    total_exp = b_exp + p_exp
    if not total_exp:
        return out
    if phase == "baseline":
        # results-файл в этот момент может содержать только СТАРУЮ серию
        # (харнесс перепишет его с нуля на первом же pressure-плече) — не
        # засчитывать чужие строки как прогресс будущей фазы.
        done_overall = min(b_rows, b_exp)
    elif phase == "pressure":  # baseline к этому моменту завершён
        done_overall = b_exp + min(p_rows, p_exp)
    else:
        done_overall = min(b_rows, b_exp) + min(p_rows, p_exp)
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


def storm_nodes_by_scenario(cfg: dict) -> dict[str, set]:
    """scenario-колонка (pressure:<name>) -> множество штормимых нод из конфига."""
    out = {}
    for sc in cfg.get("pressure_scenarios", []):
        out[f"pressure:{sc['name']}"] = set(sc.get("aggressor_nodes") or [])
    return out


def pressure_results(path: Path, cfg: dict) -> dict:
    """Money-метрика по results.parquet: на каждый сценарий и плечо — жертв,
    сколько село в шторм, средний makespan и regret. Это прямой ответ «уводит
    ли планировщик жертв от шторма» — считается инкрементально по мере
    прогона."""
    if not path.exists():
        return {"exists": False}
    try:
        import pandas as pd

        df = pd.read_parquet(path)
        out: dict = {
            "exists": True,
            "rows": len(df),
            "mtime": time.strftime("%H:%M:%S", time.localtime(path.stat().st_mtime)),
        }
        if "approximation" in df:
            out["error_rows"] = int(
                df["approximation"].astype(str).str.startswith("error:").sum()
            )
        storm = storm_nodes_by_scenario(cfg)
        exp_sc = expected_by_scenario(cfg)
        scenarios: dict = {}
        if "scenario" in df and "config" in df:
            dfp = df[df["scenario"].astype(str).str.startswith("pressure:")]
            for sc, g in dfp.groupby("scenario"):
                storm_nodes = storm.get(sc, set())
                arms: dict = {}
                for arm, ga in g.groupby("config"):
                    ok = ga[
                        ga["makespan_s"].notna()
                        & ~ga["approximation"].astype(str).str.startswith("error:")
                    ]
                    in_storm = int(ok["node"].isin(storm_nodes).sum()) if len(ok) else 0
                    reg = ok["placement_regret"].dropna() if "placement_regret" in ok else []
                    arms[arm] = {
                        "victims": int(len(ga)),
                        "measured": int(len(ok)),
                        "storm": in_storm,
                        "storm_pct": round(100 * in_storm / len(ok)) if len(ok) else None,
                        "makespan": round(float(ok["makespan_s"].mean()), 1) if len(ok) else None,
                        "regret": round(float(reg.mean()), 3) if len(reg) else None,
                    }
                scenarios[sc] = {
                    "storm_node": ", ".join(sorted(storm_nodes)) or "?",
                    "expected": exp_sc.get(sc),
                    "done": int(len(g)),
                    "arms": arms,
                }
        out["scenarios"] = scenarios
        return out
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": str(e)}


def baseline_summary(path: Path) -> dict:
    """Соло-бейзлайны как матрица профиль × нода (медианный makespan) — сразу
    видно неоднородность нод (медленная w7)."""
    if not path.exists():
        return {"exists": False}
    try:
        import pandas as pd

        df = pd.read_parquet(path)
        out: dict = {
            "exists": True,
            "rows": len(df),
            "mtime": time.strftime("%H:%M:%S", time.localtime(path.stat().st_mtime)),
        }
        solo = df[df["scenario"] == "baseline"] if "scenario" in df else df
        solo = solo[solo["makespan_s"].notna()]
        if len(solo) and "node" in solo and "profile" in solo:
            nodes = sorted(n for n in solo["node"].dropna().unique())
            profiles = sorted(solo["profile"].dropna().unique())
            med = solo.groupby(["profile", "node"])["makespan_s"].median()
            out["nodes"] = nodes
            out["matrix"] = {
                p: {n: (round(float(med[(p, n)])) if (p, n) in med else None) for n in nodes}
                for p in profiles
            }
        return out
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": str(e)}


def run_plan(cfg: dict, phase: str, baselines: dict, results: dict, report: dict) -> list[dict]:
    """Краткий план эксперимента для шапки страницы: этапы по порядку
    (эталонные прогоны -> каждый сценарий фоновой нагрузки -> анализ), у
    каждого — формула объёма («3 планировщика × 10 повторений × 6 задач»),
    сделано/ожидается и состояние done|active|partial|pending. План считается
    из того же config.yaml, по которому работает харнесс."""
    variants = cfg.get("scheduler_variants", ["default", "sensitivityscore"])
    arms = sum(len(variants) if c in ("A", "B") else 1 for c in cfg.get("configs", []))

    profiles = list(cfg.get("profiles", []))
    for sc in cfg.get("pressure_scenarios", []):
        v = sc.get("victim_profile", "high-s")
        if v not in profiles:
            profiles.append(v)
    b_reps = cfg.get("baseline", {}).get("repetitions", 5)
    per_node = cfg.get("baseline", {}).get("per_node", True)
    nodes_n = max(worker_node_count(cfg), 1) if per_node else 1
    b_exp = len(profiles) * b_reps * nodes_n
    b_detail = f"{len(profiles)} профиля × {b_reps} повторения"
    if per_node:
        b_detail = f"{len(profiles)} профиля × {nodes_n} узлов × {b_reps} повторения"

    stages: list[dict] = [{
        "key": "baseline",
        "label": "Эталонные прогоны (изолированно, на каждом узле)",
        "detail": b_detail + " — нормировочная база для замедления",
        "done": min(baselines.get("rows", 0), b_exp),
        "expected": b_exp,
    }]

    res_sc = results.get("scenarios") or {}
    for sc in cfg.get("pressure_scenarios", []):
        col = f"pressure:{sc['name']}"
        reps = sc.get("repetitions", cfg.get("repetitions", 10))
        victims = sc.get("victim_count", 6)
        intensities = len(sc.get("aggressors_per_node", [1]))
        exp_i = arms * intensities * reps * victims
        detail = f"{arms} планировщика × {reps} повторений × {victims} задач"
        if intensities > 1:
            detail = (f"{arms} планировщика × {intensities} уровня нагрузки × "
                      f"{reps} повторений × {victims} задач")
        stages.append({
            "key": col,
            "label": scenario_label(col),
            "detail": detail,
            "done": min((res_sc.get(col) or {}).get("done", 0), exp_i),
            "expected": exp_i,
        })

    stages.append({
        "key": "analysis",
        "label": "Статистический анализ",
        "detail": "критерий Манна-Уитни с поправкой Холма, размер эффекта "
                  "(Cliff's δ), нормированное замедление, графики — секция ниже",
        "done": 1 if report.get("exists") or (report.get("digest") or {}).get("exists") else 0,
        "expected": 1,
    })

    # Состояния: завершённый этап — done; первый незавершённый — active, пока
    # прогон жив (этап анализа active не бывает — он запускается вручную после
    # прогона); остальные — pending. Особый случай: незавершённые эталонные
    # прогоны при уже идущей основной серии — не активный этап, а partial
    # («дополнить»): например, после добавления узлов в кластер эталоны для
    # них появятся только отдельным прогоном --baseline.
    active_assigned = False
    for st in stages:
        if st["done"] >= st["expected"] and st["expected"] > 0:
            st["state"] = "done"
        elif st["key"] == "baseline" and phase != "baseline":
            st["state"] = "partial"
        elif not active_assigned and phase in ("baseline", "pressure"):
            st["state"] = "active" if st["key"] != "analysis" else "pending"
            active_assigned = True
        else:
            st["state"] = "pending"
    return stages


DIGEST_METRICS = [
    ("makespan_s", "время выполнения, с", "{:.1f}"),
    ("slowdown", "замедление", "{:.2f}×"),
    ("placement_regret", "ошибка размещения", "{:.3f}"),
]


def analysis_digest(report_dir: Path) -> dict:
    """Компактная выжимка из comparisons.csv: на каждый сценарий и метрику —
    среднее SensitivityScore и средние соперников с Holm-p и Cliff's δ. Строки
    без данных (B/C/D) молча отбрасываются — это не «ждём», а «неприменимо на
    этом стенде». Читаем структурированный csv, а не сырой summary.md."""
    csv = report_dir / "comparisons.csv"
    if not csv.exists():
        return {"exists": False}
    try:
        import pandas as pd

        df = pd.read_csv(csv)
        df = df[df["mean_b"].notna() & (df["mw_n_b"].fillna(0) > 0)]
        scenarios: dict = {}
        for sc, g in df.groupby("scenario"):
            metrics = []
            for col, label, fmt in DIGEST_METRICS:
                gm = g[g["metric"] == col]
                if gm.empty:
                    continue
                ss_mean = float(gm.iloc[0]["mean_a"])
                opponents = {}
                for r in gm.to_dict("records"):
                    holm = r.get("mw_p_holm")
                    sig = bool(pd.notna(holm) and holm < 0.05)
                    opponents[r["config_b"]] = {
                        "mean": float(r["mean_b"]),
                        "p_holm": None if pd.isna(holm) else float(holm),
                        "delta": None if pd.isna(r.get("cliffs_delta")) else float(r["cliffs_delta"]),
                        "better": float(r["mean_a"]) < float(r["mean_b"]),
                        "sig": sig,
                    }
                metrics.append(
                    {"col": col, "label": label, "fmt": fmt,
                     "ss": ss_mean, "opponents": opponents}
                )
            if metrics:
                scenarios[sc] = metrics
        return {"exists": True, "scenarios": scenarios,
                "mtime": time.strftime("%H:%M:%S", time.localtime(csv.stat().st_mtime))}
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
    cfg = load_cfg()
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
        "stand": stand_info(),
        "phase": phase,
        "activity": current_activity(all_lines),
        "progress": progress(
            phase, starts, baselines.get("rows", 0), results.get("rows", 0), exp
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


def hero_now(d: dict) -> str:
    """Крупная строка «что выполняется прямо сейчас»."""
    phase = d["phase"]
    act = d.get("activity") or {}
    reps = d.get("reps") or {}
    if phase == "DONE":
        return "Прогон завершён ✓"
    if phase == "baseline":
        total = reps.get("baseline")
        rep = (f"повторение {act['rep'] + 1} из {total}"
               if "rep" in act and total else "")
        node = ""
        m = re.search(r"base-(worker-[\d.]+)-rep", act.get("job_id", ""))
        if m:
            node = f" · узел {m.group(1).replace('worker-', 'w-')}"
        prof = act.get("profile", "")
        return f"Эталонный прогон · профиль {esc(prof)}{esc(node)} · {esc(rep)}".strip(" ·")
    if phase == "pressure":
        sc_col = act.get("scenario") or ""
        sc = scenario_label(sc_col) if sc_col else "?"
        arm = arm_label(act.get("arm", "?"))
        total = (reps.get("pressure") or {}).get(sc_col.replace("pressure:", ""), None)
        rep = (f"повторение {act['rep'] + 1} из {total}"
               if "rep" in act and total
               else (f"повторение {act['rep'] + 1}" if "rep" in act else ""))
        vic = (f" · задача №{act['victim'] + 1}"
               if act.get("victim") is not None else "")
        return (f"Сценарий «{esc(sc)}» · планировщик <b>{esc(arm)}</b> · "
                f"{esc(rep)}{esc(vic)}").strip(" ·")
    return "ожидание запуска…"


def plan_section(plan: list[dict]) -> str:
    """Краткий план прогона под прогресс-баром: ✓ сделано / ▶ идёт / ○ впереди,
    у каждого этапа — объём формулой и сделано/ожидается."""
    if not plan:
        return ""
    icon = {"done": ("✓", "good"), "active": ("▶", "act"),
            "partial": ("◐", "warn"), "pending": ("○", "dim")}
    rows = []
    for st in plan:
        mark, cls = icon.get(st["state"], ("○", "dim"))
        if st["key"] == "analysis":
            # У анализа счётчик 0/1 не информативен — словами честнее.
            count = "готов" if st["state"] == "done" else "выполняется после прогона"
        else:
            count = f"{st['done']}/{st['expected']}"
            if st["state"] == "active" and st["expected"]:
                count += f" · {round(100 * st['done'] / st['expected'])}%"
            elif st["state"] == "partial":
                count += " · дополнить после серии (добавлены узлы)"
        rows.append(
            f"<div class='st {cls}'><span class='mark'>{mark}</span>"
            f"<span class='lbl'>{esc(st['label'])}</span>"
            f"<span class='cnt'>{esc(count)}</span>"
            f"<span class='det dim'>{esc(st['detail'])}</span></div>"
        )
    return f"<div class='plan'>{''.join(rows)}</div>"


def storm_cell(m: dict, is_best: bool) -> str:
    """Ячейка «на перегруженный узел»: N из измеренных (доля %), цвет по доле."""
    pct = m.get("storm_pct")
    if pct is None:
        return "<td class='dim'>—</td>"
    cls = "good" if pct <= 12 else ("warn" if pct <= 30 else "bad")
    star = " ★" if is_best else ""
    return (
        f"<td class='{cls}'><b>{m['storm']}</b>/{m['measured']} "
        f"<span class='pct'>({pct}%)</span>{star}</td>"
    )


def money_section(res: dict) -> str:
    if not res.get("exists"):
        return ("<p class='dim'>результатов ещё нет — появятся с первой "
                "задачей основной серии</p>")
    if "error" in res:
        return f"<p class='err'>{esc(res['error'])}</p>"
    scs = res.get("scenarios") or {}
    if not scs:
        return "<p class='dim'>строк основной серии ещё нет</p>"
    parts: list[str] = []
    for sc in sorted(scs):
        info = scs[sc]
        arms = info["arms"]
        ordered = [a for a in ARM_ORDER if a in arms] + [
            a for a in arms if a not in ARM_ORDER
        ]
        pcts = [arms[a]["storm_pct"] for a in ordered if arms[a]["storm_pct"] is not None]
        best = min(pcts) if pcts else None

        prog = ""
        if info.get("expected"):
            prog = f" · <span class='dim'>{info['done']}/{info['expected']} измерений</span>"
        parts.append(
            f"<h3>{esc(scenario_label(sc))} "
            f"<small class='dim'>перегружен узел "
            f"{esc(info['storm_node'].replace('worker-', 'w-'))}{prog}</small></h3>"
        )

        head = ("<tr><th>планировщик</th><th>задач на перегруженный узел</th>"
                "<th>время выполнения, с</th><th>ошибка размещения</th></tr>")
        body = []
        for a in ordered:
            m = arms[a]
            is_best = best is not None and m.get("storm_pct") == best and len(ordered) > 1
            row_cls = " class='hero'" if a == HERO_ARM else ""
            mk = m["makespan"] if m["makespan"] is not None else "—"
            rg = m["regret"] if m["regret"] is not None else "—"
            body.append(
                f"<tr{row_cls}><td><b>{esc(arm_label(a))}</b></td>"
                f"{storm_cell(m, is_best)}"
                f"<td>{esc(mk)}</td><td>{esc(rg)}</td></tr>"
            )
        parts.append(f"<table class='money'>{head}{''.join(body)}</table>")

        # Вывод одной строкой, когда данные по всем планировщикам уже есть.
        if best is not None and HERO_ARM in arms and arms[HERO_ARM]["storm_pct"] is not None:
            hero_pct = arms[HERO_ARM]["storm_pct"]
            others = [
                f"{arm_label(a)} — {arms[a]['storm_pct']}%"
                for a in ordered
                if a != HERO_ARM and arms[a]["storm_pct"] is not None
            ]
            good = hero_pct == best
            verdict = "✓ лучший результат" if good else "△ пока не лучший"
            parts.append(
                f"<p class='takeaway'>SensitivityScore направил на перегруженный "
                f"узел <b>{hero_pct}%</b> задач ({', '.join(others) or '—'}) "
                f"<span class='{'good' if good else 'warn'}'>{verdict}</span></p>"
            )
    parts.append(
        "<p class='note dim'>«Задач на перегруженный узел» — доля задач, "
        "размещённых планировщиком на узел с фоновой нагрузкой (меньше — "
        "лучше; прямой показатель качества решения). «Ошибка размещения» — "
        "превышение интерференции выбранного узла над лучшим доступным на "
        "момент решения, 0..1. Среднее время выполнения без нормировки "
        "смещено неоднородностью узлов — нормированное замедление считается "
        "в секции «Анализ» после прогона.</p>"
    )
    return "".join(parts)


def baseline_section(d: dict) -> str:
    if not d.get("exists"):
        return "<p class='dim'>файла ещё нет</p>"
    if "error" in d:
        return f"<p class='err'>{esc(d['error'])}</p>"
    head = f"<p class='dim'>{d['rows']} измерений · обновлено {esc(d.get('mtime','?'))}</p>"
    matrix = d.get("matrix")
    if not matrix:
        return head + "<p class='dim'>— пусто —</p>"
    nodes = d["nodes"]
    th = "<th>профиль задачи</th>" + "".join(
        f"<th>{esc(n.replace('worker-', 'w-'))}</th>" for n in nodes
    )
    rows = []
    for prof in sorted(matrix):
        cells = "".join(
            f"<td>{matrix[prof][n] if matrix[prof][n] is not None else '—'}</td>"
            for n in nodes
        )
        rows.append(f"<tr><td><b>{esc(prof)}</b></td>{cells}</tr>")
    return (
        head
        + "<table><caption>медианное время изолированного выполнения, с — "
        "нормировочная база; разброс между узлами = аппаратная "
        "неоднородность кластера</caption>"
        + f"<tr>{th}</tr>{''.join(rows)}</table>"
    )


def cluster_section(d: dict) -> str:
    cl = d["cluster"]
    aggr = cl.get("aggressors", [])
    running = sum(1 for a in aggr if len(a) >= 3 and a[2] == "Running")
    jobs = cl.get("jobs", [])
    active_jobs = sum(1 for j in jobs if len(j) >= 2 and j[1] not in ("", "<none>"))
    badge = (
        f"<span class='chip {'good' if running else 'dim'}'>генераторы фоновой "
        f"нагрузки: {running} активны</span> "
        f"<span class='chip'>задач выполняется: {active_jobs}</span>"
    )
    aggr_rows = [[a[0], a[1].replace("worker-", "w-") if len(a) > 1 else "", a[2] if len(a) > 2 else ""] for a in aggr]
    return (
        f"<p>{badge}</p>"
        "<details><summary class='dim'>подробнее (задачи / генераторы нагрузки)</summary>"
        + "<h4>Генераторы фоновой нагрузки</h4>" + table(["под", "узел", "состояние"], aggr_rows)
        + "<h4>Задачи</h4>" + table(["задача", "выполняется"], jobs)
        + "</details>"
    )


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


def digest_cell(op: dict, fmt: str) -> str:
    """Ячейка соперника в дайджест-таблице: среднее + значок значимости."""
    val = fmt.format(op["mean"])
    if op.get("sig") and op.get("better"):
        mark, cls = "✓", "good"  # SensitivityScore значимо лучше
    elif op.get("sig"):
        mark, cls = "✗", "bad"  # значимо ХУЖЕ
    else:
        mark, cls = "·", "dim"  # разницы нет
    p = op.get("p_holm")
    ptxt = f" p={p:.2g}" if p is not None else ""
    return f"<td class='{cls}'>{esc(val)}<span class='pct'>{esc(ptxt)}</span> {mark}</td>"


def digest_section(dig: dict) -> str:
    """Компактный вердикт: таблица метрика × (SensitivityScore | соперники)."""
    if not dig.get("exists"):
        return ""
    if "error" in dig:
        return f"<p class='err'>{esc(dig['error'])}</p>"
    scs = dig.get("scenarios") or {}
    if not scs:
        return "<p class='dim'>сравнений ещё нет</p>"
    parts = [f"<p class='dim'>обновлено {esc(dig.get('mtime','?'))} · "
             "✓ — преимущество SensitivityScore статистически значимо "
             "(p&lt;0.05 с поправкой Холма); · — различие не значимо</p>"]
    for sc in sorted(scs):
        metrics = scs[sc]
        opponents = []
        for m in metrics:
            for cb in m["opponents"]:
                if cb not in opponents:
                    opponents.append(cb)
        opp_order = [a for a in ARM_ORDER if a in opponents] + [
            a for a in opponents if a not in ARM_ORDER
        ]
        head = ("<tr><th>показатель</th><th>SensitivityScore</th>"
                + "".join(f"<th>против {esc(arm_label(a))}</th>" for a in opp_order)
                + "</tr>")
        rows = []
        for m in metrics:
            ss = m["fmt"].format(m["ss"])
            cells = "".join(
                digest_cell(m["opponents"][a], m["fmt"]) if a in m["opponents"]
                else "<td class='dim'>—</td>"
                for a in opp_order
            )
            rows.append(
                f"<tr><td>{esc(m['label'])}</td><td><b>{esc(ss)}</b></td>{cells}</tr>"
            )
        parts.append(
            f"<h3>{esc(scenario_label(sc))}</h3>"
            f"<table class='money'>{head}{''.join(rows)}</table>"
        )
    return "".join(parts)


def report_section(rep: dict) -> str:
    dig_html = digest_section(rep.get("digest") or {})
    if not rep["exists"] and not dig_html:
        return ("<p class='dim'>появится после прогона: "
                f"<code>analyze.py ... --outdir {esc(rep['dir'])}</code></p>")
    parts = []
    if dig_html:
        parts.append(dig_html)
    # Графики — компактной сеткой-миниатюрами, каждая кликается в полный размер.
    if rep["plots"]:
        thumbs = "".join(
            f"<a href='/report/{esc(png)}' target='_blank' class='thumb'>"
            f"<img src='/report/{esc(png)}' alt='{esc(png)}' loading='lazy'>"
            f"<span>{esc(png.replace('.png','').replace('-pressure',''))}</span></a>"
            for png in rep["plots"]
        )
        parts.append(
            "<details data-k='plots'><summary>графики "
            f"<span class='dim'>({len(rep['plots'])})</span></summary>"
            f"<div class='gallery'>{thumbs}</div></details>"
        )
    # Полный текстовый отчёт — для тех, кому нужны все p/CV/fingerprint.
    if rep["exists"]:
        try:
            md = (Path(rep["dir"]) / "summary.md").read_text(encoding="utf-8")
            parts.append(
                "<details data-k='fullreport'><summary>полный текстовый "
                f"отчёт</summary><div class='fullmd'>{md_to_html(md)}</div></details>"
            )
        except OSError as e:
            parts.append(f"<p class='err'>{esc(e)}</p>")
    return "".join(parts)


PHASE_META = {
    "DONE": ("#22a06b", "завершено"),
    "pressure": ("#e8590c", "основная серия"),
    "baseline": ("#1c7ed6", "эталонные прогоны"),
    "not started": ("#868e96", "ожидание"),
}


def render_html(d: dict) -> str:
    phase = d["phase"]
    color, phase_word = PHASE_META.get(phase, ("#868e96", phase))
    prog = d.get("progress", {})
    pct = prog.get("overall_pct")

    bar = ""
    if pct is not None:
        eta = (
            f"ожидаемое завершение ~{prog['eta']} (осталось ~{prog['eta_minutes']} мин)"
            if "eta" in prog
            else ""
        )
        phase_pct = f"этап «{phase_word}»: {prog['phase_pct']}%" if "phase_pct" in prog else ""
        meta = " · ".join(x for x in (phase_pct, eta) if x)
        bar = f"""<div class="prog">
<div class="barbg"><div class="bar" style="background:{color};width:{pct}%"></div>
<span class="barlabel">{pct}%</span></div>
<div class="progmeta dim">{esc(meta)}</div></div>"""

    st = d["stand"]
    stand_label = esc(st.get("label") or "стенд")

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{stand_label} · {esc(phase_word)} {pct if pct is not None else ''}%</title>
<script>
/* Тема применяется ДО отрисовки — без вспышки не той темы при каждом обновлении.
   Режим (auto|light|dark) в localStorage; auto резолвится по системной теме. */
(function(){{
  var m=localStorage.getItem('ssTheme')||'auto';
  var dark=m==='dark'||(m==='auto'&&matchMedia('(prefers-color-scheme:dark)').matches);
  var r=document.documentElement;
  r.dataset.theme=dark?'dark':'light'; r.dataset.themeMode=m;
}})();
</script>
<style>
:root{{
  --bg:#f6f7f9; --card:#fff; --ink:#1f2328; --dim:#6b7280; --line:#e5e7eb;
  --good:#1a7f52; --goodbg:#e6f6ee; --warn:#b45309; --warnbg:#fdf2e0;
  --bad:#c0392b; --badbg:#fdecea; --hero:#eef4ff; --herobd:#c9dcff;
}}
:root[data-theme="dark"]{{
  --bg:#0f1115; --card:#181b21; --ink:#e6e8eb; --dim:#9aa4b2;
  --line:#2a2f3a; --good:#4ade80; --goodbg:#12241a; --warn:#fbbf24;
  --warnbg:#2a1f0a; --bad:#f87171; --badbg:#2a1414; --hero:#12203a; --herobd:#1e3a66;
}}
*{{box-sizing:border-box}}
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;
  background:var(--bg);color:var(--ink);line-height:1.5}}
.wrap{{max-width:60em;margin:0 auto;padding:1.2em 1em 4em}}
.top{{display:flex;align-items:center;gap:.7em;flex-wrap:wrap;margin-bottom:.2em}}
.badge{{background:{color};color:#fff;font-weight:600;font-size:.8em;
  padding:.18em .7em;border-radius:999px;text-transform:uppercase;letter-spacing:.03em}}
.top h1{{font-size:1.15em;margin:0;font-weight:600}}
.top .upd{{margin-left:auto;color:var(--dim);font-size:.82em}}
.now{{font-size:1.35em;font-weight:500;margin:.35em 0 .1em}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:1em 1.2em;margin:1em 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
.card>h2{{margin:.1em 0 .6em;font-size:1.05em;border:none;padding:0}}
h3{{margin:1em 0 .3em;font-size:.98em}} h4{{margin:.8em 0 .3em;font-size:.9em;color:var(--dim)}}
.prog{{margin:.7em 0 .2em}}
.barbg{{position:relative;background:var(--line);height:22px;border-radius:11px;overflow:hidden}}
.bar{{height:22px;border-radius:11px;transition:width .6s}}
.barlabel{{position:absolute;top:0;left:.8em;line-height:22px;font-weight:700;
  font-size:.82em;color:var(--ink);mix-blend-mode:difference;filter:invert(1)}}
.progmeta{{font-size:.85em;margin-top:.35em}}
.plan{{margin:.7em 0 .3em;font-size:.9em;max-width:46em}}
.plan .st{{display:flex;gap:.55em;align-items:baseline;padding:.14em 0;flex-wrap:wrap}}
.plan .mark{{width:1.1em;text-align:center;flex-shrink:0}}
.plan .lbl{{font-weight:600;min-width:13em}}
.plan .cnt{{font-variant-numeric:tabular-nums}}
.plan .det{{font-size:.85em}}
.plan .st.done .mark,.plan .st.done .cnt{{color:var(--good)}}
.plan .st.act .mark{{color:{color}}}
.plan .st.act .cnt{{font-weight:700}}
.plan .st.warn .mark,.plan .st.warn .cnt{{color:var(--warn)}}
.plan .st.dim .lbl{{font-weight:500;color:var(--dim)}}
table{{border-collapse:collapse;margin:.4em 0;font-size:.9em;width:auto}}
caption{{text-align:left;color:var(--dim);font-size:.82em;padding-bottom:.3em}}
th,td{{border:1px solid var(--line);padding:.34em .7em;text-align:left}}
th{{background:transparent;color:var(--dim);font-weight:600;font-size:.86em}}
table.money td,table.money th{{padding:.4em .8em}}
tr.hero td{{background:var(--hero)}}
tr.hero td:first-child{{border-left:3px solid var(--herobd)}}
.good{{color:var(--good)}} .warn{{color:var(--warn)}} .bad{{color:var(--bad)}}
td.good{{background:var(--goodbg)}} td.warn{{background:var(--warnbg)}} td.bad{{background:var(--badbg)}}
.pct{{font-size:.85em;color:var(--dim)}}
.dim{{color:var(--dim)}} .err{{color:var(--bad)}}
.chip{{display:inline-block;background:var(--line);border-radius:999px;
  padding:.15em .7em;font-size:.82em;margin-right:.3em}}
.chip.good{{background:var(--goodbg);color:var(--good)}}
.takeaway{{margin:.3em 0 .8em;font-size:.92em}}
.note{{font-size:.82em;margin-top:.6em}}
details summary{{cursor:pointer;font-size:.85em;margin:.4em 0}}
pre{{background:var(--bg);padding:.7em;overflow-x:auto;font-size:.82em;
  border-radius:8px;border:1px solid var(--line)}}
code{{background:var(--bg);padding:.1em .35em;border-radius:4px;font-size:.9em}}
.gallery{{display:flex;flex-wrap:wrap;gap:.6em;margin-top:.5em}}
.thumb{{border:1px solid var(--line);border-radius:8px;padding:.35em;text-decoration:none;
  color:var(--dim);font-size:.76em;text-align:center;background:var(--card)}}
.thumb img{{display:block;max-height:200px;max-width:100%;border-radius:4px;margin-bottom:.2em}}
.thumb:hover{{border-color:var(--herobd)}}
.fullmd{{font-size:.9em;opacity:.92}}
.fullmd table{{font-size:.88em}}
a{{color:#4c8dff}}
.themebtn{{background:var(--card);border:1px solid var(--line);color:var(--ink);
  border-radius:999px;padding:.2em .8em;font-size:.8em;cursor:pointer;font-family:inherit}}
.themebtn:hover{{border-color:var(--herobd)}}
.refreshing{{opacity:.5;transition:opacity .3s}}
</style></head><body><div class="wrap">

<div class="top">
  <span class="badge">{esc(phase_word)}</span>
  <h1>{stand_label}</h1>
  <span class="upd">обновлено {esc(d['time'])} · авто-10с · <a href="/json">/json</a></span>
  <button id="themebtn" class="themebtn" onclick="cycleTheme()" title="тема">🌗</button>
</div>
<div class="now">{hero_now(d)}</div>
{bar}
{plan_section(d.get('plan') or [])}

<div class="card">
  <h2>Размещение задач по планировщикам</h2>
  {money_section(d['results'])}
</div>

<div class="card">
  <h2>Текущее состояние кластера</h2>
  {cluster_section(d)}
</div>

<div class="card">
  <h2>Эталонные прогоны <span class='dim' style='font-weight:400;font-size:.8em'>(каждая задача изолированно на каждом узле — база нормировки)</span></h2>
  {baseline_section(d['baselines'])}
</div>

<div class="card">
  <h2>Статистический анализ</h2>
  {report_section(d['report'])}
</div>

<details class="card" data-k="standlogs">
  <summary>Стенд и журнал прогона</summary>
  <p class='dim'>{esc(st.get('server',''))}</p>
  {table(["узел", "kubelet", "ядро ОС", "CPU", "память"], st.get("nodes", []))}
  <h4>Последние строки журнала</h4>
  <pre>{esc(chr(10).join(d['log_tail']))}</pre>
  <h4>Последние ошибки в журнале</h4>
  <pre>{esc(chr(10).join(d['log_errors']) or '—')}</pre>
</details>

</div>
<script>
/* Кнопка темы: цикл авто -> светлая -> тёмная. */
function paintThemeBtn(){{
  var m=document.documentElement.dataset.themeMode||'auto';
  var b=document.getElementById('themebtn');
  if(b) b.textContent=({{auto:'🌗 авто',light:'☀️ светлая',dark:'🌙 тёмная'}})[m];
}}
function cycleTheme(){{
  var order=['auto','light','dark'];
  var m=localStorage.getItem('ssTheme')||'auto';
  var next=order[(order.indexOf(m)+1)%order.length];
  localStorage.setItem('ssTheme',next);
  var dark=next==='dark'||(next==='auto'&&matchMedia('(prefers-color-scheme:dark)').matches);
  var r=document.documentElement; r.dataset.theme=dark?'dark':'light'; r.dataset.themeMode=next;
  paintThemeBtn();
}}
/* Мягкое авто-обновление: перезагрузка каждые 10с, но с сохранением прокрутки
   и раскрытых <details> — иначе открытый блок «графики» схлопывался бы, а
   страница прыгала бы вверх на каждом тике. */
var UIK='ssStatusUI';
history.scrollRestoration='manual';
function saveUI(){{
  try{{
    var open=[].slice.call(document.querySelectorAll('details[data-k]'))
      .filter(function(d){{return d.open}}).map(function(d){{return d.dataset.k}});
    sessionStorage.setItem(UIK, JSON.stringify({{open:open, y:window.scrollY}}));
  }}catch(e){{}}
}}
function restoreUI(){{
  try{{
    var s=JSON.parse(sessionStorage.getItem(UIK)||'{{}}');
    (s.open||[]).forEach(function(k){{
      var d=document.querySelector('details[data-k="'+k+'"]'); if(d) d.open=true;
    }});
    if(s.y) window.scrollTo(0,s.y);
  }}catch(e){{}}
}}
paintThemeBtn(); restoreUI();
setTimeout(function(){{ saveUI(); document.body.classList.add('refreshing'); location.reload(); }}, 10000);
</script>
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
        "(для процента готовности и ETA в шапке) и штормимые ноды сценариев",
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
