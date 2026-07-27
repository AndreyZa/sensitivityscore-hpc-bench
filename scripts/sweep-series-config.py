#!/usr/bin/env python3
"""Печатает разрешённый (extends раскрыт) конфиг серии io-sensitivity с
переопределёнными плечами и числом повторов — для свипа веса (C2).

Свип оптимизирован: эталоны и плечи default/trimaran к весу плагина
sensitivityscore инвариантны, поэтому прогоняются ОДИН раз, а на каждый вес
гоняется только плечо A-sensitivityscore. Это переопределение scheduler_variants:

    python sweep-series-config.py --variants sensitivityscore --reps 5 > /tmp/ss.yaml
    python sweep-series-config.py --variants default,trimaran --reps 5 > /tmp/ref.yaml

output.results_file и output.baselines_file тоже переопределяются, чтобы
прогоны свипа не затирали parquet основной серии. baselines_file — не
косметика: он наследовался от config-stage-io-sensitivity.yaml, а эталоны
пишутся простым to_parquet без ротации (run_experiment.py), поэтому REF-фаза
свипа МОЛЧА затирала июльские эталоны io-sensitivity. Заодно расходились имена:
weight-sweep.sh грузил в ClickHouse baselines-sweep-ref.parquet, которого не
существовало, и знаменатели slowdown для оракула в базу не попадали (ошибка
всплыла бы только на анализе, через 5-8 часов прогона).
"""

import argparse
import sys
from pathlib import Path

import yaml

HARNESS = Path(__file__).resolve().parent.parent / "harness"
sys.path.insert(0, str(HARNESS))
from config_loader import load_config  # noqa: E402

BASE = HARNESS / "config-stage-io-sensitivity.yaml"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--variants", required=True,
                   help="плечи через запятую: sensitivityscore | default,trimaran")
    p.add_argument("--reps", type=int, required=True)
    p.add_argument("--results-file", required=True)
    p.add_argument("--baselines-file",
                   help="по умолчанию — results-файл с префиксом baselines- "
                        "вместо results- (соглашение остальных серий)")
    args = p.parse_args()

    baselines_file = args.baselines_file
    if not baselines_file:
        stem = args.results_file
        baselines_file = ("baselines-" + stem[len("results-"):]
                          if stem.startswith("results-") else "baselines-" + stem)

    cfg = load_config(BASE)  # extends раскрыт -> плоский dict
    cfg["scheduler_variants"] = args.variants.split(",")
    # repetitions переопределяем и глобально, и в каждом pressure-сценарии
    # (сценарий может нести своё значение, оно приоритетнее глобального).
    cfg["repetitions"] = args.reps
    for sc in cfg.get("pressure_scenarios", []):
        sc["repetitions"] = args.reps
    cfg.setdefault("output", {})["results_file"] = args.results_file
    cfg["output"]["baselines_file"] = baselines_file

    sys.stdout.write(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
