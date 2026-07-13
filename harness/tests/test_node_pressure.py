"""Тесты чистой математики placement regret (submit/node_pressure.py) —
формула обязана зеркалить Score() плагина SensitivityScore (fork
scheduler-plugins, pkg/sensitivityscore/sensitivityscore.go): high|medium|low
-> 1.0|0.5|0.0, ВСЕ четыре оси (llc/numa/net/io) в сумме и в знаменателе,
нормировка на сумму весов. Ось выключается весом 0 (абляция/некалиброванный
стенд), а не кодом.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from profiles import PROFILES, Sensitivity
from submit.node_pressure import (
    DEFAULT_WEIGHTS,
    interference,
    placement_regret,
)

HIGH_ALL = Sensitivity(llc="high", numa="high", net="high", io="high")
LOW_ALL = Sensitivity(llc="low", numa="low", net="low", io="low")


class TestInterference(unittest.TestCase):
    def test_zero_pressure_is_zero(self):
        self.assertEqual(
            interference(
                HIGH_ALL, {"llc": 0, "numa": 0, "net": 0, "io": 0}, DEFAULT_WEIGHTS
            ),
            0.0,
        )

    def test_full_pressure_full_sensitivity_is_one(self):
        # Максимум по всем четырём осям -> ровно 1.0 (нормировка).
        self.assertAlmostEqual(
            interference(
                HIGH_ALL, {"llc": 1, "numa": 1, "net": 1, "io": 1}, DEFAULT_WEIGHTS
            ),
            1.0,
        )

    def test_low_sensitivity_ignores_pressure(self):
        self.assertEqual(
            interference(
                LOW_ALL, {"llc": 1, "numa": 1, "net": 1, "io": 1}, DEFAULT_WEIGHTS
            ),
            0.0,
        )

    def test_net_participates_like_other_axes(self):
        # Профиль чувствителен ТОЛЬКО к net: давление по net учитывается
        # (раньше Net был исключён из score — теперь есть net_pressure).
        net_only = Sensitivity(llc="low", numa="low", net="high", io="low")
        self.assertAlmostEqual(
            interference(
                net_only, {"llc": 1, "numa": 1, "net": 1, "io": 1}, DEFAULT_WEIGHTS
            ),
            1.0 / 4,
        )

    def test_net_weight_zero_disables_axis(self):
        # Абляция/некалиброванный стенд: вес net=0 убирает ось и из суммы,
        # и из знаменателя — вклад остальных осей не разбавляется.
        net_only = Sensitivity(llc="low", numa="low", net="high", io="low")
        w = {"llc": 1.0, "numa": 1.0, "net": 0.0, "io": 1.0}
        self.assertEqual(
            interference(net_only, {"llc": 1, "numa": 1, "net": 1, "io": 1}, w), 0.0
        )
        self.assertAlmostEqual(
            interference(HIGH_ALL, {"llc": 1, "numa": 1, "net": 1, "io": 1}, w), 1.0
        )

    def test_medium_is_half(self):
        medium_llc = Sensitivity(llc="medium", numa="low", net="low", io="low")
        # 0.5 * 1.0 * w_llc / (w_llc + w_numa + w_net + w_io) = 0.5 / 4
        self.assertAlmostEqual(
            interference(
                medium_llc, {"llc": 1, "numa": 0, "net": 0, "io": 0}, DEFAULT_WEIGHTS
            ),
            0.5 / 4,
        )

    def test_weights_rescale(self):
        llc_only = Sensitivity(llc="high", numa="low", net="low", io="low")
        w = {"llc": 2.0, "numa": 1.0, "net": 1.0, "io": 1.0}
        self.assertAlmostEqual(
            interference(llc_only, {"llc": 1, "numa": 1, "net": 1, "io": 1}, w),
            2.0 / 5.0,
        )

    def test_zero_weights_no_division_by_zero(self):
        w = {"llc": 0.0, "numa": 0.0, "net": 0.0, "io": 0.0}
        self.assertEqual(interference(HIGH_ALL, {"llc": 1}, w), 0.0)

    def test_real_profile_high_s(self):
        # high-s: llc=high, numa=high, net=low, io=low -> (1*0.6 + 1*0.3) / 4
        s = PROFILES["high-s"].sensitivity
        got = interference(
            s, {"llc": 0.6, "numa": 0.3, "net": 0.5, "io": 0.9}, DEFAULT_WEIGHTS
        )
        self.assertAlmostEqual(got, (0.6 + 0.3) / 4)

    def test_real_profile_high_s_net(self):
        # high-s-net: только net=high -> давление остальных осей игнорируется.
        s = PROFILES["high-s-net"].sensitivity
        got = interference(
            s, {"llc": 0.6, "numa": 0.3, "net": 0.8, "io": 0.9}, DEFAULT_WEIGHTS
        )
        self.assertAlmostEqual(got, 0.8 / 4)

    def test_stage_weights_io_net_only(self):
        # Абляция STAGE: llc/numa выключены весами, net+io активны.
        w = {"llc": 0.0, "numa": 0.0, "net": 1.0, "io": 1.0}
        io_high = PROFILES["high-s-io"].sensitivity  # llc/numa=high, но вес 0
        got = interference(
            io_high, {"llc": 1, "numa": 1, "net": 0.2, "io": 0.9}, w
        )
        self.assertAlmostEqual(got, 0.9 / 2)  # net=low у профиля, вклад только io


class TestPlacementRegret(unittest.TestCase):
    SNAPSHOT = {
        "worker-1": {"llc": 0.8, "numa": 0.0, "net": 0.0, "io": 0.0},  # придавленная
        "worker-2": {"llc": 0.1, "numa": 0.0, "net": 0.0, "io": 0.0},  # почти чистая
    }
    LLC_ONLY = Sensitivity(llc="high", numa="low", net="low", io="low")

    def test_best_choice_zero_regret(self):
        chosen, regret = placement_regret(
            self.LLC_ONLY, self.SNAPSHOT, "worker-2", DEFAULT_WEIGHTS
        )
        self.assertAlmostEqual(chosen, 0.1 / 4)
        self.assertEqual(regret, 0.0)

    def test_worst_choice_positive_regret(self):
        chosen, regret = placement_regret(
            self.LLC_ONLY, self.SNAPSHOT, "worker-1", DEFAULT_WEIGHTS
        )
        self.assertAlmostEqual(chosen, 0.8 / 4)
        self.assertAlmostEqual(regret, (0.8 - 0.1) / 4)

    def test_insensitive_profile_zero_regret_everywhere(self):
        # low-s не различает ноды -> regret 0 на любой (интерференции нет).
        _, regret = placement_regret(
            LOW_ALL, self.SNAPSHOT, "worker-1", DEFAULT_WEIGHTS
        )
        self.assertEqual(regret, 0.0)

    def test_unknown_node_is_nan(self):
        chosen, regret = placement_regret(
            self.LLC_ONLY, self.SNAPSHOT, "worker-99", DEFAULT_WEIGHTS
        )
        self.assertTrue(math.isnan(chosen) and math.isnan(regret))

    def test_empty_snapshot_is_nan(self):
        chosen, regret = placement_regret(self.LLC_ONLY, {}, "worker-1", DEFAULT_WEIGHTS)
        self.assertTrue(math.isnan(chosen) and math.isnan(regret))

    def test_none_node_is_nan(self):
        chosen, regret = placement_regret(
            self.LLC_ONLY, self.SNAPSHOT, None, DEFAULT_WEIGHTS
        )
        self.assertTrue(math.isnan(chosen) and math.isnan(regret))


if __name__ == "__main__":
    unittest.main()
