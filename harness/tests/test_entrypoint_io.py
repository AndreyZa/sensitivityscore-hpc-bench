"""Тесты дисковой логики workload/entrypoint.sh — механизм cˢ_io без реального
диска и без полного образа Geant4: geant4-app подменяется стабом (G4_BIN),
писатель гоняется на локальной FS. Проверяется КОНТРАКТ, ради которого сделан
режим blocking: makespan = время compute + время последовательного сброса
вывода на диск. Под штормом фаза сброса (V/пропускная) растёт в разы (замерено
×23) и удлиняет makespan — это и есть cˢ_io > 0. Сброс последователен (после
compute, не параллельно) — иначе dd конкурировал бы с geant4 за CPU-квоту.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "workload" / "entrypoint.sh"


@pytest.fixture
def stub_env(tmp_path):
    """Стаб geant4-app: спит COMPUTE_SECONDS и выходит с COMPUTE_RC. MACRO —
    пустышка с плейсхолдерами (sed в entrypoint только подставляет их)."""
    stub = tmp_path / "g4stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'sleep "${COMPUTE_SECONDS:-0.3}"\n'
        'exit "${COMPUTE_RC:-0}"\n'
    )
    stub.chmod(0o755)
    macro = tmp_path / "run.mac"
    macro.write_text("/run/numberOfThreads __G4_THREADS__\n/run/beamOn __N_PRIMARIES__\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = {
        **os.environ,
        "G4_BIN": str(stub),
        "MACRO": str(macro),
        "IO_SCRATCH_DIR": str(scratch),
        "IO_BURST_MB": "1",  # мелкие порции: время писателя задаётся интервалом
    }
    return env, scratch


def _run(env, timeout=60):
    t0 = time.monotonic()
    p = subprocess.run(
        ["bash", str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=timeout
    )
    return p, time.monotonic() - t0


def test_blocking_flush_on_critical_path(stub_env):
    """Фаза сброса идёт ПОСЛЕ compute и на критическом пути: makespan >= время
    сброса. Медленный диск (шторм) => большой сброс => большой makespan =
    цена cˢ_io."""
    env, _ = stub_env
    env.update(
        OUTPUT_MODE="blocking", COMPUTE_SECONDS="0.3",
        IO_INTERVAL_SECONDS="0.3", IO_TOTAL_BURSTS="10",  # сброс ~3 с
    )
    p, wall = _run(env)
    assert p.returncode == 0
    assert wall >= 2.5, f"сброс не на критическом пути: makespan {wall:.2f}s < ~3s"


def test_blocking_makespan_is_compute_plus_flush(stub_env):
    """makespan(blocking) ≈ compute + сброс: тот же compute в режиме none даёт
    только compute, разница = время сброса (аддитивно, последовательно)."""
    env, _ = stub_env
    common = dict(COMPUTE_SECONDS="1")
    env.update(OUTPUT_MODE="none", **common)
    _, wall_none = _run(env)
    env.update(OUTPUT_MODE="blocking", IO_INTERVAL_SECONDS="0.2", IO_TOTAL_BURSTS="10", **common)
    _, wall_block = _run(env)
    # сброс добавляет ~2с поверх того же compute.
    assert wall_block - wall_none >= 1.3, (
        f"сброс не аддитивен: blocking {wall_block:.2f}s − none {wall_none:.2f}s")


def test_blocking_skips_flush_on_compute_failure(stub_env):
    """compute упал (rc=7) -> фаза сброса пропускается (незачем писать вывод
    провалившегося job), быстрый выход с кодом compute."""
    env, _ = stub_env
    env.update(
        OUTPUT_MODE="blocking", COMPUTE_SECONDS="0.2", COMPUTE_RC="7",
        IO_INTERVAL_SECONDS="0.5", IO_TOTAL_BURSTS="10",  # сброс ~5с, но должен быть пропущен
    )
    p, wall = _run(env)
    assert p.returncode == 7
    assert wall < 2.0, f"сброс не пропущен при падении compute: makespan {wall:.2f}s"


def test_blocking_cleans_up_scratch(stub_env):
    """После завершения временный файл вывода убран (не копится между job)."""
    env, scratch = stub_env
    env.update(
        OUTPUT_MODE="blocking", COMPUTE_SECONDS="0.2",
        IO_INTERVAL_SECONDS="0.1", IO_TOTAL_BURSTS="2",
    )
    _run(env)
    assert not list(scratch.glob("output-burst.*.dat"))


def test_none_runs_without_writer(stub_env):
    """OUTPUT_MODE=none: писателя нет, scratch пуст, makespan ≈ compute."""
    env, scratch = stub_env
    env.update(OUTPUT_MODE="none", COMPUTE_SECONDS="0.3")
    p, wall = _run(env)
    assert p.returncode == 0
    assert wall < 2.0
    assert not list(scratch.glob("output-burst.*.dat"))


def test_unknown_mode_still_runs_binary(stub_env):
    """Неизвестный OUTPUT_MODE не роняет job — предупреждение + прогон."""
    env, _ = stub_env
    env.update(OUTPUT_MODE="garbage", COMPUTE_SECONDS="0.2")
    p, _ = _run(env)
    assert p.returncode == 0
    assert "unknown OUTPUT_MODE" in p.stderr


def test_burst_does_not_gate_on_infinite_writer(stub_env):
    """burst — бесконечный ФОНОВЫЙ писатель: makespan определяется compute, а
    не писателем (в проде осиротевший писатель убивается вместе с cgroup
    контейнера; здесь контейнера нет, поэтому запускаем в своей сессии и
    прибиваем группу вручную — иначе dd держал бы pipe). Проверяем: exec
    geant4-app отдаёт код возврата сразу по завершении compute, не дожидаясь
    писателя."""
    env, _ = stub_env
    env.update(OUTPUT_MODE="burst", COMPUTE_SECONDS="0.5", IO_INTERVAL_SECONDS="0.2")
    t0 = time.monotonic()
    p = subprocess.Popen(
        ["bash", str(ENTRYPOINT)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        rc = p.wait(timeout=10)
        wall = time.monotonic() - t0
        assert rc == 0
        assert wall < 2.5, f"burst-писатель задержал выход: {wall:.2f}s"
    finally:
        # Прибить осиротевший dd-писатель (в тесте нет cgroup, который это
        # сделал бы в проде).
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        subprocess.run(["pkill", "-f", "output-burst"], capture_output=True)
