"""Снимок давления узлов переживает мгновенную заминку Redis.

Regret считается из снимка, взятого В МОМЕНТ САБМИТА, и второго шанса нет:
ландшафт давления через минуту уже другой. Redis виден через port-forward,
а тот идёт сквозь API-сервер — заминка последнего (`TLS handshake timeout`
на облачном стенде, наблюдалось 20.07) раньше стоила строке метрики решения.

Запуск из harness/:  .venv/bin/python -m unittest discover tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import redis

sys.path.insert(0, str(Path(__file__).parent.parent))

from submit import node_pressure


class FakeRedis:
    """Минимальный Redis: одна нода с давлением по всем осям."""

    def scan_iter(self, match):
        yield "node:metrics:w9"

    def hgetall(self, key):
        return {"llc_miss_rate": "0.1", "numa_remote_ratio": "0.0",
                "net_pressure": "0.2", "io_pressure": "0.9"}


class TestSnapshotRetry(unittest.TestCase):
    def test_reads_snapshot(self):
        with mock.patch.object(node_pressure, "_connect", return_value=FakeRedis()):
            snap = node_pressure.snapshot_node_pressure("localhost:16379")
        self.assertEqual(snap["w9"]["io"], 0.9)

    def test_survives_one_blip(self):
        """Первая попытка падает, вторая проходит — строка сохраняет regret."""
        conns = [redis.exceptions.ConnectionError("TLS handshake timeout"), FakeRedis()]

        def connect(_addr):
            item = conns.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(node_pressure, "_connect", side_effect=connect), \
             mock.patch.object(node_pressure.time, "sleep"):
            snap = node_pressure.snapshot_node_pressure("localhost:16379")
        self.assertEqual(snap["w9"]["io"], 0.9)
        self.assertFalse(conns, "вторая попытка должна была случиться")

    def test_gives_up_loudly(self):
        """Redis мёртв: снимок пуст (regret станет NaN), но в лог уходит
        причина — иначе NaN потом не с чем связать."""
        err = redis.exceptions.ConnectionError("connection refused")
        with mock.patch.object(node_pressure, "_connect", side_effect=err), \
             mock.patch.object(node_pressure.time, "sleep"), \
             self.assertLogs(node_pressure.log, level="WARNING") as logs:
            snap = node_pressure.snapshot_node_pressure("localhost:16379")
        self.assertEqual(snap, {})
        self.assertIn("placement_regret", "".join(logs.output))

    def test_empty_redis_is_not_a_failure(self):
        """Пустой Redis (ключей нет) — это НЕ отказ связи: повторять нечего,
        и предупреждение было бы ложной тревогой."""
        class Empty(FakeRedis):
            def scan_iter(self, match):
                return iter(())

        with mock.patch.object(node_pressure, "_connect", return_value=Empty()) as c:
            snap = node_pressure.snapshot_node_pressure("localhost:16379")
        self.assertEqual(snap, {})
        self.assertEqual(c.call_count, 1)


if __name__ == "__main__":
    unittest.main()
