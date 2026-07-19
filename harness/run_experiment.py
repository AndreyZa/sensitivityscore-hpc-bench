#!/usr/bin/env python3
"""run_experiment.py — experiment orchestration harness (docs §4).

Iterates over the full plan matrix (config × profile × overcommit × repetition),
dispatches submission to the right backend (k8s_submit / slurm_submit) per
config, waits for completion, records makespan + job:metrics:* from Redis into a
Parquet dataset matching the §5.1 schema (extended with batch_size/batch_index —
see below):

    config | profile | overcommit | rep | node | makespan_s | makespan_source |
    submit_ts | start_ts | end_ts | llc_miss_rate | numa_remote_ratio | net_bw |
    io_iops | io_pressure | approximation | batch_size | batch_index |
    interference_chosen | placement_regret | sensitivity_{llc,numa,net,io}

    interference_chosen/placement_regret — качество решения планировщика по
    снапшоту node:metrics на момент сабмита (submit/node_pressure.py);
    sensitivity_* — заявленный S-вектор профиля (для fingerprint-таблицы
    «заявленный vs измеренный» в analysis).

makespan_s is the job's pure runtime measured by the cluster itself — pod
container terminated startedAt->finishedAt for K8s backends, sacct Elapsed for
Slurm — so K8s and Slurm configs are compared on the same definition (queue
wait, image pull and pod startup excluded; harness-side wall clock is only a
tagged fallback, see makespan_source). submit_ts/start_ts/end_ts let analysis
verify that batch members actually overlapped in time on the node.

IMPORTANT (fixed after a pilot run showed no real interference effect):
overcommit ratio now actually drives how many jobs run CONCURRENTLY for a
given plan point — previously it was purely a label (job_id/log/results
column) with zero effect on submission behavior, meaning every job ran in
total isolation regardless of its overcommit value, so H1's core
co-location scenario was never actually being exercised. See
batch_size_for() / node_capacity_jobs in config.yaml.

Usage:
    python run_experiment.py --config config.yaml [--pilot] [--configs A] [--dry-run]

--pilot restricts to 1 plan point (profile=high-s, overcommit=2.0) x 3 reps on
config A, matching the checklist item 9 sanity-check ("Прогнать пилотную серию
(1 точка плана, 3 повтора) на A — sanity-check всего пайплайна перед полной
матрицей").
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import random
import sys
import time
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(
    0, str(Path(__file__).parent)
)  # allow `from submit import ...` / `from profiles import ...`

import config_loader
import provenance
from profiles import PROFILES, make_job_id
from submit import aggressors, k8s_submit, node_pressure, slurm_submit
from submit.redis_metrics import purge_job_metrics

# Провенанс серии: чем снята строка (коммит, конфиг, калибровки, веса).
# Постоянен в пределах прогона, поэтому собирается один раз и подмешивается
# в каждую строку — включая строки ошибок, иначе неудачные точки плана
# оказались бы без контекста ровно там, где он нужнее всего.
# Модульный уровень, а не параметр: строки собираются в шести местах, и
# протаскивать неизменяемое значение через все — больше шансов забыть.
_PROVENANCE: dict[str, str] = {}


def init_provenance(cfg: dict) -> None:
    global _PROVENANCE
    _PROVENANCE = provenance.collect(
        cfg,
        cfg.get("kubernetes", {}).get("system_namespace", "sensitivityscore-system"),
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_experiment")

BACKENDS = {"k8s": k8s_submit, "slurm": slurm_submit}


def expand_configs(cfg: dict) -> list[str]:
    """Configs A and B are each split into scheduler-variant arms sharing the
    same infra (docs §4: direct comparison of scheduler variants within one
    infra config — H1 for A, H2 for B). C and D run as single variants.

    Which arms A/B expand into is driven by cfg["scheduler_variants"] (default
    ["default", "sensitivityscore"]). Add "trimaran" there to enable the
    load-aware control baseline (H1-trimaran) as a third arm — opt-in, since
    it multiplies the matrix/pressure cost by another arm and needs
    metrics-server on the stand (see scheduler-config.yaml)."""
    variants = cfg.get("scheduler_variants", ["default", "sensitivityscore"])
    expanded = []
    for c in cfg["configs"]:
        if c in ("A", "B"):
            expanded.extend([f"{c}-{v}" for v in variants])
        else:
            expanded.append(c)
    return expanded


def backend_for(config: str, cfg: dict) -> str:
    base_config = config.split("-")[0]  # "A-default" -> "A"
    return cfg["backends"][base_config]


# Delay before the single submit-phase retry — short on purpose: a member
# resubmitted much later would no longer run concurrently with its batch.
RESUBMIT_DELAY_SECONDS = 5


def sensitivity_columns(profile: str) -> dict:
    """Заявленный S-вектор профиля -> колонки строки результата. Нужен
    fingerprint-таблице анализа («заявленный vs измеренный S») — analysis
    не импортирует harness/profiles.py, декларация едет в самих данных."""
    spec = PROFILES.get(profile)
    if spec is None:
        return {f"sensitivity_{axis}": None for axis in ("llc", "numa", "net", "io")}
    s = spec.sensitivity
    return {
        "sensitivity_llc": s.llc,
        "sensitivity_numa": s.numa,
        "sensitivity_net": s.net,
        "sensitivity_io": s.io,
    }


def error_row(
    config: str, profile: str, overcommit: float, rep: int, exc: Exception, **extra
) -> dict:
    """One results row for a failed attempt — keeps the parquet schema stable
    so a partial matrix still loads in analysis (rows are dropped there by
    the error: prefix in `approximation`)."""
    row = {
        "config": config,
        "profile": profile,
        "overcommit": overcommit,
        "rep": rep,
        "node": None,
        "makespan_s": float("nan"),
        "makespan_source": None,
        "submit_ts": None,
        "start_ts": None,
        "end_ts": None,
        "llc_miss_rate": float("nan"),
        "numa_remote_ratio": float("nan"),
        "net_bw": float("nan"),
        "net_pressure": float("nan"),
        "io_iops": float("nan"),
        "io_pressure": float("nan"),
        "approximation": f"error:{exc}",
        "scenario": None,
        "batch_size": None,
        "batch_index": None,
        "interference_chosen": float("nan"),
        "placement_regret": float("nan"),
        "storm_nodes": "",
        **sensitivity_columns(profile),
        **_PROVENANCE,
    }
    row.update(extra)
    return row


def run_one(
    job_id: str,
    config: str,
    profile: str,
    overcommit: float,
    rep: int,
    cfg: dict,
    dry_run: bool,
    pin_node: str | None = None,
) -> dict:
    """Submit one job, wait, record, clean up.

    Retry policy (replaces the old blanket tenacity retry around the whole
    submit/wait/record cycle): only the SUBMIT step is retried, once. A retry
    after a wait-phase failure would rerun the job after the rest of its batch
    already finished — measured in isolation but recorded under the batch's
    overcommit label, a poisoned data point for H1; and re-applying a K8s Job
    name whose first attempt already completed/failed (backoffLimit=0) doesn't
    rerun anything, yielding a bogus near-zero makespan. A submit-phase retry
    is safe: the job hasn't started, so after cleanup of any half-created
    object it still runs concurrently with its batch.
    """
    backend_name = backend_for(config, cfg)
    backend = BACKENDS[backend_name]

    log.info(
        "submit: job_id=%s config=%s profile=%s overcommit=%s rep=%s backend=%s",
        job_id,
        config,
        profile,
        overcommit,
        rep,
        backend_name,
    )

    if dry_run:
        return {
            "config": config,
            "profile": profile,
            "overcommit": overcommit,
            "rep": rep,
            "node": "dry-run",
            "makespan_s": 0.0,
            "makespan_source": "dry-run",
            "submit_ts": None,
            "start_ts": None,
            "end_ts": None,
            "llc_miss_rate": float("nan"),
            "numa_remote_ratio": float("nan"),
            "net_bw": float("nan"),
            "net_pressure": float("nan"),
            "io_iops": float("nan"),
            "io_pressure": float("nan"),
            "approximation": "dry-run",
            "scenario": None,  # caller (run_batch / pressure arm) fills this in
            "interference_chosen": float("nan"),
            "placement_regret": float("nan"),
            **sensitivity_columns(profile),
        }

    # Drop any leftover job:metrics keys from a previous run that crashed
    # before reading them — job_ids repeat across runs and the keys accumulate.
    purge_job_metrics(cfg["redis"]["addr"], job_id)

    # Снапшот давления всех нод НА МОМЕНТ САБМИТА — из него после размещения
    # считается placement regret (см. submit/node_pressure.py). До submit_job,
    # а не после: собственная нагрузка job не должна попадать в ландшафт,
    # по которому оценивается решение планировщика о её размещении.
    pressure_snapshot = node_pressure.snapshot_node_pressure(cfg["redis"]["addr"])

    try:
        handle = backend.submit_job(
            job_id, config, profile, overcommit, cfg, pin_node=pin_node
        )
    except Exception as exc:  # noqa: BLE001 — one quick submit retry, see docstring
        log.warning(
            "submit failed for %s (%s) — cleaning up and retrying once", job_id, exc
        )
        backend.abort_submission(job_id, cfg)
        time.sleep(RESUBMIT_DELAY_SECONDS)
        handle = backend.submit_job(
            job_id, config, profile, overcommit, cfg, pin_node=pin_node
        )

    try:
        backend.wait_for_completion(handle, cfg)
        row = backend.record_result(
            handle, job_id, config, profile, overcommit, rep, cfg
        )
        chosen, regret = node_pressure.placement_regret(
            PROFILES[profile].sensitivity,
            pressure_snapshot,
            row.get("node"),
            cfg.get("score_weights") or node_pressure.DEFAULT_WEIGHTS,
        )
        row["interference_chosen"] = chosen
        row["placement_regret"] = regret
        row.update(sensitivity_columns(profile))
        # Провенанс серии сначала, затем workload_image из самой строки:
        # digest резолвится на под, а не на серию (imagePullPolicy: Always
        # означает, что пуш нового образа переключает часть Job'ов прямо
        # посреди прогона — именно это и нужно увидеть в данных).
        for key, value in _PROVENANCE.items():
            row.setdefault(key, value)
        return row
    finally:
        # Always clean up, including on wait timeout/failure — a job left
        # running would keep loading the node and contaminate the next plan
        # points (the cooldown between points assumes an idle cluster).
        backend.cleanup(handle)


def batch_size_for(overcommit: float, cfg: dict) -> int:
    """Translates an overcommit ratio into an actual number of CONCURRENT jobs
    to submit for one plan point (docs §4: overcommit ratio — "способ упаковки
    job на узел", е.g. ratio 1.5 на узел с capacity на 4 параллельных job
    сабмитим 6). node_capacity_jobs (harness/config.yaml) is the tunable
    baseline — adjust it to match the real per-node job capacity of your
    stand; the default (2) is a conservative placeholder chosen so that
    overcommit=1.0/1.5/2.0 stay visibly distinct (2/3/4 concurrent jobs)
    without needing real hardware capacity numbers for local dev testing.
    """
    base = cfg.get("node_capacity_jobs", 1)
    return max(1, round(base * overcommit))


def run_batch(
    config: str, profile: str, overcommit: float, rep: int, cfg: dict, dry_run: bool
) -> list[dict]:
    """Submits batch_size_for(overcommit, cfg) CONCURRENT jobs sharing this
    plan point, so overcommit > 1.0 actually creates real co-location
    contention on the target node(s) — this is the fix for the gap found
    after the first pilot run: overcommit used to be purely a label with no
    effect on submission behavior, so every job ran in complete isolation
    regardless of its value, meaning H1's core co-location scenario was
    never actually being exercised."""
    size = batch_size_for(overcommit, cfg)
    base_job_id = make_job_id(config, profile, overcommit, rep)

    def _run_member(batch_index: int) -> dict:
        job_id = base_job_id if size == 1 else f"{base_job_id}-b{batch_index}"
        row = run_one(job_id, config, profile, overcommit, rep, cfg, dry_run)
        row["scenario"] = "batch"
        row["batch_size"] = size
        row["batch_index"] = batch_index
        return row

    if size == 1:
        return [_run_member(0)]

    log.info(
        "batch: %s x%d concurrent jobs (overcommit=%s -> node_capacity_jobs=%s)",
        base_job_id,
        size,
        overcommit,
        cfg.get("node_capacity_jobs", 1),
    )

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=size) as pool:
        futures = {pool.submit(_run_member, i): i for i in range(size)}
        for future in concurrent.futures.as_completed(futures):
            batch_index = futures[future]
            try:
                rows.append(future.result())
            except (
                Exception
            ) as exc:  # noqa: BLE001 — one batch member failing shouldn't lose the rest
                log.error(
                    "batch member %s (index %d) failed after retries: %s",
                    base_job_id,
                    batch_index,
                    exc,
                )
                rows.append(
                    error_row(
                        config,
                        profile,
                        overcommit,
                        rep,
                        exc,
                        scenario="batch",
                        batch_size=size,
                        batch_index=batch_index,
                    )
                )
    return rows


def victim_offsets(count: int, arrival: dict, rng: random.Random) -> list[float]:
    """Моменты сабмита жертв относительно конца стабилизации давления.

    fixed  — равные интервалы interval_seconds;
    poisson — экспоненциальные межприбытия со средним interval_seconds
    (пуассоновский поток — реалистичная модель поступления job).

    Интервал должен быть больше лага метрик-пайплайна (~тик агента 5с +
    refresh плагина до 10с): иначе планировщик принимает решения по давлению,
    в котором ещё не видно предыдущих жертв, и сценарий вырождается в
    одновременный батч.
    """
    interval = float(arrival.get("interval_seconds", 30))
    if arrival.get("mode", "fixed") == "poisson":
        offsets, t = [], 0.0
        for _ in range(count):
            t += rng.expovariate(1.0 / interval)
            offsets.append(t)
        return offsets
    return [i * interval for i in range(count)]


def victim_profiles_for(scenario: dict) -> list[str]:
    """Профили жертв плеча в порядке прибытия.

    Легаси-формат: victim_profile × victim_count (один профиль).
    Смешанный сценарий: victims = [{profile, count}, ...] — профили
    чередуются round-robin, чтобы каждый был равномерно размазан по
    пуассоновскому окну прибытия, а не «сначала все кэш-жертвы, потом все
    дисковые» (порядок детерминирован — оба плеча репа получают одинаковую
    последовательность)."""
    if "victims" in scenario:
        groups = [
            [v["profile"]] * int(v.get("count", 1)) for v in scenario["victims"]
        ]
        seq: list[str] = []
        i = 0
        while any(groups):
            g = groups[i % len(groups)]
            if g:
                seq.append(g.pop(0))
            i += 1
        return seq
    return [scenario.get("victim_profile", "high-s")] * scenario.get("victim_count", 6)


def run_pressure_arm(
    scenario: dict,
    scenario_col: str,
    config: str,
    profiles_seq: list[str],
    intensity: int,
    rep: int,
    offsets: list[float],
    nodes: list[str] | None,
    cfg: dict,
    dry_run: bool,
) -> list[dict]:
    """Одно плечо pressure-точки: развернуть агрессоров -> дать давлению
    стабилизироваться -> подать поток жертв через планировщик этого плеча ->
    снести агрессоров. В колонку overcommit пишется intensity (агрессоров на
    pressured-ноду) — это ось dose-response, по ней analysis сравнивает плечи.

    profiles_seq — профиль каждой жертвы по индексу прибытия (в смешанном
    сценарии они разные; в job_id плеча тогда пишется «mixed», а профиль
    конкретной жертвы — в её строке результата)."""
    arm_profile = (
        profiles_seq[0] if len(set(profiles_seq)) == 1 else "mixed"
    )
    base_job_id = make_job_id(config, arm_profile, float(intensity), rep)
    victim_count = len(offsets)

    log.info(
        "pressure arm: %s — %d victims, arrival offsets %s",
        base_job_id,
        victim_count,
        [round(o, 1) for o in offsets],
    )

    def _run_victim(index: int) -> dict:
        if not dry_run:
            time.sleep(offsets[index])
        row = run_one(
            f"{base_job_id}-v{index}", config, profiles_seq[index],
            float(intensity), rep, cfg, dry_run
        )
        row["scenario"] = scenario_col
        row["batch_size"] = victim_count
        row["batch_index"] = index
        # Какие узлы несли шторм. Раньше это знание жило только внутри
        # aggressors.deploy и в строку не попадало, поэтому анализ
        # twin-контраста вынужден был бы выводить штормовой узел косвенно —
        # по максимуму измеренного давления. Пишем прямо: «на каком узле был
        # шторм» — факт постановки эксперимента, а не результат измерения.
        row["storm_nodes"] = ";".join(nodes or [])
        return row

    rows: list[dict] = []
    try:
        if not dry_run:
            aggressors.deploy(nodes or [], intensity, scenario, cfg)
            time.sleep(scenario.get("stabilize_seconds", 30))
            # Давление должно быть живым к моменту подачи жертв — молча
            # вышедший агрессор превращает плечо в измерение на чистой ноде.
            aggressors.assert_running(
                aggressors.expected_pods(len(nodes or []), intensity, scenario), cfg
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=victim_count) as pool:
            futures = {pool.submit(_run_victim, i): i for i in range(victim_count)}
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    rows.append(future.result())
                except Exception as exc:  # noqa: BLE001 — не терять остальных жертв
                    log.error(
                        "victim %s-v%d failed: %s", base_job_id, index, exc
                    )
                    rows.append(
                        error_row(
                            config,
                            profiles_seq[index],
                            float(intensity),
                            rep,
                            exc,
                            scenario=scenario_col,
                            batch_size=victim_count,
                            batch_index=index,
                        )
                    )
    finally:
        # Агрессоры сносятся даже при сбое плеча: следующее плечо обязано
        # стартовать с чистого ландшафта давления.
        if not dry_run:
            aggressors.teardown(cfg)
    return rows


def run_pressure_scenario(scenario: dict, cfg: dict, dry_run: bool):
    """Генератор списков строк (по одному на плечо) pressure-сценария:

    для каждой intensity из aggressors_per_node, для каждого повтора — оба
    плеча (A-default, A-sensitivityscore) подряд, с ИДЕНТИЧНЫМ паттерном
    прибытия жертв (seed не зависит от плеча) и идентичным набором
    pressured-нод (nodeName-пин, мимо планировщиков). Единственное различие
    между плечами — планировщик, принимающий решения о размещении жертв.
    """
    name = scenario["name"]
    scenario_col = f"pressure:{name}"
    profiles_seq = victim_profiles_for(scenario)
    victim_count = len(profiles_seq)
    arm_profile = profiles_seq[0] if len(set(profiles_seq)) == 1 else "mixed"
    arrival = scenario.get("victim_arrival", {})
    reps = scenario.get("repetitions", cfg["repetitions"])

    base_configs = scenario.get("configs", ["A"])
    configs = [
        c
        for c in expand_configs({**cfg, "configs": base_configs})
        if backend_for(c, cfg) == "k8s"
    ]

    nodes: list[str] | None = None
    if not dry_run:
        nodes = aggressors.resolve_pressured_nodes(scenario, cfg)
        log.info("pressure scenario %s: pressured nodes = %s", name, ",".join(nodes))

    for intensity in scenario.get("aggressors_per_node", [1]):
        for rep in range(reps):
            # Один seed на (сценарий, intensity, rep) — оба плеча получают
            # одинаковые моменты прибытия жертв.
            seed = f"{name}/{intensity}/{rep}"
            for config in configs:
                offsets = victim_offsets(victim_count, arrival, random.Random(seed))
                try:
                    yield run_pressure_arm(
                        scenario,
                        scenario_col,
                        config,
                        profiles_seq,
                        intensity,
                        rep,
                        offsets,
                        nodes,
                        cfg,
                        dry_run,
                    )
                except Exception as exc:  # noqa: BLE001 — одно упавшее плечо
                    # (например, деплой агрессоров не прошёл из-за моргнувшего
                    # apiserver) не должно ронять весь сценарий: фиксируем
                    # error-строку и идём к следующему плечу/повтору.
                    log.error(
                        "pressure arm %s/%s oc=%s rep=%d failed: %s",
                        name,
                        config,
                        intensity,
                        rep,
                        exc,
                    )
                    yield [
                        error_row(
                            config,
                            arm_profile,
                            float(intensity),
                            rep,
                            exc,
                            scenario=scenario_col,
                        )
                    ]


def build_plan(cfg: dict, pilot: bool, only_configs: list[str] | None) -> list[tuple]:
    configs = expand_configs(cfg)
    if only_configs:
        configs = [
            c for c in configs if c.split("-")[0] in only_configs or c in only_configs
        ]

    if pilot:
        # Single plan point, 3 reps, config A only (checklist item 9).
        pilot_configs = [c for c in configs if c.startswith("A")] or configs[:1]
        return [(c, "high-s", 2.0, rep) for c in pilot_configs for rep in range(3)]

    plan = []
    for config in configs:
        base_config = config.split("-")[0]
        for profile in cfg["profiles"]:
            for overcommit in cfg["overcommit_ratios"]:
                # slurm-bridge (D) is whole-node exclusive allocation only —
                # co-location / overcommit isn't testable there (Программа
                # экспериментов §3.1). Skip overcommit > 1.0 for D.
                if base_config == "D" and overcommit != 1.0:
                    continue
                for rep in range(cfg["repetitions"]):
                    plan.append((config, profile, overcommit, rep))
    return plan


def run_pressure_mode(cfg: dict, args) -> None:
    """--pressure: гоняет pressure-сценарии вместо матрицы плана. Результаты
    пишутся в тот же results-файл (колонка scenario отличает их от батчевых
    строк; overcommit в pressure-строках = агрессоров на pressured-ноду)."""
    scenarios = cfg.get("pressure_scenarios", [])
    if args.scenarios:
        scenarios = [s for s in scenarios if s["name"] in args.scenarios]
    if not scenarios:
        log.error("no pressure scenarios selected (config: pressure_scenarios)")
        sys.exit(1)

    results_dir = Path(cfg["output"]["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = cfg["output"]["results_file"]
    if args.dry_run:
        results_file = f"dry-run-{results_file}"
    results_path = results_dir / results_file

    rows: list[dict] = []
    for scenario in scenarios:
        log.info("pressure scenario: %s", scenario["name"])
        for arm_rows in run_pressure_scenario(scenario, cfg, args.dry_run):
            rows.extend(arm_rows)
            # Persist incrementally so a crash mid-scenario doesn't lose arms.
            pd.DataFrame(rows).to_parquet(results_path, index=False)
            if not args.dry_run:
                time.sleep(cfg["cooldown_seconds"])

    log.info("done: %d pressure results written to %s", len(rows), results_path)


def baseline_profiles(cfg: dict) -> list[str]:
    """Профили для соло-бейзлайнов: матричные (cfg.profiles) плюс жертвы
    pressure-сценариев (например high-s-io, которого нет в матрице) — slowdown
    нужен всем строкам, где встречается профиль."""
    profiles = list(cfg["profiles"])
    for scenario in cfg.get("pressure_scenarios", []):
        for victim in victim_profiles_for(scenario):
            if victim not in profiles:
                profiles.append(victim)
    return profiles


def run_baseline_mode(cfg: dict, args) -> None:
    """--baseline: соло-прогоны каждого профиля на пустом кластере ->
    baselines.parquet (отдельный файл, не results!). Двойное назначение:

    1) знаменатели slowdown = makespan_s / makespan_isolated — безразмерная
       метрика замедления, на которой analysis гоняет сравнения (профили
       разной длительности становятся объединяемыми);
    2) fingerprint-таблица «заявленный S vs измеренный»: соло-строки несут и
       декларацию (sensitivity_* колонки), и фактические метрики агента без
       чужой интерференции.

    PER-NODE: каждый профиль прогоняется на КАЖДОЙ worker-ноде (nodeSelector-
    пин), а не там, куда планировщик положит. Урок STAGE: «одинаковые» облачные
    ноды бывают в разы разной реальной скорости (пересозданный worker оказался
    ~1.9x медленнее соседей), а непиновые соло-прогоны пустого кластера
    ложатся на ОДНУ ноду — общий знаменатель slowdown тогда систематически
    лжёт для остальных нод. analysis нормирует per (profile, node).
    Отключается baseline.per_node: false (например, на заведомо однородном
    bare-metal, чтобы не платить x<nodes> за прогон).

    Прогоны СТРОГО последовательные (никакого батча): любой сосед или живой
    агрессор в кластере делает бейзлайн не изолированным — это молча занизит
    все slowdown. Харнесс это не проверяет, чистота кластера на совести
    запускающего."""
    bl_cfg = cfg.get("baseline", {})
    reps = bl_cfg.get("repetitions", 5)
    config = bl_cfg.get("config", "A-default")
    profiles = baseline_profiles(cfg)

    nodes: list[str | None] = [None]
    if bl_cfg.get("per_node", True) and not args.dry_run:
        if backend_for(config, cfg) != "k8s":
            log.warning(
                "baseline per_node: %s is not a k8s backend — falling back to "
                "unpinned solo runs (one shared denominator per profile)",
                config,
            )
        else:
            nodes = list(k8s_submit.list_worker_nodes(exclude=cfg.get("exclude_nodes", [])))
            if not nodes:
                raise RuntimeError("baseline per_node: no worker nodes found")
            log.info("baseline mode: per-node pinning across %s", ",".join(nodes))

    log.warning(
        "baseline mode: cluster MUST be idle (no aggressors, no leftover jobs) — "
        "these rows become slowdown denominators and fingerprint ground truth"
    )

    results_dir = Path(cfg["output"]["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    baselines_file = cfg["output"].get("baselines_file", "baselines.parquet")
    if args.dry_run:
        baselines_file = f"dry-run-{baselines_file}"
    baselines_path = results_dir / baselines_file

    rows: list[dict] = []
    for profile in profiles:
        for node in nodes:
            # Нода — часть job_id (и Redis-ключей job:metrics): бейзлайны
            # одного профиля на разных нодах не должны коллидировать.
            node_tag = f"-{node}" if node else ""
            for rep in range(reps):
                # Свой суффикс вместо make_job_id: бейзлайн не должен
                # коллидировать по job_id с матричными прогонами.
                job_id = f"{config}-{profile}-base{node_tag}-rep{rep:02d}"
                try:
                    row = run_one(
                        job_id, config, profile, 1.0, rep, cfg, args.dry_run,
                        pin_node=node,
                    )
                except Exception as exc:  # noqa: BLE001 — не терять остальные бейзлайны
                    log.error("baseline %s failed: %s", job_id, exc)
                    row = error_row(config, profile, 1.0, rep, exc, node=node)
                row["scenario"] = "baseline"
                row["batch_size"] = 1
                row["batch_index"] = 0
                rows.append(row)
                pd.DataFrame(rows).to_parquet(baselines_path, index=False)
                if not args.dry_run:
                    time.sleep(cfg["cooldown_seconds"])

    log.info("done: %d baseline rows written to %s", len(rows), baselines_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="config.yaml", help="Path to harness config.yaml"
    )
    parser.add_argument(
        "--pilot", action="store_true", help="Run the pilot sanity-check series only"
    )
    parser.add_argument(
        "--configs", nargs="*", help="Restrict to these configs, e.g. --configs A"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the plan and log it without submitting anything",
    )
    parser.add_argument(
        "--pressure",
        action="store_true",
        help="Run the pressure scenarios (config.yaml: pressure_scenarios) "
        "instead of the plan matrix",
    )
    parser.add_argument(
        "--scenarios",
        nargs="*",
        help="With --pressure: restrict to these scenario names",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Run solo per-profile baselines on an IDLE cluster -> "
        "baselines.parquet (slowdown denominators + fingerprint ground truth)",
    )
    args = parser.parse_args()

    cfg = config_loader.load_config(args.config)
    chain = config_loader.config_chain(args.config)
    if len(chain) > 1:
        log.info("конфиг: %s", " <- ".join(chain))

    init_provenance(cfg)

    if args.pressure and args.baseline:
        parser.error("--pressure and --baseline are mutually exclusive")
    if args.baseline:
        run_baseline_mode(cfg, args)
        return
    if args.pressure:
        run_pressure_mode(cfg, args)
        return

    plan = build_plan(cfg, pilot=args.pilot, only_configs=args.configs)
    total_jobs = sum(batch_size_for(overcommit, cfg) for _, _, overcommit, _ in plan)
    log.info(
        "plan has %d points, %d actual job submissions (batch_size varies by overcommit)",
        len(plan),
        total_jobs,
    )

    results_dir = Path(cfg["output"]["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = cfg["output"]["results_file"]
    if args.dry_run:
        # Never clobber real results with a plan preview.
        results_file = f"dry-run-{results_file}"
    results_path = results_dir / results_file

    rows = []
    for config, profile, overcommit, rep in plan:
        job_id = make_job_id(config, profile, overcommit, rep)
        try:
            batch_rows = run_batch(config, profile, overcommit, rep, cfg, args.dry_run)
            rows.extend(batch_rows)
        except Exception as exc:  # noqa: BLE001 — log and continue the matrix
            log.error("job %s failed after retries: %s", job_id, exc)
            rows.append(error_row(config, profile, overcommit, rep, exc, scenario="batch"))

        # Persist incrementally so a crash mid-matrix doesn't lose completed runs.
        pd.DataFrame(rows).to_parquet(results_path, index=False)

        if not args.dry_run:
            time.sleep(cfg["cooldown_seconds"])

    log.info("done: %d results written to %s", len(rows), results_path)


if __name__ == "__main__":
    main()
