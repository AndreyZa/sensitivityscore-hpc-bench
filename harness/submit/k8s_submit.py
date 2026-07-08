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

import subprocess
import tempfile
import time
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


class K8sJobHandle:
    def __init__(self, job_id: str, k8s_name: str, namespace: str):
        self.job_id = job_id
        self.k8s_name = k8s_name
        self.namespace = namespace
        self.node: str | None = None
        self.start_time: float | None = None
        self.end_time: float | None = None


def submit_job(job_id: str, config: str, profile: str, overcommit: float, cfg: dict) -> K8sJobHandle:
    spec = PROFILES[profile]
    namespace = cfg["kubernetes"]["namespace"]
    scheduler_name = SCHEDULER_NAME_BY_CONFIG.get(config, "default-scheduler")

    template = _env.get_template("job-template.yaml.j2")
    manifest = template.render(
        job_id=job_id,
        config=config,
        profile=profile,
        namespace=namespace,
        scheduler_name=scheduler_name,
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
    handle.start_time = time.time()
    return handle


def wait_for_completion(handle: K8sJobHandle, cfg: dict) -> None:
    timeout = cfg["kubernetes"]["job_timeout_seconds"]
    subprocess.run(
        [
            "kubectl", "wait",
            f"job/{handle.k8s_name}",
            "-n", handle.namespace,
            "--for=condition=complete",
            f"--timeout={timeout}s",
        ],
        check=True,
    )
    handle.end_time = time.time()

    # Resolve which node the pod actually landed on — needed to look up
    # job:metrics:<job_id>:<node> in Redis.
    result = subprocess.run(
        [
            "kubectl", "get", "pod",
            "-n", handle.namespace,
            "-l", f"job-name={handle.k8s_name}",
            "-o", "jsonpath={.items[0].spec.nodeName}",
        ],
        check=True, capture_output=True, text=True,
    )
    handle.node = result.stdout.strip()


def record_result(handle: K8sJobHandle, job_id: str, config: str, profile: str,
                   overcommit: float, rep: int, cfg: dict) -> dict:
    makespan_s = (handle.end_time - handle.start_time) if handle.end_time else float("nan")
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


def cleanup(handle: K8sJobHandle) -> None:
    """Delete the completed Job so repeated runs don't accumulate cluster state.
    Called by run_experiment.py after record_result()."""
    subprocess.run(
        ["kubectl", "delete", "job", handle.k8s_name, "-n", handle.namespace, "--ignore-not-found"],
        check=False,
    )
