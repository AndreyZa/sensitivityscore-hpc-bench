#!/usr/bin/env python3
"""check-redis-contract.py — сверить имена полей Redis во всех трёх копиях.

Отказ, ради которого это написано, не падает, а деградирует в правдоподобие:
все читатели подставляют 0.0 на отсутствующее поле (осознанно — иначе version
skew агента ронял бы regret серии в NaN), поэтому переименование поля в одном
месте не вызывает ошибку. Планировщик видит нулевое давление на всех узлах,
раздаёт одинаковый score, плечо A-sensitivityscore молча вырождается в
default, а серия отрабатывает часы и выдаёт «различий нет».

Проверяются два инварианта:
  1. каждое имя из контракта присутствует в своём файле (ловит переименование);
  2. читаемые поля ⊆ записываемых (ловит читателя, ждущего то, чего никто не
     пишет — например поле, добавленное в плагин раньше, чем в агент).

Форк планировщика лежит отдельным репозиторием; если его нет на диске, эта
часть пропускается с предупреждением, а не роняет проверку — путь задаётся
через --plugin-repo или SCHEDULER_PLUGINS_REPO.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

DEFAULT_PLUGIN_REPO = Path.home() / "phd" / "scheduler-plugins"


def quoted_names(text: str) -> set[str]:
    """Все строковые литералы файла — и в Go ("x"), и в Python ("x" / 'x')."""
    return set(re.findall(r'"([^"\n]+)"', text)) | set(re.findall(r"'([^'\n]+)'", text))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--contract", type=Path, default=Path(__file__).resolve().parent.parent / "contract" / "redis-fields.yaml")
    p.add_argument("--plugin-repo", type=Path,
                   default=Path(os.environ.get("SCHEDULER_PLUGINS_REPO", DEFAULT_PLUGIN_REPO)))
    args = p.parse_args()

    repo = args.contract.resolve().parent.parent
    spec = yaml.safe_load(args.contract.read_text(encoding="utf-8"))

    problems: list[str] = []
    skipped: list[str] = []
    checked = 0

    for family, fam in spec.items():
        writes: set[str] = set()
        reads: dict[str, set[str]] = {}

        for name, src in fam["sources"].items():
            base = args.plugin_repo if src.get("repo") == "scheduler-plugins" else repo
            path = base / src["path"]
            fields = set(src["fields"])
            if src["role"] == "writes":
                writes |= fields
            else:
                reads[name] = fields

            if not path.exists():
                skipped.append(f"{family}/{name}: нет файла {path}")
                continue

            present = quoted_names(path.read_text(encoding="utf-8"))
            missing = sorted(fields - present)
            checked += 1
            if missing:
                problems.append(
                    f"{family}/{name} ({src['path']}): в файле нет полей {missing} — "
                    f"переименовали здесь и забыли в контракте/других копиях?"
                )

        # Инвариант «читатели ⊆ писатель» проверяем по контракту: он не зависит
        # от того, доступен ли на диске репозиторий плагина.
        for name, fields in reads.items():
            orphan = sorted(fields - writes)
            if orphan:
                problems.append(
                    f"{family}/{name}: читает поля, которых никто не пишет: {orphan} — "
                    f"читатель получит 0.0 и не заметит этого"
                )

    for s in skipped:
        print(f"ПРОПУЩЕНО: {s}")
    if skipped and args.plugin_repo == DEFAULT_PLUGIN_REPO:
        print(f"  (репозиторий плагина ищется в {DEFAULT_PLUGIN_REPO}; "
              f"переопределяется SCHEDULER_PLUGINS_REPO=<путь>)")

    if problems:
        print("\nКОНТРАКТ REDIS-ПОЛЕЙ НАРУШЕН:")
        for pr in problems:
            print(f"  - {pr}")
        print("\nЭто НЕ уронит серию — читатели подставят 0.0 и данные будут")
        print("правдоподобными. Починить до запуска: contract/redis-fields.yaml")
        return 1

    print(f"контракт Redis-полей цел: {checked} источников сверено"
          + (f", {len(skipped)} пропущено" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
