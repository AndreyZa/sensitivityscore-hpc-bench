"""Как звать energy-window.py для каждого источника энергии — ОДНО место.

Определение источника (метрика, метка узла, режим) нужно трём вызывающим:
калибровке лестницы, окнам плеч серии и контроллеру гашения. Пока оно
жило в каждом по отдельности, копии разошлись: WindowRecorder в
power-save.py звал energy-window.py с флагами `--start/--end` вместо
`--t0/--t1` и вовсе без `--prom` и `--metric`. Ошибка не всплывала до
первого живого цикла гашения (21.08.2026): политика отработала, узел
погас и поднялся, а окна cycle-off/cycle-boot не записались — то есть
цена цикла, ради которой цикл и гоняли, потерялась.

Сюда же вынесена сборка команды целиком: вызывающему остаётся передать
окно и адреса, перепутать порядок флагов негде.
"""
from __future__ import annotations

import pathlib
import sys

EW = pathlib.Path(__file__).with_name("energy-window.py")

# Аргументы источника. Одно определение — иначе окна серий, окна лестницы
# и окна циклов окажутся посчитаны по-разному и станут несравнимы.
SOURCES: dict[str, list[str]] = {
    "ipmi":      ["--metric", "idrac_power_watts", "--node-label", "node",
                  "--source", "ipmi", "--mode", "power"],
    "rapl-pkg":  ["--metric",
                  'sum by(node)(ss_node_rapl_joules_total{domain=~"package-.*"})',
                  "--node-label", "node", "--source", "rapl-pkg"],
    "rapl-dram": ["--metric",
                  'sum by(node)(ss_node_rapl_joules_total{domain="dram"})',
                  "--node-label", "node", "--source", "rapl-dram"],
}


def window_cmd(source: str, prom: str, t0: float, t1: float, window: str,
               config: str, stand: str, run_label: str,
               ch_host: str, ch_port: int, dry_run: bool = False) -> list[str]:
    """Полная командная строка energy-window.py для одного окна."""
    if source not in SOURCES:
        raise ValueError(f"неизвестный источник энергии: {source!r}")
    cmd = [sys.executable, str(EW), "--prom", prom, *SOURCES[source],
           "--factor", "1", "--t0", str(int(t0)), "--t1", str(int(t1)),
           "--window", window, "--config", config,
           "--stand", stand, "--run-label", run_label,
           "--ch-host", ch_host, "--ch-port", str(ch_port)]
    if dry_run:
        cmd.append("--dry-run")
    return cmd
