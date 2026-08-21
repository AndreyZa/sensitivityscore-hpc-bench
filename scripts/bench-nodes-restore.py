#!/usr/bin/env python3
"""bench-nodes-restore.py — вернуть измерительные узлы в строй после P3.

ЗАЧЕМ. Гашение в P3 делает внешний контроллер (scripts/power-save.py).
Раннер снимает контроллер и раскордонивает узлы всегда, даже когда проход
упал, но ВКЛЮЧИТЬ погашенный узел он не умеет: если контроллер умер между
power_off и подъёмом — или прогон убили ровно в этот момент, — узел так и
останется выключенным. Следующая серия тогда тихо померяет два узла вместо
трёх, и по числам результата этого не видно вовсе: Дж/задача посчитается,
интервалы сойдутся, а стенд будет другой.

Что делает: по каждому bench-узлу смотрит питание через BMC и состояние в
кластере; выключенный включает, закордоненный раскордонивает, ждёт Ready.
Узел, который не вернулся, называет по имени и возвращает ненулевой код —
очередь прогонов на таком коде останавливается.

  bench-nodes-restore.py --idrac-map wrk-b6=10.21.200.106,wrk-b7=...
  bench-nodes-restore.py --dry-run
  bench-nodes-restore.py --self-test
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

# power-save.py через дефис — обычным import не берётся, а дублировать
# Redfish-исполнитель и доступ к кластеру ради одного скрипта тем более
# незачем: пароль BMC читается там через stdin curl'а, и второй такой
# реализации в репозитории быть не должно.
_spec = importlib.util.spec_from_file_location(
    "power_save", pathlib.Path(__file__).resolve().with_name("power-save.py"))
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


def restore(cluster, executor, wait_s: float = 600.0) -> list[str]:
    """Возвращает список узлов, которые вернуть НЕ удалось."""
    failed = []
    for node, st in sorted(cluster.nodes().items()):
        state = executor.power_state(node)
        acted = []
        if state not in ("On", "Unknown"):
            executor.power_on(node)
            acted.append(f"включён (был {state})")
        if not st["schedulable"]:
            cluster.cordon(node, False)
            acted.append("раскордонен")
        # Ready ждём и после включения, и когда узел просто ещё не поднялся:
        # выключить его мог не только контроллер.
        if acted or not st["ready"]:
            if cluster.wait_ready(node, wait_s):
                acted.append("Ready")
            else:
                failed.append(node)
                acted.append(f"НЕ поднялся за {wait_s:.0f} c")
        print(f"  {node}: {', '.join(acted) if acted else 'в строю'}")
    return failed


def self_test() -> int:
    cluster, ex = ps.FakeCluster(), ps.FakeExecutor()

    # Узел выключен контроллером и закордонен — обязан вернуться целиком.
    ex.state["wrk-b7"] = "Off"
    cluster.cordon("wrk-b7", True)
    cluster.state["wrk-b7"]["ready"] = False
    assert restore(cluster, ex) == []
    assert ("on", "wrk-b7") in ex.calls, ex.calls
    assert cluster.state["wrk-b7"]["schedulable"] and cluster.state["wrk-b7"]["ready"]

    # Здоровый стенд не трогаем: лишний power_on — это ребут узла.
    ex.calls.clear()
    assert restore(cluster, ex) == []
    assert ex.calls == [], ex.calls

    # Узел не поднялся — это отказ, а не «ну ладно».
    cluster2, ex2 = ps.FakeCluster(), ps.FakeExecutor()
    ex2.state["wrk-b8"] = "Off"
    cluster2.ready_after_resume = False
    assert restore(cluster2, ex2, wait_s=1.0) == ["wrk-b8"]

    print("self-test: ок (гашеный узел возвращается, здоровый не трогается, "
          "не поднявшийся считается отказом)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--kubeconfig", default="")
    ap.add_argument("--idrac-map", default="", help="node=host,... для Redfish")
    ap.add_argument("--idrac-user", default="root")
    ap.add_argument("--idrac-pass-file", default="~/phd/.idrac-pass.txt")
    ap.add_argument("--wait", type=float, default=600.0,
                    help="сколько ждать Ready после включения, с")
    ap.add_argument("--node-selector", default=ps.BENCH_SELECTOR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.idrac_map:
        ap.error("--idrac-map обязателен: без BMC включить узел нечем")

    idrac = dict(p.split("=", 1) for p in args.idrac_map.split(",") if p)
    cluster = ps.Cluster(args.kubeconfig, args.dry_run, args.node_selector)
    executor = ps.RedfishExecutor(idrac, args.idrac_user,
                                  args.idrac_pass_file, args.dry_run)
    print("проверка измерительных узлов:")
    failed = restore(cluster, executor, args.wait)
    if failed:
        print(f"НЕ ВЕРНУЛИСЬ: {', '.join(failed)} — стенд неполный, "
              f"следующую серию запускать нельзя", file=sys.stderr)
        return 1
    print("все измерительные узлы в строю")
    return 0


if __name__ == "__main__":
    sys.exit(main())
