"""slurm_submit.py — submission backend for config C (classical Slurm, docs §4:
"C (Slurm) — sbatch со скриптом ... ожидание через squeue/sacct, makespan берётся
из sacct -j <id> --format=Elapsed").
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from profiles import PROFILES
from submit.redis_metrics import fetch_job_metrics

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))

_SBATCH_ID_RE = re.compile(r"Submitted batch job (\d+)")


class SlurmJobHandle:
    def __init__(self, job_id: str, slurm_job_id: str):
        self.job_id = job_id
        self.slurm_job_id = slurm_job_id
        self.node: str | None = None
        self.submit_time: float | None = None  # harness wall clock, sbatch returned


def submit_job(
    job_id: str, config: str, profile: str, overcommit: float, cfg: dict
) -> SlurmJobHandle:
    spec = PROFILES[profile]

    template = _env.get_template("sbatch-template.sh.j2")
    script = template.render(
        job_id=job_id,
        profile=profile,
        overcommit=overcommit,
        image=cfg["images"]["workload"],
        env=spec.env,
        resources=spec.resources,
    )

    with tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False) as f:
        f.write(script)
        script_path = f.name

    result = subprocess.run(
        ["sbatch", script_path], check=True, capture_output=True, text=True
    )
    match = _SBATCH_ID_RE.search(result.stdout)
    if not match:
        raise RuntimeError(f"could not parse sbatch output: {result.stdout!r}")

    handle = SlurmJobHandle(job_id=job_id, slurm_job_id=match.group(1))
    handle.submit_time = time.time()
    return handle


def wait_for_completion(handle: SlurmJobHandle, cfg: dict) -> None:
    poll_interval = cfg["slurm"]["poll_interval_seconds"]
    timeout = cfg["slurm"]["job_timeout_seconds"]
    deadline = time.time() + timeout

    while time.time() < deadline:
        result = subprocess.run(
            ["squeue", "-j", handle.slurm_job_id, "-h", "-o", "%T"],
            capture_output=True,
            text=True,
        )
        state = result.stdout.strip()
        if state == "":  # no longer in the queue -> finished (completed or failed)
            break
        time.sleep(poll_interval)
    else:
        raise TimeoutError(
            f"Slurm job {handle.slurm_job_id} did not complete within {timeout}s"
        )

    # Resolve the node it ran on, for the Redis job:metrics lookup.
    result = subprocess.run(
        [
            "sacct",
            "-j",
            handle.slurm_job_id,
            "--format=NodeList",
            "--noheader",
            "--parsable2",
        ],
        capture_output=True,
        text=True,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    handle.node = lines[0].strip() if lines else None


def _parse_sacct_time(raw: str) -> float | None:
    """sacct Start/End timestamp (e.g. 2026-07-10T16:52:35, cluster-local tz)
    -> unix. sacct prints "Unknown"/"None" for jobs that never started."""
    if not raw or raw in ("Unknown", "None"):
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def record_result(
    handle: SlurmJobHandle,
    job_id: str,
    config: str,
    profile: str,
    overcommit: float,
    rep: int,
    cfg: dict,
) -> dict:
    # Elapsed (pure runtime, no queue wait) is the makespan — the same
    # definition the K8s backend now uses via container terminated times, so
    # H3/H4 compare like with like. Start/End land in start_ts/end_ts so
    # analysis can verify batch members actually overlapped on the node.
    result = subprocess.run(
        [
            "sacct",
            "-j",
            handle.slurm_job_id,
            "--format=Elapsed,Start,End",
            "--noheader",
            "--parsable2",
        ],
        capture_output=True,
        text=True,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    makespan_s = float("nan")
    start_ts: float | None = None
    end_ts: float | None = None
    if lines:
        elapsed_raw, _, times = lines[0].partition("|")
        start_raw, _, end_raw = times.partition("|")
        makespan_s = _parse_elapsed(elapsed_raw)
        start_ts = _parse_sacct_time(start_raw)
        end_ts = _parse_sacct_time(end_raw)

    metrics = fetch_job_metrics(cfg["redis"]["addr"], job_id, handle.node or "unknown")

    return {
        "config": config,
        "profile": profile,
        "overcommit": overcommit,
        "rep": rep,
        "node": handle.node,
        "makespan_s": makespan_s,
        "makespan_source": "sacct",
        "submit_ts": handle.submit_time,
        "start_ts": start_ts,
        "end_ts": end_ts,
        **metrics,
    }


def _parse_elapsed(elapsed: str) -> float:
    """Parse Slurm's [DD-]HH:MM:SS Elapsed format into seconds."""
    days = 0
    if "-" in elapsed:
        day_part, elapsed = elapsed.split("-", 1)
        days = int(day_part)
    parts = [int(p) for p in elapsed.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def cleanup(handle: SlurmJobHandle) -> None:
    """scancel the job if it's still in the queue. For a normally completed job
    this is a no-op (scancel on a finished job just returns an error we
    ignore); the case that matters is cleanup after a wait timeout — a job
    left running would keep loading the node through the following plan
    points. Called by run_experiment.py in a finally block."""
    subprocess.run(
        ["scancel", handle.slurm_job_id],
        check=False,
        capture_output=True,
    )


def abort_submission(job_id: str, cfg: dict) -> None:
    """Nothing to clean for Slurm: a failed sbatch doesn't enqueue anything we
    could address by our job_id (the Slurm job id is only known from sbatch's
    stdout, which we didn't get)."""
    return
