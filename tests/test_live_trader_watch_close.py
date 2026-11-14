"""平倉紀錄、watch 心跳、階梯不再因沒預掛止盈每分鐘跑 manage。不連網、不讀金鑰。"""
import io
import json
import os
import sys
import tempfile
import unittest
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
        self.assertIn("XUSDT 已平倉（交易所停損/止盈或手動），清除 meta", buf.getvalue())
        closed = [kw for kind, kw in logs if kind == "position_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["symbol"], "XUSDT")
        self.assertEqual(closed[0]["setup"], "瘸師弟")
        self.assertEqual(closed[0]["stop"], "1.1")
        self.assertEqual(closed[0]["target"], "2.2")
        self.assertIsNone(closed[0]["pnl"])

    def test_live_close_saves_and_sums_pnl(self):
        state = {"pending": {}, "meta": {"XUSDT": {"setup": "布鹿流", "stop": "9", "target": "12"}}}
        saved = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/income":
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
        self.assertEqual(closed[0]["pnl"], 1.25)

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

    def test_finished_levels_trigger_even_if_qty_unchanged(self):
        self.ladder(status="FILLED", executed="2", last_exec=5.0,
                    algos=[_algo("XUSDT", "STOP_MARKET", 9), _algo("XUSDT", "TAKE_PROFIT_MARKET", 8)])
        self.orders[2] = {"orderId": 2, "status": "FILLED", "executedQty": "3"}
        self.watch()
        self.assertEqual(self.managed, [1])

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


if __name__ == "__main__":
    unittest.main()
