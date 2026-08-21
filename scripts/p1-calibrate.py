#!/usr/bin/env python3
"""p1-calibrate.py — фаза P1: ступенчатая калибровка модели мощности узла.

Держит на ОДНОМ узле последовательность уровней CPU-нагрузки (stress-ng
--cpu-load, по умолчанию 0→100 % с шагом 10), на каждой ступени:
маркер эпохи в stdout → выдержка --settle (тепловое установление) → окно
--hold с периодическим замером фактической утилизации через metrics.k8s.io
(ТОТ ЖЕ источник, из которого load-watcher кормит Peaks — x модели обязан
быть в его единицах, процентах ёмкости узла) → маркер DONE.

После прогона для каждого источника энергии зовёт energy-window.py по
границам каждой ступени и пишет:
  steps.csv          step,load,t0,t1,x       (фактическая утилизация)
  calib-<источник>.csv  x,watts,node          (вход analysis/fit_power_model.py)
и, если задан --ch-host, вставляет окна calib-step-<n> в ClickHouse.

Фит по предрегистрации — по PDU; до Д2 калибровку можно прогнать по ipmi
(само сравнение фитов по двум источникам — материал §5 статьи).

Под нагрузки садится через nodeName (мимо планировщиков — калибровке не
нужен ни один из них) в bench-namespace; Guaranteed-пиннинг сознательно НЕ
используется: модели нужна суммарная утилизация узла, а не изоляция.

Запуск НЕ во время серий (узел греется; preflight серий увидит чужой под).
--dry-run печатает план и команды, ничего не запуская.

  p1-calibrate.py --node wrk-b6 --prom http://localhost:19090 \\
      --kubeconfig ~/.kube/configs/prod --sources ipmi [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

_EW = pathlib.Path(__file__).with_name("energy-window.py")
_spec = importlib.util.spec_from_file_location("energy_window", _EW)
_ew = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ew)

NS = "sensitivityscore-bench"
POD = "p1-calib-load"

POD_TMPL = """\
apiVersion: v1
kind: Pod
metadata:
  name: {pod}
  namespace: {ns}
  labels: {{app: p1-calib}}
spec:
  nodeName: {node}
  restartPolicy: Never
  containers:
    - name: stress
      image: {image}
      command: ["stress-ng", "--cpu", "{cpus}", "--cpu-load", "{load}",
                "--timeout", "{timeout}s"]
      resources:
        requests: {{cpu: "1", memory: 256Mi}}
        limits: {{memory: 2Gi}}
"""

# Источник -> аргументы energy-window.py (PDU дополняется из CLI).
# RAPL — через sum by(node): у узла ДВЕ package-зоны (и две dram), а
# energy-window ключует по метке узла — без суммы вторая зона молча
# затёрла бы первую. Дельта суммы = сумме дельт; сброс одной зоны
# (рестарт агента) роняет и сумму — защита от сброса сохраняет силу.
SOURCES = {
    "ipmi":      ["--metric", "idrac_power_watts", "--node-label", "node",
                  "--source", "ipmi", "--mode", "power"],
    "rapl-pkg":  ["--metric",
                  'sum by(node)(ss_node_rapl_joules_total{domain=~"package-.*"})',
                  "--node-label", "node", "--source", "rapl-pkg"],
    "rapl-dram": ["--metric",
                  'sum by(node)(ss_node_rapl_joules_total{domain="dram"})',
                  "--node-label", "node", "--source", "rapl-dram"],
}


def sh(args, cmd: list[str], **kw) -> subprocess.CompletedProcess:
    if args.dry_run:
        print("  $ " + " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def kubectl(args, *rest: str) -> list[str]:
    base = ["kubectl"]
    if args.kubeconfig:
        base += ["--kubeconfig", args.kubeconfig]
    return base + list(rest)


def node_util_percent(args) -> float | None:
    """Утилизация узла из metrics.k8s.io, % ёмкости — единицы load-watcher."""
    r = sh(args, kubectl(args, "get", "--raw",
                         f"/apis/metrics.k8s.io/v1beta1/nodes/{args.node}"))
    if args.dry_run or r.returncode != 0:
        return None
    usage = json.loads(r.stdout)["usage"]["cpu"]          # напр. "1234567890n"
    cores = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
    used = (float(usage[:-1]) * cores[usage[-1]] if usage[-1] in cores
            else float(usage))
    r = sh(args, kubectl(args, "get", "node", args.node,
                         "-o", "jsonpath={.status.capacity.cpu}"))
    return used / float(r.stdout) * 100.0


def run_step(args, load: int) -> tuple[float, float, float | None]:
    """Одна ступень: под (при load>0) -> settle -> окно с замерами x."""
    print(f"=== ступень {load}% ===", flush=True)
    sh(args, kubectl(args, "-n", NS, "delete", "pod", POD,
                     "--ignore-not-found", "--now"))
    if load > 0:
        manifest = POD_TMPL.format(pod=POD, ns=NS, node=args.node,
                                   image=args.image, cpus=args.cpus, load=load,
                                   timeout=args.settle + args.hold + 300)
        if args.dry_run:
            print("  kubectl apply -f - <<манифест stress-ng "
                  f"--cpu {args.cpus} --cpu-load {load}>>")
        else:
            r = subprocess.run(kubectl(args, "apply", "-f", "-"),
                               input=manifest, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"apply пода: {r.stderr}")
            r = subprocess.run(kubectl(args, "-n", NS, "wait", "--for=condition=Ready",
                                       f"pod/{POD}", "--timeout=180s"),
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"под не вышел в Ready: {r.stderr}")
    if not args.dry_run:
        time.sleep(args.settle)
    t0 = time.time()
    print(f"P1 STEP {load} START epoch={t0:.0f}", flush=True)
    xs: list[float] = []
    if args.dry_run:
        print(f"  <окно {args.hold}с, замер утилизации каждые {args.sample}с>")
    else:
        end = t0 + args.hold
        while time.time() < end:
            u = node_util_percent(args)
            if u is not None:
                xs.append(u)
            time.sleep(min(args.sample, max(1.0, end - time.time())))
    t1 = time.time() if not args.dry_run else t0 + args.hold
    print(f"P1 STEP {load} DONE epoch={t1:.0f}", flush=True)
    x = sum(xs) / len(xs) if xs else None
    if x is not None:
        print(f"  фактическая утилизация: {x:.1f}% ({len(xs)} замеров)")
    return t0, t1, x


def collect_energy(args, steps: list[dict]) -> None:
    """Окна энергии по ступеням: dry-run строки energy-window -> calib-CSV
    (+ вставка в ClickHouse тем же вызовом, если задан --ch-host)."""
    for source in args.sources:
        ew_extra = SOURCES.get(source)
        if ew_extra is None:  # pdu
            ew_extra = ["--metric", args.pdu_metric, "--node-label", args.pdu_label,
                        "--source", "pdu", "--factor", str(args.pdu_factor)]
        rows = []
        for s in steps:
            argv = ["--prom", args.prom, "--t0", f"{s['t0']:.0f}",
                    "--t1", f"{s['t1']:.0f}", "--window", f"calib-step-{s['load']}",
                    "--stand", args.stand, "--run-label", args.run_label,
                    "--dry-run"] + ew_extra
            if args.dry_run:
                print("  energy-window.py " + " ".join(argv))
                continue
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _ew.run(_ew.parse_args(argv))
            for line in buf.getvalue().splitlines():
                if not line.startswith("{"):
                    continue
                r = json.loads(line)
                if r["node"] != args.node or s["x"] is None:
                    continue
                rows.append({"x": f"{s['x']:.2f}",
                             "watts": f"{r['avg_power_w']:.2f}",
                             "node": args.node})
            if args.ch_host:
                _ew.run(_ew.parse_args(
                    [a for a in argv if a != "--dry-run"]
                    + ["--ch-host", args.ch_host, "--ch-port", str(args.ch_port)]))
        if args.dry_run:
            continue
        path = pathlib.Path(args.out_dir) / f"calib-{source}.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["x", "watts", "node"])
            w.writeheader()
            w.writerows(rows)
        print(f"записано: {path} ({len(rows)} ступеней) — вход fit_power_model.py")


def steps_from_csv(path) -> list[dict]:
    """Ступени уже снятой лестницы: границы окон из steps.csv.

    Нужно, чтобы пересчитать окна энергии, НЕ повторяя двухчасовой прогон:
    Prometheus держит ряды 365 дней, и границы ступеней — единственное, чего
    не хватает для пересчёта. Так лестница P1 доехала до ClickHouse задним
    числом: снималась она до того, как окна стали писаться в базу, и жила
    только в CSV репозитория.
    """
    steps = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            steps.append({"load": int(row["load"]),
                          "t0": float(row["t0"]), "t1": float(row["t1"]),
                          "x": float(row["x"]) if row["x"] else None})
    return steps


def main(argv) -> int:
    args = parse_args(argv)
    if args.from_steps:
        steps = steps_from_csv(args.from_steps)
        print(f"пересчёт окон по {args.from_steps}: {len(steps)} ступеней, "
              f"узел {args.node}, нагрузка НЕ запускается")
        collect_energy(args, steps)
        return 0
    loads = [int(x) for x in args.steps.split(",")]
    est = len(loads) * (args.settle + args.hold) / 60
    print(f"узел {args.node}, ступени {loads}, ~{est:.0f} мин"
          + (" [DRY-RUN]" if args.dry_run else ""))
    pathlib.Path(args.out_dir).mkdir(parents=True, exist_ok=True)

    steps = []
    try:
        for load in loads:
            t0, t1, x = run_step(args, load)
            steps.append({"load": load, "t0": t0, "t1": t1, "x": x})
    finally:
        sh(args, kubectl(args, "-n", NS, "delete", "pod", POD,
                         "--ignore-not-found", "--now"))

    if not args.dry_run:
        path = pathlib.Path(args.out_dir) / "steps.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["step", "load", "t0", "t1", "x"])
            w.writeheader()
            for i, s in enumerate(steps):
                w.writerow({"step": i, "load": s["load"], "t0": f"{s['t0']:.0f}",
                            "t1": f"{s['t1']:.0f}",
                            "x": "" if s["x"] is None else f"{s['x']:.2f}"})
        print(f"записано: {path}")
    collect_energy(args, steps)
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--node", required=True)
    ap.add_argument("--prom", required=True)
    ap.add_argument("--kubeconfig", default="")
    ap.add_argument("--steps", default="0,10,20,30,40,50,60,70,80,90,100")
    ap.add_argument("--hold", type=int, default=600,
                    help="длительность окна ступени, с (Э0.1 хочет ≥600)")
    ap.add_argument("--settle", type=int, default=60,
                    help="выдержка после смены нагрузки до окна, с")
    ap.add_argument("--sample", type=float, default=30.0,
                    help="период замера утилизации, с")
    ap.add_argument("--image", default="andreyza/aggressor:dev")
    ap.add_argument("--cpus", type=int, default=64,
                    help="потоков stress-ng (все ядра узла: SMT off = 64)")
    ap.add_argument("--sources", default="ipmi,rapl-pkg,rapl-dram",
                    help="через запятую: ipmi | rapl-pkg | rapl-dram | pdu")
    ap.add_argument("--pdu-metric", default="")
    ap.add_argument("--pdu-label", default="pdu")
    ap.add_argument("--pdu-factor", type=float, default=3.6e6)
    ap.add_argument("--stand", default="prod")
    ap.add_argument("--run-label", default="p1-calib")
    ap.add_argument("--out-dir", default="analysis/p1-calib")
    ap.add_argument("--ch-host", default="", help="вставлять окна в ClickHouse")
    ap.add_argument("--ch-port", type=int, default=8123)
    ap.add_argument("--from-steps", default="",
                    help="пересчитать окна по готовому steps.csv, "
                         "не запуская нагрузку")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    args.sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in args.sources if s not in SOURCES and s != "pdu"]
    if unknown:
        ap.error(f"неизвестные источники: {unknown}")
    if "pdu" in args.sources and not args.pdu_metric:
        ap.error("--sources pdu требует --pdu-metric")
    return args


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
