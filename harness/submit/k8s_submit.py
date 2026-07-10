"""k8s_submit.py — submission backend for configs A (bare-metal), B (KubeVirt, as a
plain Job for harness simplicity — see note below), and D (Slinky/slurm-bridge).

All three share the same kubectl apply / wait / delete lifecycle; only
schedulerName and the job manifest's env values differ (docs §4: "A / B
(K8s/KubeVirt) — kubectl apply -f сгенерированного из шаблона манифеста").

Note on config B: the harness renders job-template.yaml.j2 (a plain Job) rather
than a VirtualMachineInstance for the automated matrix run, to keep submit/wait/
parse logic uniform across A/B/D. For an actual KubeVirt run, swap RENDER_TEMPLATE
below for k8s/config-b-kubevirt/vmi-*.yaml and adjust wait_for_completion to poll
VMI phase instead of Job status (kubectl get vmi -o jsonpath='{.status.phase}').
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from profiles import PROFILES, Sensitivity
from submit.redis_metrics import fetch_job_metrics

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))

SCHEDULER_NAME_BY_CONFIG = {
    "A-default": "default-scheduler",
    "A-sensitivityscore": "sensitivityscore",
    "B-default": "default-scheduler",
    "B-sensitivityscore": "sensitivityscore",
    "D": "slurm-bridge",
}

# Namespaces we've already confirmed exist this process run — avoids
# re-running `kubectl create namespace` before every single job submission
# across a full matrix (hundreds of calls otherwise); idempotent either way
# since it uses --dry-run=client | apply, but no need to hit the API server
# that often.
_namespaces_ensured: set[str] = set()


def _ensure_namespace(namespace: str) -> None:
    """Auto-creates the namespace if it doesn't exist yet (idempotent).
    Fixes: 'Error from server (NotFound): namespaces "..." not found' on the
    very first job submission against a fresh cluster."""
    if namespace in _namespaces_ensured:
        return
    result = subprocess.run(
        ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "yaml"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["kubectl", "apply", "-f", "-"], input=result.stdout, check=True, text=True
    )
    _namespaces_ensured.add(namespace)


log = logging.getLogger(__name__)


class K8sJobHandle:
    def __init__(self, job_id: str, k8s_name: str, namespace: str):
        self.job_id = job_id
        self.k8s_name = k8s_name
        self.namespace = namespace
        self.node: str | None = None
        self.submit_time: float | None = None  # harness wall clock, kubectl apply
        self.wait_end_time: float | None = None  # harness wall clock, wait returned
        # Container runtime window from the pod's containerStatuses (cluster
        # clock) — the honest makespan, comparable to Slurm's sacct Elapsed.
        self.container_start: float | None = None
        self.container_end: float | None = None


def submit_job(
    job_id: str, config: str, profile: str, overcommit: float, cfg: dict
) -> K8sJobHandle:
    spec = PROFILES[profile]
    namespace = cfg["kubernetes"]["namespace"]
    scheduler_name = SCHEDULER_NAME_BY_CONFIG.get(config, "default-scheduler")

    _ensure_namespace(namespace)

    template = _env.get_template("job-template.yaml.j2")
    manifest = template.render(
        job_id=job_id,
        config=config,
        profile=profile,
        namespace=namespace,
        scheduler_name=scheduler_name,
        image=cfg["images"]["workload"],
        env=spec.env,
        sensitivity=spec.sensitivity,
        resources=spec.resources,
    )

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(manifest)
        manifest_path = f.name

    subprocess.run(["kubectl", "apply", "-f", manifest_path], check=True)

    k8s_name = job_id.lower().replace("_", "-").replace(".", "-")
    handle = K8sJobHandle(job_id=job_id, k8s_name=k8s_name, namespace=namespace)
    handle.submit_time = time.time()
    return handle


def _parse_k8s_time(raw: str) -> float | None:
    """RFC3339 timestamp from the API server (e.g. 2026-07-10T16:52:35Z) -> unix."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def wait_for_completion(handle: K8sJobHandle, cfg: dict) -> None:
    timeout = cfg["kubernetes"]["job_timeout_seconds"]
    subprocess.run(
        [
            "kubectl",
            "wait",
            f"job/{handle.k8s_name}",
            "-n",
            handle.namespace,
            "--for=condition=complete",
            f"--timeout={timeout}s",
        ],
        check=True,
    )
    handle.wait_end_time = time.time()

    # Resolve where and WHEN the pod actually ran: node for the Redis
    # job:metrics:<job_id>:<node> lookup, container terminated start/finish
    # for the makespan. Measuring makespan harness-side (apply -> wait) mixed
    # in scheduling latency, image pull, pod startup and kubectl-wait poll
    # granularity — and was incomparable with Slurm's sacct Elapsed (pure
    # runtime), biasing every K8s-vs-Slurm comparison (H3/H4).
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "pod",
            "-n",
            handle.namespace,
            "-l",
            f"job-name={handle.k8s_name}",
            "-o",
            "jsonpath={.items[0].spec.nodeName}"
            "{'|'}{.items[0].status.containerStatuses[0].state.terminated.startedAt}"
            "{'|'}{.items[0].status.containerStatuses[0].state.terminated.finishedAt}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    node, _, times = result.stdout.strip().partition("|")
    started_raw, _, finished_raw = times.partition("|")
    handle.node = node
    handle.container_start = _parse_k8s_time(started_raw)
    handle.container_end = _parse_k8s_time(finished_raw)


def record_result(
    handle: K8sJobHandle,
    job_id: str,
    config: str,
    profile: str,
    overcommit: float,
    rep: int,
    cfg: dict,
) -> dict:
    if handle.container_start is not None and handle.container_end is not None:
        makespan_s = handle.container_end - handle.container_start
        makespan_source = "container"
        start_ts, end_ts = handle.container_start, handle.container_end
    else:
        # Terminated container status missing (shouldn't happen for a job that
        # passed --for=condition=complete) — fall back to the harness-side
        # apply->wait wall clock rather than dropping the run, but tag it so
        # analysis can exclude/inspect these rows.
        log.warning(
            "job %s: no terminated containerStatuses times — falling back to "
            "harness wall-clock makespan",
            job_id,
        )
        makespan_s = (
            (handle.wait_end_time - handle.submit_time)
            if handle.wait_end_time and handle.submit_time
            else float("nan")
        )
        makespan_source = "wallclock"
        start_ts, end_ts = handle.submit_time, handle.wait_end_time

    metrics = fetch_job_metrics(cfg["redis"]["addr"], job_id, handle.node or "unknown")

    return {
        "config": config,
        "profile": profile,
        "overcommit": overcommit,
        "rep": rep,
        "node": handle.node,
        "makespan_s": makespan_s,
        "makespan_source": makespan_source,
        "submit_ts": handle.submit_time,
        "start_ts": start_ts,
        "end_ts": end_ts,
        **metrics,
    }


def cleanup(handle: K8sJobHandle) -> None:
    """Delete the Job so repeated runs don't accumulate cluster state. Called
    by run_experiment.py in a finally block — including after a wait timeout
    or failure, so a stuck/failed Job's pod doesn't keep loading the node
    while the next plan points run."""
    subprocess.run(
        [
            "kubectl",
            "delete",
            "job",
            handle.k8s_name,
            "-n",
            handle.namespace,
            "--ignore-not-found",
        ],
        check=False,
    )


def abort_submission(job_id: str, cfg: dict) -> None:
    """Best-effort removal of whatever a failed submit_job() attempt may have
    half-created, so run_experiment.py's single submit retry starts clean —
    re-applying a Job name that already exists would not rerun it."""
    k8s_name = job_id.lower().replace("_", "-").replace(".", "-")
    subprocess.run(
        [
            "kubectl",
            "delete",
            "job",
            k8s_name,
            "-n",
            cfg["kubernetes"]["namespace"],
            "--ignore-not-found",
        ],
        check=False,
    )
