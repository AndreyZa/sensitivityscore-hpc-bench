#!/usr/bin/env python3
"""Печатает разрешённый (extends раскрыт) конфиг серии io-sensitivity с
переопределёнными плечами и числом повторов — для свипа веса (C2).

Свип оптимизирован: эталоны и плечи default/trimaran к весу плагина
sensitivityscore инвариантны, поэтому прогоняются ОДИН раз, а на каждый вес
гоняется только плечо A-sensitivityscore. Это переопределение scheduler_variants:

    python sweep-series-config.py --variants sensitivityscore --reps 5 > /tmp/ss.yaml
    python sweep-series-config.py --variants default,trimaran --reps 5 > /tmp/ref.yaml

output.results_file тоже переопределяется (--results-file), чтобы прогоны
свипа не затирали parquet основной серии.
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
    args = p.parse_args()

    cfg = load_config(BASE)  # extends раскрыт -> плоский dict
    cfg["scheduler_variants"] = args.variants.split(",")
    # repetitions переопределяем и глобально, и в каждом pressure-сценарии
    # (сценарий может нести своё значение, оно приоритетнее глобального).
    cfg["repetitions"] = args.reps
    for sc in cfg.get("pressure_scenarios", []):
        sc["repetitions"] = args.reps
    cfg.setdefault("output", {})["results_file"] = args.results_file

    sys.stdout.write(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
