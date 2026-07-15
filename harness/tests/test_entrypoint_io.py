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
import socket
import subprocess
import threading
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


# --- Сетевой режим stream (ось Net, зеркало disk-blocking) -----------------

@pytest.fixture
def tcp_sink():
    """Локальный TCP-sink, сливающий поток в никуда — стенд-ин для ss-sink."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        srv.settimeout(0.5)
        conns = []
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            conns.append(c)
            threading.Thread(target=_drain, args=(c, stop), daemon=True).start()
        srv.close()

    def _drain(c, stop):
        c.settimeout(0.5)
        while not stop.is_set():
            try:
                if not c.recv(1 << 20):
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        c.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield port
    stop.set()
    t.join(timeout=2)


def test_stream_gates_on_send(stub_env, tcp_sink):
    """stream: makespan >= compute + отправки; сетевой вывод на критическом
    пути (job не готов, пока не отправлен)."""
    env, _ = stub_env
    env.update(OUTPUT_MODE="stream", COMPUTE_SECONDS="0.3",
               NET_SINK_HOST="127.0.0.1", NET_SINK_PORT=str(tcp_sink),
               NET_TOTAL_MB="64", NET_TIMEOUT="30")
    p, wall = _run(env)
    assert p.returncode == 0
    # 64МБ на localhost быстро, но фаза реально исполнилась (compute+отправка).
    assert wall >= 0.3


def test_stream_unreachable_sink_does_not_hang(stub_env):
    """Недоступный sink: job не висит вечно и не падает — timeout + rc compute
    (сеть не должна ронять расчёт)."""
    env, _ = stub_env
    # порт 1 — гарантированно closed; timeout 3с страхует.
    env.update(OUTPUT_MODE="stream", COMPUTE_SECONDS="0.2",
               NET_SINK_HOST="127.0.0.1", NET_SINK_PORT="1",
               NET_TOTAL_MB="8", NET_TIMEOUT="3")
    p, wall = _run(env, timeout=30)
    assert p.returncode == 0
    assert wall < 12, f"stream завис на недоступном sink: {wall:.1f}s"


def test_stream_skips_on_compute_failure(stub_env, tcp_sink):
    """compute упал -> сетевой вывод пропускается, код compute сохраняется."""
    env, _ = stub_env
    env.update(OUTPUT_MODE="stream", COMPUTE_SECONDS="0.2", COMPUTE_RC="5",
               NET_SINK_HOST="127.0.0.1", NET_SINK_PORT=str(tcp_sink),
               NET_TOTAL_MB="64")
    p, _ = _run(env)
    assert p.returncode == 5


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
