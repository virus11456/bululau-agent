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

    def cand(self, k, idx, symbol="XUSDT"):
        return {"symbol": symbol, "setup": "瘸師弟", "bar": label(k[idx]["t"])}

    def ref(self, k):
        return k[-1]["ct"] + 1

    def test_btc_eth_single_2pct_passes_1_9_blocks(self):
        # 收黑 (o/c-1)。102→100 是 2%；101.9→100 是 1.9%。量 40 / 前均量 10 = 4 倍。
        for symbol in ("BTCUSDT", "ETHUSDT"):
            k = self.k15([(102, 100, 40)])
            v, why = hf.rule_kuai_long_black(self.cand(k, 30, symbol), k, self.ref(k))
            self.assertEqual(v, "pass", (symbol, why))
            k = self.k15([(101.9, 100, 40)])
            v, why = hf.rule_kuai_long_black(self.cand(k, 30, symbol), k, self.ref(k))
            self.assertEqual(v, "block", (symbol, why))
            self.assertIn("不是單根長黑", why)

    def test_small_coin_single_4pct_passes_3_9_blocks(self):
        k = self.k15([(104, 100, 40)])
        v, why = hf.rule_kuai_long_black(self.cand(k, 30), k, self.ref(k))
        self.assertEqual(v, "pass", why)
        k = self.k15([(103.9, 100, 40)])
        v, why = hf.rule_kuai_long_black(self.cand(k, 30), k, self.ref(k))
        self.assertEqual(v, "block", why)
        self.assertIn("不是單根長黑", why)
        # 24h 成交額很大的小幣也不適用 2%
        k = self.k15([(102, 100, 40)])
        c = self.cand(k, 30, "SOLUSDT")
        c["big"] = True
        self.assertEqual(hf.rule_kuai_long_black(c, k, self.ref(k))[0], "block")

    def test_neighbors_do_not_matter(self):
        # 前後都是紅 K，訊號本身 4% 且爆量，單根就過
        k = self.k15([(100, 101, 10), (104, 100, 40), (100, 101, 10)])
        v, why = hf.rule_kuai_long_black(self.cand(k, 31), k, self.ref(k))
        self.assertEqual(v, "pass", why)

    def test_long_black_without_volume_blocks(self):
        k = self.k15([(104, 100, 29)])  # 4% 長黑，29/10 = 2.9 倍
        v, why = hf.rule_kuai_long_black(self.cand(k, 30), k, self.ref(k))
        self.assertEqual(v, "block", why)
        self.assertIn("爆量不足", why)

    def test_volume_without_long_black_blocks(self):
        k = self.k15([(100, 100.5, 50)])  # 收紅，量 5 倍
        v, why = hf.rule_kuai_long_black(self.cand(k, 30), k, self.ref(k))
        self.assertEqual(v, "block", why)
        self.assertIn("不是單根長黑", why)

    def test_either_lookback_reaches_3x(self):
        # 前 10 根安靜（量 10）、更早的 10 根很大：前 10 達 3 倍、前 20 不到，仍算有爆量
        spec = [(100, 100, 100)] * 20 + [(100, 100, 10)] * 10 + [(104, 100, 30)]
        k = bars(spec, M15)
        v, why = hf.rule_kuai_long_black(self.cand(k, 30), k, self.ref(k))
        self.assertEqual(v, "pass", why)

    def test_uncertain_cases_keep(self):
        k = self.k15([(104, 100, 40)])
        ref = self.ref(k)
        for cand, kk in (
            ({"symbol": "XUSDT", "setup": "瘸師弟", "bar": "99-99 99:99"}, k),
            ({"symbol": "XUSDT", "setup": "瘸師弟"}, k),
            ({"symbol": "XUSDT", "setup": "瘸師弟", "bar": label(T0 + 500 * M15)}, k),
            ({"setup": "瘸師弟", "bar": label(k[30]["t"])}, k),
            (self.cand(k, 30), None),
            (self.cand(k, 30), k[:2]),
        ):
            with self.subTest(cand=cand):
                self.assertEqual(hf.rule_kuai_long_black(cand, kk, ref)[0], "uncertain")

    def test_flat_signal_bar_is_blocked_not_uncertain(self):
        k = self.k15([(104, 100, 40)])
        v, why = hf.rule_kuai_long_black(self.cand(k, 29), k, self.ref(k))
        self.assertEqual(v, "block", why)


class BuluTest(unittest.TestCase):
    def test_rr_after_5pct_floor(self):
        c = {"symbol": "XUSDT", "setup": "布鹿流", "entry": 1.0, "stop": 0.97, "prior_high": 1.07, "target": 1.06}
        # 前高只有 1.4R。目標改放到 1.5R，可以超過前高，不擋。
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")
        c["prior_high"] = 1.075
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")   # 1.5 剛好
        self.assertEqual(hf.rule_bulu_rr_after_widening(c, "plan_target")[0], "block")  # 0.06/0.05

    def test_wider_structural_stop_is_kept(self):
        c = {"symbol": "XUSDT", "setup": "布鹿流", "entry": 1.0, "stop": 0.90, "prior_high": 1.16}
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")  # 0.16/0.10 = 1.6，不會被 5% 改窄
        c["prior_high"] = 1.14
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")  # 1.4R 的前高改放到 1.5R

    def test_rr_cap_defaults_off_and_two_r_still_passes(self):
        self.assertIsNone(hf.BULU_RR_CAP)
        # 結構停損 10% 比 5% 寬。前高在 4R。不封頂就停在前高；2R 上限收到 1.20。
        _wide, target = hf.bulu_r3_prices("XUSDT", 1.0, 0.90, 1.40)
        self.assertAlmostEqual(target, 1.40)
        _wide, capped = hf.bulu_r3_prices("XUSDT", 1.0, 0.90, 1.40, rr_cap=2.0)
        self.assertAlmostEqual(capped, 1.20)
        cand = {"symbol": "XUSDT", "entry": 1.0, "stop": 0.90, "prior_high": 1.40}
        self.assertEqual(hf.rule_bulu_rr_after_widening(cand)[0], "pass")
        self.assertEqual(hf.rule_bulu_rr_after_widening(cand, rr_cap=2.0)[0], "pass")

    def test_entry_not_below_prior_high_is_chase(self):
        c = {"symbol": "XUSDT", "setup": "布鹿流", "entry": 1.0, "stop": 0.90, "prior_high": 1.0}
        verdict, why = hf.rule_bulu_rr_after_widening(c)
        self.assertEqual(verdict, "block")
        self.assertIn("追價", why)
        c["prior_high"] = 0.99
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "block")

    def test_btc_eth_use_3pct(self):
        c = {"symbol": "ETHUSDT", "setup": "布鹿流", "entry": 100.0, "stop": 99.0, "prior_high": 104.6}
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")  # 4.6/3 = 1.53
        c["symbol"] = "XUSDT"
        # 套 5% 之後前高只剩 0.92R，目標放到 1.5R（107.5，超過前高 104.6），不擋。
        self.assertEqual(hf.rule_bulu_rr_after_widening(c)[0], "pass")

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

    def main_out(self, stdin_text, argv=None, sleep=None):
        buf = io.StringIO()
        if sleep is None:
            sleep = lambda _s: None
        with mock.patch("sys.stdout", buf):
            code = hf.main(argv or [], stdin=io.StringIO(stdin_text), sleep=sleep)
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
        # MERL 這兩筆：AI 用 prefilter 的 2R 截斷 target 算 RR；以前高算約 2.6，目標價定義待確認，放行給 AI。
        # 2026-11-11 起瘸師弟改單根長黑。PLAY／BULLA／ARIA／CYBER 不再因「只有 1 根」被擋，交給 AI。
        # AIN、CAP 仍被黑名單擋，所以不在這份放行清單。
        self.assertEqual(missed, [
            ("15:04", "PLAYUSDT"),
            ("19:30", "BULLAUSDT"),
            ("19:30", "ARIAUSDT"),
            ("20:30", "CYBERUSDT"),
            ("20:30", "MERLUSDT"),
            ("21:29", "MERLUSDT"),
            # BEAMX：前高套 5% 後 RR 約 1.0。目標改放到 1.5R，可以超過前高，不再擋。
            ("22:28", "BEAMXUSDT"),
        ])
        quiet_now = [r["time"] for r in rounds if r["before"].startswith("WAKE") and r["after"].startswith("QUIET")]
        self.assertEqual(quiet_now, ["23:06"])

    def test_plan_target_mode_matches_ai_except_single_black(self):
        rows, rounds = replay_hardfilter.replay("plan_target")
        released = sorted((r["time"], r["symbol"]) for r in rows if not r["consistent"])
        self.assertEqual(released, [
            ("15:04", "PLAYUSDT"),
            ("19:30", "ARIAUSDT"),
            ("19:30", "BULLAUSDT"),
            ("20:30", "CYBERUSDT"),
        ])
        quiet_now = [r["time"] for r in rounds if r["before"].startswith("WAKE") and r["after"].startswith("QUIET")]
        self.assertEqual(quiet_now, ["22:28", "23:06"])


CLOSED = [T0, "1", "2", "0.5", "1.5", "111", T0 + M15 - 1, "999", 5, "0", "0", "0"]


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def http_error(code, retry_after=None):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://fapi.binance.com/fapi/v1/klines", code, "err", headers, None)


class KlineRateLimitRetryTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.calls = 0

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def fetch(self, urlopen):
        with mock.patch("urllib.request.urlopen", side_effect=urlopen), \
                mock.patch("hardfilter.time.sleep", side_effect=AssertionError("不該真的 sleep")):
            return hf.fetch_klines("XUSDT", "15m", 2, end_ms=T0 + M15, sleep=self.sleep)

    def main_with(self, urlopen, text, sleep=None, monotonic=None):
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=urlopen), mock.patch("sys.stdout", buf), \
                mock.patch("hardfilter.time.sleep", side_effect=AssertionError("不該真的 sleep")):
            code = hf.main(
                [], stdin=io.StringIO(text),
                sleep=self.sleep if sleep is None else sleep,
                monotonic=monotonic,
            )
        line = buf.getvalue().strip().splitlines()[-1]
        return code, json.loads(line)

    def test_agreed_caps_fit_under_gate_timeout(self):
        import gate as gate_mod
        self.assertEqual(hf.HARDFILTER_MAX_RETRIES, 2)
        self.assertEqual(hf.HARDFILTER_BACKOFF_S, (2.0, 6.0))
        self.assertEqual(hf.HARDFILTER_WAIT_CAP_S, 8.0)
        self.assertEqual(hf.HARDFILTER_TOTAL_WAIT_CAP_S, 15.0)
        self.assertEqual(hf.REQUEST_TIMEOUT_S, 10)
        self.assertEqual(hf.HARDFILTER_TIME_MARGIN_S, 5.0)
        self.assertEqual(hf.HARDFILTER_RUN_BUDGET_S, gate_mod.HARDFILTER_TIMEOUT_S)
        self.assertEqual(hf.HARDFILTER_RUN_BUDGET_S, 60.0)
        reserve = hf.REQUEST_TIMEOUT_S + hf.HARDFILTER_TIME_MARGIN_S
        self.assertLess(reserve, hf.HARDFILTER_RUN_BUDGET_S)
        self.assertLessEqual(sum(hf.HARDFILTER_BACKOFF_S), hf.HARDFILTER_TOTAL_WAIT_CAP_S)
        self.assertTrue(all(step <= hf.HARDFILTER_WAIT_CAP_S for step in hf.HARDFILTER_BACKOFF_S))
        self.assertTrue(all(step + reserve <= hf.HARDFILTER_RUN_BUDGET_S for step in hf.HARDFILTER_BACKOFF_S))

    def test_429_then_success(self):
        def urlopen(_req, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise http_error(429)
            return _Body([CLOSED])
        bars = self.fetch(urlopen)
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual(bars[0]["v"], 999.0)
        # 同一種 429 後成功，再走 main，確認結束碼 0 且 JSON 帶重試次數。
        self.calls = 0
        self.sleeps = []
        code, out = self.main_with(
            urlopen,
            json.dumps({"candidates": [{"symbol": "XUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"}]}),
        )
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["rate_limit_retries"], 1)
        self.assertEqual(out["rate_limit_wait_s"], 2.0)
        self.assertEqual(out["rate_limit_waits_s"], [2.0])
        self.assertEqual(out["rate_limit_statuses"], [429])
        self.assertIsNone(out["rate_limit_stopped"])
        self.assertEqual(self.sleeps, [2.0])

    def test_418_then_success(self):
        def urlopen(_req, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise http_error(418)
            return _Body([CLOSED])
        budget_bars = self.fetch(urlopen)
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual(len(budget_bars), 1)

    def test_retries_exhausted_exits_2(self):
        def urlopen(_req, timeout=None):
            self.calls += 1
            raise http_error(429)
        code, out = self.main_with(
            urlopen,
            json.dumps({"candidates": [{"symbol": "XUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"}]}),
        )
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertIn("rate_limited", out["error"])
        self.assertIn("429", out["error"])
        self.assertEqual(self.calls, 3)
        self.assertEqual(self.sleeps, [2.0, 6.0])
        self.assertEqual(out["rate_limit_retries"], 2)
        self.assertEqual(out["rate_limit_wait_s"], 8.0)
        self.assertEqual(out["rate_limit_waits_s"], [2.0, 6.0])
        self.assertEqual(out["rate_limit_statuses"], [429, 429, 429])
        self.assertEqual(out["rate_limit_stopped"], "retries_exhausted")

    def test_retry_after_is_honored(self):
        def urlopen(_req, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise http_error(429, retry_after=3)
            return _Body([CLOSED])
        self.fetch(urlopen)
        self.assertEqual(self.sleeps, [3.0])
        self.assertEqual(self.calls, 2)

    def test_retry_after_zero_uses_backoff(self):
        def urlopen(_req, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise http_error(429, retry_after=0)
            return _Body([CLOSED])
        self.fetch(urlopen)
        self.assertEqual(self.sleeps, [2.0])

    def test_retry_after_over_single_cap_does_not_wait(self):
        def urlopen(_req, timeout=None):
            self.calls += 1
            raise http_error(418, retry_after=9)
        with self.assertRaises(hf.RateLimited):
            self.fetch(urlopen)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_total_wait_cap_stops_before_the_next_sleep(self):
        def urlopen(_req, timeout=None):
            self.calls += 1
            raise http_error(429, retry_after=8)
        with self.assertRaises(hf.RateLimited):
            self.fetch(urlopen)
        # 第一次 8 秒剛好等於單次上限；第二次 8+8 超過累計 15 秒，不等。
        self.assertEqual(self.sleeps, [8.0])
        self.assertEqual(self.calls, 2)

    def test_http_500_and_url_error_do_not_retry(self):
        def denied(_req, timeout=None):
            self.calls += 1
            raise http_error(500)
        with self.assertRaises(hf.DataError):
            self.fetch(denied)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.sleeps, [])

        self.calls = 0
        def broken(_req, timeout=None):
            self.calls += 1
            raise urllib.error.URLError("timed out")
        with self.assertRaises(hf.DataError):
            self.fetch(broken)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_process_budget_is_shared_across_symbols(self):
        """整輪最多再試 2 次。前兩個幣各用掉一次之後，第三個 429 不再等。"""
        symbols = []

        def urlopen(req, timeout=None):
            self.calls += 1
            symbols.append(req.full_url)
            # 每個幣第一次 429，第二次成功；第三個幣不該有第二次。
            so_far = symbols.count(req.full_url)
            if "CUSDT" in req.full_url:
                raise http_error(429)
            if so_far == 1:
                raise http_error(429)
            return _Body([CLOSED])

        text = json.dumps({"candidates": [
            {"symbol": "AUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"},
            {"symbol": "BUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"},
            {"symbol": "CUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"},
        ]})
        code, out = self.main_with(urlopen, text)
        self.assertEqual(code, 2)
        self.assertEqual(self.sleeps, [2.0, 2.0])
        self.assertEqual(out["rate_limit_retries"], 2)
        self.assertEqual(out["rate_limit_wait_s"], 4.0)
        self.assertEqual(out["rate_limit_stopped"], "retries_exhausted")
        self.assertTrue(any("CUSDT" in url for url in symbols))
        self.assertEqual(sum(1 for url in symbols if "CUSDT" in url), 1)

    def test_exact_reserve_still_waits(self):
        # 剩餘剛好等於「等待 + 10 秒請求 + 5 秒餘裕」時仍可等。
        clock = _StepClock()
        budget = hf.KlineRetryBudget(clock.sleep, clock)

        def urlopen(_req, timeout=None):
            self.calls += 1
            if self.calls == 1:
                clock.advance(hf.HARDFILTER_RUN_BUDGET_S - (2 + hf.REQUEST_TIMEOUT_S + hf.HARDFILTER_TIME_MARGIN_S))
                raise http_error(429)
            return _Body([CLOSED])

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            bars = hf.fetch_klines("XUSDT", "15m", 2, end_ms=T0 + M15, budget=budget)
        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(budget.stopped, None)
        self.assertEqual(bars[0]["v"], 999.0)

    def test_tight_remaining_does_not_wait_and_fails_open(self):
        clock = _StepClock()

        def urlopen(_req, timeout=None):
            self.calls += 1
            clock.advance(50)  # 剩 10 秒，蓋不住 2 + 10 + 5
            raise http_error(429)

        text = json.dumps({"candidates": [{"symbol": "XUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"}]})
        code, out = self.main_with(urlopen, text, sleep=clock.sleep, monotonic=clock)
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertIn("rate_limited", out["error"])
        self.assertEqual(out["rate_limit_retries"], 0)
        self.assertEqual(out["rate_limit_wait_s"], 0)
        self.assertEqual(out["rate_limit_stopped"], "budget_exhausted")
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(self.calls, 1)

    def test_second_wait_skipped_after_time_is_used(self):
        clock = _StepClock()

        def urlopen(_req, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise http_error(429)
            clock.advance(40)  # 睡了 2 秒之後再加 40，剩下 18，蓋不住下一次 6 + 15
            raise http_error(429)

        text = json.dumps({"candidates": [{"symbol": "XUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"}]})
        code, out = self.main_with(urlopen, text, sleep=clock.sleep, monotonic=clock)
        self.assertEqual(code, 2)
        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(self.calls, 2)
        self.assertEqual(out["rate_limit_retries"], 1)
        self.assertEqual(out["rate_limit_wait_s"], 2.0)
        self.assertEqual(out["rate_limit_stopped"], "budget_exhausted")


class _StepClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += float(seconds)

    def advance(self, seconds):
        self.t += float(seconds)


if __name__ == "__main__":
    unittest.main()
