#!/usr/bin/env python3
"""run_experiment.py — experiment orchestration harness (docs §4).

Iterates over the full plan matrix (config × profile × overcommit × repetition),
dispatches submission to the right backend (k8s_submit / slurm_submit) per
config, waits for completion, records makespan + job:metrics:* from Redis into a
Parquet dataset matching the §5.1 schema (extended with batch_size/batch_index —
see below):

    config | profile | overcommit | rep | node | makespan_s | makespan_source |
    submit_ts | start_ts | end_ts | llc_miss_rate | numa_remote_ratio | net_bw |
    io_iops | approximation | batch_size | batch_index

makespan_s is the job's pure runtime measured by the cluster itself — pod
container terminated startedAt->finishedAt for K8s backends, sacct Elapsed for
Slurm — so K8s and Slurm configs are compared on the same definition (queue
wait, image pull and pod startup excluded; harness-side wall clock is only a
tagged fallback, see makespan_source). submit_ts/start_ts/end_ts let analysis
verify that batch members actually overlapped in time on the node.

IMPORTANT (fixed after a pilot run showed no real interference effect):
overcommit ratio now actually drives how many jobs run CONCURRENTLY for a
given plan point — previously it was purely a label (job_id/log/results
column) with zero effect on submission behavior, meaning every job ran in
total isolation regardless of its overcommit value, so H1's core
co-location scenario was never actually being exercised. See
batch_size_for() / node_capacity_jobs in config.yaml.

Usage:
    python run_experiment.py --config config.yaml [--pilot] [--configs A] [--dry-run]

--pilot restricts to 1 plan point (profile=high-s, overcommit=2.0) x 3 reps on
config A, matching the checklist item 9 sanity-check ("Прогнать пилотную серию
(1 точка плана, 3 повтора) на A — sanity-check всего пайплайна перед полной
матрицей").
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(
    0, str(Path(__file__).parent)
)  # allow `from submit import ...` / `from profiles import ...`

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


# Delay before the single submit-phase retry — short on purpose: a member
# resubmitted much later would no longer run concurrently with its batch.
RESUBMIT_DELAY_SECONDS = 5


def run_one(
    job_id: str,
    config: str,
    profile: str,
    overcommit: float,
    rep: int,
    cfg: dict,
    dry_run: bool,
) -> dict:
    """Submit one job, wait, record, clean up.

    Retry policy (replaces the old blanket tenacity retry around the whole
    submit/wait/record cycle): only the SUBMIT step is retried, once. A retry
    after a wait-phase failure would rerun the job after the rest of its batch
    already finished — measured in isolation but recorded under the batch's
    overcommit label, a poisoned data point for H1; and re-applying a K8s Job
    name whose first attempt already completed/failed (backoffLimit=0) doesn't
    rerun anything, yielding a bogus near-zero makespan. A submit-phase retry
    is safe: the job hasn't started, so after cleanup of any half-created
    object it still runs concurrently with its batch.
    """
    backend_name = backend_for(config, cfg)
    backend = BACKENDS[backend_name]

    log.info(
        "submit: job_id=%s config=%s profile=%s overcommit=%s rep=%s backend=%s",
        job_id,
        config,
        profile,
        overcommit,
        rep,
        backend_name,
    )

    if dry_run:
        return {
            "config": config,
            "profile": profile,
            "overcommit": overcommit,
            "rep": rep,
            "node": "dry-run",
            "makespan_s": 0.0,
            "makespan_source": "dry-run",
            "submit_ts": None,
            "start_ts": None,
            "end_ts": None,
            "llc_miss_rate": float("nan"),
            "numa_remote_ratio": float("nan"),
            "net_bw": float("nan"),
            "io_iops": float("nan"),
            "approximation": "dry-run",
        }

    try:
        handle = backend.submit_job(job_id, config, profile, overcommit, cfg)
    except Exception as exc:  # noqa: BLE001 — one quick submit retry, see docstring
        log.warning(
            "submit failed for %s (%s) — cleaning up and retrying once", job_id, exc
        )
        backend.abort_submission(job_id, cfg)
        time.sleep(RESUBMIT_DELAY_SECONDS)
        handle = backend.submit_job(job_id, config, profile, overcommit, cfg)

    try:
        backend.wait_for_completion(handle, cfg)
        return backend.record_result(
            handle, job_id, config, profile, overcommit, rep, cfg
        )
    finally:
        # Always clean up, including on wait timeout/failure — a job left
        # running would keep loading the node and contaminate the next plan
        # points (the cooldown between points assumes an idle cluster).
        backend.cleanup(handle)


def batch_size_for(overcommit: float, cfg: dict) -> int:
    """Translates an overcommit ratio into an actual number of CONCURRENT jobs
    to submit for one plan point (docs §4: overcommit ratio — "способ упаковки
    job на узел", е.g. ratio 1.5 на узел с capacity на 4 параллельных job
    сабмитим 6). node_capacity_jobs (harness/config.yaml) is the tunable
    baseline — adjust it to match the real per-node job capacity of your
    stand; the default (2) is a conservative placeholder chosen so that
    overcommit=1.0/1.5/2.0 stay visibly distinct (2/3/4 concurrent jobs)
    without needing real hardware capacity numbers for local dev testing.
    """
    base = cfg.get("node_capacity_jobs", 1)
    return max(1, round(base * overcommit))


def run_batch(
    config: str, profile: str, overcommit: float, rep: int, cfg: dict, dry_run: bool
) -> list[dict]:
    """Submits batch_size_for(overcommit, cfg) CONCURRENT jobs sharing this
    plan point, so overcommit > 1.0 actually creates real co-location
    contention on the target node(s) — this is the fix for the gap found
    after the first pilot run: overcommit used to be purely a label with no
    effect on submission behavior, so every job ran in complete isolation
    regardless of its value, meaning H1's core co-location scenario was
    never actually being exercised."""
    size = batch_size_for(overcommit, cfg)
    base_job_id = make_job_id(config, profile, overcommit, rep)

    def _run_member(batch_index: int) -> dict:
        job_id = base_job_id if size == 1 else f"{base_job_id}-b{batch_index}"
        row = run_one(job_id, config, profile, overcommit, rep, cfg, dry_run)
        row["batch_size"] = size
        row["batch_index"] = batch_index
        return row

    if size == 1:
        return [_run_member(0)]

    log.info(
        "batch: %s x%d concurrent jobs (overcommit=%s -> node_capacity_jobs=%s)",
        base_job_id,
        size,
        overcommit,
        cfg.get("node_capacity_jobs", 1),
    )

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=size) as pool:
        futures = {pool.submit(_run_member, i): i for i in range(size)}
        for future in concurrent.futures.as_completed(futures):
            batch_index = futures[future]
            try:
                rows.append(future.result())
            except (
                Exception
            ) as exc:  # noqa: BLE001 — one batch member failing shouldn't lose the rest
                log.error(
                    "batch member %s (index %d) failed after retries: %s",
                    base_job_id,
                    batch_index,
                    exc,
                )
                rows.append(
                    {
                        "config": config,
                        "profile": profile,
                        "overcommit": overcommit,
                        "rep": rep,
                        "node": None,
                        "makespan_s": float("nan"),
                        "makespan_source": None,
                        "submit_ts": None,
                        "start_ts": None,
                        "end_ts": None,
                        "llc_miss_rate": float("nan"),
                        "numa_remote_ratio": float("nan"),
                        "net_bw": float("nan"),
                        "io_iops": float("nan"),
                        "approximation": f"error:{exc}",
                        "batch_size": size,
                        "batch_index": batch_index,
                    }
                )
    return rows


def build_plan(cfg: dict, pilot: bool, only_configs: list[str] | None) -> list[tuple]:
    configs = expand_configs(cfg)
    if only_configs:
        configs = [
            c for c in configs if c.split("-")[0] in only_configs or c in only_configs
        ]

    if pilot:
        # Single plan point, 3 reps, config A only (checklist item 9).
        pilot_configs = [c for c in configs if c.startswith("A")] or configs[:1]
        return [(c, "high-s", 2.0, rep) for c in pilot_configs for rep in range(3)]

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
    parser.add_argument(
        "--config", default="config.yaml", help="Path to harness config.yaml"
    )
    parser.add_argument(
        "--pilot", action="store_true", help="Run the pilot sanity-check series only"
    )
    parser.add_argument(
        "--configs", nargs="*", help="Restrict to these configs, e.g. --configs A"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the plan and log it without submitting anything",
    )
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    plan = build_plan(cfg, pilot=args.pilot, only_configs=args.configs)
    total_jobs = sum(batch_size_for(overcommit, cfg) for _, _, overcommit, _ in plan)
    log.info(
        "plan has %d points, %d actual job submissions (batch_size varies by overcommit)",
        len(plan),
        total_jobs,
    )

    results_dir = Path(cfg["output"]["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = cfg["output"]["results_file"]
    if args.dry_run:
        # Never clobber real results with a plan preview.
        results_file = f"dry-run-{results_file}"
    results_path = results_dir / results_file

    rows = []
    for config, profile, overcommit, rep in plan:
        job_id = make_job_id(config, profile, overcommit, rep)
        try:
            batch_rows = run_batch(config, profile, overcommit, rep, cfg, args.dry_run)
            rows.extend(batch_rows)
        except Exception as exc:  # noqa: BLE001 — log and continue the matrix
            log.error("job %s failed after retries: %s", job_id, exc)
            rows.append(
                {
                    "config": config,
                    "profile": profile,
                    "overcommit": overcommit,
                    "rep": rep,
                    "node": None,
                    "makespan_s": float("nan"),
                    "makespan_source": None,
                    "submit_ts": None,
                    "start_ts": None,
                    "end_ts": None,
                    "llc_miss_rate": float("nan"),
                    "numa_remote_ratio": float("nan"),
                    "net_bw": float("nan"),
                    "io_iops": float("nan"),
                    "approximation": f"error:{exc}",
                    "batch_size": None,
                    "batch_index": None,
                }
            )

        # Persist incrementally so a crash mid-matrix doesn't lose completed runs.
        pd.DataFrame(rows).to_parquet(results_path, index=False)

        if not args.dry_run:
            time.sleep(cfg["cooldown_seconds"])

    log.info("done: %d results written to %s", len(rows), results_path)


if __name__ == "__main__":
    main()
