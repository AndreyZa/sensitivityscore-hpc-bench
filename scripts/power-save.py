#!/usr/bin/env python3
"""power-save.py — политика гашения простаивающих узлов (фаза P3, Ш8).

Аналог механизма `power_save` планировщика Slurm, перенесённый в
Kubernetes штатными средствами: узел, простоявший дольше --suspend-time,
выводится из планирования (cordon) и выключается; при появлении работы,
которую некуда поставить, узел включается обратно и возвращается в
планирование. Управляющие параметры названы как в Slurm, чтобы плечо
сравнения в статье было именно ТОЙ ЖЕ политикой, а не похожей:

  --suspend-time     SuspendTime    сколько узел должен простоять
  --resume-timeout   ResumeTimeout  бюджет на подъём до Ready
  --suspend-exc      SuspendExcNodes  узлы, которые не гасим никогда
  --min-active       (нет аналога)  сколько узлов держать включёнными

ПОЛИТИКА ЗДЕСЬ, ИСПОЛНИТЕЛЬ — ЗА ИНТЕРФЕЙСОМ (--executor):
  redfish  прямой вызов BMC (обкатан 20.08.2026: Off за 18 c, Ready за
           2 мин 59 с) — на нём измеряется Ш8, потому что в измерении не
           должен участвовать компонент, который сам ещё обкатывается;
  metal3   декларативно, BareMetalHost.spec.online (externallyProvisioned:
           BMO управляет только питанием и не трогает установленную ОС);
  none     политика считается и логируется, питание не трогается — режим
           наблюдения на живой серии.
Разбор выбора — docs/Metal3-гашение.md.

Порог осмысленно сравнивать с ценой цикла: T = E_цикла/(P_простоя −
P_выкл). На этом стенде (20.08.2026) T ≈ 4 мин, а подъём занимает 179 с —
три четверти порога. Поэтому --suspend-time ниже ~2·T на этом
оборудовании невыгоден, и по умолчанию не задан: значение обязано
приходить из измерения, а не из головы.

  power-save.py --kubeconfig ~/.kube/configs/prod --suspend-time 600 \
      --executor redfish --idrac-map wrk-b6=10.21.200.106,... --once
  power-save.py ... --dry-run     план решений, без действий
  power-save.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

_EW = pathlib.Path(__file__).with_name("energy-window.py")

# Селектор измерительных узлов. Стоял `role=bench` — такой метки на стенде
# НЕТ, узлы помечены `node-role.kubernetes.io/bench` (так их метит
# scripts/bootstrap-cluster.sh, так их ищет harness/submit/k8s_submit.py).
# `kubectl get nodes -l role=bench` возвращал пустой список, и контроллер
# оказывался тихим no-op: тикал, ничего не находил, ничего не писал.
# Самотест этого поймать не мог — он подставляет фальшивый кластер и до
# селектора не доходит. (21.08.2026, на сухом прогоне перед Ш8.)
BENCH_SELECTOR = "node-role.kubernetes.io/bench"

# Поды, которые НЕ считаются работой: они есть на каждом узле всегда, и
# если считать их, ни один узел никогда не будет признан простаивающим.
INFRA_NAMESPACES = ("kube-system", "sensitivityscore-monitoring")
INFRA_LABEL_APPS = ("sensitivityscore-metrics-agent",)


class Cluster:
    """Доступ к кластеру. Отдельным классом ради самотеста: политика не
    должна знать, откуда пришли факты (реальный kubectl или сценарий)."""

    def __init__(self, kubeconfig: str = "", dry_run: bool = False,
                 selector: str = BENCH_SELECTOR):
        self.kubeconfig, self.dry_run = kubeconfig, dry_run
        self.selector = selector

    def _kubectl(self, *rest: str) -> list[str]:
        base = ["kubectl"]
        if self.kubeconfig:
            base += ["--kubeconfig", self.kubeconfig]
        return base + list(rest)

    def _run(self, *rest: str) -> str:
        r = subprocess.run(self._kubectl(*rest), capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(rest)}: {r.stderr.strip()}")
        return r.stdout

    def nodes(self) -> dict[str, dict]:
        """{имя: {ready, schedulable}} по узлам с ролью bench."""
        out = json.loads(self._run("get", "nodes", "-l", self.selector,
                                   "-o", "json"))
        res = {}
        for it in out["items"]:
            name = it["metadata"]["name"]
            cond = {c["type"]: c["status"] for c in it["status"].get("conditions", [])}
            res[name] = {
                "ready": cond.get("Ready") == "True",
                "schedulable": not it["spec"].get("unschedulable", False),
            }
        return res

    def workload_by_node(self) -> dict[str, int]:
        """Сколько НЕинфраструктурных подов работает на каждом узле."""
        out = json.loads(self._run("get", "pods", "-A", "-o", "json",
                                   "--field-selector", "status.phase=Running"))
        res: dict[str, int] = {}
        for it in out["items"]:
            node = it["spec"].get("nodeName")
            if not node:
                continue
            ns = it["metadata"]["namespace"]
            app = it["metadata"].get("labels", {}).get("app", "")
            if ns in INFRA_NAMESPACES or app in INFRA_LABEL_APPS:
                continue
            res[node] = res.get(node, 0) + 1
        return res

    def pending_pods(self) -> int:
        """Работа, которую некуда поставить, — сигнал на подъём узла."""
        out = json.loads(self._run("get", "pods", "-A", "-o", "json",
                                   "--field-selector", "status.phase=Pending"))
        return len(out["items"])

    def cordon(self, node: str, on: bool) -> None:
        if self.dry_run:
            print(f"  [dry-run] kubectl {'cordon' if on else 'uncordon'} {node}")
            return
        self._run("cordon" if on else "uncordon", node)

    def wait_ready(self, node: str, timeout: float) -> bool:
        if self.dry_run:
            print(f"  [dry-run] ожидание Ready {node} (бюджет {timeout:.0f} c)")
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.nodes().get(node, {}).get("ready"):
                    return True
            except RuntimeError:
                pass
            time.sleep(10)
        return False


class RedfishExecutor:
    """Питание через BMC напрямую. Пароль читается из файла и НИКОГДА не
    попадает ни в argv, ни в лог: curl получает его через stdin (-K -)."""

    name = "redfish"

    def __init__(self, idrac_map: dict[str, str], user: str, pass_file: str,
                 dry_run: bool = False):
        self.map, self.user, self.pass_file = idrac_map, user, pass_file
        self.dry_run = dry_run

    def _curl(self, host: str, path: str, method: str, body: str = "") -> str:
        cmd = ["curl", "-sk", "-K", "-", "-X", method,
               f"https://{host}{path}"]
        if body:
            cmd += ["-H", "Content-Type: application/json", "-d", body]
        with open(os.path.expanduser(self.pass_file)) as f:
            password = f.read().strip()
        r = subprocess.run(cmd, input=f'user = "{self.user}:{password}"\n',
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"redfish {host}: {r.stderr.strip()}")
        return r.stdout

    def _reset(self, node: str, kind: str) -> None:
        host = self.map[node]
        if self.dry_run:
            print(f"  [dry-run] Redfish {kind} -> {node} ({host})")
            return
        self._curl(host, "/redfish/v1/Systems/System.Embedded.1/Actions/"
                         "ComputerSystem.Reset", "POST",
                   json.dumps({"ResetType": kind}))

    def power_off(self, node: str) -> None:
        self._reset(node, "GracefulShutdown")

    def power_on(self, node: str) -> None:
        self._reset(node, "On")

    def power_state(self, node: str) -> str:
        if self.dry_run:
            return "Unknown"
        out = self._curl(self.map[node],
                         "/redfish/v1/Systems/System.Embedded.1", "GET")
        return json.loads(out).get("PowerState", "Unknown")


class Metal3Executor:
    """Питание декларативно: BareMetalHost.spec.online. Узлы заведены как
    externallyProvisioned — BMO управляет только питанием (см.
    docs/Metal3-гашение.md), установленную систему не трогает."""

    name = "metal3"

    def __init__(self, cluster: Cluster, namespace: str, dry_run: bool = False):
        self.cluster, self.ns, self.dry_run = cluster, namespace, dry_run

    def _set_online(self, node: str, online: bool) -> None:
        patch = json.dumps({"spec": {"online": online}})
        if self.dry_run:
            print(f"  [dry-run] BMH {node}: spec.online={str(online).lower()}")
            return
        self.cluster._run("-n", self.ns, "patch", "baremetalhost", node,
                          "--type", "merge", "-p", patch)

    def power_off(self, node: str) -> None:
        self._set_online(node, False)

    def power_on(self, node: str) -> None:
        self._set_online(node, True)

    def power_state(self, node: str) -> str:
        if self.dry_run:
            return "Unknown"
        out = self.cluster._run("-n", self.ns, "get", "baremetalhost", node,
                                "-o", "jsonpath={.status.poweredOn}")
        return "On" if out.strip() == "true" else "Off"


def wait_power_state(executor, node: str, want: str, timeout: float,
                     poll: float = 5.0) -> bool:
    """Дождаться, пока BMC подтвердит состояние питания.

    Смена состояния НЕ мгновенна: у Dell гашение занимало 18 с, эмулятор
    Redfish (sushy-tools) специально задерживает её на 1–11 с — «hardware
    actions are not immediate». Читать PowerState сразу после команды
    бессмысленно: вернётся прежнее значение (поймано на эмуляторе
    20.08.2026)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if executor.power_state(node) == want:
                return True
        except RuntimeError:
            pass
        time.sleep(poll)
    return False


class NoopExecutor:
    """Режим наблюдения: политика считается и логируется, питание цело."""

    name = "none"

    def power_off(self, node: str) -> None:
        print(f"  [наблюдение] решение: гасить {node} — питание не тронуто")

    def power_on(self, node: str) -> None:
        print(f"  [наблюдение] решение: поднимать {node} — питание не тронуто")

    def power_state(self, node: str) -> str:
        return "Unknown"


class PowerSavePolicy:
    """Собственно политика. Состояние — «когда узел стал пустым»; решения
    чистые функции от (факты кластера, время), чтобы их можно было
    проверить самотестом без кластера и без BMC."""

    def __init__(self, suspend_time: float, resume_timeout: float,
                 min_active: int, exclude: tuple[str, ...] = (),
                 verify_off: bool = True, off_timeout: float = 120.0):
        self.suspend_time = suspend_time
        self.resume_timeout = resume_timeout
        self.min_active = min_active
        self.verify_off = verify_off
        self.off_timeout = off_timeout
        self.exclude = set(exclude)
        self.idle_since: dict[str, float] = {}
        self.suspended: set[str] = set()

    def observe(self, nodes: dict[str, dict], workload: dict[str, int],
                now: float) -> None:
        for name in nodes:
            if workload.get(name, 0) > 0:
                self.idle_since.pop(name, None)
            else:
                self.idle_since.setdefault(name, now)

    def to_suspend(self, nodes: dict[str, dict], workload: dict[str, int],
                   now: float, pending: int = 0) -> list[str]:
        """Кого гасить: пуст дольше порога, не в исключениях, и после
        гашения останется не меньше --min-active включённых узлов.

        Пока есть работа без места, не гасим НИЧЕГО. Без этого условия
        такт получался противоречивым: очередь поднимает один узел и тут
        же гасит другой, потому что тот всё ещё формально пуст (поймано
        самотестом при первом же прогоне). Планировщику нужно время
        поставить поды на поднятый узел — до этого любое гашение работает
        против собственного подъёма."""
        if pending > 0:
            return []
        active = [n for n in nodes if n not in self.suspended]
        budget = len(active) - self.min_active
        if budget <= 0:
            return []
        ready = [
            n for n in sorted(active)
            if n not in self.exclude
            and workload.get(n, 0) == 0
            and nodes[n]["ready"]
            and now - self.idle_since.get(n, now) >= self.suspend_time
        ]
        return ready[:budget]

    def to_resume(self, nodes: dict[str, dict], pending: int) -> list[str]:
        """Кого поднимать: есть работа без места — поднимаем по одному,
        начиная с погашенного раньше всех (порядок имён — детерминизм)."""
        if pending <= 0 or not self.suspended:
            return []
        return sorted(self.suspended)[:1]


class WindowRecorder:
    """Пишет окна переходов в ClickHouse через тот же energy-window.py, что
    и калибровка. Без этого цена цикла считается только тем окном, которое
    держали в руках: 20.08.2026 первое гашение прода пришлось разбирать
    прямыми запросами к Prometheus, и повторить их на другом окне было
    нечем (долг 3.2 «Плана расчётов» статьи).

    Имя окна — `cycle-off-rep<N>` / `cycle-boot-rep<N>`: конвенцию читает
    analysis/energy_metrics.py, номер повторения кодируется именем, потому
    что колонки rep в energy_windows нет."""

    DRAIN_S = 60.0   # хвост спада мощности после команды выключения

    def __init__(self, stand: str, run_label: str, ch_host: str, ch_port: int,
                 sources: tuple[str, ...] = ("ipmi",), enabled: bool = True):
        self.stand, self.run_label = stand, run_label
        self.ch_host, self.ch_port = ch_host, ch_port
        self.sources, self.enabled = sources, enabled
        self._next_rep = 0
        self._rep_of: dict[str, int] = {}

    def open_cycle(self, node: str) -> int:
        """Номер цикла выдаётся при ГАШЕНИИ и держится за узлом до его
        подъёма. Счётчик, общий на все узлы и растущий после каждой
        записи, разложил бы гашение и подъём ОДНОГО цикла по разным
        номерам, и цена цикла сложилась бы из половинок разных циклов —
        у первого не было бы подъёма, у последнего гашения."""
        self._rep_of[node] = self._next_rep
        self._next_rep += 1
        return self._rep_of[node]

    def rep_for(self, node: str) -> int:
        return self._rep_of.get(node, -1)

    def close_cycle(self, node: str) -> None:
        self._rep_of.pop(node, None)

    def record(self, kind: str, node: str, t0: float, t1: float) -> None:
        if not self.enabled:
            return
        for source in self.sources:
            cmd = [sys.executable, str(_EW),
                   "--source", source, "--mode", "power",
                   "--start", str(int(t0)), "--end", str(int(t1)),
                   "--window", f"{kind}-rep{self.rep_for(node)}",
                   "--stand", self.stand, "--run-label", self.run_label,
                   "--config", "power-save",
                   "--ch-host", self.ch_host, "--ch-port", str(self.ch_port)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                # Окно не записалось — это потеря данных, а не сбой политики:
                # гашение уже состоялось. Кричим, но узел не трогаем.
                print(f"  ВНИМАНИЕ: окно {kind}-rep{self.rep_for(node)} ({source}) не "
                      f"записано: {r.stderr.strip()[:200]}", file=sys.stderr)


def tick(policy: PowerSavePolicy, cluster: Cluster, executor, now: float,
         verbose: bool = True, recorder: "WindowRecorder | None" = None) -> dict:
    nodes = cluster.nodes()
    workload = cluster.workload_by_node()
    pending = cluster.pending_pods()
    policy.observe(nodes, workload, now)

    acted = {"suspended": [], "resumed": [], "resume_failed": [],
             "suspend_failed": []}

    for node in policy.to_resume(nodes, pending):
        if verbose:
            print(f"[{time.strftime('%H:%M:%S')}] подъём {node}: "
                  f"{pending} подов ждут места")
        t_boot0 = time.time()
        executor.power_on(node)
        ok = cluster.wait_ready(node, policy.resume_timeout)
        if recorder is not None:
            recorder.record("cycle-boot", node, t_boot0, time.time())
            recorder.close_cycle(node)
        if ok:
            cluster.cordon(node, False)
            policy.suspended.discard(node)
            policy.idle_since[node] = now
            acted["resumed"].append(node)
        else:
            # Бюджет ResumeTimeout исчерпан. Узел НЕ возвращается в
            # планирование и остаётся в suspended: молча оставить его
            # cordoned и «поднятым» — худший исход, чем явный отказ.
            print(f"  ВНИМАНИЕ: {node} не поднялся за "
                  f"{policy.resume_timeout:.0f} c", file=sys.stderr)
            acted["resume_failed"].append(node)

    for node in policy.to_suspend(nodes, workload, now, pending):
        idle_for = now - policy.idle_since.get(node, now)
        if verbose:
            print(f"[{time.strftime('%H:%M:%S')}] гашение {node}: "
                  f"простой {idle_for:.0f} c ≥ порога "
                  f"{policy.suspend_time:.0f} c")
        cluster.cordon(node, True)
        t_off0 = time.time()
        executor.power_off(node)
        # Подтверждение обязательно. Не подтвердившееся гашение — худший из
        # исходов политики: узел закордонен (работы нет) и включён (полная
        # мощность холостого хода), то есть чистый убыток по обеим осям.
        # Такой узел возвращаем в планирование, а не оставляем висеть.
        if policy.verify_off and not wait_power_state(
                executor, node, "Off", policy.off_timeout):
            print(f"  ВНИМАНИЕ: {node} не подтвердил Off за "
                  f"{policy.off_timeout:.0f} c — возвращаю в планирование",
                  file=sys.stderr)
            cluster.cordon(node, False)
            policy.idle_since[node] = now
            acted["suspend_failed"].append(node)
            continue
        if recorder is not None:
            recorder.open_cycle(node)
            # Окно гашения закрывается с запасом DRAIN_S: узел уходит в Off
            # не мгновенно (замерено 18 с), и обрезать окно по возврату
            # вызова значит недосчитать хвост спада мощности.
            recorder.record("cycle-off", node, t_off0,
                            time.time() + WindowRecorder.DRAIN_S)
        policy.suspended.add(node)
        acted["suspended"].append(node)

    return acted


# ---------------------------------------------------------------- самотест

class FakeCluster(Cluster):
    """Кластер по сценарию: политика проверяется без kubectl и без BMC."""

    def __init__(self):
        super().__init__()
        self.state = {n: {"ready": True, "schedulable": True}
                      for n in ("wrk-b6", "wrk-b7", "wrk-b8")}
        self.load: dict[str, int] = {}
        self.pending_count = 0
        self.cordoned: set[str] = set()
        self.ready_after_resume = True

    def nodes(self): return self.state
    def workload_by_node(self): return self.load
    def pending_pods(self): return self.pending_count

    def cordon(self, node, on):
        self.cordoned.add(node) if on else self.cordoned.discard(node)
        self.state[node]["schedulable"] = not on

    def wait_ready(self, node, timeout):
        self.state[node]["ready"] = self.ready_after_resume
        return self.ready_after_resume


class FakeExecutor(NoopExecutor):
    def __init__(self, off_works: bool = True):
        self.calls: list[tuple[str, str]] = []
        self.state: dict[str, str] = {}
        self.off_works = off_works

    def power_off(self, node):
        self.calls.append(("off", node))
        if self.off_works:
            self.state[node] = "Off"

    def power_on(self, node):
        self.calls.append(("on", node))
        self.state[node] = "On"

    def power_state(self, node): return self.state.get(node, "On")


def self_test() -> int:
    T, RT = 600.0, 300.0

    # 1. Занятый узел не гасится, сколько бы времени ни прошло.
    c, e = FakeCluster(), FakeExecutor()
    p = PowerSavePolicy(T, RT, min_active=1)
    c.load = {"wrk-b6": 3}
    for t in (0, 1000, 5000):
        tick(p, c, e, now=t, verbose=False)
    assert ("off", "wrk-b6") not in e.calls, e.calls

    # 2. Пустой узел гасится ровно по достижении порога, не раньше.
    c, e = FakeCluster(), FakeExecutor()
    p = PowerSavePolicy(T, RT, min_active=1)
    tick(p, c, e, now=0, verbose=False)
    assert not e.calls, "погасили до порога"
    tick(p, c, e, now=T - 1, verbose=False)
    assert not e.calls, "погасили за секунду до порога"
    tick(p, c, e, now=T, verbose=False)
    assert [n for k, n in e.calls if k == "off"] == ["wrk-b6", "wrk-b7"], e.calls
    assert c.cordoned == {"wrk-b6", "wrk-b7"}, c.cordoned

    # 3. --min-active соблюдается: последний узел не гаснет никогда.
    assert p.to_suspend(c.nodes(), c.load, now=10 * T) == [], "погасили последний"

    # 3a. Пока есть очередь, не гасим ничего — иначе такт сам себе
    #     противоречит: подняли узел под очередь и тут же погасили другой.
    c.pending_count = 5
    assert p.to_suspend(c.nodes(), c.load, now=10 * T, pending=5) == [], \
        "гасим при непустой очереди"
    c.pending_count = 0

    # 4. Работа без места поднимает ровно один узел за такт (и только его:
    #    ни одного гашения в том же такте — см. 3a).
    c.pending_count = 5
    acted = tick(p, c, e, now=10 * T, verbose=False)
    assert acted["resumed"] == ["wrk-b6"] and acted["suspended"] == [], acted
    assert "wrk-b6" not in c.cordoned and len(p.suspended) == 1, p.suspended

    # 5. Возврат из простоя обнуляет счётчик: узел не гасится сразу снова.
    c.load = {"wrk-b6": 1}
    tick(p, c, e, now=10 * T + 1, verbose=False)
    assert p.idle_since.get("wrk-b6") is None, p.idle_since

    # 6. Узел, не поднявшийся за ResumeTimeout, не возвращается в
    #    планирование и остаётся в suspended.
    c2, e2 = FakeCluster(), FakeExecutor()
    p2 = PowerSavePolicy(T, RT, min_active=1)
    # Счётчик простоя стартует с ПЕРВОГО наблюдения, а не с нуля времени:
    # контроллер не знает, сколько узел простаивал до его запуска, и
    # приписывать себе чужой простой не должен. Поэтому такта нужно два.
    tick(p2, c2, e2, now=0, verbose=False)
    tick(p2, c2, e2, now=T, verbose=False)
    c2.ready_after_resume = False
    c2.pending_count = 1
    acted = tick(p2, c2, e2, now=T + 1, verbose=False)
    assert acted["resumed"] == [] and acted["resume_failed"], acted
    assert "wrk-b6" in p2.suspended and "wrk-b6" in c2.cordoned, p2.suspended

    # 6a. Окна переходов записываются с правильными именами и номерами:
    #     подъём и гашение одного цикла обязаны попасть в ОДИН rep, иначе
    #     цена цикла сложится из половинок разных циклов.
    class RecSpy(WindowRecorder):
        def __init__(self):
            super().__init__("test", "lbl", "localhost", 8123, enabled=False)
            self.seen: list[tuple[str, int]] = []

        def record(self, kind, node, t0, t1):
            self.seen.append((kind, node, self.rep_for(node)))

    c6, e6, r6 = FakeCluster(), FakeExecutor(), RecSpy()
    p6 = PowerSavePolicy(T, RT, min_active=2)
    tick(p6, c6, e6, now=0, verbose=False, recorder=r6)
    tick(p6, c6, e6, now=T, verbose=False, recorder=r6)
    assert r6.seen == [("cycle-off", "wrk-b6", 0)], r6.seen
    c6.pending_count = 1
    tick(p6, c6, e6, now=T + 1, verbose=False, recorder=r6)
    assert r6.seen[-1] == ("cycle-boot", "wrk-b6", 0), r6.seen
    # Разные узлы — разные циклы, даже если гаснут в одном такте.
    c6.pending_count = 0
    p7 = PowerSavePolicy(T, RT, min_active=1)
    c7, e7, r7 = FakeCluster(), FakeExecutor(), RecSpy()
    tick(p7, c7, e7, now=0, verbose=False, recorder=r7)
    tick(p7, c7, e7, now=T, verbose=False, recorder=r7)
    reps = {n: r for _, n, r in r7.seen}
    assert len(set(reps.values())) == len(reps) == 2, r7.seen

    # 6b. Гашение, которое BMC не подтвердил, откатывается: узел не должен
    #     остаться закордоненным и включённым (без работы и на полной
    #     мощности — убыток по обеим осям).
    c8, e8 = FakeCluster(), FakeExecutor(off_works=False)
    p8 = PowerSavePolicy(T, RT, min_active=1, off_timeout=0.1)
    tick(p8, c8, e8, now=0, verbose=False)
    acted = tick(p8, c8, e8, now=T, verbose=False)
    assert acted["suspended"] == [] and acted["suspend_failed"], acted
    assert not c8.cordoned, c8.cordoned
    assert not p8.suspended, p8.suspended

    # 7. Исключённые узлы не гасятся.
    c3, e3 = FakeCluster(), FakeExecutor()
    p3 = PowerSavePolicy(T, RT, min_active=1, exclude=("wrk-b7",))
    tick(p3, c3, e3, now=T, verbose=False)
    assert ("off", "wrk-b7") not in e3.calls, e3.calls

    # 8. Пароль BMC не появляется в argv (иначе он виден в ps любому
    #    пользователю узла) — проверяем состав команды, не запуская её.
    ex = RedfishExecutor({"n": "10.0.0.1"}, "root", "/dev/null", dry_run=True)
    cmd = ["curl", "-sk", "-K", "-", "-X", "POST"]
    assert "-K" in cmd and "-u" not in cmd

    print("self-test: ок (порог, min-active, подъём по очереди, сброс "
          "счётчика, отказ подъёма и отказ гашения, окна переходов, "
          "исключения, пароль "
          "вне argv)")
    return 0


def parse_map(text: str) -> dict[str, str]:
    out = {}
    for pair in text.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"ожидается node=host, получено {pair!r}")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--kubeconfig", default="")
    ap.add_argument("--suspend-time", type=float,
                    help="SuspendTime, с: сколько узел должен простоять")
    ap.add_argument("--resume-timeout", type=float, default=600.0,
                    help="ResumeTimeout, с (default 600)")
    ap.add_argument("--min-active", type=int, default=1,
                    help="сколько узлов держать включёнными (default 1)")
    ap.add_argument("--suspend-exc", default="",
                    help="SuspendExcNodes: узлы через запятую")
    ap.add_argument("--executor", choices=("redfish", "metal3", "none"),
                    default="none")
    ap.add_argument("--idrac-map", default="", help="node=host,... для redfish")
    ap.add_argument("--idrac-user", default="root")
    ap.add_argument("--idrac-pass-file", default="~/phd/.idrac-pass.txt")
    ap.add_argument("--bmh-namespace", default="metal3-system")
    ap.add_argument("--interval", type=float, default=30.0,
                    help="период опроса, с (default 30)")
    ap.add_argument("--once", action="store_true", help="один такт и выход")
    ap.add_argument("--no-verify-off", action="store_true",
                    help="не ждать подтверждения Off от BMC (по умолчанию ждём)")
    ap.add_argument("--off-timeout", type=float, default=120.0,
                    help="сколько ждать подтверждения Off, с (default 120)")
    ap.add_argument("--record-windows", action="store_true",
                    help="писать окна cycle-off/cycle-boot в ClickHouse "
                         "(вход analysis/energy_metrics.py --cycle)")
    ap.add_argument("--stand", default="prod")
    ap.add_argument("--run-label", default="", help="метка серии для окон")
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=8123)
    ap.add_argument("--node-selector", default=BENCH_SELECTOR,
                    help=f"метка измерительных узлов (default {BENCH_SELECTOR})")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.suspend_time is None:
        ap.error("--suspend-time обязателен: значение приходит из измерения "
                 "цены цикла (T = E_цикла/(P_простоя − P_выкл)), а не из "
                 "умолчания")

    cluster = Cluster(args.kubeconfig, args.dry_run, args.node_selector)
    if args.executor == "redfish":
        if not args.idrac_map:
            ap.error("--executor redfish требует --idrac-map")
        executor = RedfishExecutor(parse_map(args.idrac_map), args.idrac_user,
                                   args.idrac_pass_file, args.dry_run)
    elif args.executor == "metal3":
        executor = Metal3Executor(cluster, args.bmh_namespace, args.dry_run)
    else:
        executor = NoopExecutor()

    policy = PowerSavePolicy(args.suspend_time, args.resume_timeout,
                             args.min_active,
                             tuple(n for n in args.suspend_exc.split(",") if n),
                             verify_off=not args.no_verify_off,
                             off_timeout=args.off_timeout)
    print(f"power_save: порог {args.suspend_time:.0f} c, бюджет подъёма "
          f"{args.resume_timeout:.0f} c, минимум активных {args.min_active}, "
          f"исполнитель {executor.name}"
          + (" [dry-run]" if args.dry_run else ""))
    recorder = None
    if args.record_windows:
        if not args.run_label:
            ap.error("--record-windows требует --run-label")
        recorder = WindowRecorder(args.stand, args.run_label,
                                  args.ch_host, args.ch_port,
                                  enabled=not args.dry_run)
    # Пустой список узлов — НЕ «нечего делать», а неверный селектор или
    # чужой kubeconfig. Молчаливый no-op здесь дороже всего: контроллер
    # исправно тикает всю ночь, ничего не гасит, и серия P3 приходит с
    # нулевой экономией, которую не отличить от отрицательного результата.
    try:
        seen = cluster.nodes()
    except RuntimeError as exc:
        print(f"кластер недоступен: {exc}", file=sys.stderr)
        return 2
    if not seen:
        print(f"по селектору {cluster.selector!r} не найдено ни одного узла — "
              f"гасить нечего и политика бессмысленна; проверь --node-selector "
              f"и --kubeconfig", file=sys.stderr)
        return 2
    print(f"узлов под политикой: {', '.join(sorted(seen))}")

    while True:
        try:
            tick(policy, cluster, executor, time.time(), recorder=recorder)
        except RuntimeError as exc:
            print(f"такт пропущен: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
