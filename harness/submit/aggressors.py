"""aggressors.py — управление фоновыми LLC/membw-агрессорами pressure-сценария.

Агрессоры (stress-ng --stream, см. aggressor/Dockerfile) прибиваются к нодам
через spec.nodeName — МИМО планировщиков, чтобы оба плеча сравнения
(A-default / A-sensitivityscore) видели идентичный ландшафт давления. Малый
cpu-request при большом реальном давлении — намеренно: это тот случай, где
ресурсная модель default-планировщика слепа, а SensitivityScore видит.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))

AGGRESSOR_LABEL = "app=ss-aggressor"


def resolve_pressured_nodes(scenario: dict, cfg: dict) -> list[str]:
    """Ноды под давление: явный список из сценария, либо первые
    pressured_node_count worker-нод (без control-plane), отсортированных по
    имени — детерминированно от прогона к прогону."""
    explicit = scenario.get("aggressor_nodes") or []
    if explicit:
        return list(explicit)

    result = subprocess.run(
        [
            "kubectl",
            "get",
            "nodes",
            "--selector=!node-role.kubernetes.io/control-plane",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    workers = sorted(result.stdout.split())
    count = scenario.get("pressured_node_count", 1)
    if count >= len(workers):
        raise RuntimeError(
            f"pressured_node_count={count} would pressure ALL {len(workers)} "
            "worker nodes — the scheduler needs at least one clean node to "
            "steer victims to, otherwise both arms are placement-forced"
        )
    return workers[:count]


def deploy(
    nodes: list[str], per_node: int, scenario_name: str, cfg: dict
) -> None:
    """Разворачивает per_node агрессоров на каждой из nodes и ждёт их Ready."""
    namespace = cfg["kubernetes"]["namespace"]
    template = _env.get_template("aggressor-pod.yaml.j2")
    agg_cfg = cfg.get("aggressor", {})

    manifests = []
    for node in nodes:
        for slot in range(per_node):
            manifests.append(
                template.render(
                    name=f"ss-aggressor-{node}-{slot}".lower(),
                    namespace=namespace,
                    scenario=scenario_name,
                    node_name=node,
                    image=cfg["images"]["aggressor"],
                    stream_workers=agg_cfg.get("stream_workers", 2),
                    cpu_request=agg_cfg.get("cpu_request", "500m"),
                    cpu_limit=agg_cfg.get("cpu_limit", "2"),
                )
            )

    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input="\n---\n".join(manifests),
        check=True,
        text=True,
        capture_output=True,
    )
    log.info(
        "aggressors: %d pods on nodes %s (x%d per node), waiting for Ready",
        len(manifests),
        ",".join(nodes),
        per_node,
    )
    subprocess.run(
        [
            "kubectl",
            "wait",
            "pod",
            "-l",
            AGGRESSOR_LABEL,
            "-n",
            namespace,
            "--for=condition=Ready",
            "--timeout=120s",
        ],
        check=True,
        capture_output=True,
    )


def assert_running(expected: int, cfg: dict) -> None:
    """Проверяет, что все expected агрессоров реально Running — вызывается
    ПОСЛЕ фазы стабилизации, прямо перед потоком жертв. Ловит класс ошибок
    'агрессор молча вышел' (например, неверный флаг stress-ng: '--timeout 0'
    означает мгновенный выход, а не бесконечность — найдено первым smoke) —
    иначе жертвы измеряются на чистой ноде, а плечо выглядит валидным."""
    namespace = cfg["kubernetes"]["namespace"]
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "pods",
            "-l",
            AGGRESSOR_LABEL,
            "-n",
            namespace,
            "--field-selector=status.phase=Running",
            "--no-headers",
            "-o",
            "name",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    running = len(result.stdout.split())
    if running != expected:
        raise RuntimeError(
            f"pressure phase is broken: {running}/{expected} aggressors are "
            "Running after the stabilize window — victims would be measured "
            "against an unpressured node"
        )


def teardown(cfg: dict) -> None:
    """Сносит всех агрессоров (по лейблу) и дожидается фактического удаления —
    следующее плечо/точка интенсивности должны стартовать с чистого давления."""
    namespace = cfg["kubernetes"]["namespace"]
    subprocess.run(
        [
            "kubectl",
            "delete",
            "pods",
            "-l",
            AGGRESSOR_LABEL,
            "-n",
            namespace,
            "--ignore-not-found",
            "--wait=true",
            "--timeout=120s",
        ],
        check=False,
        capture_output=True,
    )
