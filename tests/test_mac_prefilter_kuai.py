"""Mac 實盤 prefilter 的瘸師弟長黑。只打到 backups 那份，不連網。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DISC = os.path.abspath(os.path.join(HERE, "..", "backups", "mac", "quantboy-paper", "discretionary"))
sys.path.insert(0, DISC)

import common  # noqa: E402


def _no_net(*_a, **_k):
    raise AssertionError("不該打網路")


common.get = _no_net
common.klines = _no_net

import prefilter  # noqa: E402


class MacKuaiTest(unittest.TestCase):
    def bars(self, o, c, v, n=25, prior_v=10.0):
        out = []
        t0 = 1700000000000
        step = 15 * 60 * 1000
        for i in range(n):
            out.append({"t": t0 + i * step, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0,
                        "v": prior_v, "ct": t0 + (i + 1) * step - 1})
        i = n
        out.append({"t": t0 + i * step, "o": o, "h": max(o, c), "l": min(o, c, 99.0), "c": c,
                    "v": v, "ct": t0 + (i + 1) * step - 1})
        return out

    def rs(self, big=False):
        return {"big": big}

    def test_btc_eth_single_2pct_passes_1_9_does_not(self):
        for symbol in ("BTCUSDT", "ETHUSDT"):
            hit = prefilter.kuai_shidi(self.bars(102, 100, 30), self.rs(True), symbol)
            self.assertIsNotNone(hit, symbol)
            self.assertGreaterEqual(hit["black_pct"], 2.0)
            self.assertIsNone(prefilter.kuai_shidi(self.bars(101.9, 100, 30), self.rs(True), symbol), symbol)

    def test_small_coin_4pct_passes_3_9_does_not(self):
        self.assertIsNotNone(prefilter.kuai_shidi(self.bars(104, 100, 30), self.rs(False), "XUSDT"))
        self.assertIsNone(prefilter.kuai_shidi(self.bars(103.9, 100, 30), self.rs(False), "XUSDT"))

    def test_big_volume_alt_still_needs_4pct(self):
        # 舊版 big=True 會把 2.6% 當成長黑。筆記的小幣要 4–5% 以上，程式取 4%。
        self.assertIsNone(prefilter.kuai_shidi(self.bars(102.6, 100, 40), self.rs(True), "AKEUSDT"))
        self.assertIsNotNone(prefilter.kuai_shidi(self.bars(104, 100, 40), self.rs(True), "AKEUSDT"))

    def test_long_black_and_volume_both_required(self):
        self.assertIsNone(prefilter.kuai_shidi(self.bars(104, 100, 29), self.rs(False), "XUSDT"))
        self.assertIsNone(prefilter.kuai_shidi(self.bars(100, 101, 50), self.rs(False), "XUSDT"))

    def test_ladder_is_3_4_5_below_signal_low(self):
        small = prefilter.kuai_shidi(self.bars(104, 100, 40), self.rs(False), "XUSDT")
        big = prefilter.kuai_shidi(self.bars(104, 100, 40), self.rs(True), "XUSDT")
        # 大幣小幣都從訊號低點往下 3%/4%/5%。停損仍在最深一檔之下，大幣再 1.5%、小幣再 3%。
        self.assertEqual(small["ladder"], [round(99 * s, 8) for s in (0.97, 0.96, 0.95)])
        self.assertEqual(big["ladder"], [round(99 * s, 8) for s in (0.97, 0.96, 0.95)])
        self.assertEqual(small["stop"], round(small["ladder"][2] * 0.97, 8))
        self.assertEqual(big["stop"], round(big["ladder"][2] * 0.985, 8))
        btc = prefilter.kuai_shidi(self.bars(102, 100, 40), self.rs(False), "BTCUSDT")
        self.assertIsNotNone(btc)
        self.assertEqual(btc["ladder"], [round(99 * s, 8) for s in (0.97, 0.96, 0.95)])

    def test_ref_is_signal_low_not_prior_20_low(self):
        bars = self.bars(104, 100, 40)
        for b in bars[:-1]:
            b["l"] = 90.0  # 前 20 根最低 90，舊規則會從 90 往下掛
        bars[-1]["l"] = 98.0  # 訊號長黑低點 98
        hit = prefilter.kuai_shidi(bars, self.rs(False), "XUSDT")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["ladder"], [round(98 * s, 8) for s in (0.97, 0.96, 0.95)])
        self.assertNotEqual(hit["ladder"][0], round(90 * 0.97, 8))


if __name__ == "__main__":
    unittest.main()
