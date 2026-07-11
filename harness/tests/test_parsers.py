"""Parser unit tests for the submit backends — the pure functions that turn
kubectl/sacct output into numbers. Run from harness/:

    .venv/bin/python -m unittest discover tests
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from submit.k8s_submit import _parse_k8s_time
from submit.slurm_submit import _parse_elapsed, _parse_sacct_time


class TestParseElapsed(unittest.TestCase):
    def test_hms(self):
        self.assertEqual(_parse_elapsed("01:02:03"), 3723)

    def test_ms_only(self):
        # sacct prints MM:SS for short jobs
        self.assertEqual(_parse_elapsed("02:03"), 123)

    def test_days(self):
        self.assertEqual(_parse_elapsed("1-01:00:00"), 90000)


class TestParseSacctTime(unittest.TestCase):
    def test_iso(self):
        ts = _parse_sacct_time("2026-07-10T16:52:35")
        self.assertIsNotNone(ts)
        # Round-trips through local time — check the wall-clock fields rather
        # than an absolute epoch, so the test doesn't depend on the TZ.
        from datetime import datetime

        self.assertEqual(
            datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S"),
            "2026-07-10T16:52:35",
        )

    def test_never_started(self):
        self.assertIsNone(_parse_sacct_time("Unknown"))
        self.assertIsNone(_parse_sacct_time("None"))
        self.assertIsNone(_parse_sacct_time(""))

    def test_garbage(self):
        self.assertIsNone(_parse_sacct_time("not-a-time"))


class TestParseK8sTime(unittest.TestCase):
    def test_rfc3339_zulu(self):
        ts = _parse_k8s_time("2026-07-10T13:52:35Z")
        self.assertIsNotNone(ts)
        # Z suffix is UTC — compare against an explicitly-UTC reference.
        from datetime import datetime, timezone

        want = datetime(2026, 7, 10, 13, 52, 35, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ts, want)

    def test_empty_and_garbage(self):
        self.assertIsNone(_parse_k8s_time(""))
        self.assertIsNone(_parse_k8s_time("garbage"))

    def test_window_is_positive(self):
        start = _parse_k8s_time("2026-07-10T13:52:35Z")
        end = _parse_k8s_time("2026-07-10T13:53:40Z")
        self.assertFalse(math.isnan(end - start))
        self.assertEqual(end - start, 65.0)


if __name__ == "__main__":
    unittest.main()
