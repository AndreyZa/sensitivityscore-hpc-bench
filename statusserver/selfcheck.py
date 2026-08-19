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
    """Темп не должен верить всплеску пачечной подачи.

    Регрессия, ради которой это здесь: в смешанном сценарии 6 жертв уходят в
    кластер за минуту, дальше плечо десять минут работает. Окно из последних
    12 сабмитов — это ровно две пачки, и если мерить его по РАЗНИЦЕ МЕТОК,
    рабочее время плеча в замер почти не попадает: темп выходит в полтора
    раза бодрее настоящего, а страница обещает финиш на два часа раньше
    (19.08, серия mixed-calib-v2 — «завершится ~02:26» при реальных ~05:00).

    Синтетика повторяет ту же форму: плечо 660 с, внутри пачка из 6 сабмитов
    с реальными смещениями подачи."""
    import time as _t

    from statusserver.progress import recent_pace

    ARM, OFFSETS = 660.0, (0.0, 10.0, 20.0, 30.0, 90.0, 94.0)
    true_rate = len(OFFSETS) / ARM

    def lines(stamps):
        return [
            _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(s))
            + f" INFO submit: job_id=J{i} config=A profile=p overcommit=2.0 rep=0"
            for i, s in enumerate(stamps)
        ]

    # «Сейчас» — середина текущего плеча: последняя пачка ушла 300 с назад.
    now = _t.time()
    last_arm = now - 300.0 - OFFSETS[-1]
    stamps = [
        last_arm - k * ARM + off
        for k in range(3, -1, -1)
        for off in OFFSETS
    ]
    got = recent_pace(lines(stamps))

    problems = []
    if got is None:
        problems.append("темп не посчитан на 24 сабмитах")
    else:
        tail = stamps[-12:]
        by_marks = (len(tail) - 1) / (tail[-1] - tail[0])   # прежняя формула
        if got >= by_marks:
            problems.append(
                f"всплеск не погашен: {got:.5f}/с >= замера по меткам {by_marks:.5f}/с"
            )
        if not 0.7 * true_rate <= got <= 1.4 * true_rate:
            problems.append(
                f"темп {got:.5f}/с далёк от истинного {true_rate:.5f}/с "
                f"(допуск 0.7–1.4×)"
            )

    # Часы лога впереди часов процесса (страница в контейнере с чужим TZ):
    # абсолютное сравнение с «сейчас» дало бы отрицательный интервал, а с ним
    # ETA в прошлом. Ожидаем откат к разнице меток — она от зоны не зависит.
    skewed = [s + 3 * 3600 for s in stamps]
    got_skew = recent_pace(lines(skewed))
    tail_s = skewed[-12:]
    want_skew = (len(tail_s) - 1) / (tail_s[-1] - tail_s[0])
    if got_skew is None or abs(got_skew - want_skew) > 1e-9:
        problems.append(
            f"сдвиг TZ: ожидался откат к меткам {want_skew:.5f}/с, получено {got_skew}"
        )

    if recent_pace(lines(stamps[:3])) is not None:
        problems.append("на трёх сабмитах темп обязан быть None (нечего мерить)")

    for p in problems:
        print(f"FAIL recent pace: {p}")
    if not problems:
        print("recent pace: ok")
    return bool(problems)


def main() -> int:
    failed = False

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
