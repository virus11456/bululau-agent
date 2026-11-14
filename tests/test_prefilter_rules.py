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


def bar(o, h, l, c, v=10.0):
    return {"t": 0, "o": o, "h": h, "l": l, "c": c, "v": v, "ct": 1}


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
    def series(self, high, low, last_close, symbol_scale=1):
        bars = [_quiet(100, 101, 99, 100, 10) for _ in range(20)]
        window = [_quiet(high - 0.2, high - 0.1, high - 0.3, high - 0.2, 10) for _ in range(40)]
        window[5] = _quiet(high - 0.5, high, high - 0.2, high - 0.4, 10)
        window[20] = _quiet(low * 1.012, low * 1.012, low, low, 40)
        window[-1] = _quiet(last_close - 0.2, last_close + 0.2, last_close - 0.1, last_close, 10)
        return bars + window

    def test_eth_needs_more_than_100_points_and_uses_market(self):
        hit, why = prefilter.xiao_shidi(self.series(3500, 3300, 3400), "ETHUSDT")
        self.assertIsNotNone(hit, why)
        self.assertEqual(hit["entry_type"], "market")
        self.assertEqual(hit["setup"], "小師弟")
        self.assertLessEqual(hit["stop"], 3300)
        self.assertGreater(hit["target"], hit["entry"])
        self.assertTrue(any("未確認" in line for line in hit["unconfirmed"]))
        miss, why = prefilter.xiao_shidi(self.series(3400, 3300, 3350), "ETHUSDT")
        self.assertIsNone(miss)
        self.assertIn("100", why)

    def test_other_coins_use_about_3_percent(self):
        hit, why = prefilter.xiao_shidi(self.series(100, 96, 98), "SOLUSDT")
        self.assertIsNotNone(hit, why)
        self.assertEqual(hit["entry_type"], "market")
        self.assertGreaterEqual(hit["drop_pct"], 3)
        miss, why = prefilter.xiao_shidi(self.series(100, 98, 99), "SOLUSDT")
        self.assertIsNone(miss)
        self.assertIn("3%", why)

    def test_da_shixiong_not_on_the_black_bar_and_keeps_stop_floor(self):
        bars = []
        for i in range(10):
            bars.append(bar(100, 102, 99, 101, 10))
        bars.append(bar(110, 112, 90, 95, 30))
        for i in range(9):
            bars.append(bar(100, 108, 96, 101, 10))
        for i in range(9):
            bars.append(bar(98, 104, 94, 99, 10))
        bars.append(bar(96, 97, 92, 96, 10))
        hit, why = prefilter.da_shixiong(bars, "BTCUSDT")
        self.assertIsNotNone(hit, why)
        self.assertGreaterEqual(hit["rebound_bars"], 10)
        self.assertNotEqual(hit["entry_mode"], "")
        self.assertLessEqual(hit["stop"], 90)
        self.assertAlmostEqual(hit["target_3r"] - hit["entry"], 1.5 * (hit["target"] - hit["entry"]), places=4)
        self.assertIn("4R", hit["exit"])
        self.assertTrue(any("未確認" in line for line in hit["unconfirmed"]))
        bars[12]["c"] = 111
        miss, why = prefilter.da_shixiong(bars, "BTCUSDT")
        self.assertIsNone(miss)
        self.assertIn("V", why)
        clean = []
        for i in range(10):
            clean.append(bar(100, 102, 99, 101, 10))
        clean.append(bar(110, 112, 90, 95, 30))
        for i in range(9):
            clean.append(bar(100, 108, 96, 101, 10))
        for i in range(9):
            clean.append(bar(98, 104, 94, 99, 10))
        clean.append(bar(96, 97, 92, 96, 10))
        alt, why = prefilter.da_shixiong(clean, "SOLUSDT")
        self.assertIsNotNone(alt, why)
        self.assertLessEqual(alt["stop"], alt["entry"] * 0.9 + 1e-6)
        import inspect
        self.assertIn('setup="funding_rate"', inspect.getsource(prefilter.funding))


if __name__ == "__main__":
    unittest.main()
