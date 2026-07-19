"""Рендер HTML-страницы из словаря collect() (server.py). Единственный файл,
знающий про вёрстку; данные приходят готовыми структурами."""

from __future__ import annotations

import re
from pathlib import Path

from .labels import ARM_ORDER, HERO_ARM, arm_label, esc, scenario_label

PHASE_META = {
    "DONE": ("#22a06b", "завершено"),
    "pressure": ("#e8590c", "основная серия"),
    "baseline": ("#1c7ed6", "эталонные прогоны"),
    "not started": ("#868e96", "ожидание"),
}


def table(headers: list[str], rows: list[list], caption: str = "") -> str:
    if not rows:
        return "<p class='dim'>— пусто —</p>"
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    trs = "".join(
        "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return f"<table>{cap}<tr>{th}</tr>{trs}</table>"


def fmt_dur(minutes) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def hero_now(d: dict) -> str:
    """Крупная строка «что выполняется прямо сейчас»."""
    phase = d["phase"]
    act = d.get("activity") or {}
    reps = d.get("reps") or {}
    if phase == "DONE":
        prog = d.get("progress") or {}
        extra = ""
        if prog.get("duration_min") is not None:
            what = ("эталонные прогоны длились"
                    if prog.get("duration_phase") == "baseline"
                    else "основная серия длилась")
            extra = f" · {what} ~{fmt_dur(prog['duration_min'])}"
            if prog.get("finished_at"):
                extra += f", финиш в {prog['finished_at']}"
        return "Прогон завершён ✓" + esc(extra)
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


def storm_cell(m: dict, is_best: bool, nominal: bool = False) -> str:
    """Ячейка «на перегруженный узел»: N из измеренных (доля %), цвет по доле.
    В смешанном сценарии (nominal) счётчик справочный — совпадение с
    декларированной осью задачи; узел дешёвой оси может быть намеренным
    выбором, поэтому ни расцветки «хуже/лучше», ни звёздочки."""
    pct = m.get("storm_pct")
    if pct is None:
        return "<td class='dim'>—</td>"
    if nominal:
        return (
            f"<td class='dim'><b>{m['storm']}</b>/{m['measured']} "
            f"<span class='pct'>({pct}%)</span></td>"
        )
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
        nominal = bool(info.get("nominal"))
        pcts = [arms[a]["storm_pct"] for a in ordered if arms[a]["storm_pct"] is not None]
        best = min(pcts) if pcts else None
        regs = [arms[a]["regret"] for a in ordered if arms[a]["regret"] is not None]
        best_reg = min(regs) if regs else None

        prog = ""
        if info.get("expected"):
            prog = f" · <span class='dim'>{info['done']}/{info['expected']} измерений</span>"
        parts.append(
            f"<h3>{esc(scenario_label(sc))} "
            f"<small class='dim'>перегружен узел "
            f"{esc(info['storm_node'].replace('worker-', 'w-'))}{prog}</small></h3>"
        )

        storm_col = ("задач на узел своей оси¹" if nominal
                     else "задач на перегруженный узел")
        head = (f"<tr><th>планировщик</th><th>{storm_col}</th>"
                "<th>время выполнения, с</th><th>ошибка размещения</th></tr>")
        body = []
        for a in ordered:
            m = arms[a]
            is_best = (not nominal and best is not None
                       and m.get("storm_pct") == best and len(ordered) > 1)
            reg_best = (nominal and best_reg is not None
                        and m.get("regret") == best_reg and len(ordered) > 1)
            row_cls = " class='hero'" if a == HERO_ARM else ""
            mk = m["makespan"] if m["makespan"] is not None else "—"
            rg = m["regret"] if m["regret"] is not None else "—"
            rg_html = f"{esc(rg)}{' ★' if reg_best else ''}"
            body.append(
                f"<tr{row_cls}><td><b>{esc(arm_label(a))}</b></td>"
                f"{storm_cell(m, is_best, nominal)}"
                f"<td>{esc(mk)}</td><td>{rg_html}</td></tr>"
            )
        parts.append(f"<table class='money'>{head}{''.join(body)}</table>")

        # Вывод одной строкой, когда данные по всем планировщикам уже есть.
        # В смешанном сценарии вердикт по ошибке размещения (номинальный
        # счётчик оси решением не является), в одноосевых — по доле в шторм.
        if nominal:
            if best_reg is not None and arms.get(HERO_ARM, {}).get("regret") is not None:
                hero_reg = arms[HERO_ARM]["regret"]
                others = [
                    f"{arm_label(a)} — {arms[a]['regret']}"
                    for a in ordered
                    if a != HERO_ARM and arms[a]["regret"] is not None
                ]
                good = hero_reg == best_reg
                verdict = "✓ лучший результат" if good else "△ пока не лучший"
                parts.append(
                    f"<p class='takeaway'>Ошибка размещения SensitivityScore — "
                    f"<b>{hero_reg}</b> ({', '.join(others) or '—'}) "
                    f"<span class='{'good' if good else 'warn'}'>{verdict}</span></p>"
                )
        elif best is not None and HERO_ARM in arms and arms[HERO_ARM]["storm_pct"] is not None:
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
        "<p class='note dim'>«Ошибка размещения» показывает, насколько "
        "выбранный узел хуже лучшего из доступных в момент решения: "
        "0 — выбран лучший узел, 1 — худший из возможных. Это главная "
        "метрика качества решения планировщика.<br>"
        "¹ Счётчик «на узел своей оси» — справочный. Дешёвую ось (на этом "
        "стенде — кэш) занимать выгодно, поэтому такое размещение — не "
        "ошибка. Судите по ошибке размещения и времени.<br>"
        "Точное сравнение времени (с учётом разной скорости узлов) — в "
        "секции «Анализ» после прогона.</p>"
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


def method_section() -> str:
    """Краткая методика: кто создаёт нагрузку, что измеряется и в какой фазе.
    Полная версия — docs/Методика измерений.md."""
    return """<details class="card" data-k="method">
<summary>Методика: что и на чём измеряется</summary>
<ul class="method">
<li><b>Эталонная задача Geant4</b> (симуляция частиц) — единственное, что
измеряется: время выполнения, узел размещения, замедление, ошибка
размещения. Профили задают чувствительность: high-s-io — к диску,
high-s-net — к сети, high-s — к кэшу, low-s — контрольный.</li>
<li><b>Генераторы фоновой нагрузки</b> — по одному на ось: диск —
stress-ng (запись), сеть — пары iperf3 (~400 Мбит/с), кэш — stress-ng
(потоковый проход по памяти). Создают «перегруженный узел» и
<b>не измеряются</b>; ресурсов запрашивают минимум, поэтому для
планировщиков без учёта интерференции узел выглядит свободным.</li>
<li><b>Эталонные прогоны</b>: кластер пуст, без генераторов; каждый профиль
изолированно на каждом узле — база замедления и сверка чувствительности.</li>
<li><b>Основная серия</b>: генераторы на одном узле → 30 с стабилизации →
6 задач Geant4 пуассоновским потоком; куда ставить — решает испытуемый
планировщик; измеряются только задачи Geant4.</li>
<li><b>Давление узла</b> измеряет агент на каждом узле (промахи кэша/сек,
ожидание диска PSI, сетевой трафик — нормированные калибровкой стенда),
безотносительно источника нагрузки.</li>
</ul>
<p class="dim">Полная версия — «Методика измерений.md» в docs репозитория.</p>
</details>"""


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


# Человекочитаемые подписи графиков analyze.py: имя файла -> (группа, подпись).
# Порядок групп = порядок показа; главный график механизма — первым.
PLOT_SCENARIOS = {
    "pressure-io": "Диск (IO)",
    "pressure-net": "Сеть (Net)",
    "pressure-llc": "Кэш (LLC)",
    "pressure-mixed3": "Смешанный (кэш+диск+сеть)",
}
PLOT_STEMS = {
    "placement_regret": ("Механизм размещения",
                         "Ошибка размещения по планировщикам"),
    "interference_vs_makespan": ("Механизм размещения",
                                 "Интерференция узла и время выполнения"),
    "cv_comparison": ("Стабильность", "Разброс времени выполнения (CV)"),
    "llc_vs_makespan": ("Оси чувствительности", "Кэш (LLC) и время выполнения"),
    "io_pressure_vs_makespan": ("Оси чувствительности",
                                "Диск (IO) и время выполнения"),
    "net_vs_makespan": ("Оси чувствительности", "Сеть (Net) и время выполнения"),
    "numa_vs_makespan": ("Оси чувствительности", "NUMA и время выполнения"),
    "makespan_boxplot": ("Время выполнения", "Время по конфигурациям"),
}
PLOT_GROUP_ORDER = ["Механизм размещения", "Стабильность",
                    "Оси чувствительности", "Время выполнения", "Прочее"]


def plot_caption(png: str) -> tuple[str, str]:
    """-> (группа, подпись) для файла графика; незнакомые имена — в «Прочее»."""
    base = png.removesuffix(".png")
    scenario = ""
    for suf, label in PLOT_SCENARIOS.items():
        if base.endswith("-" + suf):
            scenario = label
            base = base[: -len(suf) - 1]
            break
    group, title = PLOT_STEMS.get(base, ("Прочее", base))
    return group, (f"{title} — {scenario}" if scenario else title)


def gallery_section(plots: list[str]) -> str:
    """Графики группами по смыслу; клик открывает лайтбокс с листанием
    (сам лайтбокс — статический #lb в конце страницы, вне .wrap)."""
    groups: dict[str, list[str]] = {}
    for png in plots:
        g, cap = plot_caption(png)
        groups.setdefault(g, []).append(
            f"<a href='/report/{esc(png)}' class='thumb' data-cap='{esc(cap)}'>"
            f"<img src='/report/{esc(png)}' alt='{esc(cap)}' loading='lazy'>"
            f"<span>{esc(cap)}</span></a>"
        )
    parts = []
    for g in PLOT_GROUP_ORDER:
        if g in groups:
            parts.append(f"<h4>{esc(g)}</h4><div class='gallery'>{''.join(groups[g])}</div>")
    return "".join(parts)


def report_section(rep: dict) -> str:
    dig_html = digest_section(rep.get("digest") or {})
    if not rep["exists"] and not dig_html:
        return ("<p class='dim'>появится после прогона: "
                f"<code>analyze.py ... --outdir {esc(rep['dir'])}</code></p>")
    parts = []
    if dig_html:
        parts.append(dig_html)
    if rep["plots"]:
        parts.append(
            "<details data-k='plots'><summary>графики "
            f"<span class='dim'>({len(rep['plots'])})</span></summary>"
            f"{gallery_section(rep['plots'])}</details>"
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


def phase_favicon(phase: str, color: str) -> str:
    """Фавиконка-индикатор фазы (data-URI SVG): цветной кружок, при
    завершении — с галочкой. Видно из панели вкладок, закончился ли прогон."""
    c = color.replace("#", "%23")
    check = (
        "<path d='M4.5 8.5l2.5 2.5 4.5-5.5' stroke='white' stroke-width='2' fill='none'/>"
        if phase == "DONE" else ""
    )
    return ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
            f"viewBox='0 0 16 16'><circle cx='8' cy='8' r='7' fill='{c}'/>{check}</svg>")


def warn_banners(d: dict, prog: dict) -> str:
    """Плашки «этим цифрам верить нельзя».

    Все три случая раньше были молчаливыми: страница выглядела полностью
    исправной и показывала неверные числа, что для многочасового прогона
    хуже, чем честная ошибка.
    """
    out = []
    if d.get("config_error"):
        out.append(
            "Конфиг серии не прочитан — объёмы, проценты и план недоступны: "
            f"{esc(d['config_error'])} (файл {esc(d.get('config_path', '?'))})"
        )
    if d.get("topology_unknown"):
        out.append(
            "Список измерительных узлов не получен (kubectl недоступен) — "
            "ожидаемый объём эталонных прогонов неизвестен, доля эталонов в "
            "общем проценте не учтена. Число узлов можно задать явно: "
            "baseline.nodes в конфиге серии."
        )
    if prog.get("incomplete"):
        out.append(
            f"Прогон помечен завершённым, но не хватает {prog['missing_rows']} "
            "строк из ожидаемых. Маркер завершения печатается скриптом серии "
            "безусловно — проверьте ошибки в хвосте лога, прежде чем считать "
            "серию собранной."
        )
    if not out:
        return ""
    items = "".join(f"<div class='warnrow'>⚠ {x}</div>" for x in out)
    return f"<div class='card warnbox'>{items}</div>"


def render_html(d: dict) -> str:
    phase = d["phase"]
    color, phase_word = PHASE_META.get(phase, ("#868e96", phase))
    prog = d.get("progress", {})
    pct = prog.get("overall_pct")

    bar = ""
    if pct is not None:
        eta = (
            f"этап завершится ~{prog['eta']} (осталось ~{prog['eta_minutes']} мин)"
            if "eta" in prog
            else ""
        )
        phase_pct = f"этап «{phase_word}»: {prog['phase_pct']}%" if "phase_pct" in prog else ""
        elapsed = (
            f"идёт уже {fmt_dur(prog['phase_elapsed_min'])}"
            if prog.get("phase_elapsed_min") else ""
        )
        dur = (
            f"длилась {fmt_dur(prog['duration_min'])}"
            if prog.get("duration_min") is not None else ""
        )
        meta = " · ".join(x for x in (phase_pct, elapsed, eta, dur) if x)
        bar = f"""<div class="prog">
<div class="barbg"><div class="bar" style="background:{color};width:{pct}%"></div>
<span class="barlabel">{pct}%</span></div>
<div class="progmeta dim">{esc(meta)}</div></div>"""

    st = d["stand"]
    stand_label = esc(st.get("label") or "стенд")
    title_pct = f" {pct}%" if pct is not None else ""

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{stand_label} · {esc(phase_word)}{title_pct}</title>
<link id="fav" rel="icon" href="{phase_favicon(phase, color)}">
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
  color-scheme:light;
}}
:root[data-theme="dark"]{{
  --bg:#0f1115; --card:#181b21; --ink:#e6e8eb; --dim:#9aa4b2;
  --line:#2a2f3a; --good:#4ade80; --goodbg:#12241a; --warn:#fbbf24;
  --warnbg:#2a1f0a; --bad:#f87171; --badbg:#2a1414; --hero:#12203a; --herobd:#1e3a66;
  color-scheme:dark;
}}
*{{box-sizing:border-box}}
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;
  background:var(--bg);color:var(--ink);line-height:1.5}}
.wrap{{max-width:60em;margin:0 auto;padding:1.2em 1em 4em}}
.top{{display:flex;align-items:center;gap:.7em;flex-wrap:wrap;margin-bottom:.2em}}
.badge{{background:var(--accent);color:#fff;font-weight:600;font-size:.8em;
  padding:.18em .7em;border-radius:999px;text-transform:uppercase;letter-spacing:.03em}}
.top h1{{font-size:1.15em;margin:0;font-weight:600}}
.top .upd{{margin-left:auto;color:var(--dim);font-size:.82em}}
.top .ctl{{display:flex;gap:.35em;align-items:center}}
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
.plan .st.act .mark{{color:var(--accent)}}
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
.warnbox{{border-left:4px solid var(--warn);background:var(--warnbg)}}
.warnrow{{color:var(--warn);font-weight:600;line-height:1.5}}
.warnrow+.warnrow{{margin-top:.5em}}
.good{{color:var(--good)}} .warn{{color:var(--warn)}} .bad{{color:var(--bad)}}
td.good{{background:var(--goodbg)}} td.warn{{background:var(--warnbg)}} td.bad{{background:var(--badbg)}}
.pct{{font-size:.85em;color:var(--dim)}}
.dim{{color:var(--dim)}} .err{{color:var(--bad)}}
.chip{{display:inline-block;background:var(--line);border-radius:999px;
  padding:.15em .7em;font-size:.82em;margin-right:.3em}}
.chip.good{{background:var(--goodbg);color:var(--good)}}
.takeaway{{margin:.3em 0 .8em;font-size:.92em}}
.method{{font-size:.9em;padding-left:1.2em}} .method li{{margin:.35em 0}}
.note{{font-size:.82em;margin-top:.6em}}
details summary{{cursor:pointer;font-size:.85em;margin:.4em 0}}
pre{{background:var(--bg);padding:.7em;overflow-x:auto;font-size:.82em;
  border-radius:8px;border:1px solid var(--line)}}
code{{background:var(--bg);padding:.1em .35em;border-radius:4px;font-size:.9em}}
.gallery{{display:flex;flex-wrap:wrap;gap:.6em;margin-top:.3em}}
.thumb{{border:1px solid var(--line);border-radius:8px;padding:.35em;text-decoration:none;
  color:var(--dim);font-size:.76em;text-align:center;background:var(--card);
  max-width:240px;cursor:zoom-in}}
.thumb img{{display:block;max-height:150px;max-width:100%;border-radius:4px;margin:0 auto .2em}}
.thumb:hover{{border-color:var(--herobd)}}
#lb{{position:fixed;inset:0;z-index:50;background:rgba(10,12,16,.82);
  display:flex;align-items:center;justify-content:center;gap:.6em}}
#lb[hidden]{{display:none}}
#lb figure{{margin:0;max-width:88vw;text-align:center}}
#lb img{{max-width:88vw;max-height:82vh;border-radius:8px;background:#fff}}
#lb figcaption{{color:#e6e8eb;font-size:.9em;margin-top:.5em}}
#lb figcaption a{{color:#9ec2ff;margin-left:.7em;font-size:.85em}}
#lb button{{background:rgba(255,255,255,.12);border:none;color:#fff;font-size:1.5em;
  border-radius:999px;width:1.7em;height:1.7em;cursor:pointer;line-height:1}}
#lb button:hover{{background:rgba(255,255,255,.25)}}
#lbclose{{position:absolute;top:.6em;right:.7em}}
.fullmd{{font-size:.9em;opacity:.92}}
.fullmd table{{font-size:.88em}}
a{{color:#4c8dff}}
.pill{{background:var(--card);border:1px solid var(--line);color:var(--ink);
  border-radius:999px;padding:.2em .8em;font-size:.8em;cursor:pointer;font-family:inherit}}
.pill:hover{{border-color:var(--herobd)}}
select.pill{{padding:.2em .5em}}
#conn{{color:var(--bad);font-weight:600}}
@media (max-width:640px){{ table{{display:block;overflow-x:auto}} }}
</style></head><body><div class="wrap" style="--accent:{color}">

<div class="top">
  <span class="badge">{esc(phase_word)}</span>
  <h1>{stand_label}</h1>
  <span class="upd"><span id="conn"></span>обновлено {esc(d['time'])} · <a href="/json">/json</a></span>
  <span class="ctl">
    <button id="refnow" class="pill" onclick="refreshNow()" title="обновить сейчас">⟳</button>
    <select id="refsel" class="pill" onchange="setRefresh(this.value)" title="интервал авто-обновления">
      <option value="0">без обновления</option>
      <option value="5">5 с</option>
      <option value="10">10 с</option>
      <option value="30">30 с</option>
      <option value="60">1 мин</option>
      <option value="300">5 мин</option>
    </select>
    <button id="themebtn" class="pill" onclick="cycleTheme()" title="тема">🌗</button>
  </span>
</div>
{warn_banners(d, prog)}
<div class="now">{hero_now(d)}</div>
{bar}
{plan_section(d.get('plan') or [])}

<div class="card">
  <h2>Размещение задач по планировщикам</h2>
  {money_section(d['results'])}
</div>

{method_section()}

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
  {table(["узел", "группа", "kubelet", "ядро ОС", "CPU", "память"], st.get("nodes", []))}
  <h4>Последние строки журнала</h4>
  <pre>{esc(chr(10).join(d['log_tail']))}</pre>
  <h4>Последние ошибки в журнале</h4>
  <pre>{esc(chr(10).join(d['log_errors']) or '—')}</pre>
</details>

</div>

<div id="lb" hidden>
  <button id="lbprev" title="предыдущий">‹</button>
  <figure><img id="lbimg" alt="">
    <figcaption><span id="lbcap"></span><a id="lbfile" href="#" target="_blank">файл ↗</a></figcaption>
  </figure>
  <button id="lbnext" title="следующий">›</button>
  <button id="lbclose" title="закрыть (Esc)">✕</button>
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

/* Мягкое авто-обновление вместо location.reload: страница перезапрашивается
   fetch'ем и подменяется только содержимое .wrap — без белой вспышки, с
   сохранением прокрутки и раскрытых <details>. Если сервер недоступен
   (перезапуск между прогонами), остаются последние данные с пометкой
   «нет связи» и попытки продолжаются. Интервал — как в Grafana: селектор
   в шапке, значение в localStorage; в фоновой вкладке обновление спит и
   навёрстывает при возвращении. */
var REFK='ssRefresh', DETK='ssOpenDetails';
var timer=null, lastOk=Date.now();
function refreshSeconds(){{
  var v=parseInt(localStorage.getItem(REFK)||'10',10);
  return isNaN(v)?10:v;
}}
function armTimer(){{
  clearTimeout(timer);
  var s=refreshSeconds();
  if(s>0) timer=setTimeout(refreshNow, s*1000);
}}
function setRefresh(v){{ localStorage.setItem(REFK, String(v)); armTimer(); }}
function openSet(){{
  return [].slice.call(document.querySelectorAll('details[data-k]'))
    .filter(function(d){{return d.open}}).map(function(d){{return d.dataset.k}});
}}
function applyOpen(list){{
  (list||[]).forEach(function(k){{
    var d=document.querySelector('details[data-k="'+k+'"]'); if(d) d.open=true;
  }});
}}
function setConn(lost){{
  var c=document.getElementById('conn');
  if(c) c.textContent=lost?'нет связи с сервером · ':'';
}}
function paintControls(){{
  paintThemeBtn();
  var sel=document.getElementById('refsel');
  if(sel) sel.value=String(refreshSeconds());
}}
function refreshNow(){{
  if(document.hidden){{ armTimer(); return; }}
  clearTimeout(timer);
  fetch(location.pathname+location.search, {{cache:'no-store'}})
    .then(function(r){{ if(!r.ok) throw new Error(r.status); return r.text(); }})
    .then(function(html){{
      var nd=new DOMParser().parseFromString(html,'text/html');
      var w=document.querySelector('.wrap'), nw=nd.querySelector('.wrap');
      if(w&&nw){{
        var open=openSet();
        w.setAttribute('style', nw.getAttribute('style')||'');
        w.innerHTML=nw.innerHTML;
        applyOpen(open);
      }}
      document.title=nd.title;
      var f=document.getElementById('fav'), nf=nd.getElementById('fav');
      if(f&&nf&&f.href!==nf.href) f.href=nf.href;
      lastOk=Date.now();
      setConn(false); paintControls(); armTimer();
    }})
    .catch(function(){{ setConn(true); armTimer(); }});
}}
/* Фоновая вкладка не обновляется; при возвращении — сразу, если данные устарели. */
document.addEventListener('visibilitychange', function(){{
  if(document.hidden) return;
  var s=refreshSeconds();
  if(s>0 && Date.now()-lastOk>s*1000) refreshNow(); else armTimer();
}});
/* Раскрытые <details> переживают и жёсткую перезагрузку (F5). */
document.addEventListener('toggle', function(){{
  try{{ sessionStorage.setItem(DETK, JSON.stringify(openSet())); }}catch(e){{}}
}}, true);
try{{ applyOpen(JSON.parse(sessionStorage.getItem(DETK)||'[]')); }}catch(e){{}}

/* Лайтбокс графиков: клик по миниатюре открывает оверлей с листанием
   (кнопки, стрелки клавиатуры), Esc или клик по фону закрывает. Живёт вне
   .wrap — мягкое обновление страницы его не трогает. */
var lbIdx=-1;
function lbThumbs(){{ return [].slice.call(document.querySelectorAll('a.thumb')); }}
function lbShow(i){{
  var t=lbThumbs(); if(!t.length) return;
  lbIdx=(i+t.length)%t.length;
  document.getElementById('lbimg').src=t[lbIdx].getAttribute('href');
  document.getElementById('lbcap').textContent=t[lbIdx].dataset.cap||'';
  document.getElementById('lbfile').href=t[lbIdx].getAttribute('href');
  document.getElementById('lb').hidden=false;
}}
function lbHide(){{ document.getElementById('lb').hidden=true; lbIdx=-1; }}
document.addEventListener('click', function(e){{
  var a=e.target.closest&&e.target.closest('a.thumb');
  if(a){{ e.preventDefault(); lbShow(lbThumbs().indexOf(a)); return; }}
  if(e.target.id==='lb'||e.target.id==='lbclose') lbHide();
  else if(e.target.id==='lbprev') lbShow(lbIdx-1);
  else if(e.target.id==='lbnext') lbShow(lbIdx+1);
}});
document.addEventListener('keydown', function(e){{
  if(document.getElementById('lb').hidden) return;
  if(e.key==='Escape') lbHide();
  else if(e.key==='ArrowLeft') lbShow(lbIdx-1);
  else if(e.key==='ArrowRight') lbShow(lbIdx+1);
}});

paintControls(); armTimer();
</script>
</body></html>"""
