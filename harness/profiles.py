"""profiles.py — low-S / high-S parameter tables, mirroring
docs/Технический_план_экспериментов.md §1.2 and the example manifests in
k8s/config-a-baremetal/. Kept in one place so the harness, the analysis pipeline,
and manual sanity-checks all agree on what "low-s"/"high-s" mean numerically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _ov(profile: str, key: str, default: str) -> str:
    """Per-stand override of a profile knob via env var, keeping the default
    otherwise. The plan's profiles.py comments already say "adjust here per
    stand" for threads/cpu — this makes it an env override instead of an edit,
    so one shared profiles.py fits nodes of different sizes (e.g. the STAGE
    cloud cluster has 2-vCPU nodes and can't take the default cpu=8 high-s).

    Env key: HARNESS_OVERRIDE_<PROFILE>_<KEY>, PROFILE upper-cased with '-'->'_',
    e.g. HARNESS_OVERRIDE_HIGH_S_IO_CPU=1, HARNESS_OVERRIDE_HIGH_S_THREADS=1,
    HARNESS_OVERRIDE_LOW_S_PRIMARIES=200000."""
    env_key = f"HARNESS_OVERRIDE_{profile.upper().replace('-', '_')}_{key}"
    return os.environ.get(env_key, default)


@dataclass(frozen=True)
class Sensitivity:
    llc: str
    numa: str
    net: str
    io: str


@dataclass(frozen=True)
class ProfileSpec:
    env: dict[str, str]
    sensitivity: Sensitivity
    resources: dict[str, str]


PROFILES: dict[str, ProfileSpec] = {
    "low-s": ProfileSpec(
        env={
            "G4_THREADS": _ov("low-s", "THREADS", "1"),
            "PHYSICS_LIST": "QGSP_BERT",
            # 300k, not 10k: at 10k the job finished in ~1-2s — below the
            # stand's measurement resolution (makespan dominated by container
            # startup + Geant4 physics-table init, and the metrics agent got
            # 0-1 real samples per job even at a 1s tick; the first full
            # matrix run returned 180/180 low-s rows without metrics). Low
            # SENSITIVITY must not mean short DURATION — this targets a
            # ~30-60s runtime so interference on low-s jobs is measurable.
            "N_PRIMARIES": _ov("low-s", "PRIMARIES", "300000"),
            "OUTPUT_MODE": "none",
            "RNG_SEED": "42",
        },
        sensitivity=Sensitivity(llc="low", numa="low", net="low", io="low"),
        resources={"cpu": _ov("low-s", "CPU", "1"), "memory_request": "1Gi", "memory_limit": "2Gi"},
    ),
    "high-s": ProfileSpec(
        env={
            "G4_THREADS": _ov("high-s", "THREADS", "8"),  # = physical cores per
            # NUMA domain on the target node — adjust per stand (env override)
            "PHYSICS_LIST": "FTFP_BERT_HP",
            "N_PRIMARIES": _ov("high-s", "PRIMARIES", "1000000"),
            "OUTPUT_MODE": "none",
            "RNG_SEED": "42",
        },
        sensitivity=Sensitivity(llc="high", numa="high", net="low", io="low"),
        resources={
            "cpu": _ov("high-s", "CPU", "8"),
            "memory_request": _ov("high-s", "MEM_REQ", "4Gi"),
            "memory_limit": _ov("high-s", "MEM_LIM", "6Gi"),
        },
    ),
    # Жертва Net pressure-сценария: ДЕКЛАРИРУЕТ net=high (плагин уводит её от
    # нод с высоким net_pressure), по вычислительной части — тот же Geant4.
    # ЧЕСТНАЯ ОГОВОРКА: сам воркер сети пока не генерирует (OUTPUT_MODE=none),
    # так что fingerprint-таблица покажет для net "заявлено high / измерено
    # ~0" — сценарий валидирует ДЕТЕКЦИЮ шторма агентом и РЕШЕНИЯ планировщика
    # (placement/regret), а не деградацию самой жертвы от сетевой контенции.
    # Воркер с реальным сетевым выводом (стрим результатов на sink, как burst
    # для IO) — осознанный TODO в README.
    "high-s-net": ProfileSpec(
        env={
            "G4_THREADS": _ov("high-s-net", "THREADS", "1"),
            "PHYSICS_LIST": "QGSP_BERT",
            "N_PRIMARIES": _ov("high-s-net", "PRIMARIES", "300000"),
            # По умолчанию none (детекция/уклонение). Режим stream (реальный
            # сетевой вывод на sink, крит. путь для cˢ_net) включается
            # оверрайдом HARNESS_OVERRIDE_HIGH_S_NET_OUTPUT_MODE=stream в серии
            # — ПОСЛЕ smoke-проверки, что сетевой шторм тормозит стрим (по
            # аналогии с диском). NET_* — ручки стрима (sink и объём).
            "OUTPUT_MODE": _ov("high-s-net", "OUTPUT_MODE", "none"),
            "NET_SINK_HOST": _ov("high-s-net", "NET_SINK_HOST", "ss-sink"),
            "NET_SINK_PORT": _ov("high-s-net", "NET_SINK_PORT", "9000"),
            "NET_TOTAL_MB": _ov("high-s-net", "NET_TOTAL_MB", "512"),
            "RNG_SEED": "42",
        },
        sensitivity=Sensitivity(llc="low", numa="low", net="high", io="low"),
        resources={
            "cpu": _ov("high-s-net", "CPU", "1"),
            "memory_request": _ov("high-s-net", "MEM_REQ", "1Gi"),
            "memory_limit": _ov("high-s-net", "MEM_LIM", "2Gi"),
        },
    ),
    # high-s + реальный дисковый вывод на КРИТИЧЕСКОМ ПУТИ (blocking-писатель
    # entrypoint.sh) — жертва IO pressure-сценария: декларирует io=high И
    # реально страдает от дисковой контенции. OUTPUT_MODE=blocking: job не
    # завершается, пока вывод не сброшен на диск (gate exit на fsync-хвост),
    # поэтому под штормом makespan жертвы РАСТЁТ — это делает cˢ_io > 0
    # измеримым (в отличие от burst, где писатель фоновый и makespan от диска
    # не зависит: тот режим мерил детекцию/уклонение, а не деградацию).
    # IO_TOTAL_BURSTS×IO_BURST_MB — доза вывода; калибруется smoke под целевой
    # штраф (на простое диска писатель спрятан за compute, под штормом —
    # обнажается). NB: high-s-io и низкочувствительный контраст low-s под тем
    # же штормом дают разность slowdown = вклад cˢ_io.
    # Двойник high-s-io для серии differentiation: ТОТ ЖЕ compute и ТЕ ЖЕ
    # ресурсы, но без дискового вывода (OUTPUT_MODE=none) и с io=low. Нужен,
    # чтобы профиль-слепой планировщик (default) не мог развести профили по
    # ресурсным заявкам (как в io-sensitivity, где low-s просил 1Gi против
    # 384Mi у high-s-io) — тогда любое различие размещения принадлежит учёту
    # чувствительности. Под диск-штормом платит только базовую цену c⁰_io
    # (через iowait на compute), надбавку cˢ_io — нет.
    "io-insensitive": ProfileSpec(
        env={
            "G4_THREADS": _ov("io-insensitive", "THREADS", "8"),
            "PHYSICS_LIST": "FTFP_BERT_HP",
            "N_PRIMARIES": _ov("io-insensitive", "PRIMARIES", "1000000"),
            "OUTPUT_MODE": "none",
            "RNG_SEED": "42",
        },
        sensitivity=Sensitivity(llc="high", numa="high", net="low", io="low"),
        resources={
            "cpu": _ov("io-insensitive", "CPU", "8"),
            "memory_request": _ov("io-insensitive", "MEM_REQ", "4Gi"),
            "memory_limit": _ov("io-insensitive", "MEM_LIM", "6Gi"),
        },
    ),
    "high-s-io": ProfileSpec(
        env={
            "G4_THREADS": _ov("high-s-io", "THREADS", "8"),
            "PHYSICS_LIST": "FTFP_BERT_HP",
            "N_PRIMARIES": _ov("high-s-io", "PRIMARIES", "1000000"),
            # blocking = compute + последовательный сброс V=IO_TOTAL×IO_BURST_MB
            # с fsync. Диск-шторм STAGE режет запись ×~23 (замерено): ~1ГБ =
            # сброс ~2с на чистом узле, ~55с под штормом. interval=0 — сброс
            # = чистое IO-время (без сна), полностью чувствителен к шторму.
            "OUTPUT_MODE": _ov("high-s-io", "OUTPUT_MODE", "blocking"),
            "IO_BURST_MB": _ov("high-s-io", "IO_BURST_MB", "32"),
            "IO_INTERVAL_SECONDS": _ov("high-s-io", "IO_INTERVAL_SECONDS", "0"),
            "IO_TOTAL_BURSTS": _ov("high-s-io", "IO_TOTAL_BURSTS", "32"),
            "RNG_SEED": "42",
        },
        sensitivity=Sensitivity(llc="high", numa="high", net="low", io="high"),
        resources={
            "cpu": _ov("high-s-io", "CPU", "8"),
            "memory_request": _ov("high-s-io", "MEM_REQ", "4Gi"),
            "memory_limit": _ov("high-s-io", "MEM_LIM", "6Gi"),
        },
    ),
}


def make_job_id(config: str, profile: str, overcommit: float, rep: int) -> str:
    """job_id naming scheme used consistently across §1.3 annotations, Redis keys
    (job:metrics:<job_id>:<node>), and the results schema (§5.1)."""
    return f"{config}-{profile}-oc{overcommit}-rep{rep:02d}"
