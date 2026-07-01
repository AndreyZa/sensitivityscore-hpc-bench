#!/usr/bin/env python3
"""run_experiment.py — experiment orchestration harness (docs §4).

Iterates over the full plan matrix (config × profile × overcommit × repetition),
dispatches submission to the right backend (k8s_submit / slurm_submit) per
config, waits for completion, records makespan + job:metrics:* from Redis into a
Parquet dataset matching the §5.1 schema:

    config | profile | overcommit | rep | node | makespan_s | llc_miss_rate |
    numa_remote_ratio | net_bw | io_iops | approximation

Usage:
    python run_experiment.py --config config.yaml [--pilot] [--configs A] [--dry-run]

--pilot restricts to 1 plan point (profile=high-s, overcommit=2.0) x 3 reps on
config A, matching the checklist item 9 sanity-check ("Прогнать пилотную серию
(1 точка плана, 3 повтора) на A — sanity-check всего пайплайна перед полной
матрицей").
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import yaml
from tenacity import retry, stop_after_attempt, wait_fixed

sys.path.insert(0, str(Path(__file__).parent))  # allow `from submit import ...` / `from profiles import ...`

from profiles import make_job_id
from submit import k8s_submit, slurm_submit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_experiment")

BACKENDS = {"k8s": k8s_submit, "slurm": slurm_submit}


def expand_configs(cfg: dict) -> list[str]:
    """Config A is special-cased into A-default / A-sensitivityscore (docs §4:
    direct A/B comparison of scheduler variants within the same infra config,
    needed for H1). Config B likewise gets a default/sensitivityscore split for
    the same reason (H2). C and D run as single variants."""
    expanded = []
    for c in cfg["configs"]:
        if c in ("A", "B"):
            expanded.extend([f"{c}-default", f"{c}-sensitivityscore"])
        else:
            expanded.append(c)
    return expanded


def backend_for(config: str, cfg: dict) -> str:
    base_config = config.split("-")[0]  # "A-default" -> "A"
    return cfg["backends"][base_config]


@retry(stop=stop_after_attempt(2), wait=wait_fixed(10))
def run_one(job_id: str, config: str, profile: str, overcommit: float, rep: int,
            cfg: dict, dry_run: bool) -> dict:
    backend_name = backend_for(config, cfg)
    backend = BACKENDS[backend_name]

    log.info("submit: job_id=%s config=%s profile=%s overcommit=%s rep=%s backend=%s",
              job_id, config, profile, overcommit, rep, backend_name)

    if dry_run:
        return {
            "config": config, "profile": profile, "overcommit": overcommit,
            "rep": rep, "node": "dry-run", "makespan_s": 0.0,
            "llc_miss_rate": float("nan"), "numa_remote_ratio": float("nan"),
            "net_bw": float("nan"), "io_iops": float("nan"), "approximation": "dry-run",
        }

    handle = backend.submit_job(job_id, config, profile, overcommit, cfg)
    backend.wait_for_completion(handle, cfg)
    result = backend.record_result(handle, job_id, config, profile, overcommit, rep, cfg)
    backend.cleanup(handle)
    return result


def build_plan(cfg: dict, pilot: bool, only_configs: list[str] | None) -> list[tuple]:
    configs = expand_configs(cfg)
    if only_configs:
        configs = [c for c in configs if c.split("-")[0] in only_configs or c in only_configs]

    if pilot:
        # Single plan point, 3 reps, config A only (checklist item 9).
        pilot_configs = [c for c in configs if c.startswith("A")] or configs[:1]
        return [
            (c, "high-s", 2.0, rep)
            for c in pilot_configs
            for rep in range(3)
        ]

    plan = []
    for config in configs:
        base_config = config.split("-")[0]
        for profile in cfg["profiles"]:
            for overcommit in cfg["overcommit_ratios"]:
                # slurm-bridge (D) is whole-node exclusive allocation only —
                # co-location / overcommit isn't testable there (Программа
                # экспериментов §3.1). Skip overcommit > 1.0 for D.
                if base_config == "D" and overcommit != 1.0:
                    continue
                for rep in range(cfg["repetitions"]):
                    plan.append((config, profile, overcommit, rep))
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="Path to harness config.yaml")
    parser.add_argument("--pilot", action="store_true", help="Run the pilot sanity-check series only")
    parser.add_argument("--configs", nargs="*", help="Restrict to these configs, e.g. --configs A")
    parser.add_argument("--dry-run", action="store_true", help="Build the plan and log it without submitting anything")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    plan = build_plan(cfg, pilot=args.pilot, only_configs=args.configs)
    log.info("plan has %d points", len(plan))

    results_dir = Path(cfg["output"]["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = results_dir / cfg["output"]["results_file"]

    rows = []
    for config, profile, overcommit, rep in plan:
        job_id = make_job_id(config, profile, overcommit, rep)
        try:
            row = run_one(job_id, config, profile, overcommit, rep, cfg, args.dry_run)
            rows.append(row)
        except Exception as exc:  # noqa: BLE001 — log and continue the matrix
            log.error("job %s failed after retries: %s", job_id, exc)
            rows.append({
                "config": config, "profile": profile, "overcommit": overcommit,
                "rep": rep, "node": None, "makespan_s": float("nan"),
                "llc_miss_rate": float("nan"), "numa_remote_ratio": float("nan"),
                "net_bw": float("nan"), "io_iops": float("nan"),
                "approximation": f"error:{exc}",
            })

        # Persist incrementally so a crash mid-matrix doesn't lose completed runs.
        pd.DataFrame(rows).to_parquet(results_path, index=False)

        if not args.dry_run:
            time.sleep(cfg["cooldown_seconds"])

    log.info("done: %d results written to %s", len(rows), results_path)


if __name__ == "__main__":
    main()
