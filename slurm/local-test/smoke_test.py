#!/usr/bin/env python3
"""smoke_test.py — exercises harness/submit/slurm_submit.py against a REAL
slurmctld/slurmd/slurmdbd (see README.md in this dir), not just code review.
This module has never actually run against a live Slurm before — everything
here was previously validated by reading the code only.

Run inside the slurm-local-test container (the harness/ tree is bind-mounted
at /workspace/harness):

    docker exec -u testuser -w /workspace/harness slurm-local-test \\
        /opt/harness-venv/bin/python3 /workspace/slurm/local-test/smoke_test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/workspace/harness")

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from submit import slurm_submit  # noqa: E402

CFG = {
    "slurm": {"poll_interval_seconds": 1, "job_timeout_seconds": 60},
    # Deliberately unreachable — exercises fetch_job_metrics' documented
    # "no-agent" fallback (no Redis/metrics-agent in this smoke test).
    "redis": {"addr": "127.0.0.1:6399"},
    "images": {"workload": "andreyza/geant4:11.2"},
}


def test_completed_path() -> None:
    print("=== TEST 1: COMPLETED path (trivial payload standing in for Pyxis) ===")
    # The real sbatch-template.sh.j2 launches the workload via
    # `srun --container-image=...` (Pyxis/enroot) — not installed in this
    # smoke-test container (that's the partner stand's job, already an open
    # question in docs §8). Swap in a trivial payload so the job can
    # actually reach COMPLETED, to validate submit/poll/parse end to end
    # against real sbatch/squeue/sacct rather than assuming the code is
    # right from reading it.
    tmp_templates = tempfile.mkdtemp()
    (Path(tmp_templates) / "sbatch-template.sh.j2").write_text(
        """#!/usr/bin/env bash
#SBATCH --job-name={{ job_id }}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={{ env.G4_THREADS }}
#SBATCH --mem={{ resources.memory_request }}
#SBATCH --time=00:05:00
#SBATCH --output=/tmp/{{ job_id }}-%j.out
{% if overcommit > 1.0 -%}
#SBATCH --oversubscribe
{% endif -%}
set -euo pipefail
echo "smoke-test payload for {{ job_id }}"
sleep 2
echo "done"
"""
    )
    orig_env = slurm_submit._env
    slurm_submit._env = Environment(loader=FileSystemLoader(tmp_templates))
    try:
        handle = slurm_submit.submit_job(
            job_id="smoketest-completed",
            config="C",
            profile="low-s",
            overcommit=1.0,
            cfg=CFG,
        )
        print(f"submitted slurm_job_id={handle.slurm_job_id}")
        assert handle.slurm_job_id.isdigit(), "sbatch id regex parse failed"

        slurm_submit.wait_for_completion(handle, CFG)
        print(f"completed on node={handle.node!r}")
        assert handle.node, "node not resolved from sacct NodeList"

        row = slurm_submit.record_result(
            handle,
            job_id="smoketest-completed",
            config="C",
            profile="low-s",
            overcommit=1.0,
            rep=0,
            cfg=CFG,
        )
        print("result row:", row)
        assert row["makespan_source"] == "sacct"
        assert row["makespan_s"] and row["makespan_s"] > 0, "makespan_s not parsed"
        assert row["start_ts"] and row["end_ts"], "start_ts/end_ts not parsed"
        assert row["approximation"] == "no-agent", (
            f"expected no-agent (Redis unreachable by design), got {row['approximation']!r}"
        )

        slurm_submit.cleanup(handle)  # no-op on a finished job — must not raise
        print("TEST 1 PASSED\n")
    finally:
        slurm_submit._env = orig_env


def test_failed_path() -> None:
    print("=== TEST 2: FAILED path (real production template, Pyxis absent) ===")
    handle = slurm_submit.submit_job(
        job_id="smoketest-failed",
        config="C",
        profile="low-s",
        overcommit=1.0,
        cfg=CFG,
    )
    print(f"submitted slurm_job_id={handle.slurm_job_id}")
    try:
        try:
            slurm_submit.wait_for_completion(handle, CFG)
        except RuntimeError as e:
            print(f"got expected RuntimeError: {e}")
            assert "not COMPLETED" in str(e)
            print("TEST 2 PASSED\n")
        else:
            raise AssertionError(
                "expected RuntimeError: job cannot succeed without Pyxis/enroot"
            )
    finally:
        slurm_submit.cleanup(handle)


def test_abort_submission_noop() -> None:
    print("=== TEST 3: abort_submission is a no-op, must not raise ===")
    slurm_submit.abort_submission("whatever", CFG)
    print("TEST 3 PASSED\n")


if __name__ == "__main__":
    test_completed_path()
    test_failed_path()
    test_abort_submission_noop()
    print("ALL TESTS PASSED")
