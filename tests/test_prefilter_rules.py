"""瘸師弟參考點與布鹿流進場。只打 src/prefilter.py，不連網。"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import common  # noqa: E402


def _no_net(*_a, **_k):
    raise AssertionError("不該打網路")


common.get = _no_net
common.klines = _no_net
if not hasattr(common, "DATA_DIR"):
    common.DATA_DIR = "/tmp"

_spec = importlib.util.spec_from_file_location("src_prefilter_rules", os.path.join(SRC, "prefilter.py"))
prefilter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prefilter)


def bar(o, h, l, c, v=10.0, t=0):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "ct": t + 1}


class KuaiRefTest(unittest.TestCase):
    def test_src_ladder_uses_signal_low(self):
        bars = []
        for _ in range(20):
            bars.append(bar(100, 101, 90, 100, 10))
        bars.append(bar(104, 104, 98, 100, 40))
        hit = prefilter.kuai_shidi(bars, {"big": False})
        self.assertIsNotNone(hit)
        self.assertEqual(hit["ladder"], [round(98 * s, 8) for s in (0.97, 0.96, 0.95)])
        self.assertGreater(hit["ladder"][0], 90)


class BuluRuleTest(unittest.TestCase):
    def daily(self, last_close=None):
        out = []
        for i in range(30):
            lo = 100 + i
            out.append(bar(lo + 1, lo + 3, lo, lo + 2, 10))
        if last_close is not None:
            out[-1]["c"] = last_close
        return out

    def hourly(self, last, peak=122.0):
        """90 根。最後 48 根對半：前段高 112、低 105；後段只有一根打到 peak，然後回檔。"""
        out = [bar(100, 101, 99, 100, 10) for _ in range(42)]
        out += [bar(108, 112, 105, 110, 10) for _ in range(24)]
        for i in range(21):
            if i == 10:
                out.append(bar(118, peak, 116, min(119, peak - 0.5), 40))
            else:
                out.append(bar(118, min(120, peak - 1), 116, 118, 10))
        out += last
        self.assertEqual(len(out), 90)
        return out

    def test_passes_without_hammer_or_ma_and_can_exceed_prior_high(self):
        # 最後一根收黑、下影很短，舊規則會因沒有錘子／吞噬被刷掉。
        last = [
            bar(121.4, 121.6, 121.2, 121.4, 10),
            bar(121.3, 121.5, 121.1, 121.3, 10),
            bar(121.2, 121.4, 121.05, 121.1, 10),
        ]
        hit, why = prefilter.buluuliu(self.hourly(last, peak=122.0), self.daily())
        self.assertIsNotNone(hit, why)
        self.assertGreaterEqual(hit["rr"], 1.5)
        self.assertGreater(hit["target"], hit["prior_high"])
        self.assertNotIn("MA30", why or "")

    def test_close_below_ma20_still_passes_on_higher_lows(self):
        kd = self.daily(last_close=50)
        # 日 K 低點仍墊高，只是收盤砸在均線下。不再用 MA20 擋。
        self.assertGreater(min(x["l"] for x in kd[-10:]), min(x["l"] for x in kd[-20:-10]))
        last = [
            bar(125, 126, 124, 125, 10),
            bar(124, 125, 123, 124, 10),
            bar(123, 124, 122, 123, 10),
        ]
        hit, why = prefilter.buluuliu(self.hourly(last, peak=140.0), kd)
        self.assertIsNotNone(hit, why)

    def test_at_highs_is_chase(self):
        last = [
            bar(125, 126, 124, 125, 10),
            bar(126, 127, 125, 126, 10),
            bar(127, 140, 126, 139, 80),
        ]
        hit, why = prefilter.buluuliu(self.hourly(last, peak=130.0), self.daily())
        self.assertIsNone(hit)
        self.assertIn("at highs", why)

    def test_no_higher_structure(self):
        flat = [bar(100, 101, 99, 100, 30) for _ in range(90)]
        hit, why = prefilter.buluuliu(flat, self.daily())
        self.assertIsNone(hit)
        self.assertIn("up-divergence", why)

    def test_broken_daily_lows(self):
        kd = self.daily()
        for b in kd[-10:]:
            b["l"] = 100
        hit, why = prefilter.buluuliu(self.hourly([
            bar(125, 126, 124, 125, 10),
            bar(124, 125, 123, 124, 10),
            bar(123, 124, 122, 123, 10),
        ]), kd)
        self.assertIsNone(hit)
        self.assertIn("higher-lows", why)


class PromptTextTest(unittest.TestCase):
    def test_prompt_and_skill_use_the_three_rules(self):
        root = os.path.dirname(HERE)
        with open(os.path.join(root, "backups/mac/quantboy-gate/full_prompt.md"), encoding="utf-8") as handle:
            prompt = handle.read()
        with open(os.path.join(root, "docs/skill.md"), encoding="utf-8") as handle:
            skill = handle.read()
        for text in (prompt, skill):
            self.assertIn("七八波以上才考慮放棄", text)
            self.assertIn("3%/4%/5%" if text is prompt else "3%／4%／5%", text)
            self.assertIn("妖幣急拉不追", text)
            self.assertIn("離好球帶太遠不盲掛", text)
        self.assertNotIn("約3波不做，", prompt)
        self.assertNotIn("已走七、八波以上的不做", skill)
        self.assertIn("不得以「妖幣急拉不追」", prompt)
        self.assertIn("不得以「妖幣急拉不追」", skill)
        phrase = "七八波以上才考慮放棄（003，不是約3波不做）"
        self.assertIn(phrase, prompt)
        self.assertIn(phrase, skill)
        self.assertNotIn("不追深殺反彈", prompt)
        on_path = os.path.join(root, "backups/mac/quantboy-gate/full_prompt_3a_on.md")
        with open(on_path, encoding="utf-8") as handle:
            on = handle.read()
        self.assertIn("不追深殺反彈", on)
        self.assertNotIn("不追離好球帶或深殺反彈", on)
        self.assertNotIn("不追離好球帶或深殺反彈", prompt)


def _quiet(o, h, l, c, v=10.0):
    return bar(o, h, l, c, v)


class XiaoScreenTest(unittest.TestCase):
    def eth_series(self, flush_low=3300):
        bars = []
        for i in range(30):
            px = 3600 - i
            bars.append(bar(px + 1, px + 1.2, px, px + 0.4, 10, t=i))
        for i in range(6):
            o = 3560 - i * 12
            c = o - 8
            bars.append(bar(o, o, c, c, 10, t=30 + i))
        bars.append(bar(3480, 3480, flush_low, flush_low + 20, 40, t=36))
        for i, px in enumerate((3380, 3420, 3450)):
            bars.append(bar(px - 5, px, px - 8, px - 2, 10, t=37 + i))
        half = flush_low + 0.5 * (3450 - flush_low)
        bars.append(bar(half + 4, half + 6, half - 2, half, 10, t=40))
        return bars

    def test_eth_second_selloff_is_market_and_stop_has_cushion(self):
        hit, why = prefilter.xiao_shidi(self.eth_series(), "ETHUSDT")
        self.assertIsNotNone(hit, why)
        self.assertEqual(hit["entry_type"], "market")
        self.assertLess(hit["stop"], 3300)
        self.assertAlmostEqual(hit["stop"], min(3300 * (1 - prefilter.XIAO_STOP_CUSHION), hit["entry"] * 0.97), places=4)
        self.assertEqual(prefilter.XIAO_STOP_CUSHION, 0.005)
        self.assertIn("待使用者確認", " ".join(hit["unconfirmed"]))
        chasing = self.eth_series()
        chasing[-1] = bar(3460, 3465, 3440, 3462, 10, t=40)
        miss, why = prefilter.xiao_shidi(chasing, "ETHUSDT")
        self.assertIsNone(miss)
        self.assertTrue(why)

    def test_btc_uses_3_percent_and_alts_need_3_percent_drop(self):
        btc, why = prefilter.xiao_shidi(self.eth_series(), "BTCUSDT")
        self.assertIsNotNone(btc, why)
        self.assertLessEqual(btc["stop"], btc["entry"] * 0.97 + 1e-6)
        alt, why = prefilter.xiao_shidi(self._alt(), "SOLUSDT")
        self.assertIsNotNone(alt, why)
        self.assertGreaterEqual(alt["drop_pct"], 3)
        ok, why = prefilter.xiao_drop_ok("SOLUSDT", 100, 98)
        self.assertFalse(ok)
        self.assertIn("3%", why)
        ok, why = prefilter.xiao_drop_ok("BTCUSDT", 100, 98)
        self.assertFalse(ok)

    def _alt(self):
        bars = []
        for i in range(30):
            px = 120 - i * 0.05
            bars.append(bar(px + 0.1, px + 0.12, px, px + 0.04, 10, t=i))
        for i in range(6):
            o = 112 - i * 0.4
            bars.append(bar(o, o, o - 0.3, o - 0.3, 10, t=30 + i))
        bars.append(bar(109, 109, 100, 101, 40, t=36))
        for i, px in enumerate((103, 105, 106)):
            bars.append(bar(px - 0.4, px, px - 0.6, px - 0.2, 10, t=37 + i))
        half = 100 + 0.5 * (106 - 100)
        bars.append(bar(half + 0.3, half + 0.4, half - 0.2, half, 10, t=40))
        return bars

    def test_signal_fires_once_and_near_misses_keep_other_setups(self):
        cand = {"symbol": "ETHUSDT", "setup": "小師弟", "low_t": 36}
        path = os.path.join(prefilter.DATA_DIR, "signal-test.jsonl")
        if os.path.exists(path):
            os.remove(path)
        first = prefilter.filter_new_signals([cand], path=path)
        second = prefilter.filter_new_signals([cand], path=path)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        near = [{"setup": "布鹿流", "fail": "rr"}] + [{"setup": "小師弟", "fail": "x"} for _ in range(20)]
        capped = prefilter.cap_near(near, 15)
        self.assertEqual(capped[0]["setup"], "布鹿流")
        self.assertLessEqual(len(capped), 15)

    def test_da_retest_only_and_one_black(self):
        bars = self._da_bars()
        hit, why = prefilter.da_shixiong(bars, "BTCUSDT")
        self.assertIsNotNone(hit, why)
        self.assertEqual(hit["entry_mode"], "二次測前低")
        self.assertEqual(hit["entry_type"], "limit")
        self.assertGreaterEqual(hit["rebound_bars"], 10)
        self.assertLessEqual(hit["rebound_bars"], 40)
        self.assertEqual(hit["limit_expire_4h"], 2)
        self.assertIn("待使用者確認", " ".join(hit["unconfirmed"]))
        v = [dict(b) for b in bars]
        black = next(b for b in v if b["t"] == 40)
        v[41]["c"] = black["o"] + 1
        miss, why = prefilter.da_shixiong(v, "BTCUSDT")
        self.assertIsNone(miss, why)
        import inspect
        self.assertIn('setup="funding_rate"', inspect.getsource(prefilter.funding))

    def _da_bars(self):
        bars = []
        px = 180.0
        for i in range(40):
            px -= 1.2
            bars.append(bar(px + 2, px + 2.1, px, px + 0.2, 10, t=i))
        black_o = bars[-1]["c"] + 2
        bars.append(bar(black_o, black_o + 1, 100, black_o - 6, 25, t=40))
        for i in range(12):
            c = 112 + (i % 3)
            bars.append(bar(c + 0.4, 122 if i > 2 else 110, 108, c, 10, t=41 + i))
        bars.append(bar(102.2, 102.6, 101, 101.5, 10, t=53))
        return bars


if __name__ == "__main__":
    unittest.main()
