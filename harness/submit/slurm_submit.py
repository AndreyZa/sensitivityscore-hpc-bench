"""slurm_submit.py — submission backend for config C (classical Slurm, docs §4:
"C (Slurm) — sbatch со скриптом ... ожидание через squeue/sacct, makespan берётся
из sacct -j <id> --format=Elapsed").
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
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


def submit_job(job_id: str, config: str, profile: str, overcommit: float, cfg: dict) -> SlurmJobHandle:
    spec = PROFILES[profile]

    template = _env.get_template("sbatch-template.sh.j2")
    script = template.render(
        job_id=job_id,
        profile=profile,
        overcommit=overcommit,
        env=spec.env,
        resources=spec.resources,
    )

    with tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False) as f:
        f.write(script)
        script_path = f.name

    result = subprocess.run(["sbatch", script_path], check=True, capture_output=True, text=True)
    match = _SBATCH_ID_RE.search(result.stdout)
    if not match:
        raise RuntimeError(f"could not parse sbatch output: {result.stdout!r}")

    return SlurmJobHandle(job_id=job_id, slurm_job_id=match.group(1))


def wait_for_completion(handle: SlurmJobHandle, cfg: dict) -> None:
    poll_interval = cfg["slurm"]["poll_interval_seconds"]
    timeout = cfg["slurm"]["job_timeout_seconds"]
    deadline = time.time() + timeout

    while time.time() < deadline:
        result = subprocess.run(
            ["squeue", "-j", handle.slurm_job_id, "-h", "-o", "%T"],
            capture_output=True, text=True,
        )
        state = result.stdout.strip()
        if state == "":  # no longer in the queue -> finished (completed or failed)
            break
        time.sleep(poll_interval)
    else:
        raise TimeoutError(f"Slurm job {handle.slurm_job_id} did not complete within {timeout}s")

    # Resolve the node it ran on, for the Redis job:metrics lookup.
    result = subprocess.run(
        ["sacct", "-j", handle.slurm_job_id, "--format=NodeList", "--noheader", "--parsable2"],
        capture_output=True, text=True,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    handle.node = lines[0].strip() if lines else None


def record_result(handle: SlurmJobHandle, job_id: str, config: str, profile: str,
                   overcommit: float, rep: int, cfg: dict) -> dict:
    result = subprocess.run(
        ["sacct", "-j", handle.slurm_job_id, "--format=Elapsed", "--noheader", "--parsable2"],
        capture_output=True, text=True,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    makespan_s = _parse_elapsed(lines[0]) if lines else float("nan")

    metrics = fetch_job_metrics(cfg["redis"]["addr"], job_id, handle.node or "unknown")

    return {
        "config": config,
        "profile": profile,
        "overcommit": overcommit,
        "rep": rep,
        "node": handle.node,
        "makespan_s": makespan_s,
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
    """No-op for Slurm — completed jobs simply age out of squeue/sacct history,
    nothing to delete like a K8s Job object."""
    return
