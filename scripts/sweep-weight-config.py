#!/usr/bin/env python3
"""Печатает scheduler-config.yaml с заданным весом плагина SensitivityScore —
для свипа веса (C2). Меняется ТОЛЬКО профиль sensitivityscore; вес trimaran
(тоже 5) не трогается, иначе плечи перестанут отличаться лишь плагином.

    python sweep-weight-config.py 10 > /tmp/sched-w10.yaml
    kubectl create configmap scheduler-config --from-file=... -n <ns> ...

Комментарии исходника теряются (ConfigMap-у они не нужны); сам версионируемый
k8s/scheduler-config/scheduler-config.yaml НЕ трогается — генерируется временный.
"""

import sys
from pathlib import Path

import yaml

SRC = Path(__file__).resolve().parent.parent / "k8s/scheduler-config/scheduler-config.yaml"


def main() -> int:
    if len(sys.argv) != 2:
        print("использование: sweep-weight-config.py <weight>", file=sys.stderr)
        return 2
    weight = int(sys.argv[1])

    doc = yaml.safe_load(SRC.read_text())
    changed = 0
    for prof in doc.get("profiles", []):
        if prof.get("schedulerName") != "sensitivityscore":
            continue
        for plugin in prof.get("plugins", {}).get("score", {}).get("enabled", []):
            if plugin.get("name") == "SensitivityScore":
                plugin["weight"] = weight
                changed += 1
    if changed != 1:
        print(f"ОШИБКА: ожидал ровно один блок SensitivityScore, нашёл {changed}",
              file=sys.stderr)
        return 1
    sys.stdout.write(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
