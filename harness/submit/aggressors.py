"""aggressors.py — управление фоновыми агрессорами pressure-сценария.

Агрессоры (stress-ng / iperf3, см. aggressor/Dockerfile) прибиваются к нодам
через spec.nodeName — МИМО планировщиков, чтобы оба плеча сравнения
(A-default / A-sensitivityscore) видели идентичный ландшафт давления. Малый
cpu-request при большом реальном давлении — намеренно: это тот случай, где
ресурсная модель default-планировщика слепа, а SensitivityScore видит.

Два режима (scenario["aggressor_mode"]):
  - по умолчанию — stress-ng с args из scenario["aggressor_args"]
    (--stream: LLC/полоса памяти; --hdd: диск, видно в PSI io.pressure);
  - "net" — iperf3-пары НА ОДНОЙ штормимой ноде: на каждый слот свой сервер
    (iperf3 держит один тест за раз) + UDP-клиент с фиксированным
    -b scenario["net_bitrate_mbps"]. Трафик идёт pod-to-pod через veth и
    засвечивает net_bw/net_pressure ТОЛЬКО штормимой ноды — cross-node пара
    засветила бы rx-стороной и вторую (чистую) ноду.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))

AGGRESSOR_LABEL = "app=ss-aggressor"


def storm_specs(scenario: dict) -> list[dict] | None:
    """Нормализованный список штормов смешанного сценария или None (легаси).

    Новый формат — scenario["storms"]: на КАЖДОМ узле свой тип шторма
    (кросс-осевой тест: планировщик должен не «уйти от единственного шторма»,
    а СОПОСТАВИТЬ профиль каждой жертвы с осью шторма каждого узла):
        storms:
          - {node: ..., mode: stress, args: ["--stream","2"], per_node: 2,
             toxic_for: [high-s]}
          - {node: ..., mode: net, per_node: 2, net_bitrate_mbps: 400,
             toxic_for: [high-s-net]}
    toxic_for — профили, для которых узел «свой токсичный» (метрика
    ошибочного сопоставления в анализе/статусе); на деплой не влияет."""
    storms = scenario.get("storms")
    if not storms:
        return None
    return [
        {
            "node": s["node"],
            "mode": s.get("mode", "stress"),
            "args": s.get("args"),
            "per_node": int(s.get("per_node", 1)),
            "net_bitrate_mbps": int(
                s.get("net_bitrate_mbps", scenario.get("net_bitrate_mbps", 400))
            ),
        }
        for s in storms
    ]


def resolve_pressured_nodes(scenario: dict, cfg: dict) -> list[str]:
    """Ноды под давление: список storms (смешанный сценарий), явный список
    из сценария, либо первые pressured_node_count worker-нод (без
    control-plane), отсортированных по имени — детерминированно от прогона
    к прогону."""
    storms = storm_specs(scenario)
    if storms:
        # Смешанный сценарий сознательно давит ВСЕ измерительные узлы —
        # чистых нет by design, качество решения = сопоставление профиля
        # жертвы с осью шторма узла, guard «нужна чистая нода» неприменим.
        return [s["node"] for s in storms]

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
    # Отбрасываем exclude_nodes — те же ноды, что матрица обходит через
    # nodeAffinity (напр. worker без egress к registry): агрессор туда бы не
    # стянул образ, а victim'ы туда всё равно не планируются, так что давить
    # их бессмысленно. Держим согласованным с list_worker_nodes(exclude=...).
    excluded = set(cfg.get("exclude_nodes", []))
    workers = sorted(w for w in result.stdout.split() if w not in excluded)
    count = scenario.get("pressured_node_count", 1)
    if count >= len(workers):
        raise RuntimeError(
            f"pressured_node_count={count} would pressure ALL {len(workers)} "
            "worker nodes — the scheduler needs at least one clean node to "
            "steer victims to, otherwise both arms are placement-forced"
        )
    return workers[:count]


def expected_pods(node_count: int, per_node: int, scenario: dict) -> int:
    """Сколько подов-агрессоров обязано быть Running у плеча (для
    assert_running): net-режим добавляет к клиентам по iperf3-серверу на
    слот. Для storms счёт по каждому шторму отдельно (node_count/per_node
    аргументы тогда не используются)."""
    storms = storm_specs(scenario)
    if storms:
        return sum(
            s["per_node"] * (2 if s["mode"] == "net" else 1) for s in storms
        )
    if scenario.get("aggressor_mode") == "net":
        return node_count * per_node * 2
    return node_count * per_node


def _render_pod(scenario: dict, cfg: dict, **overrides) -> str:
    """Один под-агрессор по шаблону; общие для всех режимов поля берутся из
    cfg["aggressor"], специфичные (name/node/command/args) — из overrides."""
    agg_cfg = cfg.get("aggressor", {})
    params = dict(
        namespace=cfg["kubernetes"]["namespace"],
        scenario=scenario["name"],
        image=cfg["images"]["aggressor"],
        cpu_request=agg_cfg.get("cpu_request", "500m"),
        cpu_limit=agg_cfg.get("cpu_limit", "2"),
        mem_request=agg_cfg.get("mem_request", "512Mi"),
        mem_limit=agg_cfg.get("mem_limit", "2Gi"),
    )
    params.update(overrides)
    return _env.get_template("aggressor-pod.yaml.j2").render(**params)


def _apply_and_wait(manifests: list[str], namespace: str) -> None:
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input="\n---\n".join(manifests),
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        # stderr обязателен в сообщении: CalledProcessError без него прячет
        # причину ('namespaces not found' искали дольше, чем чинили).
        raise RuntimeError(f"aggressor apply failed: {result.stderr.strip()}")
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


def _pod_ip(name: str, namespace: str) -> str:
    result = subprocess.run(
        ["kubectl", "get", "pod", name, "-n", namespace,
         "-o", "jsonpath={.status.podIP}"],
        check=True,
        capture_output=True,
        text=True,
    )
    ip = result.stdout.strip()
    if not ip:
        raise RuntimeError(f"aggressor pod {name} is Ready but has no podIP")
    return ip


def _deploy_net(nodes: list[str], per_node: int, scenario: dict, cfg: dict) -> None:
    """Net-шторм: на каждой ноде per_node пар (iperf3-сервер + UDP-клиент на
    фиксированном битрейте). Серверы деплоятся первыми (клиенту нужен podIP);
    свой сервер на каждый слот — iperf3 обслуживает один тест за раз."""
    namespace = cfg["kubernetes"]["namespace"]
    bitrate = int(scenario.get("net_bitrate_mbps", 400))

    servers = []  # (node, slot, pod name)
    manifests = []
    for node in nodes:
        for slot in range(per_node):
            name = f"ss-aggressor-{node}-srv{slot}".lower()
            servers.append((node, slot, name))
            manifests.append(
                _render_pod(
                    scenario, cfg,
                    name=name,
                    node_name=node,
                    command_json=json.dumps(["iperf3", "-s"]),
                )
            )
    _apply_and_wait(manifests, namespace)

    manifests = []
    for node, slot, server_name in servers:
        ip = _pod_ip(server_name, namespace)
        # Вечный клиент с рестарт-циклом: разовый `iperf3 -t` рано или поздно
        # истечёт/оборвётся, а под с restartPolicy: Never не перезапустится —
        # шторм бы молча закончился посреди плеча.
        client_cmd = (
            f"while true; do iperf3 -u -c {ip} -b {bitrate}M -t 86400 -l 1400; "
            f'echo "iperf3 client exited; retrying"; sleep 2; done'
        )
        manifests.append(
            _render_pod(
                scenario, cfg,
                name=f"ss-aggressor-{node}-{slot}".lower(),
                node_name=node,
                command_json=json.dumps(["/bin/sh", "-c", client_cmd]),
            )
        )
    _apply_and_wait(manifests, namespace)
    log.info(
        "net aggressors: %d iperf3 pairs on nodes %s (x%d per node, %dM UDP each)",
        len(servers), ",".join(nodes), per_node, bitrate,
    )


def deploy(
    nodes: list[str],
    per_node: int,
    scenario: dict,
    cfg: dict,
) -> None:
    """Разворачивает агрессоров на каждой из nodes и ждёт их Ready.

    Режим по умолчанию — stress-ng: тип давления задаётся
    scenario["aggressor_args"] (--stream N для LLC/полосы памяти, --hdd N
    [--hdd-bytes ...] для диска — IO-давление тогда видно и в PSI
    io.pressure); дефолт — cfg["aggressor"]["default_args"]. --temp-path
    /scratch добавляется всегда (emptyDir в шаблоне): файловые стрессоры
    пишут на реальный диск ноды, а не в overlay-слой контейнера.
    aggressor_mode: "net" — см. _deploy_net.
    """
    from submit.k8s_submit import ensure_namespace  # локальный импорт: избегаем цикла на уровне модулей

    namespace = cfg["kubernetes"]["namespace"]
    # Агрессоры деплоятся ДО первого сабмита job (который сам создаёт
    # namespace) — после make harness-clean-full namespace не существует.
    ensure_namespace(namespace)

    storms = storm_specs(scenario)
    if storms:
        # Смешанный сценарий: у каждого узла свой шторм. stress-штормы
        # собираются в один apply; net-штормы деплоятся после (им нужен
        # podIP сервера).
        stress_manifests = []
        for st in storms:
            if st["mode"] == "net":
                continue
            args = ["--temp-path", "/scratch"] + list(st["args"] or ["--stream", "2"])
            for slot in range(st["per_node"]):
                stress_manifests.append(
                    _render_pod(
                        scenario, cfg,
                        name=f"ss-aggressor-{st['node']}-{slot}".lower(),
                        node_name=st["node"],
                        args_json=json.dumps(args),
                    )
                )
        if stress_manifests:
            _apply_and_wait(stress_manifests, namespace)
        for st in storms:
            if st["mode"] == "net":
                _deploy_net(
                    [st["node"]], st["per_node"],
                    {**scenario, "net_bitrate_mbps": st["net_bitrate_mbps"]},
                    cfg,
                )
        log.info(
            "mixed storms: %s",
            "; ".join(
                f"{st['node']}={st['mode']}"
                + (f"{st['args']}" if st["args"] else "")
                + f" x{st['per_node']}"
                for st in storms
            ),
        )
        return

    if scenario.get("aggressor_mode") == "net":
        _deploy_net(nodes, per_node, scenario, cfg)
        return

    agg_cfg = cfg.get("aggressor", {})
    args = ["--temp-path", "/scratch"] + list(
        scenario.get("aggressor_args") or agg_cfg.get("default_args", ["--stream", "2"])
    )

    manifests = []
    for node in nodes:
        for slot in range(per_node):
            manifests.append(
                _render_pod(
                    scenario, cfg,
                    name=f"ss-aggressor-{node}-{slot}".lower(),
                    node_name=node,
                    args_json=json.dumps(args),
                )
            )
    if not manifests:
        # Плацебо-сценарий (intensity 0 / pressured_node_count 0): фонового
        # давления нет by design, «kubectl apply» с пустым вводом упал бы.
        log.info("aggressors: none to deploy (placebo arm — zero pressure)")
        return
    _apply_and_wait(manifests, namespace)
    log.info(
        "aggressors: %d pods on nodes %s (x%d per node) Ready",
        len(manifests),
        ",".join(nodes),
        per_node,
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
    следующее плечо/точка интенсивности должны стартовать с чистого давления.

    Неудача НЕ глотается молча: если delete не прошёл (например, IO-шторм
    самого сценария на dev-стенде с общим диском довёл apiserver/etcd до
    сброса соединений — реальный случай), агрессоры продолжают давить ноду
    и после конца плеча. Одна повторная попытка после паузы, затем громкая
    ошибка в лог."""
    namespace = cfg["kubernetes"]["namespace"]
    cmd = [
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
    ]
    for attempt in (1, 2):
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return
        log.warning(
            "aggressor teardown attempt %d failed: %s", attempt, result.stderr.strip()
        )
        time.sleep(10)
    log.error(
        "aggressor teardown FAILED twice — pods with label %s may still be "
        "loading the node; clean up manually (kubectl delete pods -l %s -n %s)",
        AGGRESSOR_LABEL,
        AGGRESSOR_LABEL,
        namespace,
    )
