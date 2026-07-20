"""Лог харнесса + config.yaml -> фаза, текущая активность, проценты, ETA и
краткий план эксперимента. Ожидаемые объёмы зеркалят run_experiment.py."""

from __future__ import annotations

import re
import time

from .cluster import worker_node_count
from .labels import ru, scenario_label, scenario_victim_count


def _marker_ts(hms: str, epoch: str | None = None) -> float | None:
    """Unix-время маркера лога.

    Предпочитаем epoch из самого маркера: он абсолютен и не зависит ни от
    часового пояса контейнера, ни от даты. Без него остаётся время суток, а
    оно двусмысленно — и это уже стреляло: харнесс пишет по локальному времени
    хоста (MSK), страница живёт в контейнере с UTC, и прогон, начатый в 02:14
    MSK, выглядел для неё стартовавшим 21 час назад — «идёт 21 ч, осталось
    21456 мин». Разбор по времени суток оставлен только для логов, снятых до
    появления epoch."""
    if epoch:
        try:
            return float(epoch)
        except ValueError:
            pass
    try:
        ts = time.mktime(
            time.strptime(f"{time.strftime('%Y-%m-%d')} {hms}", "%Y-%m-%d %H:%M:%S")
        )
    except ValueError:
        return None
    if ts > time.time() + 60:
        ts -= 86400.0
    return ts


def run_phase(log_lines_all: list[str]) -> tuple[str, dict[str, float], dict[str, float]]:
    """-> (фаза, {фаза: старт}, {фаза: финиш}) по маркерам
    '=== X START|DONE HH:MM:SS ==='. Финиши нужны для итоговой длительности."""
    phase = "not started"
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    for l in log_lines_all:
        m = re.search(r"=== (BASELINE|PRESSURE) (START|DONE) (\d\d:\d\d:\d\d)(?: epoch=(\d+))?", l)
        if m:
            ph, kind = m.group(1).lower(), m.group(2)
            ts = _marker_ts(m.group(3), m.group(4))
            if kind == "START":
                phase = ph
                if ts is not None:
                    starts[ph] = ts
            else:
                if ts is not None:
                    ends[ph] = ts
                if ph == "pressure":
                    phase = "DONE"
        elif "ALL DONE" in l or "PRESSURE DONE" in l:
            phase = "DONE"
    return phase, starts, ends


def current_activity(log_lines_all: list[str], prof_map: dict[str, str]) -> dict:
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
        act["scenario"] = prof_map.get(act["profile"])
    return act


def expected_by_scenario(cfg: dict) -> dict[str, int]:
    """Ожидаемое число строк основной серии по каждому сценарию (планировщики
    × интенсивности × повторы × задачи) — для per-сценарного прогресса."""
    variants = cfg.get("scheduler_variants", ["default", "sensitivityscore"])
    arms = sum(len(variants) if c in ("A", "B") else 1 for c in cfg.get("configs", []))
    out = {}
    for sc in cfg.get("pressure_scenarios", []):
        out[f"pressure:{sc['name']}"] = (
            arms
            * len(sc.get("aggressors_per_node", [1]))
            * sc.get("repetitions", cfg.get("repetitions", 10))
            * scenario_victim_count(sc)
        )
    return out


def expected_rows(cfg: dict) -> dict[str, int]:
    """Ожидаемое число строк по фазам — зеркалит run_experiment.py."""
    profiles = list(cfg.get("profiles", []))
    for sc in cfg.get("pressure_scenarios", []):
        victims = ([v["profile"] for v in sc["victims"]] if "victims" in sc
                   else [sc.get("victim_profile", "high-s")])
        for v in victims:
            if v not in profiles:
                profiles.append(v)
    baseline_exp = len(profiles) * cfg.get("baseline", {}).get("repetitions", 5)
    unknown = False
    if cfg.get("baseline", {}).get("per_node", True):
        nodes = worker_node_count(cfg)
        if nodes is None:
            # Топология неизвестна — честно говорим «не знаю» вместо тихого
            # умножения на 1. Занижённый объём эталонов завышал бы процент и
            # показывал незаконченный этап как пройденный.
            baseline_exp = 0
            unknown = True
        else:
            baseline_exp *= nodes
    pressure_exp = sum(expected_by_scenario(cfg).values())
    return {
        "baseline": baseline_exp,
        "pressure": pressure_exp,
        "topology_unknown": unknown,
    }


def recent_pace(log_lines: list[str], window: int = 12) -> float | None:
    """Строк в секунду по последним `window` сабмитам лога.

    Темп «за всю фазу» разваливается после любой длинной паузы: 20.07 хост
    (ноутбук) ушёл в сон на 6.4 ч посреди серии, и страница, поделив сделанное
    на полное время этапа, предсказала финиш в 19:32 вместо реальных ~13:30.
    Недавнее окно к таким провалам устойчиво: пауза выпадает из него через
    несколько сабмитов. None — когда сабмитов ещё мало, тогда остаётся оценка
    по всей фазе."""
    stamps: list[float] = []
    for l in log_lines:
        if "INFO submit: job_id=" not in l:
            continue
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", l)
        if not m:
            continue
        try:
            stamps.append(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
        except ValueError:
            continue
    if len(stamps) < 4:
        return None
    tail = stamps[-window:]
    span = tail[-1] - tail[0]
    return (len(tail) - 1) / span if span > 0 else None


def progress(
    phase: str, starts: dict, ends: dict, b_rows: int, p_rows: int, exp: dict,
    scope: str = "full", log_lines: list[str] | None = None,
) -> dict:
    """Процент (текущей фазы и всего прогона) + ETA по скорости текущей фазы.
    После завершения — итоговая длительность. scope="baseline" — прогон
    только эталонный (добор): основная серия не входит в общий объём, иначе
    бар делил бы сделанное на строки, которых в этом прогоне не будет."""
    out: dict = {}
    b_exp, p_exp = exp.get("baseline", 0), exp.get("pressure", 0)
    if scope == "baseline":
        p_exp = 0
    total_exp = b_exp + p_exp
    if not total_exp:
        return out
    if phase == "baseline":
        # results-файл в этот момент может содержать только СТАРУЮ серию
        # (харнесс перепишет его с нуля на первом же плече основной серии) —
        # чужие строки не засчитываются как прогресс будущей фазы.
        done_overall = min(b_rows, b_exp)
    else:
        # Эталонные прогоны могут быть неполными и во время основной серии:
        # для узлов, добавленных в кластер позже, они добираются отдельным
        # прогоном --baseline. Считаем фактические строки обеих фаз, а не
        # «эталонный этап пройден по определению».
        done_overall = min(b_rows, b_exp) + min(p_rows, p_exp)
    out["overall_pct"] = round(100 * done_overall / total_exp)

    cur_done, cur_exp = (b_rows, b_exp) if phase == "baseline" else (p_rows, p_exp)
    if phase in ("baseline", "pressure") and cur_exp:
        out["phase_pct"] = round(100 * min(cur_done, cur_exp) / cur_exp)
        start = starts.get(phase)
        if start and time.time() > start:
            out["phase_elapsed_min"] = round((time.time() - start) / 60)
        if start and cur_done > 0:
            elapsed = time.time() - start
            if elapsed > 0:
                # ETA только ТЕКУЩЕГО этапа. Экстраполировать следующий этап
                # темпом этого нельзя: строки эталонов и основной серии
                # несравнимы по длительности, а прогон может быть и
                # baseline-only (добор эталонов) — оценка выходила кратно
                # завышенной. Подпись на странице говорит «этап завершится».
                # Темп: сначала по недавнему окну (устойчив к паузам вроде
                # засыпания хоста), иначе — по всей фазе.
                rate = recent_pace(log_lines or []) or cur_done / elapsed
                remaining = max(cur_exp - cur_done, 0) / rate
                out["eta"] = time.strftime(
                    "%H:%M", time.localtime(time.time() + remaining)
                )
                out["eta_minutes"] = round(remaining / 60)
    elif phase == "DONE":
        # НЕ рисуем 100% по одному лишь маркеру в логе. Маркер
        # «=== PRESSURE DONE ... ===» печатается run-stage-*.sh безусловно,
        # чем бы харнесс ни кончился: серия, упавшая на первом же плече,
        # показывала «Прогон завершён ✓ 100%» при трети собранных строк.
        # Считаем фактический процент; 100% — только если строки реально есть.
        out["overall_pct"] = round(100 * done_overall / total_exp)
        out["phase_pct"] = out["overall_pct"]
        if done_overall < total_exp:
            out["incomplete"] = True
            out["missing_rows"] = total_exp - done_overall
        key = "pressure" if "pressure" in starts else "baseline"
        s, e = starts.get(key), ends.get(key)
        if s is not None and e is not None and e >= s:
            out["duration_phase"] = key
            out["duration_min"] = round((e - s) / 60)
            out["finished_at"] = time.strftime("%H:%M", time.localtime(e))
    return out


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
        vs = ([v["profile"] for v in sc["victims"]] if "victims" in sc
              else [sc.get("victim_profile", "high-s")])
        for v in vs:
            if v not in profiles:
                profiles.append(v)
    b_reps = cfg.get("baseline", {}).get("repetitions", 5)
    per_node = cfg.get("baseline", {}).get("per_node", True)
    # None = топология неизвестна (kubectl не ответил). Не подставляем 1:
    # объём эталонов тогда занижается в N раз и незаконченный этап выглядит
    # пройденным. Показываем «узлов ?» и не считаем этап.
    nodes_n = worker_node_count(cfg) if per_node else 1
    b_exp = len(profiles) * b_reps * (nodes_n or 0)
    parts = [ru(len(profiles), "профиль", "профиля", "профилей")]
    if per_node:
        parts.append(
            ru(nodes_n, "узел", "узла", "узлов") if nodes_n
            else "узлов ? (нет связи с кластером)"
        )
    parts.append(ru(b_reps, "повторение", "повторения", "повторений"))
    b_detail = " × ".join(parts)

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
        victims = scenario_victim_count(sc)
        intensities = len(sc.get("aggressors_per_node", [1]))
        exp_i = arms * intensities * reps * victims
        parts = [ru(arms, "планировщик", "планировщика", "планировщиков")]
        if intensities > 1:
            parts.append(ru(intensities, "уровень", "уровня", "уровней") + " нагрузки")
        parts.append(ru(reps, "повторение", "повторения", "повторений"))
        parts.append(ru(victims, "задача", "задачи", "задач"))
        stages.append({
            "key": col,
            "label": scenario_label(col),
            "detail": " × ".join(parts),
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
