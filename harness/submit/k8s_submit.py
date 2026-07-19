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
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from profiles import PROFILES, Sensitivity
from submit.redis_metrics import fetch_job_metrics

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))

# schedulerName для каждого config-варианта. Правило: суффикс варианта = имя
# профиля планировщика (см. k8s/scheduler-config/scheduler-config.yaml —
# профили sensitivityscore и trimaran в одном Deployment), кроме "default" ->
# штатный "default-scheduler". D — отдельный случай (slurm-bridge, без суффикса).
SCHEDULER_NAME_BY_CONFIG = {
    "A-default": "default-scheduler",
    "A-sensitivityscore": "sensitivityscore",
    "A-trimaran": "trimaran",  # load-aware бейзлайн H1-trimaran
    "B-default": "default-scheduler",
    "B-sensitivityscore": "sensitivityscore",
    "B-trimaran": "trimaran",
    "D": "slurm-bridge",
}

# Namespaces we've already confirmed exist this process run — avoids
# re-running `kubectl create namespace` before every single job submission
# across a full matrix (hundreds of calls otherwise); idempotent either way
# since it uses --dry-run=client | apply, but no need to hit the API server
# that often.
_namespaces_ensured: set[str] = set()


def ensure_namespace(namespace: str) -> None:
    """Auto-creates the namespace if it doesn't exist yet (idempotent).
    Fixes: 'Error from server (NotFound): namespaces "..." not found' on the
    very first job submission against a fresh cluster. Public: aggressors.py
    needs the same guarantee — aggressor pods deploy BEFORE the first job
    submission would have created the namespace (a make harness-clean-full
    between runs deletes it, found the hard way)."""
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


def _k8s_name(job_id: str) -> str:
    """job_id -> валидное имя K8s-объекта (RFC1123). Одна точка истины:
    submit_job и abort_submission обязаны получать ОДНО имя, иначе abort
    после упавшего сабмита удалял бы не тот Job."""
    return job_id.lower().replace("_", "-").replace(".", "-")


def list_worker_nodes(exclude: Iterable[str] = ()) -> list[str]:
    """Worker-ноды кластера (без control-plane и без системного узла
    node=ss-system), отсортированные по имени — детерминированный порядок
    для per-node бейзлайнов и pressure-сценариев.

    Узел с ролью ss-system (node-role.kubernetes.io/ss-system, ставится
    scripts/bootstrap-cluster.sh) исключается всегда: он выделен под
    инфраструктуру (redis, планировщик, metrics-server), защищён taint'ом
    от экспериментальных подов и не должен попадать ни в эталоны, ни в
    выбор штормовой ноды, ни в ожидания матрицы — без ручного exclude в
    каждом конфиге. Роль — единственный критерий; ad-hoc лейблов на
    прод-стенде нет.

    exclude отбрасывает ноды, которые матрица и так обходит стороной
    (cfg["exclude_nodes"], nodeAffinity NotIn в шаблоне job) — иначе per-node
    бейзлайн/агрессор запиннился бы к ноде, на которую эксперимент никогда не
    планирует (напр. worker без egress к registry -> ImagePullBackOff, либо
    знаменатель slowdown для ноды, которой в матрице нет)."""
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "nodes",
            "--selector=!node-role.kubernetes.io/control-plane,"
            "!node-role.kubernetes.io/ss-system",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    excluded = set(exclude)
    return sorted(n for n in result.stdout.split() if n not in excluded)


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
        # Разрешённый digest образа нагрузки (provenance): тег :dev
        # перезаписывается, digest — нет.
        self.workload_image: str = ""


def submit_job(
    job_id: str,
    config: str,
    profile: str,
    overcommit: float,
    cfg: dict,
    pin_node: str | None = None,
) -> K8sJobHandle:
    spec = PROFILES[profile]
    namespace = cfg["kubernetes"]["namespace"]
    scheduler_name = SCHEDULER_NAME_BY_CONFIG.get(config, "default-scheduler")

    ensure_namespace(namespace)

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
        # Ноды, на которые НЕ ставить поды (например, worker без egress к
        # registry на STAGE) — nodeAffinity NotIn по kubernetes.io/hostname,
        # без cordon/taint самих нод.
        exclude_nodes=cfg.get("exclude_nodes", []),
        # Жёсткая привязка к конкретной ноде (nodeSelector) — для per-node
        # соло-бейзлайнов: облачные "одинаковые" ноды бывают в разы разной
        # скорости (пересозданный worker STAGE оказался ~1.9x медленнее
        # соседей), общий на все ноды знаменатель slowdown тогда лжёт.
        pin_node=pin_node,
    )

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(manifest)
        manifest_path = f.name

    try:
        subprocess.run(["kubectl", "apply", "-f", manifest_path], check=True)
    finally:
        Path(manifest_path).unlink(missing_ok=True)

    handle = K8sJobHandle(job_id=job_id, k8s_name=_k8s_name(job_id), namespace=namespace)
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
    """Polls the Job's conditions until Complete — or raises as soon as it is
    Failed. The previous `kubectl wait --for=condition=complete` could only
    see success: with backoffLimit=0 a failed Job never becomes Complete, so
    every failure burned the full job_timeout (30 min by default), twice with
    the old retry policy, before the harness moved on."""
    timeout = cfg["kubernetes"]["job_timeout_seconds"]
    poll_interval = cfg["kubernetes"].get("poll_interval_seconds", 5)
    deadline = time.time() + timeout

    while time.time() < deadline:
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "job",
                handle.k8s_name,
                "-n",
                handle.namespace,
                "-o",
                "jsonpath={range .status.conditions[*]}{.type}={.status} {end}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        conditions = dict(
            pair.split("=", 1) for pair in result.stdout.split() if "=" in pair
        )
        if conditions.get("Complete") == "True":
            break
        if conditions.get("Failed") == "True":
            raise RuntimeError(
                f"job {handle.k8s_name} failed (conditions: {result.stdout.strip()})"
            )
        time.sleep(poll_interval)
    else:
        raise TimeoutError(
            f"job {handle.k8s_name} did not complete within {timeout}s"
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
            # imageID тем же запросом: это РАЗРЕШЁННЫЙ digest, а не тег.
            # Тег :dev перезаписывается, а imagePullPolicy: Always означает,
            # что пуш нового образа переключает часть Job'ов даже внутри одной
            # серии — по тегу это неотличимо, по digest видно построчно.
            "jsonpath={.items[0].spec.nodeName}"
            "{'|'}{.items[0].status.containerStatuses[0].state.terminated.startedAt}"
            "{'|'}{.items[0].status.containerStatuses[0].state.terminated.finishedAt}"
            "{'|'}{.items[0].status.containerStatuses[0].imageID}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    node, _, rest = result.stdout.strip().partition("|")
    started_raw, _, rest = rest.partition("|")
    finished_raw, _, image_id = rest.partition("|")
    handle.node = node
    handle.container_start = _parse_k8s_time(started_raw)
    handle.container_end = _parse_k8s_time(finished_raw)
    handle.workload_image = image_id


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
        "workload_image": handle.workload_image,
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
    k8s_name = _k8s_name(job_id)
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
