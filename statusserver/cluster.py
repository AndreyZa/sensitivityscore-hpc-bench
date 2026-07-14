"""Снимки состояния кластера через kubectl (KUBECONFIG процесса).

Всё best-effort: если kubectl недоступен, страница живёт дальше с пустыми
списками — статус-сервер не должен падать из-за стенда.
"""

from __future__ import annotations

import subprocess
import time

_STAND_CACHE: dict = {"ts": 0.0, "data": {}}
STAND_TTL_SECONDS = 300  # топология кластера меняется редко


def stand_info(label: str = "") -> dict:
    """API-сервер + ноды кластера. Кэш на STAND_TTL_SECONDS — не дёргать API
    каждые 10с; подпись стенда подставляется поверх кэша при каждом вызове."""
    now = time.time()
    if now - _STAND_CACHE["ts"] >= STAND_TTL_SECONDS or not _STAND_CACHE["data"]:
        data: dict = {}
        try:
            r = subprocess.run(
                ["kubectl", "config", "view", "--minify",
                 "-o", "jsonpath={.clusters[0].cluster.server}"],
                capture_output=True, text=True, timeout=6,
            )
            data["server"] = r.stdout.strip() or "(kubectl недоступен)"
            r = subprocess.run(
                ["kubectl", "get", "nodes", "--no-headers", "-o",
                 "custom-columns=N:.metadata.name,V:.status.nodeInfo.kubeletVersion,"
                 "K:.status.nodeInfo.kernelVersion,CPU:.status.allocatable.cpu,"
                 "MEM:.status.allocatable.memory"],
                capture_output=True, text=True, timeout=6,
            )
            rows = (
                [l.split() for l in r.stdout.strip().splitlines()]
                if r.returncode == 0 else []
            )
            # Измерительные узлы — по ролям (node-role.kubernetes.io/*,
            # ставятся scripts/bootstrap-cluster.sh), тем же селектором, что
            # у харнесса (k8s_submit.list_worker_nodes): системный узел
            # ss-system и control-plane в счёт эталонов не входят.
            r = subprocess.run(
                ["kubectl", "get", "nodes",
                 "--selector=!node-role.kubernetes.io/control-plane,"
                 "!node-role.kubernetes.io/ss-system",
                 "-o", "jsonpath={.items[*].metadata.name}"],
                capture_output=True, text=True, timeout=6,
            )
            bench = set(r.stdout.split()) if r.returncode == 0 else set()
            data["bench"] = sorted(bench)
            data["nodes"] = [
                [row[0], ("bench" if row[0] in bench else "система")] + row[1:]
                for row in rows if row
            ]
        except Exception as e:  # noqa: BLE001 — страница статуса не должна падать
            data.setdefault("server", f"({e})")
            data.setdefault("nodes", [])
        _STAND_CACHE.update(ts=now, data=data)
    return {**_STAND_CACHE["data"], "label": label}


def worker_node_count(cfg: dict | None = None) -> int:
    """Число измерительных (bench) узлов для расчёта ожидаемых per-node
    эталонных прогонов: узлы без ролей control-plane/ss-system (см.
    stand_info) минус exclude_nodes конфига — ровно те узлы, которые
    харнесс перебирает в эталонах и матрице (k8s_submit.list_worker_nodes,
    тот же селектор по ролям)."""
    names = set(stand_info().get("bench", []))
    excluded = set((cfg or {}).get("exclude_nodes", []))
    return len(names - excluded) or 1


_SNAP_CACHE: dict = {"ts": 0.0, "data": {}}
SNAP_TTL_SECONDS = 5  # частое авто-обновление страницы не должно долбить API


def kubectl_snapshot() -> dict:
    """Живые Job'ы и генераторы фоновой нагрузки в bench-неймспейсе."""
    now = time.time()
    if now - _SNAP_CACHE["ts"] < SNAP_TTL_SECONDS and _SNAP_CACHE["data"]:
        return _SNAP_CACHE["data"]
    out: dict = {}
    for name, cmd in {
        "jobs": ["kubectl", "get", "jobs", "-n", "sensitivityscore-bench",
                 "--no-headers", "-o",
                 "custom-columns=N:.metadata.name,ACTIVE:.status.active"],
        "aggressors": ["kubectl", "get", "pods", "-n", "sensitivityscore-bench",
                       "-l", "app=ss-aggressor", "--no-headers", "-o",
                       "custom-columns=N:.metadata.name,NODE:.spec.nodeName,P:.status.phase"],
    }.items():
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            out[name] = (
                [l.split() for l in r.stdout.strip().splitlines()]
                if r.returncode == 0
                else [[r.stderr.strip()[:120]]]
            )
        except Exception as e:  # noqa: BLE001
            out[name] = [[f"({e})"]]
    _SNAP_CACHE.update(ts=now, data=out)
    return out
