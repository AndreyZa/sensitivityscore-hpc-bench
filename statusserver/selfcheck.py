#!/usr/bin/env python3
"""Самопроверка statusserver без браузера — гоняется после правок render.py:

  1. компиляция всех модулей пакета (py_compile);
  2. рендер страницы на синтетических данных (пустой прогон и завершённый);
  3. каждый встроенный <script> — через `node --check` (если node есть);
     JS живёт внутри f-строки Python, где ошибка в {{...}} ломает синтаксис
     молча — браузер узнал бы об этом первым.

Запуск: harness/.venv/bin/python statusserver/selfcheck.py
"""

from __future__ import annotations

import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG.parent))

from statusserver.render import render_html  # noqa: E402

EMPTY_RUN = {
    "time": "00:00:00",
    "phase": "not started",
    "progress": {},
    "activity": {},
    "reps": {},
    "stand": {"label": "selfcheck", "server": "", "nodes": []},
    "log_tail": [],
    "log_errors": [],
    "results": {"exists": False},
    "baselines": {"exists": False},
    "cluster": {"jobs": [], "aggressors": []},
    "report": {"exists": False, "dir": "report", "plots": [], "digest": {"exists": False}},
    "plan": [],
}

FINISHED_RUN = {
    **EMPTY_RUN,
    "phase": "DONE",
    "progress": {"overall_pct": 100, "phase_pct": 100,
                 "duration_min": 383, "finished_at": "01:52"},
    "report": {"exists": False, "dir": "report", "digest": {"exists": False},
               "plots": ["placement_regret-pressure-io.png",
                         "interference_vs_makespan-pressure-net.png",
                         "cv_comparison-pressure-net.png",
                         "something_custom.png"]},
    "plan": [
        {"key": "baseline", "label": "Эталонные прогоны", "detail": "d",
         "done": 54, "expected": 54, "state": "done"},
        {"key": "pressure:io", "label": "Диск (IO)", "detail": "d",
         "done": 180, "expected": 180, "state": "done"},
        {"key": "analysis", "label": "Анализ", "detail": "d",
         "done": 0, "expected": 1, "state": "pending"},
    ],
}


def check_costly_counter() -> bool:
    """Счётчик «задач на дорогой узел» обязан РАЗЛИЧАТЬ планировщики.

    Регрессия, ради которой это здесь: в смешанном сценарии считалось
    номинальное совпадение с объявленной осью задачи, и таблица показывала
    33% / 57% / 42% — по ней планировщики выглядели одинаковыми, а
    SensitivityScore даже хуже прочих. При калиброванных ценах осей узел
    дешёвой оси занимать ВЫГОДНО, поэтому такое совпадение ошибкой не
    является. Считать надо попадания на дорогой узел: там 0% / 17% / 54%.

    Проверяем на синтетике: три плеча, дисковый шторм на w9 (io — дорогая
    ось по base-весам), кэш-шторм на w8 (бесплатный)."""
    try:
        import pandas as pd
    except ImportError:
        print("costly counter: пропущен (нет pandas)")
        return False

    cfg = {
        "score_weights": {"base": {"llc": 0.0, "numa": 0.0, "net": 0.09, "io": 1.0}},
        "pressure_scenarios": [{
            "name": "mixed3",
            "storms": [
                {"node": "w8", "toxic_for": ["high-s"]},
                {"node": "w9", "toxic_for": ["high-s-io"]},
            ],
        }],
    }
    # high-s-io объявлен high сразу по llc/numa/io — «главная ось» из этого
    # не выводится, поэтому счётчик обязан спрашивать про КОНКРЕТНУЮ ось.
    rows = []
    for arm, nodes in [("A-default", ["w8", "w9"]),
                       ("A-sensitivityscore", ["w8", "w8"]),
                       ("A-trimaran", ["w9", "w9"])]:
        for node in nodes:
            rows.append({
                "scenario": "pressure:mixed3", "config": arm, "node": node,
                "profile": "high-s-io", "makespan_s": 100.0,
                "placement_regret": 0.1, "approximation": "",
                "sensitivity_llc": "high", "sensitivity_numa": "high",
                "sensitivity_net": "low", "sensitivity_io": "high",
            })
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        tmp = Path(f.name)
    pd.DataFrame(rows).to_parquet(tmp)
    try:
        from statusserver.data import pressure_results

        info = pressure_results(tmp, cfg)["scenarios"]["pressure:mixed3"]
    finally:
        tmp.unlink()

    problems = []
    if info.get("costly_axis") != "диск":
        problems.append(f"дорогая ось не определена: {info.get('costly_axis')!r}")
    if info.get("nominal"):
        problems.append("остался номинальный счётчик, хотя цены осей известны")
    got = {a: m["storm"] for a, m in info["arms"].items()}
    want = {"A-default": 1, "A-sensitivityscore": 0, "A-trimaran": 2}
    if got != want:
        problems.append(f"попаданий на дорогой узел {got}, ожидалось {want}")
    for p in problems:
        print(f"FAIL costly counter: {p}")
    if not problems:
        print("costly counter: ok")
    return bool(problems)


def check_recent_pace() -> bool:
    """Темп обязан НЕ зависеть от фазы цикла плеча.

    Регрессия, ради которой это здесь (19–20.08, серия mixed-calib-v2): 6
    жертв плеча уходят в кластер за минуту, дальше плечо девять минут
    работает. Пока темп мерили по сабмитам, окно из 12 штук было ровно двумя
    пачками, и ответ зависел от момента взгляда: 52 строки/ч сразу после
    пачки, 36 в середине плеча при истинных 40 — обещанный финиш прыгал на
    час с лишним туда-сюда каждые несколько минут.

    Синтетика повторяет форму: плечо 660 с, внутри пачка из 6 сабмитов с
    реальными смещениями подачи. Главная проверка — два взгляда в разных
    фазах цикла дают ОДНО число."""
    import time as _t

    from statusserver.progress import recent_pace

    ARM, OFFSETS = 660.0, (0.0, 10.0, 20.0, 30.0, 90.0, 94.0)
    true_rate = len(OFFSETS) / ARM
    problems = []

    def lines(stamps_ids):
        return [
            _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(s))
            + f" INFO submit: job_id={jid} config=A profile=p overcommit=2.0 rep=0"
            for s, jid in stamps_ids
        ]

    def series(last_arm_start, arms=5, shift=0.0):
        """arms плеч подряд; последнее стартовало в last_arm_start."""
        out = []
        for k in range(arms - 1, -1, -1):
            t0 = last_arm_start - k * ARM
            for i, off in enumerate(OFFSETS):
                out.append((t0 + off + shift, f"A-mixed-rep{arms - k:02d}-v{i}"))
        return out

    now = _t.time()
    # Два взгляда: сразу после пачки текущего плеча и в его середине.
    just_after = recent_pace(lines(series(now - OFFSETS[-1] - 5)))
    mid_cycle = recent_pace(lines(series(now - 300.0)))
    if just_after is None or mid_cycle is None:
        problems.append("темп не посчитан на пяти плечах")
    else:
        if abs(just_after - mid_cycle) > 1e-9:
            problems.append(
                f"темп зависит от фазы цикла: сразу после пачки {just_after * 3600:.1f} "
                f"строк/ч, в середине плеча {mid_cycle * 3600:.1f} строк/ч"
            )
        if abs(mid_cycle - true_rate) > 0.03 * true_rate:
            problems.append(
                f"темп {mid_cycle * 3600:.1f} строк/ч вместо истинных "
                f"{true_rate * 3600:.1f} (допуск 3%)"
            )

    # Сдвиг часов лога относительно процесса (страница в контейнере с чужим
    # TZ — ловушка из _marker_ts): расчёт идёт по разностям меток и обязан
    # остаться прежним.
    skewed = recent_pace(lines(series(now - 300.0, shift=3 * 3600)))
    if skewed is None or mid_cycle is None or abs(skewed - mid_cycle) > 1e-9:
        problems.append(f"сдвиг TZ поменял темп: {skewed} вместо {mid_cycle}")

    # Встало: текущее плечо тянется три цикла вместо одного — оценка обязана
    # просесть, а не показывать бодрый темп мёртвого прогона.
    stalled = recent_pace(lines(series(now - 3 * ARM)))
    if stalled is None or stalled > 0.6 * true_rate:
        problems.append(
            f"вставшая серия: темп {stalled} не просел (истинный {true_rate:.5f}/с)"
        )

    # Эталонная фаза: у каждого прогона свой job_id, плечо = один сабмит.
    step = 240.0
    base = [(now - k * step, f"A-default-high-s-base-wrk-b6-rep{k:02d}")
            for k in range(13, -1, -1)]
    got_base = recent_pace(lines(base))
    if got_base is None or abs(got_base - 1 / step) > 0.03 / step:
        problems.append(
            f"эталонная фаза: темп {got_base} вместо {1 / step:.5f}/с"
        )

    if recent_pace(lines(series(now, arms=1))) is not None:
        problems.append("на одном плече темп обязан быть None (мерить нечего)")

    for p in problems:
        print(f"FAIL recent pace: {p}")
    if not problems:
        print("recent pace: ok")
    return bool(problems)


def check_running_scenarios() -> bool:
    """Объём прогона считается по запущенным сценариям, а не по всем в конфиге.

    Регрессия 21.08.2026: серия P2 стартовала с SCENARIOS=feed-mid (один
    уровень подачи из трёх), а страница делила прогресс на все три —
    «этап 1 %, осталось ~1414 мин» на прогоне длиной пять часов. Ошибка
    не безобидная: по такой оценке серию хочется убить как зависшую.
    """
    from statusserver.progress import expected_by_scenario, running_scenarios

    cfg = {
        "configs": ["A"],
        "scheduler_variants": ["default", "packing", "peaks", "trimaran"],
        "repetitions": 10,
        "pressure_scenarios": [
            {"name": "feed-low", "victims": [{"profile": "p", "count": 6}]},
            {"name": "feed-mid", "victims": [{"profile": "p", "count": 6}]},
            {"name": "feed-high", "victims": [{"profile": "p", "count": 9}]},
        ],
    }
    ok = True

    marked = ["=== PRESSURE START 10:19:36 epoch=1787296776 сценарии=feed-mid ==="]
    if running_scenarios(marked) != {"feed-mid"}:
        print("FAIL running_scenarios: пометка сценариев не разобрана")
        ok = False

    plain = ["=== PRESSURE START 10:19:36 epoch=1787296776 ==="]
    if running_scenarios(plain) is not None:
        print("FAIL running_scenarios: маркер без пометки обязан давать None "
              "(старые раннеры гонят все сценарии)")
        ok = False

    full = sum(expected_by_scenario(cfg).values())
    one = sum(expected_by_scenario(cfg, {"feed-mid"}).values())
    if one >= full or one != 240:
        print(f"FAIL expected_by_scenario: один уровень {one}, все {full}")
        ok = False

    if ok:
        print("running_scenarios: ok")
    return ok


def main() -> int:
    failed = False

    if not check_running_scenarios():
        failed = True

    for mod in sorted(PKG.glob("*.py")):
        try:
            py_compile.compile(str(mod), doraise=True)
        except py_compile.PyCompileError as e:
            print(f"FAIL py_compile {mod.name}: {e}")
            failed = True
    print("py_compile: ok")

    node = shutil.which("node")
    for name, d in [("empty", EMPTY_RUN), ("finished", FINISHED_RUN)]:
        html = render_html(d)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        if len(scripts) < 2:
            print(f"FAIL render {name}: ожидались 2 <script>, найдено {len(scripts)}")
            failed = True
            continue
        if not node:
            continue
        for i, js in enumerate(scripts):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js)
                tmp = f.name
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            Path(tmp).unlink()
            if r.returncode != 0:
                print(f"FAIL node --check ({name}, script #{i}):\n{r.stderr}")
                failed = True
    print("node --check: ok" if node else "node --check: пропущен (node не найден)")

    failed = check_costly_counter() or failed
    failed = check_recent_pace() or failed

    if failed:
        return 1
    print("selfcheck: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
