"""瘸師弟新舊回放。只讀已存的 K 線，不連網。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import replay_kuai_single_black as replay  # noqa: E402


class KuaiReplayTest(unittest.TestCase):
    def test_saved_klines_match_expected_counts(self):
        _rows, summary = replay.main(["--no-write"])
        self.assertEqual(summary["n"], 52)
        self.assertEqual(len(summary["released"]), 40)
        self.assertEqual(len(summary["leaked"]), 0)
        self.assertEqual(len(summary["missing"]), 7)
        self.assertEqual(len(summary["tightened"]), 1)
        ake = summary["tightened"][0]
        self.assertEqual(ake["symbol"], "AKEUSDT")
        self.assertEqual(ake["new_hardfilter"], "擋")
        self.assertEqual(ake["new_prefilter"], "不放行")
        self.assertFalse(ake["meets_original"])
        for row in summary["released"]:
            self.assertEqual(row["old_hardfilter"], "擋")
            self.assertEqual(row["new_hardfilter"], "不擋")
            self.assertTrue(row["meets_original"])
        missing = {row["symbol"] for row in summary["missing"]}
        self.assertEqual(missing, {"ONEUSDT", "BLUAIUSDT", "AGTUSDT", "ARIAUSDT", "MAGMAUSDT"})
        for row in summary["missing"]:
            self.assertTrue(row["time"].startswith("2026-11-11"))
            self.assertEqual(row["new_hardfilter"], "未取得")


if __name__ == "__main__":
    unittest.main()
