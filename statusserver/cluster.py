"""Снимки состояния кластера через kubectl (KUBECONFIG процесса).

Всё best-effort: если kubectl недоступен, страница живёт дальше с пустыми
списками — статус-сервер не должен падать из-за стенда.
"""

from __future__ import annotations

import subprocess
import time

_STAND_CACHE: dict = {"ts": 0.0, "data": {}}
STAND_TTL_SECONDS = 300  # топология кластера меняется редко

# 6 секунд не хватало: API облачного стенда под нагрузкой серии отвечает
# медленнее, и `kubectl get nodes` регулярно упирался в таймаут. Ценой был не
# один пустой список, а весь блок — исключение уносило и bench-узлы, после
# чего страница честно, но пугающе писала «kubectl недоступен» посреди
# исправного прогона (наблюдалось 20.07 на серии ablation).
KUBECTL_TIMEOUT_SECONDS = 20


def _kubectl(args: list[str], timeout: int = KUBECTL_TIMEOUT_SECONDS) -> tuple[bool, str, str]:
    """kubectl без исключений наружу: (успех, stdout, причина неудачи).

    Каждый вызов отдельно — медленный ответ на один запрос не должен стирать
    результат остальных."""
    try:
        r = subprocess.run(["kubectl", *args], capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode == 0:
            return True, r.stdout, ""
        return False, "", r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "код возврата != 0"
    except subprocess.TimeoutExpired:
        return False, "", f"kubectl не ответил за {timeout} с"
    except Exception as e:  # noqa: BLE001 — страница статуса не должна падать
        return False, "", str(e)


def _mem_human(quantity: str) -> str:
    """K8s-quantity памяти («378848116Ki») -> человеческое «361.3 ГиБ».

    Сырые кибибайты в таблице узлов нечитаемы (замечание пользователя
    19.08.2026). Непонятный формат возвращается как есть — честнее сырого
    числа, чем молчаливый ноль."""
    mult = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
            "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    for suf, m in sorted(mult.items(), key=lambda kv: -len(kv[0])):
        if quantity.endswith(suf):
            try:
                return f"{float(quantity[:-len(suf)]) * m / 1024**3:.1f} ГиБ"
            except ValueError:
                return quantity
    try:
        return f"{float(quantity) / 1024**3:.1f} ГиБ"   # голые байты
    except ValueError:
        return quantity


def stand_info(label: str = "") -> dict:
    """API-сервер + ноды кластера. Кэш на STAND_TTL_SECONDS — не дёргать API
    каждые 10с; подпись стенда подставляется поверх кэша при каждом вызове.

    При неудачном опросе показываются ПРОШЛЫЕ значения из кэша: топология
    стенда за прогон не меняется, и «узлы те же, что десять минут назад»
    ближе к истине, чем «узлов нет». Причина неудачи не проглатывается —
    она уезжает в stand_error и видна на странице."""
    now = time.time()
    if now - _STAND_CACHE["ts"] >= STAND_TTL_SECONDS or not _STAND_CACHE["data"]:
        prev = _STAND_CACHE["data"]
        data: dict = {}
        problems: list[str] = []

        ok, out, err = _kubectl(["config", "view", "--minify",
                                 "-o", "jsonpath={.clusters[0].cluster.server}"])
        data["server"] = out.strip() or prev.get("server") or "(kubectl недоступен)"
        if not ok:
            problems.append(f"адрес API: {err}")

        ok, out, err = _kubectl(
            ["get", "nodes", "--no-headers", "-o",
             "custom-columns=N:.metadata.name,V:.status.nodeInfo.kubeletVersion,"
             "K:.status.nodeInfo.kernelVersion,CPU:.status.allocatable.cpu,"
             "MEM:.status.allocatable.memory"])
        rows = [l.split() for l in out.strip().splitlines()] if ok else []
        if not ok:
            problems.append(f"список узлов: {err}")

        # Измерительные узлы — по ролям (node-role.kubernetes.io/*,
        # ставятся scripts/bootstrap-cluster.sh), тем же селектором, что
        # у харнесса (k8s_submit.list_worker_nodes): системный узел
        # ss-system и control-plane в счёт эталонов не входят.
        ok, out, err = _kubectl(
            ["get", "nodes",
             "--selector=!node-role.kubernetes.io/control-plane,"
             "!node-role.kubernetes.io/ss-system",
             "-o", "jsonpath={.items[*].metadata.name}"])
        if ok:
            bench = sorted(set(out.split()))
        else:
            bench = list(prev.get("bench") or [])
            problems.append(f"измерительные узлы: {err}")
        data["bench"] = bench

        data["nodes"] = [
            [row[0], ("bench" if row[0] in set(bench) else "система")]
            + row[1:-1] + [_mem_human(row[-1])]
            for row in rows if row
        ] or list(prev.get("nodes") or [])
        if problems:
            data["stand_error"] = "; ".join(problems)
        _STAND_CACHE.update(ts=now, data=data)
    return {**_STAND_CACHE["data"], "label": label}


def worker_node_count(cfg: dict | None = None) -> int | None:
    """Число измерительных (bench) узлов для расчёта ожидаемых per-node
    эталонных прогонов: узлы без ролей control-plane/ss-system (см.
    stand_info) минус exclude_nodes конфига — ровно те узлы, которые
    харнесс перебирает в эталонах и матрице (k8s_submit.list_worker_nodes,
    тот же селектор по ролям).

    None = топология НЕИЗВЕСТНА (kubectl недоступен, протух токен, страница
    в кластере без kubeconfig). Раньше здесь стояло `or 1`, и «узлов не
    видно» было неотличимо от «узел ровно один»: ожидаемый объём эталонов
    занижался в N раз, страница рисовала «Эталонные прогоны 6/6 ✓» на
    шести из восемнадцати, и решение «эталоны собраны, пора запускать
    серию» принималось по заведомо ложному индикатору. Число узлов можно
    задать явно (baseline.nodes в конфиге) — тогда счёт верен и без kubectl.
    """
    explicit = (cfg or {}).get("baseline", {}).get("nodes")
    if explicit:
        return int(explicit)
    names = set(stand_info().get("bench", []))
    if not names:
        return None
    excluded = set((cfg or {}).get("exclude_nodes", []))
    return len(names - excluded) or None


_SNAP_CACHE: dict = {"ts": 0.0, "data": {}}
SNAP_TTL_SECONDS = 5  # частое авто-обновление страницы не должно долбить API


# Что штатно живёт на измерительном узле и посторонним не считается:
# сетевой слой k0s, агент метрик стенда и балансировщик апстрима. Всё
# остальное там — повод присмотреться (методика: «кроме агента метрик на
# узле не работает ничего»).
EXPECTED_ON_BENCH = (
    "kube-proxy", "kube-router", "konnectivity-agent", "nllb-",
    "sensitivityscore-metrics-agent",
)


def foreign_on_bench(bench: list[str]) -> list[list[str]]:
    """Посторонняя занятость измерительных узлов.

    Страница знает только про серии: она читает лог прогона и parquet.
    Всё, что запущено мимо харнесса — калибровочная лестница P1, разовый
    нагрузочный тест, случайно приземлившийся на bench системный под, —
    для неё невидимо, и стенд выглядит свободным, когда он занят
    (замечено 20.08.2026 на лестнице P1). Здесь перечисляется ЛЮБОЙ под на
    измерительных узлах, кроме заведомо штатных; жертвы серии показаны
    отдельным списком и сюда не дублируются.
    """
    if not bench:
        return []
    ok, out, _ = _kubectl(
        ["get", "pods", "-A", "--no-headers", "--field-selector",
         "status.phase=Running", "-o",
         "custom-columns=NS:.metadata.namespace,N:.metadata.name,"
         "NODE:.spec.nodeName,OWNER:.metadata.ownerReferences[0].kind"])
    if not ok:
        return []
    rows = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] not in set(bench):
            continue
        ns, name, node = parts[0], parts[1], parts[2]
        owner = parts[3] if len(parts) > 3 else ""
        if any(name.startswith(pref) for pref in EXPECTED_ON_BENCH):
            continue
        if owner == "Job":          # жертвы серии — в своём списке
            continue
        rows.append([f"{ns}/{name}", node])
    return rows


def kubectl_snapshot() -> dict:
    """Живые Job'ы, генераторы нагрузки и посторонняя занятость bench."""
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
    out["foreign"] = foreign_on_bench(_STAND_CACHE["data"].get("bench") or [])
    _SNAP_CACHE.update(ts=now, data=out)
    return out
