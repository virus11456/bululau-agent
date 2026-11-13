"""回放不得用決策當下那根已經在走的 K。不連網。"""
import os
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "replay"))
import replay_three_rules as replay  # noqa: E402


def bar(t, low, high):
    return {"t": t, "o": 100.0, "h": high, "l": low, "c": 100.0, "v": 1.0, "ct": t + replay.M15 - 1}


class LookaheadTest(unittest.TestCase):
    def test_decision_mid_bar_starts_at_the_next_open(self):
        dec = datetime(2026, 11, 12, 12, 0, 29, tzinfo=replay.TPE)
        start = replay.next_bar_open(int(dec.timestamp() * 1000))
        got = datetime.fromtimestamp(start / 1000, replay.TPE)
        self.assertEqual((got.hour, got.minute), (12, 15))

    def test_decision_on_the_open_keeps_that_bar(self):
        dec = datetime(2026, 11, 12, 12, 15, tzinfo=replay.TPE)
        ms = int(dec.timestamp() * 1000)
        self.assertEqual(replay.next_bar_open(ms), ms)

    def test_in_progress_bar_is_not_used(self):
        t0 = int(datetime(2026, 11, 12, 12, 0, tzinfo=replay.TPE).timestamp() * 1000)
        bars = [bar(t0, 1, 1000), bar(t0 + replay.M15, 50, 51)]
        dec = int(datetime(2026, 11, 12, 12, 0, 29, tzinfo=replay.TPE).timestamp() * 1000)
        future = replay.bars_from_decision(bars, dec)
        self.assertEqual([b["t"] for b in future], [t0 + replay.M15])

    def test_fill_bar_target_counts_in_a_not_in_b(self):
        t0 = 1_700_000_000_000
        bars = [bar(t0, 96, 120)]
        for i in range(1, 12):
            bars.append(bar(t0 + i * replay.M15, 99, 101))
        plan = {"ladder": [97, 96, 95], "stop": 80, "target": 110, "entry": 96.1}
        a = replay.simulate_kuai(bars, plan, ignore_target_on_fill_bar=False)
        b = replay.simulate_kuai(bars, plan, ignore_target_on_fill_bar=True)
        self.assertEqual(a["result"], "目標")
        self.assertEqual(b["result"], "未出場")

    def test_same_bar_stop_and_target_is_stop(self):
        t0 = 1_700_000_000_000
        bars = [bar(t0, 70, 120)]
        plan = {"ladder": [97, 96, 95], "stop": 80, "target": 110, "entry": 96.1}
        for flag in (False, True):
            got = replay.simulate_kuai(bars, plan, ignore_target_on_fill_bar=flag)
            self.assertEqual(got["result"], "停損")
            self.assertIn("停損", got["note"])

    def test_bulu_b_needs_a_touch_within_two_hours(self):
        t0 = 1_700_000_000_000
        bars = [bar(t0 + i * replay.M15, 100, 101) for i in range(10)]
        got = replay.simulate_bulu_b(bars, 90, 85, 100)
        self.assertEqual(got["result"], "沒成交")

    def test_bulu_b_widens_stop_and_can_pass_prior_high(self):
        wide, target = replay.bulu_b_prices("XUSDT", 1.0, 0.97, 1.07)
        self.assertAlmostEqual(wide, 0.95)
        self.assertAlmostEqual(target, 1.0 + 1.5 * 0.05)


if __name__ == "__main__":
    unittest.main()
