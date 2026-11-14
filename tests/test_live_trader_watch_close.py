"""平倉紀錄、watch 心跳、階梯不再因沒預掛止盈每分鐘跑 manage。不連網、不讀金鑰。"""
import io
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="live-trader-watch-close-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402

ACC = {
    "totalWalletBalance": "1000",
    "totalUnrealizedProfit": "0",
    "availableBalance": "1000",
    "positions": [],
}
DAY = {"day": "2026-11-14", "day_start_equity": 1000.0, "slice_margin": 50.0,
       "halted_new": False, "killed": False}


def _algo(symbol, kind, algo_id):
    return {"symbol": symbol, "orderType": kind, "algoId": algo_id, "quantity": "1",
            "closePosition": "false", "triggerPrice": "1"}


class CloseAndHeartbeatTest(unittest.TestCase):
    def test_meta_without_position_is_logged_and_dry_run_does_not_save(self):
        state = {"pending": {}, "meta": {"XUSDT": {"setup": "瘸師弟", "stop": "1.1", "target": "2.2"}}}
        saved = []
        logs = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/income":
                raise lt.BinanceError(500, -1000, "income down")
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            raise AssertionError((method, path))

        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state", side_effect=lambda s: saved.append(s)), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                patch.object(lt, "load_api_keys", side_effect=AssertionError("keys")):
            with redirect_stdout(buf):
                lt.cmd_manage(type("A", (), {"live": False})())
        self.assertEqual(saved, [])
        self.assertNotIn("XUSDT", state["meta"])
        self.assertIn("XUSDT 已平倉 出場價 null 觸發=未知 已實現 null", buf.getvalue())
        closed = [kw for kind, kw in logs if kind == "position_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["symbol"], "XUSDT")
        self.assertEqual(closed[0]["setup"], "瘸師弟")
        self.assertEqual(closed[0]["stop"], "1.1")
        self.assertEqual(closed[0]["target"], "2.2")
        self.assertIsNone(closed[0]["pnl"])
        self.assertIsNone(closed[0]["exit_price"])
        self.assertEqual(closed[0]["reason"], "unknown")

    def test_live_close_saves_and_sums_pnl(self):
        state = {"pending": {}, "meta": {"XUSDT": {"setup": "布鹿流", "stop": "9", "target": "12"}}}
        saved = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/income":
                if (params or {}).get("incomeType") == "TRANSFER":
                    return []
                self.assertEqual(params["incomeType"], "REALIZED_PNL")
                self.assertEqual(params["symbol"], "XUSDT")
                self.assertIn("startTime", params)
                return [{"income": "1.5"}, {"income": "-0.25"}]
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            raise AssertionError((method, path))

        logs = []
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state", side_effect=lambda s: saved.append(json.loads(json.dumps(s)))), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            lt.cmd_manage(type("A", (), {"live": True})())
        self.assertEqual(saved[-1]["meta"], {})
        closed = [kw for kind, kw in logs if kind == "position_closed"]
        self.assertEqual(closed[0]["pnl"], "1.2500")

    def test_heartbeat_at_most_once_per_hour_and_no_api(self):
        store = {"pending": {}}
        calls = []
        now = datetime(2026, 11, 14, 9, 5, tzinfo=lt.TPE)

        def load():
            return json.loads(json.dumps(store))

        def save(s):
            store.clear()
            store.update(s)

        def fake_req(*_a, **_k):
            calls.append(1)
            raise AssertionError("沒有待成交單，不該打 API")

        def run():
            buf = io.StringIO()
            with patch.object(lt, "req", fake_req), \
                    patch.object(lt, "load_state", side_effect=load), \
                    patch.object(lt, "save_state", side_effect=save), \
                    patch.object(lt, "now_tpe", return_value=now), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                with redirect_stdout(buf):
                    lt.cmd_watch(type("A", (), {"live": False})())
            return buf.getvalue()

        first = run()
        second = run()
        self.assertEqual(first.count("[watch] 無待成交限價單 09:05"), 1)
        self.assertEqual(second, "")
        self.assertEqual(calls, [])
        store["watch_heartbeat"] = (now - timedelta(hours=2)).isoformat(timespec="seconds")
        third = run()
        self.assertIn("[watch] 無待成交限價單 09:05", third)
        self.assertEqual(calls, [])


class LadderWatchTest(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.managed = []
        self.orders = {}
        self.algos = []

    def load(self):
        return json.loads(json.dumps(self.store))

    def save(self, s):
        self.store.clear()
        self.store.update(s)

    def fake_req(self, method, path, params=None, signed=False, retries=5):
        if path == "/fapi/v1/openAlgoOrders":
            return list(self.algos)
        if path == "/fapi/v1/openOrders":
            return []
        if path == "/fapi/v2/positionRisk":
            return [{"symbol": "XUSDT", "positionAmt": "0", "markPrice": "0.01"}]
        if method == "GET" and path == "/fapi/v1/order":
            return dict(self.orders[params["orderId"]])
        raise AssertionError((method, path, params))

    def watch(self):
        with patch.object(lt, "req", self.fake_req), \
                patch.object(lt, "log"), \
                patch.object(lt, "load_state", side_effect=self.load), \
                patch.object(lt, "save_state", side_effect=self.save), \
                patch.object(lt, "cmd_manage", side_effect=lambda a: self.managed.append(1)), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            lt.cmd_watch(type("A", (), {"live": True})())

    def ladder(self, status="NEW", executed="0", algos=None, last_exec=None):
        pd = {"ladder": True, "orders": [{"orderId": 1}, {"orderId": 2}]}
        if last_exec is not None:
            pd["last_exec"] = last_exec
        self.store = {"pending": {"XUSDT": pd}}
        self.orders = {
            1: {"orderId": 1, "status": status, "executedQty": executed},
            2: {"orderId": 2, "status": "NEW", "executedQty": "0"},
        }
        self.algos = algos if algos is not None else [_algo("XUSDT", "STOP_MARKET", 9)]

    def test_prefill_without_tp_does_not_trigger(self):
        self.ladder(status="NEW", executed="0")
        self.watch()
        self.watch()
        self.assertEqual(self.managed, [])

    def test_partial_fill_triggers_once(self):
        self.ladder(status="PARTIALLY_FILLED", executed="2",
                    algos=[_algo("XUSDT", "STOP_MARKET", 9), _algo("XUSDT", "TAKE_PROFIT_MARKET", 8)])
        self.watch()
        self.watch()
        self.assertEqual(self.managed, [1])

    def test_missing_stop_triggers(self):
        self.ladder(status="NEW", executed="0", algos=[_algo("XUSDT", "TAKE_PROFIT_MARKET", 8)])
        self.watch()
        self.assertEqual(self.managed, [1])

    def test_one_filled_level_does_not_call_manage_every_minute(self):
        both = [_algo("XUSDT", "STOP_MARKET", 9), _algo("XUSDT", "TAKE_PROFIT_MARKET", 8)]
        self.ladder(status="FILLED", executed="2", algos=both)
        self.watch()
        self.watch()
        self.assertEqual(self.managed, [1])
        self.assertEqual(self.store["pending"]["XUSDT"]["last_done"], 1)
        self.orders[2] = {"orderId": 2, "status": "FILLED", "executedQty": "3"}
        self.watch()
        self.assertEqual(self.managed, [1, 1])

    def test_single_order_watch_is_unchanged(self):
        # 單張：掛著且沒成交，就算沒有止盈也不叫 manage。終結了才叫。
        self.store = {"pending": {"XUSDT": {"orderId": 5}}}
        self.orders = {5: {"orderId": 5, "status": "NEW", "executedQty": "0"}}
        self.algos = []
        self.watch()
        self.assertEqual(self.managed, [])
        self.orders[5] = {"orderId": 5, "status": "PARTIALLY_FILLED", "executedQty": "1"}
        self.algos = [_algo("XUSDT", "STOP_MARKET", 9), _algo("XUSDT", "TAKE_PROFIT_MARKET", 8)]
        self.watch()
        self.assertEqual(self.managed, [])
        self.orders[5] = {"orderId": 5, "status": "FILLED", "executedQty": "1"}
        self.watch()
        self.assertEqual(self.managed, [1])


class MarkReducedTest(unittest.TestCase):
    def test_manage_cancels_tp_and_replaces_stop_at_cost(self):
        lt.STOP_READBACK_SLEEP_S = 0
        state = {"pending": {}, "meta": {"XUSDT": {
            "setup": "布鹿流", "stop": "90", "target": "120",
        }}}
        saved = []
        logs = []
        calls = []

        def load():
            return json.loads(json.dumps(state))

        def save(s):
            state.clear()
            state.update(json.loads(json.dumps(s)))
            saved.append(json.loads(json.dumps(s)))

        posted = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "XUSDT", "positionAmt": "4", "entryPrice": "100.239"}]
            if path == "/fapi/v1/openAlgoOrders":
                # 有止盈、沒有停損。撤掉止盈之後仍要看得到剛掛上的停損。
                # 這次呼叫自己也在 calls 裡，不能把「正在讀」算成已撤。
                prior = calls[:-1]
                base = [] if any(c[1] == "/fapi/v1/algoOrder" and c[0] == "DELETE" for c in prior) else [
                    {"symbol": "XUSDT", "orderType": "TAKE_PROFIT_MARKET", "algoId": 8,
                     "quantity": "10", "closePosition": "false", "triggerPrice": "120"}]
                return base + list(posted)
            if path == "/fapi/v1/openOrders":
                return []
            if method == "POST" and path == "/fapi/v1/algoOrder":
                posted.append({"symbol": (params or {}).get("symbol"), "orderType": (params or {}).get("type"),
                               "algoId": 20, "algoStatus": "NEW", "quantity": (params or {}).get("quantity"),
                               "triggerPrice": (params or {}).get("triggerPrice"), "closePosition": "false"})
                return {"algoId": 20}
            if method == "DELETE":
                return {}
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return []
            raise AssertionError((method, path, params))

        acc = dict(ACC)
        acc["positions"] = [{"symbol": "XUSDT", "positionAmt": "4", "positionSide": "BOTH"}]
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch.object(lt, "sym_info", return_value={
                    "tick": __import__("decimal").Decimal("0.01"),
                    "step": __import__("decimal").Decimal("0.001"),
                    "status": "TRADING"}), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            lt.cmd_mark_reduced(type("A", (), {"live": True, "symbol": "XUSDT"})())
            self.assertEqual(state["meta"]["XUSDT"]["stop"], "100.23")
            self.assertTrue(state["meta"]["XUSDT"]["reduced"])
            self.assertTrue(state["meta"]["XUSDT"]["no_tp"])
            self.assertTrue(any(k == "mark_reduced" for k, _kw in logs))
            before = len(calls)
            lt.cmd_manage(type("A", (), {"live": True})())
        posts = [c for c in calls[before:] if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        self.assertEqual([c[2]["type"] for c in posts], ["STOP_MARKET"])
        self.assertEqual(posts[0][2]["triggerPrice"], "100.23")
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" for c in calls[before:]))
        self.assertTrue(any(k == "no_tp_cancel" for k, _kw in logs))
        # 減倉後的數量調整：停損數量與持倉不符時仍會改數量，且不補止盈。
        calls.clear()
        logs.clear()
        state["meta"]["XUSDT"]["stop"] = "100.23"

        posted_qty = []

        def fake_req_qty(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/openAlgoOrders":
                return [{"symbol": "XUSDT", "orderType": "STOP_MARKET", "algoId": 9,
                         "quantity": "10", "closePosition": "false", "triggerPrice": "100.23"}] + list(posted_qty)
            if path == "/fapi/v1/openOrders":
                return []
            if method == "POST" and path == "/fapi/v1/algoOrder":
                posted_qty.append({"symbol": (params or {}).get("symbol"), "orderType": (params or {}).get("type"),
                                   "algoId": 21, "algoStatus": "NEW", "quantity": (params or {}).get("quantity"),
                                   "triggerPrice": (params or {}).get("triggerPrice"), "closePosition": "false"})
                return {"algoId": 21}
            if method == "DELETE":
                return {}
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return []
            raise AssertionError((method, path))

        with patch.object(lt, "req", fake_req_qty), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            lt.cmd_manage(type("A", (), {"live": True})())
        posts = [c for c in calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["type"], "STOP_MARKET")
        self.assertEqual(posts[0][2]["quantity"], "4")
        self.assertEqual(posts[0][2]["triggerPrice"], "100.23")
        self.assertFalse(any(c[2].get("type") == "TAKE_PROFIT_MARKET" for c in posts))

    def test_second_mark_reduced_refuses(self):
        state = {"pending": {}, "meta": {"XUSDT": {"setup": "布鹿流", "stop": "100.23", "reduced": True, "no_tp": True}}}
        saved = []
        calls = []

        def fake_req(*_a, **_k):
            calls.append(1)
            raise AssertionError("不該再打 API")

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log"), \
                patch.object(lt, "load_state", return_value=json.loads(json.dumps(state))), \
                patch.object(lt, "save_state", side_effect=lambda s: saved.append(s)), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with self.assertRaises(SystemExit) as caught:
                lt.cmd_mark_reduced(type("A", (), {"live": True, "symbol": "xusdt"})())
        self.assertEqual(str(caught.exception), "已減過倉，不重複標記")
        self.assertEqual(saved, [])
        self.assertEqual(calls, [])
        self.assertEqual(state["meta"]["XUSDT"]["stop"], "100.23")

    def test_position_closed_classifies_stop_tp_and_unknown(self):
        def run(trades, income=None, user_fail=False):
            state = {"pending": {}, "meta": {"XUSDT": {"setup": "瘸師弟", "stop": "100", "target": "120"}}}
            logs = []

            def fake_req(method, path, params=None, signed=False, retries=5):
                if path == "/fapi/v1/userTrades":
                    if user_fail:
                        raise lt.BinanceError(500, -1000, "down")
                    return trades
                if path == "/fapi/v1/income":
                    return income or []
                if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                    return []
                raise AssertionError((method, path, params))

            with patch.object(lt, "req", fake_req), \
                    patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                    patch.object(lt, "account", return_value=ACC), \
                    patch.object(lt, "day_state", return_value=dict(DAY)), \
                    patch.object(lt, "load_state", return_value=state), \
                    patch.object(lt, "save_state"), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    lt.cmd_manage(type("A", (), {"live": False})())
            closed = [kw for kind, kw in logs if kind == "position_closed"]
            return closed[0], buf.getvalue()

        sl, text = run([
            {"side": "SELL", "price": "100.1", "qty": "2", "realizedPnl": "-1", "time": 2},
            {"side": "BUY", "price": "110", "qty": "2", "realizedPnl": "0", "time": 1},
        ], [{"income": "-1"}])
        self.assertEqual(sl["symbol"], "XUSDT")
        self.assertEqual(sl["reason"], "stop")
        self.assertEqual(sl["exit_side"], "SELL")
        self.assertEqual(sl["qty"], "2")
        self.assertEqual(sl["exit_price"], "100.1")
        self.assertEqual(sl["pnl"], "-1.0000")
        self.assertIn("出場價 100.1 觸發=停損 已實現 -1.0000", text)

        tp, text = run([
            {"side": "SELL", "price": "119.8", "qty": "1", "realizedPnl": "3", "time": 3},
            {"side": "SELL", "price": "120.2", "qty": "1", "realizedPnl": "3", "time": 4},
        ])
        self.assertEqual(tp["reason"], "tp")
        self.assertEqual(tp["exit_price"], "120")
        self.assertEqual(tp["qty"], "2")
        self.assertIn("觸發=止盈", text)

        bad, text = run([], user_fail=True)
        self.assertEqual(bad["symbol"], "XUSDT")
        self.assertIsNone(bad["exit_price"])
        self.assertEqual(bad["reason"], "unknown")
        self.assertIn("出場價 null 觸發=未知", text)

    def test_shell_tp_slippage_over_0_3_pct_is_tp(self):
        # 實盤：SHELLUSDT 出場 0.03908、目標 0.03869，約 1%。沒有訂單類型也要靠放寬的價差帶。
        state = {"pending": {}, "meta": {"SHELLUSDT": {"setup": "布鹿流", "stop": "0.03000", "target": "0.03869"}}}
        logs = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/userTrades":
                return [{"side": "SELL", "price": "0.03908", "qty": "100", "realizedPnl": "1", "time": 5}]
            if path == "/fapi/v1/income":
                return [{"income": "1"}]
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            raise AssertionError((method, path))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state"):
            lt.cmd_manage(type("A", (), {"live": False})())
        closed = [kw for kind, kw in logs if kind == "position_closed"]
        self.assertEqual(closed[0]["reason"], "tp")
        self.assertEqual(closed[0]["symbol"], "SHELLUSDT")

    def test_market_trigger_uses_close_position_algo(self):
        # 觸發後的實單是 MARKET，價又離目標很遠。要對到條件單歷史才知道是止盈。
        state = {"pending": {}, "meta": {"SHELLUSDT": {"setup": "布鹿流", "stop": "0.03000", "target": "0.03869"}}}
        logs = []
        calls = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/userTrades":
                return [{"side": "SELL", "price": "0.05000", "qty": "10", "realizedPnl": "1",
                         "time": 9, "orderId": 1041970304}]
            if path == "/fapi/v1/order":
                return {"orderId": 1041970304, "type": "MARKET", "origType": "MARKET", "symbol": "SHELLUSDT"}
            if path == "/fapi/v1/allAlgoOrders":
                self.assertEqual(method, "GET")
                return [{
                    "algoId": 3000002369870752,
                    "actualOrderId": "1041970304",
                    "orderType": "TAKE_PROFIT_MARKET",
                    "closePosition": True,
                    "symbol": "SHELLUSDT",
                }]
            if path == "/fapi/v1/income":
                return []
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            raise AssertionError((method, path))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state"):
            lt.cmd_manage(type("A", (), {"live": False})())
        closed = [kw for kind, kw in logs if kind == "position_closed"]
        self.assertEqual(closed[0]["reason"], "tp")
        self.assertTrue(any(m == "GET" and p == "/fapi/v1/allAlgoOrders" for m, p, _q in calls))
        self.assertFalse(any(m != "GET" for m, _p, _q in calls))

    def test_plain_market_far_from_stop_and_target_stays_other(self):
        state = {"pending": {}, "meta": {"XUSDT": {"setup": "瘸師弟", "stop": "100", "target": "120"}}}
        logs = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/userTrades":
                return [{"side": "SELL", "price": "150", "qty": "1", "realizedPnl": "1", "time": 3, "orderId": 7}]
            if path == "/fapi/v1/order":
                return {"orderId": 7, "type": "MARKET", "origType": "MARKET"}
            if path == "/fapi/v1/allAlgoOrders":
                return []
            if path == "/fapi/v1/income":
                return []
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            raise AssertionError((method, path))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state"):
            lt.cmd_manage(type("A", (), {"live": False})())
        closed = [kw for kind, kw in logs if kind == "position_closed"]
        self.assertEqual(closed[0]["reason"], "other")

    def test_exit_price_quantizes_to_tick_and_pnl_to_4dp(self):
        state = {"pending": {}, "meta": {"XUSDT": {"setup": "瘸師弟", "stop": "0.01", "target": "0.03"}}}
        logs = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/userTrades":
                return [
                    {"side": "SELL", "price": "0.02083", "qty": "2", "realizedPnl": "1", "time": 2},
                    {"side": "SELL", "price": "0.02085", "qty": "1", "realizedPnl": "1", "time": 3},
                ]
            if path == "/fapi/v1/income":
                return [{"income": "-9.354600999999999"}]
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            raise AssertionError((method, path))

        def fake_info(_symbol):
            return {"tick": Decimal("0.00001")}

        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "sym_info", fake_info), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state"):
            with redirect_stdout(buf):
                lt.cmd_manage(type("A", (), {"live": False})())
        closed = [kw for kind, kw in logs if kind == "position_closed"][0]
        text = buf.getvalue()
        self.assertEqual(closed["exit_price"], "0.02084")
        self.assertIn("exit_price_raw", closed)
        self.assertNotIn("0.020837", text)
        self.assertEqual(closed["pnl"], "-9.3546")
        self.assertIn("pnl_raw", closed)
        self.assertIn("出場價 0.02084", text)
        self.assertIn("已實現 -9.3546", text)
        self.assertNotIn("-9.354600999999999", text)

    def test_manual_flatten_writes_position_closed_and_drops_meta(self):
        state = {"pending": {}, "meta": {"XUSDT": {"setup": "布鹿流", "stop": "1", "target": "2"}}}
        saved = []
        logs = []
        acc = dict(ACC)
        acc["positions"] = [{"symbol": "XUSDT", "positionAmt": "3", "positionSide": "BOTH"}]

        def fake_req(method, path, params=None, signed=False, retries=5):
            if method == "POST" and path == "/fapi/v1/order":
                return {"orderId": 1}
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
                return []
            raise AssertionError((method, path))

        def load():
            return json.loads(json.dumps(state))

        def save(s):
            state.clear()
            state.update(json.loads(json.dumps(s)))
            saved.append(json.loads(json.dumps(s)))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save):
            buf = io.StringIO()
            with redirect_stdout(buf):
                lt.flatten_all(True, "manual_flatten")
        closed = [kw for kind, kw in logs if kind == "position_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["reason"], "manual")
        self.assertEqual(closed[0]["symbol"], "XUSDT")
        self.assertEqual(closed[0]["qty"], "3")
        self.assertNotIn("XUSDT", state.get("meta") or {})
        self.assertTrue(saved)
        self.assertIn("觸發=手動", buf.getvalue())
        self.assertTrue(any(kind == "market_close" and kw.get("reason") == "manual_flatten" for kind, kw in logs))

    def test_ladder_finish_keeps_reduced_flags(self):
        state = {
            "pending": {"XUSDT": {
                "ladder": True, "stop": "90", "target": "120", "setup": "布鹿流",
                "orders": [{"orderId": 1}],
            }},
            "meta": {"XUSDT": {"setup": "布鹿流", "stop": "100.23", "target": "120", "reduced": True, "no_tp": True}},
        }
        acc = dict(ACC)
        acc["positions"] = [{"symbol": "XUSDT", "positionAmt": "4", "positionSide": "BOTH"}]

        def fake_req(method, path, params=None, signed=False, retries=5):
            if method == "GET" and path == "/fapi/v1/order":
                return {"orderId": 1, "status": "FILLED", "executedQty": "4", "origQty": "4"}
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return [
                    {"symbol": "XUSDT", "orderType": "STOP_MARKET", "algoId": 9, "quantity": "4",
                     "closePosition": "false", "triggerPrice": "100.23"},
                    {"symbol": "XUSDT", "orderType": "TAKE_PROFIT_MARKET", "algoId": 8, "quantity": "4",
                     "closePosition": "false", "triggerPrice": "120"},
                ] if "Algo" in path else []
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return []
            raise AssertionError((method, path))

        def load():
            return json.loads(json.dumps(state))

        def save(s):
            state.clear()
            state.update(json.loads(json.dumps(s)))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log"), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save):
            lt.cmd_manage(type("A", (), {"live": True})())
        meta = state["meta"]["XUSDT"]
        self.assertNotIn("XUSDT", state["pending"])
        self.assertTrue(meta["reduced"])
        self.assertTrue(meta["no_tp"])
        self.assertEqual(meta["stop"], "100.23")
        self.assertEqual(meta["target"], "120")


class LossPercentFormatTest(unittest.TestCase):
    def test_profit_and_flat_day_do_not_print_negative_zero(self):
        def status_line(wallet):
            acc = dict(ACC)
            acc["totalWalletBalance"] = wallet
            buf = io.StringIO()
            with redirect_stdout(buf):
                lt.print_status_body(acc, dict(DAY), [], [], [],
                                     {"dualSidePosition": False}, {"multiAssetsMargin": False})
            return buf.getvalue()

        profit = status_line("1015.35")
        self.assertIn("當日損益 +15.35 (0.00% 虧損側)", profit)
        self.assertNotIn("-0.00%", profit)
        flat = status_line("1000")
        self.assertIn("當日損益 +0.00 (0.00% 虧損側)", flat)
        self.assertNotIn("-0.00%", flat)
        down = status_line("990")
        self.assertIn("當日損益 -10.00 (-1.00% 虧損側)", down)

        acc = dict(ACC)
        acc["totalWalletBalance"] = "1015.35"

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
                return []
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return []
            raise AssertionError((method, path))

        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log"), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value={"pending": {}, "meta": {}}), \
                patch.object(lt, "save_state"):
            with redirect_stdout(buf):
                lt.cmd_manage(type("A", (), {"live": False, "with_status": False})())
        text = buf.getvalue()
        self.assertIn("當日虧損 0.00%  持倉 0", text)
        self.assertNotIn("-0.00%", text)


class SingleOrderUnchangedTest(unittest.TestCase):
    def test_single_order_manage_matches_previous_file(self):
        import importlib.util
        import subprocess
        src = subprocess.check_output(
            ["git", "show", "38db65bc52225ba2ee452e23903f928d0285f62c:src/live_trader.py"],
            cwd=os.path.dirname(_SRC),
        )
        path = os.path.join(_DATA, "live_trader_prev.py")
        with open(path, "wb") as handle:
            handle.write(src)
        spec = importlib.util.spec_from_file_location("live_trader_prev", path)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        acc = dict(ACC)
        acc["positions"] = [{"symbol": "XUSDT", "positionAmt": "5"}]
        state = {"pending": {}, "meta": {"XUSDT": {"setup": "瘸師弟", "stop": "90", "target": "120"}}}

        def run(mod):
            calls, logs = [], []

            posted = []

            def fake_req(method, path, params=None, signed=False, retries=5):
                calls.append((method, path, dict(params or {})))
                if path == "/fapi/v1/openAlgoOrders":
                    return list(posted)
                if path == "/fapi/v1/openOrders":
                    return []
                if method == "POST" and path == "/fapi/v1/algoOrder":
                    algo_id = len(calls)
                    posted.append({"symbol": (params or {}).get("symbol"), "orderType": (params or {}).get("type"),
                                   "algoId": algo_id, "algoStatus": "NEW"})
                    return {"algoId": algo_id}
                if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                    return []
                raise AssertionError((method, path))

            buf = io.StringIO()
            with patch.object(mod, "req", fake_req), \
                    patch.object(mod, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                    patch.object(mod, "account", return_value=json.loads(json.dumps(acc))), \
                    patch.object(mod, "day_state", return_value=dict(DAY)), \
                    patch.object(mod, "load_state", return_value=json.loads(json.dumps(state))), \
                    patch.object(mod, "save_state"), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                with redirect_stdout(buf):
                    mod.cmd_manage(type("A", (), {"live": True})())
            return calls, buf.getvalue(), logs

        def posts(calls):
            return [(m, p, params) for m, p, params in calls if m == "POST"]

        def slim(logs):
            out = []
            for kind, kw in logs:
                kw = dict(kw)
                kw.pop("resp", None)
                out.append((kind, kw))
            return out

        old_calls, old_out, old_logs = run(old)
        new_calls, new_out, new_logs = run(lt)
        # O5 多了依幣種與定期全量的讀取；下單內容（方向、觸發價、數量、closePosition）不變。
        # 停損回查多一次讀取、一行「已掛上」和 stop_readback，不另下一張單。
        self.assertEqual(posts(new_calls), posts(old_calls))
        self.assertEqual(new_out.replace("停損已掛上\n", ""), old_out)
        self.assertTrue(any(kind == "stop_readback" for kind, _kw in slim(new_logs)))
        self.assertEqual([item for item in slim(new_logs) if item[0] != "stop_readback"], slim(old_logs))


if __name__ == "__main__":
    unittest.main()
