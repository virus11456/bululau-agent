"""hardfilter.py：只擋確定不符合硬條件的候選；不確定、缺資料一律留給 AI；限流與讀取失敗結束碼 2。不連網。"""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
import hardfilter as hf  # noqa: E402
import replay_hardfilter  # noqa: E402

T0 = 1762790400000  # 任意整點（毫秒）
M15 = 15 * 60 * 1000
H1 = 60 * 60 * 1000


def bars(spec, step, start=T0):
    """spec: [(o, c, v)]；h/l 由 o/c 推。"""
    out = []
    for i, (o, c, v) in enumerate(spec):
        t = start + i * step
        out.append({"t": t, "o": o, "h": max(o, c) * 1.001, "l": min(o, c) * 0.999, "c": c, "v": v, "ct": t + step - 1})
    return out


def label(ms):
    from datetime import datetime
    return datetime.fromtimestamp(ms / 1000, hf.TPE).strftime("%m-%d %H:%M")


class KuaiTest(unittest.TestCase):
    def k15(self, tail):
        flat = [(100, 100, 10)] * 30
        return bars(flat + tail, M15)

    def cand(self, k, idx, big=False):
        return {"symbol": "XUSDT", "setup": "瘸師弟", "bar": label(k[idx]["t"]), "big": big}

    def test_single_long_black_blocked(self):
        k = self.k15([(100, 100.5, 10), (100, 95, 50), (95, 96, 10)])
        v, why = hf.rule_kuai_consecutive_blacks(self.cand(k, 31), k, k[-1]["ct"] + 1)
        self.assertEqual(v, "block", why)

    def test_two_consecutive_long_blacks_pass(self):
        for tail, idx in (([(100, 95.5, 20), (95.5, 91, 50)], 31), ([(100, 95, 50), (95, 91, 20)], 30)):
            k = self.k15(tail)
            v, _ = hf.rule_kuai_consecutive_blacks(self.cand(k, idx), k, k[-1]["ct"] + 1)
            self.assertEqual(v, "pass")

    def test_small_coin_threshold_is_4pct_big_is_2pct(self):
        k = self.k15([(100, 97, 20), (97, 92, 50)])  # 前一根黑約 3.1%
        self.assertEqual(hf.rule_kuai_consecutive_blacks(self.cand(k, 31, big=False), k, k[-1]["ct"] + 1)[0], "block")
        self.assertEqual(hf.rule_kuai_consecutive_blacks(self.cand(k, 31, big=True), k, k[-1]["ct"] + 1)[0], "pass")
        c = self.cand(k, 31); c.pop("big")  # 欄位缺 → 用較寬的 2%
        self.assertEqual(hf.rule_kuai_consecutive_blacks(c, k, k[-1]["ct"] + 1)[0], "pass")

    def test_uncertain_cases_keep(self):
        k = self.k15([(100, 95, 50)])
        ref = k[-1]["ct"] + 1
        for cand, kk in (
            ({"symbol": "XUSDT", "setup": "瘸師弟", "bar": "99-99 99:99", "big": False}, k),
            ({"symbol": "XUSDT", "setup": "瘸師弟", "big": False}, k),
            ({"symbol": "XUSDT", "setup": "瘸師弟", "bar": label(T0 + 500 * M15), "big": False}, k),
            (self.cand(k, 29), k),  # 29 是平的 K，重算不是長黑
            (self.cand(k, 30), None),
            (self.cand(k, 30), k[:2]),
        ):
            with self.subTest(cand=cand):
                self.assertEqual(hf.rule_kuai_consecutive_blacks(cand, kk, ref)[0], "uncertain")


class BuluTest(unittest.TestCase):
    def test_rr_after_5pct_floor(self):
        c = {"symbol": "XUSDT", "setup": "布鹿流", "entry": 1.0, "stop": 0.97, "prior_high": 1.07, "target": 1.06}
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "block")  # 0.07/0.05 = 1.4
        c["prior_high"] = 1.075
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")   # 1.5 剛好
        self.assertEqual(hf.rule_bulu_rr_after_widening(c, "plan_target")[0], "block")  # 0.06/0.05

    def test_wider_structural_stop_is_kept(self):
        c = {"symbol": "XUSDT", "setup": "布鹿流", "entry": 1.0, "stop": 0.90, "prior_high": 1.16}
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")  # 0.16/0.10 = 1.6，不會被 5% 改窄
        c["prior_high"] = 1.14
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "block")

    def test_btc_eth_use_3pct(self):
        c = {"symbol": "ETHUSDT", "setup": "布鹿流", "entry": 100.0, "stop": 99.0, "prior_high": 104.6}
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")  # 4.6/3 = 1.53
        c["symbol"] = "XUSDT"
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "block")  # 4.6/5

    def test_missing_or_bad_fields_uncertain(self):
        for c in (
            {"symbol": "XUSDT", "entry": 1.0, "stop": 0.97},
            {"symbol": "XUSDT", "entry": "1", "stop": 0.97, "prior_high": 1.2},
            {"symbol": "XUSDT", "entry": 1.0, "stop": 1.01, "prior_high": 1.2},
            {"symbol": "XUSDT", "entry": 0, "stop": 0, "prior_high": 1.2},
            {"symbol": "XUSDT", "entry": True, "stop": 0.9, "prior_high": 1.2},
        ):
            with self.subTest(c=c):
                self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "uncertain")

    def k1h(self, spike_v, spike_at=60):
        spec = [(100, 100.2, 10)] * 100
        spec[spike_at] = (100, 103, spike_v)
        k = bars(spec, H1)
        k[spike_at + 5]["h"] = 120  # 前高在爆量之後
        return k

    def test_volume_3x(self):
        self.assertEqual(hf.rule_bulu_volume({}, self.k1h(29))[0], "block")
        self.assertEqual(hf.rule_bulu_volume({}, self.k1h(30))[0], "pass")

    def test_volume_any_lookback_counts(self):
        # 前 10 根很安靜、前 20 根有雜訊：只要前 10 根均量那種算法達 3 倍就算有
        spec = [(100, 100.2, 10)] * 100
        for i in range(40, 50):
            spec[i] = (100, 100.2, 40)
        spec[60] = (100, 103, 31)
        k = bars(spec, H1)
        k[65]["h"] = 120
        self.assertEqual(hf.rule_bulu_volume({}, k)[0], "pass")

    def test_volume_short_data_uncertain(self):
        self.assertEqual(hf.rule_bulu_volume({}, self.k1h(30)[:60])[0], "uncertain")
        self.assertEqual(hf.rule_bulu_volume({}, None)[0], "uncertain")


class BlacklistAndRunTest(unittest.TestCase):
    def test_blacklist(self):
        for s, v in (("CAPUSDT", "block"), ("AINUSDT", "block"), ("INITUSDT", "block"), ("CAPYUSDT", "pass"),
                     ("BTCUSDT", "pass"), ("CAP", "uncertain"), (None, "uncertain")):
            with self.subTest(s=s):
                self.assertEqual(hf.rule_blacklist({"symbol": s})[0], v)

    def test_other_setups_are_kept_without_fetch(self):
        def no_fetch(*_a):
            raise AssertionError("不該抓 K 線")
        body = {"candidates": [{"symbol": "ETHUSDT", "setup": "小師弟"}, {"symbol": "SOLUSDT", "setup": "funding_rate"}]}
        out = hf.run(body, no_fetch, T0)
        self.assertEqual(len(out["kept"]), 2)
        self.assertEqual(out["blocked"], [])

    def test_per_candidate_data_error_keeps(self):
        def broken(*_a):
            raise hf.DataError("HTTP 500")
        body = {"candidates": [{"symbol": "XUSDT", "setup": "瘸師弟", "bar": "11-10 22:45", "big": False}, "garbage"]}
        out = hf.run(body, broken, T0)
        self.assertEqual(len(out["kept"]), 2)
        self.assertTrue(any("HTTP 500" in n for n in out["notes"]))

    def test_rate_limit_propagates(self):
        def limited(*_a):
            raise hf.RateLimited("429")
        with self.assertRaises(hf.RateLimited):
            hf.run({"candidates": [{"symbol": "XUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"}]}, limited, T0)

    def main_out(self, stdin_text, argv=None):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = hf.main(argv or [], stdin=io.StringIO(stdin_text))
        return code, json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_main_429_exits_2(self):
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            code, out = self.main_out(json.dumps({"candidates": [{"symbol": "XUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"}]}))
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertIn("rate_limited", out["error"])

    def test_main_418_exits_2(self):
        err = urllib.error.HTTPError("u", 418, "teapot", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            code, _out = self.main_out(json.dumps({"candidates": [{"symbol": "XUSDT", "setup": "布鹿流", "entry": 1, "stop": 0.9, "prior_high": 2}]}))
        self.assertEqual(code, 2)

    def test_main_http_500_keeps_candidate(self):
        err = urllib.error.HTTPError("u", 500, "err", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            code, out = self.main_out(json.dumps({"candidates": [{"symbol": "XUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"}]}))
        self.assertEqual(code, 0)
        self.assertEqual(len(out["kept"]), 1)

    def test_main_bad_input_exits_2(self):
        for text in ("not json", "[]", json.dumps({"x": 1}), json.dumps({"candidates": "a"})):
            with self.subTest(text=text):
                code, out = self.main_out(text)
                self.assertEqual(code, 2)
                self.assertFalse(out["ok"])

    def test_fetch_uses_quote_volume_and_closed_bars_only(self):
        raw = [
            [T0, "1", "2", "0.5", "1.5", "111", T0 + M15 - 1, "999", 5, "0", "0", "0"],
            [T0 + M15, "1.5", "2", "1", "1.8", "222", T0 + 2 * M15 - 1, "888", 5, "0", "0", "0"],
        ]

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(raw).encode()
        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            k = hf.fetch_klines("XUSDT", "15m", 2, end_ms=T0 + M15 + 5)
        self.assertEqual(len(k), 1)  # 第二根還沒收
        self.assertEqual(k[0]["v"], 999.0)  # 和 prefilter/common.py 一樣用第 8 欄 quote volume
        self.assertEqual(k[0]["c"], 1.5)

    def test_bad_kline_rows_are_data_error(self):
        with self.assertRaises(hf.DataError):
            hf.normalize([["x"]], T0)


class ReplayEquivalenceTest(unittest.TestCase):
    """11/10 15:04–23:30 的實際候選回放。"""

    def test_default_never_blocks_ai_pattern_or_doubtful(self):
        rows, _rounds = replay_hardfilter.replay("prior_high")
        self.assertEqual(len(rows), 18)
        for r in rows:
            if not r["ai_hard"] or r["ai_doubtful"]:
                self.assertEqual(r["new"], "交給AI", r)

    def test_default_blocks_hard_rule_rejections_except_documented_rr_target_cases(self):
        rows, rounds = replay_hardfilter.replay("prior_high")
        missed = [(r["time"], r["symbol"]) for r in rows if r["ai_hard"] and r["new"] != "擋"]
        # 這兩筆 AI 用 prefilter 的 2R 截斷 target 算 RR；以前高算 RR 約 2.6，屬目標價定義待確認，放行給 AI。
        self.assertEqual(missed, [("20:30", "MERLUSDT"), ("21:29", "MERLUSDT")])
        quiet_now = [r["time"] for r in rounds if r["before"].startswith("WAKE") and r["after"].startswith("QUIET")]
        self.assertEqual(quiet_now, ["22:28", "23:06"])

    def test_plan_target_mode_matches_ai_fully(self):
        rows, rounds = replay_hardfilter.replay("plan_target")
        self.assertTrue(all(r["consistent"] for r in rows), [r for r in rows if not r["consistent"]])
        quiet_now = [r["time"] for r in rounds if r["before"].startswith("WAKE") and r["after"].startswith("QUIET")]
        self.assertEqual(quiet_now, ["20:30", "22:28", "23:06"])


if __name__ == "__main__":
    unittest.main()
