"""策略 context 要按 setup 取已收盤 K。空倉不掃市場。不發真 HTTP。"""
import unittest
import urllib.request

from src.phase1.market_adapter import fetch_strategy_market, run_review_entry

NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
NOW_MS = 1794025200000  # 2026-11-07T12:20:00+08:00，與 NOW 同一刻
_STEP = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000}


def _raw(interval, count, now_ms, forming=False):
    step = _STEP[interval]
    boundary = now_ms - (now_ms % step)
    start = boundary - step * count
    rows = []
    for index in range(count):
        opened = start + index * step
        rows.append([opened, "1", "2", "0.5", "1.5", "9", opened + step - 1])
    if forming:
        opened = boundary
        rows.append([opened, "1", "2", "0.5", "1.5", "9", opened + step - 1])
    return rows


def _book():
    return {"positions": []}, [], []


def _private(account, orders, algo):
    def get(path):
        if path.endswith("account"):
            return account
        if path.endswith("openOrders"):
            return orders
        if path.endswith("openAlgoOrders"):
            return algo
        raise ValueError(path)
    return get


class Run37ContextTest(unittest.TestCase):
    def test_flat_book_does_not_fetch_klines_and_setups_need_closed_bars(self):
        sockets = []
        real_open = urllib.request.urlopen

        def blocked(*_args, **_kwargs):
            sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked
        try:
            calls = []

            def public(symbol, interval, limit):
                calls.append((symbol, interval, limit))
                # 幣安回傳的 limit 根已含最後一根未收盤 K，所以已收盤的是 limit - 1 根。
                return _raw(interval, limit - 1, NOW_MS, forming=True)

            empty = fetch_strategy_market(_private(*_book()), public, [], NOW_MS)
            self.assertEqual(calls, [])
            self.assertTrue(empty["context_sufficient"])
            self.assertEqual(empty["daily_ma_parameter"], "undecided")
            self.assertEqual(empty["daily_ma_gate"], "not_evaluated")
            self.assertNotIn("30", str(empty.get("daily_ma_gate")))
            held = {"positions": [{"symbol": "ETHUSDT", "positionAmt": "1", "entryPrice": "10"}]}
            orders = [{"symbol": "ETHUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "9", "origQty": "1"}]
            unknown = fetch_strategy_market(_private(held, orders, []), public, [], NOW_MS)
            self.assertFalse(unknown["context_sufficient"])
            self.assertIn("unknown_setup:ETHUSDT", unknown["missing"])
            self.assertEqual(unknown["old_ai_poll"], "keep")
            self.assertEqual(calls, [])
            calls[:] = []
            candidate = {"symbol": "ETHUSDT", "setup": "瘸師弟", "plan": "ladder", "stop": 1, "bar_time": NOW}
            short = fetch_strategy_market(
                _private(*_book()),
                lambda symbol, interval, limit: _raw("15m", 20, NOW_MS),
                [candidate],
                NOW_MS,
            )
            self.assertFalse(short["context_sufficient"])
            self.assertTrue(any(item.startswith("klines:") for item in short["missing"]))
            calls[:] = []
            ready = fetch_strategy_market(_private(*_book()), public, [candidate], NOW_MS)
            self.assertTrue(ready["context_sufficient"], ready["missing"])
            asked = {(symbol, interval, limit) for symbol, interval, limit in calls}
            self.assertIn(("ETHUSDT", "15m", 25), asked)
            self.assertIn(("ETHUSDT", "4h", 7), asked)
            self.assertIn(("ETHUSDT", "1d", 7), asked)
            self.assertIn(("BTCUSDT", "15m", 25), asked)
            self.assertEqual(len(ready["klines"]["ETHUSDT"]["15m"]), 24)
            self.assertTrue(all(bar["closed"] is True for bar in ready["klines"]["ETHUSDT"]["15m"]))
            self.assertLess(ready["klines"]["ETHUSDT"]["15m"][-1]["open_time"] + _STEP["15m"], NOW_MS)
            bulu = dict(candidate, setup="布鹿流")
            calls[:] = []
            bulu_ready = fetch_strategy_market(_private(*_book()), public, [bulu], NOW_MS)
            self.assertTrue(bulu_ready["context_sufficient"], bulu_ready["missing"])
            self.assertIn(("ETHUSDT", "1h", 49), {(item[0], item[1], item[2]) for item in calls})
            self.assertIn(("ETHUSDT", "4h", 51), {(item[0], item[1], item[2]) for item in calls})
            self.assertNotIn(("ETHUSDT", "15m", 25), {(item[0], item[1], item[2]) for item in calls})
            odd = fetch_strategy_market(_private(*_book()), public, [dict(candidate, setup="大師兄")], NOW_MS)
            self.assertFalse(odd["context_sufficient"])
            self.assertTrue(any(item.startswith("unknown_setup:") for item in odd["missing"]))
        finally:
            urllib.request.urlopen = real_open
        self.assertEqual(sockets, [])

    def test_quiet_entry_does_not_scan_the_market(self):
        from src.phase1.session import Store
        import tempfile
        calls = []

        def private(path):
            calls.append(path)
            if path.endswith("account"):
                return {"positions": []}
            return []

        def public(symbol, interval, limit):
            calls.append(("kline", symbol, interval, limit))
            return _raw(interval, limit, NOW_MS)

        with tempfile.TemporaryDirectory() as tmp:
            result = run_review_entry(
                Store(tmp),
                {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0, "open_orders": 0, "algo_orders": 0,
                 "manage_error": False, "status_trusted": True, "queries_ok": True,
                 "queries": {"account": True, "open_orders": True, "open_algo_orders": True}},
                {"call_ai": False, "n_candidates": 0, "candidates": [], "btc": {"last": 100000.0}},
                now=NOW, scan_id="q37", now_ms=NOW_MS,
                entry={"enabled": True, "live_fetch": True, "replaces_scan_entry": False, "outbound": False, "gateway_base_url": ""},
                private_get=private, public_get=public,
            )
        self.assertTrue(result["quiet"])
        self.assertFalse(result["wakes_saved"])
        self.assertFalse(result["routine_replaced"])
        self.assertFalse(any(item[0] == "kline" for item in calls if isinstance(item, tuple)))
