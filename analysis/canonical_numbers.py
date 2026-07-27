#!/usr/bin/env python3
"""canonical_numbers.py — единственный источник ключевых чисел исследования.

ЗАЧЕМ. Контрасты близнецов были набраны ПРОЗОЙ в пяти документах сразу. Любой
пересчёт требовал ручного обхода всех пяти, и обход неизбежно оказывался
неполным: правка 19.07 обновила две точки из двенадцати, из-за чего сводка
одновременно печатала p = 3.8·10⁻⁸ как результат и объявляла его артефактом
псевдорепликации, а раздел C1 аудита противоречил сам себе через девять строк.

Схема лечения. Числа живут в ОДНОМ версионируемом файле analysis/canonical.json;
из него генерируется markdown-фрагмент, который вставляется в документы между
маркерами; расхождение ловится проверкой --check ещё до чтения человеком.

    make canonical-recompute   пересчитать из ClickHouse (нужен доступ к данным)
    make canonical-sync        вставить фрагмент в документы
    make canonical-check       поймать устаревшие числа (preflight/CI)

ВАЖНО про «устаревшие» значения. ×1.70 и ×6.11 — это отношения ОБЪЕДИНЁННЫХ
медиан; они законны как описательная статистика и лежат внутри доверительных
интервалов rep-level оценок. Устарели не сами цифры, а их роль ЗАЯВЛЕННОГО
контраста и намертво сцепленные с ними p, посчитанные по задачам. Поэтому
--check ловит не числа как таковые, а связки «число + p из счёта по задачам»
и допускает исторические цитирования по явному списку.

Псевдорепликация искажает p, а не точечную оценку: старые точечные значения
были НИЖЕ новых. Формулировка «оценки завышены псевдорепликацией» неверна.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# JSON — ИСТОЧНИК, он версионируется рядом со скриптом. Сгенерированный
# markdown-фрагмент — производное, ему место в report/ (каталог в .gitignore).
JSON_PATH = Path(__file__).resolve().parent / "canonical.json"
REPORT = Path(__file__).resolve().parent / "report"
MD_PATH = REPORT / "canonical.md"

BEGIN = "<!-- canonical:contrasts -->"
END = "<!-- /canonical:contrasts -->"

# --------------------------------------------------------------------------
# Что вообще считается каноническим числом: серия -> как её пересчитать.
# storm_node — ФАКТ постановки эксперимента из конфига серии, а не вывод по
# максимуму давления: сетевая жертва сама поднимает давление своей оси и без
# этого «штормовым» становится её собственный узел (наблюдалось, см. B1).
# --------------------------------------------------------------------------
SERIES = {
    "differentiation": {
        "title": "близнецы, диск",
        "run_label": "stage-differentiation",
        "sensitive": "high-s-io", "twin": "io-insensitive", "axis": "io",
        "storm_node": "worker-192.168.0.9",
    },
    "net-diff": {
        "title": "близнецы, сеть (v1)",
        "run_label": "stage-net-diff",
        "sensitive": "high-s-net", "twin": "net-insensitive", "axis": "net",
        "storm_node": "worker-192.168.0.9",
    },
    "net-diff-v2": {
        "title": "близнецы, сеть (v2, нейтральный приёмник)",
        "run_label": "stage-net-diff-v2",
        "sensitive": "high-s-net", "twin": "net-insensitive", "axis": "net",
        "storm_node": "worker-192.168.0.9",
    },
}

# --------------------------------------------------------------------------
# Устаревшие связки: «значение + p из счёта по 30 задачам». Ищем ИМЕННО p —
# точечные оценки сами по себе законны (см. шапку).
# --------------------------------------------------------------------------
SUPERSEDED_P = ["3.8·10⁻⁸", "1.4·10⁻⁹", "1.5·10⁻⁷", "2·10⁻⁸", "4·10⁻⁹"]

# Второй класс дефекта: величины без метки статистики. Загадка решена
# 27.07.2026: «18 с» и «46 против 54» оказались СРЕДНИМИ net-diff-v2
# (59.1/77.4 и 46.0/53.9), стоявшими рядом с медианами без пометки. В текстах
# теперь только помеченные значения (46,0 против 53,9 и т.п.); появление
# СТАРЫХ немаркированных форм — регресс к тексту до разбора.
UNVERIFIED = ["46 против 54", "ради ~18 с"]

# Исторические цитирования: там старое число ОПИСЫВАЕТ дефект, а не заявляет
# результат. Пара (хвост пути, отличительная подстрока строки).
HISTORICAL = [
    ("analysis/twin_contrast.py", "не производились"),
    ("analysis/twin_contrast.py", "без границ ничего"),
    ("analysis/canonical_numbers.py", ""),          # этот файл — сам про дефект
    ("docs/Аудит и план работ.md", "не производились ни одной строкой кода"),
    ("docs/Аудит и план работ.md", "вычищены из сводки"),
    ("docs/Аудит и план работ.md", "отношения"),
    ("docs/Аудит и план работ.md", "подтверждены кодом по направлению"),
    ("docs/Аудит и план работ.md", "было завышено псевдорепликацией"),
    ("docs/Аудит и план работ.md", "по сети —"),
    ("docs/Сводка результатов STAGE (июль 2026).md", "прежние p вида"),
    ("docs/Сводка результатов STAGE (июль 2026).md", "сняты: они посчитаны по 30 задачам"),
    ("docs/Сводка результатов STAGE (июль 2026).md", "прежние p ="),
    ("docs/Сводка результатов STAGE (июль 2026).md", "были завышены псевдорепликацией"),
    ("docs/Сводка результатов STAGE (июль 2026).md", "Загадка прежних"),
    ("text/slides/make_pptx.py", "p ≈ 10"),          # колонка «было» в таблице
    ("sensitivity-score-cloud-paper/README.md", "реабилитированы"),
    ("Статья (черновик).md", "не выводимые из приведённых медиан"),
]

SCAN_DIRS = ["docs", "analysis", "harness", "scripts"]
# Каталоги вне репозитория, которые проверка обязана видеть: доклад и слайды
# лежат в phd/text, статья вынесена отдельным репозиторием. Пропустить статью
# нельзя — именно в ней проверка ловила доаудитные числа.
SCAN_EXTRA = [Path.home() / "phd" / "text",
              Path.home() / "phd" / "sensitivity-score-cloud-paper"]


def _fmt(v, digits=2):
    return "—" if v is None else f"{v:.{digits}f}"


def load() -> dict:
    if not JSON_PATH.exists():
        raise SystemExit(f"нет {JSON_PATH} — сначала make canonical-recompute")
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def emit_md(data: dict) -> str:
    """Markdown-фрагмент: то, что вставляется в документы."""
    lines = [
        "| величина | оценка | 95% ДИ | p | критерий | наблюдений |",
        "|---|---|---|---|---|---|",
    ]
    for key, r in data["contrasts"].items():
        ci = f"[{_fmt(r.get('ci_lo'))}; {_fmt(r.get('ci_hi'))}]"
        lines.append(
            f"| {r['title']} | ×{_fmt(r.get('ratio'))} | {ci} | "
            f"{r.get('p_str', '—')} | {r.get('test', '—')} | {r.get('n', '—')} |"
        )
    src = data.get("source", "?")
    lines += [
        "",
        f"*Серий в отчётном счёте: **{data.get('series_count', '?')}**. "
        f"Источник чисел: `analysis/canonical_numbers.py` ({src}); "
        "уровень наблюдения — ПОВТОРЕНИЕ. Значения p при малых n суть "
        "дискретные полы критерия Уилкоксона (2/2ⁿ) — минимально достижимые, "
        "а не подогнанные.*",
    ]
    return "\n".join(lines)


def recompute(args) -> dict:
    """Пересчёт из ClickHouse через тот же twin_contrast, что и всегда."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from clickhouse_source import load_from_clickhouse
    from twin_contrast import twin_contrast

    out = {"source": "recomputed-from-clickhouse", "contrasts": {},
           "series_count": args.series_count}
    for key, spec in SERIES.items():
        df = load_from_clickhouse("results", host=args.ch_host, port=args.ch_port,
                                  stand="stage", run_labels=[spec["run_label"]])
        if df.empty:
            print(f"  {key}: нет данных ({spec['run_label']}) — пропуск")
            continue
        df = df[~df["approximation"].astype(str).str.startswith(("error:", "missing"))]
        res = twin_contrast(df, spec["sensitive"], spec["twin"], spec["axis"],
                            storm_node=spec["storm_node"])
        if res.empty:
            print(f"  {key}: пусто после фильтра — пропуск")
            continue
        # Каноническое число — СВОДНАЯ строка (пары всех плеч; пара никогда
        # не пересекает плечо). Первый запуск пересчёта молча брал .iloc[0] —
        # алфавитно это A-default, и канон подменялся поплечевым значением.
        from twin_contrast import POOLED_LABEL
        allrow = res[res["config"] == POOLED_LABEL]
        if allrow.empty:
            print(f"  {key}: нет сводной строки — пропуск"); continue
        row = allrow.iloc[0]
        out["contrasts"][key] = {
            "title": spec["title"], "run_label": spec["run_label"],
            "ratio": float(row.get("ratio", float("nan"))),
            "ci_lo": float(row.get("ci_lo", float("nan"))),
            "ci_hi": float(row.get("ci_hi", float("nan"))),
            "p": float(row.get("p", float("nan"))),
            "p_str": f"{float(row.get('p', float('nan'))):.3f}",
            "test": str(row.get("test", "")),
            "n": int(row.get("n_reps_sensitive", 0)),
        }
        print(f"  {key}: ×{out['contrasts'][key]['ratio']:.2f}")
    JSON_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"записано: {JSON_PATH}")
    return out


def sync(data: dict, check_only: bool = False) -> int:
    """Вставить фрагмент в документы между маркерами."""
    frag = emit_md(data)
    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text(frag + "\n", encoding="utf-8")
    stale = 0
    for path in sorted(ROOT.rglob("*.md")):
        if ".venv" in str(path) or "/report/" in str(path):
            continue
        txt = path.read_text(encoding="utf-8")
        if BEGIN not in txt:
            continue
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END),
                     f"{BEGIN}\n{frag}\n{END}", txt, flags=re.S)
        if new != txt:
            stale += 1
            if check_only:
                print(f"УСТАРЕЛ блок: {path.relative_to(ROOT)}")
            else:
                path.write_text(new, encoding="utf-8")
                print(f"обновлён блок: {path.relative_to(ROOT)}")
    if not check_only and stale == 0:
        print("вставляемые блоки: все актуальны")
    return stale


def _allowed(path: Path, line: str) -> bool:
    sp = str(path)
    for tail, sub in HISTORICAL:
        if sp.endswith(tail) and (sub == "" or sub in line):
            return True
    return False


def check() -> int:
    """Ловит устаревшие p вне списка исторических цитирований."""
    bad = []
    targets = [ROOT / d for d in SCAN_DIRS] + [p for p in SCAN_EXTRA if p.exists()]
    for base in targets:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if (not path.is_file() or ".venv" in str(path) or ".git" in str(path)
                    or path.suffix not in (".md", ".py", ".sh", ".yaml", ".yml")):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(lines, 1):
                for tok in SUPERSEDED_P + UNVERIFIED:
                    if tok in line and not _allowed(path, line):
                        bad.append((path, i, tok, line.strip()[:90]))
    if bad:
        print("НАЙДЕНЫ доаудитные числа вне исторических цитирований:\n")
        for path, i, tok, line in bad:
            kind = "устаревший p" if tok in SUPERSEDED_P else "не выводится из медиан"
            print(f"  {path}:{i}  [{tok}: {kind}]  {line}")
        print("\nЛибо пересчитать по исходным данным, либо, если это ОПИСАНИЕ")
        print("дефекта, внести место в HISTORICAL в analysis/canonical_numbers.py.")
        return 1
    print(f"доаудитных чисел не найдено (проверено {len(targets)} корней, "
          f"{len(SUPERSEDED_P) + len(UNVERIFIED)} признаков)")
    return 0


def _self_test() -> int:
    ok = True
    data = {"source": "test", "series_count": 10, "contrasts": {
        "x": {"title": "т", "ratio": 1.73, "ci_lo": 1.65, "ci_hi": 1.84,
              "p_str": "0.008", "test": "wilcoxon(n=8)", "n": 8}}}
    md = emit_md(data)
    for want in ("×1.73", "[1.65; 1.84]", "0.008", "**10**"):
        passed = want in md
        ok &= passed
        print(f"  {'OK ' if passed else 'НЕТ'} фрагмент содержит {want}")
    passed = _allowed(Path("a/analysis/twin_contrast.py"), "не производились")
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} историческое цитирование распознано")
    passed = not _allowed(Path("a/docs/Прочее.md"), "результат p = 3.8·10⁻⁸")
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} постороннее место НЕ в списке")
    passed = bool(UNVERIFIED) and all(t not in SUPERSEDED_P for t in UNVERIFIED)
    ok &= passed
    print(f"  {'OK ' if passed else 'НЕТ'} два класса дефекта не пересекаются")
    print("\nсамопроверка:", "пройдена" if ok else "ПРОВАЛЕНА")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recompute", action="store_true", help="пересчитать из ClickHouse")
    p.add_argument("--sync", action="store_true", help="вставить фрагмент в документы")
    p.add_argument("--check", action="store_true", help="поймать устаревшие p")
    p.add_argument("--emit", action="store_true", help="напечатать фрагмент")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--ch-host", default="localhost"), p.add_argument("--ch-port", type=int, default=8123)
    p.add_argument("--series-count", type=int, default=10)
    a = p.parse_args()

    if a.self_test:
        return _self_test()
    if a.recompute:
        data = recompute(a)
        return sync(data)
    if a.check:
        rc = check()
        rc |= 1 if sync(load(), check_only=True) else 0
        return rc
    if a.sync:
        return 1 if sync(load()) and False else 0
    if a.emit:
        print(emit_md(load()))
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
