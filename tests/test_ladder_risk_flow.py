"""R2 部分成交停損維持計畫總量，O12 逾時撤未成交檔，O8 資料流備援。不連網。"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="ladder-risk-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402
import user_stream  # noqa: E402

ACC = {
    "totalWalletBalance": "1000", "totalUnrealizedProfit": "0",
    "availableBalance": "1000", "positions": [],
}
DAY = {"day": "2026-11-14", "day_start_equity": 1000.0, "slice_margin": 50.0,
       "halted_new": False, "killed": False}


def _algo(kind, algo_id, qty, trig):
    return {"symbol": "XUSDT", "orderType": kind, "algoId": algo_id,
            "quantity": qty, "closePosition": "false", "triggerPrice": trig}


class PartialStopTest(unittest.TestCase):
    def test_partial_fill_keeps_full_stop_qty_and_reduce_only(self):
        calls = []
        orders = {
            1: {"orderId": 1, "status": "FILLED", "executedQty": "2", "origQty": "2", "time": 1},
            2: {"orderId": 2, "status": "NEW", "executedQty": "0", "origQty": "8", "time": 1},
        }
        protects = [
            _algo("STOP_MARKET", 9, "2", "90"),
            _algo("TAKE_PROFIT_MARKET", 8, "2", "120"),
        ]

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if method == "GET" and path == "/fapi/v1/order":
                return dict(orders[params["orderId"]])
            if path == "/fapi/v1/openAlgoOrders":
                return list(protects)
            if path == "/fapi/v1/openOrders":
                return []
            if method == "POST" and path == "/fapi/v1/algoOrder":
                return {"algoId": 30}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path, params))

        pd = {"ladder": True, "qty": "10", "stop": "90", "target": "120", "setup": "瘸師弟",
              "placed_at": datetime.now(lt.TPE).isoformat(timespec="seconds"),
              "orders": [{"orderId": 1}, {"orderId": 2}]}
        with patch.object(lt, "req", fake_req), patch.object(lt, "log"), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            done = lt.manage_ladder_pending("XUSDT", pd, {"XUSDT": 2.0}, True)
        self.assertFalse(done)
        posts = [c for c in calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        stops = [c for c in posts if c[2]["type"] == "STOP_MARKET"]
        tps = [c for c in posts if c[2]["type"] == "TAKE_PROFIT_MARKET"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][2]["quantity"], "10")
        self.assertEqual(stops[0][2]["reduceOnly"], "true")
        self.assertEqual(stops[0][2]["side"], "SELL")
        self.assertNotIn("closePosition", stops[0][2])
        self.assertEqual(tps, [])
        buys = [c for c in posts if c[2].get("side") == "BUY"]
        self.assertEqual(buys, [])


class LadderExpireTest(unittest.TestCase):
    def _orders(self, specs):
        return {oid: dict(row, orderId=oid) for oid, row in specs.items()}

    def drive(self, orders, amt, placed_hours, protects):
        calls, logs = [], []

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if method == "GET" and path == "/fapi/v1/order":
                return dict(orders[params["orderId"]])
            if path == "/fapi/v1/openAlgoOrders":
                return list(protects)
            if path == "/fapi/v1/openOrders":
                return []
            if method == "DELETE":
                return {}
            if method == "POST":
                raise AssertionError("不該送出新單或平倉")
            raise AssertionError((method, path))

        placed = (datetime.now(lt.TPE) - timedelta(hours=placed_hours)).isoformat(timespec="seconds")
        pd = {"ladder": True, "qty": "10", "stop": "90", "target": "120",
              "placed_at": placed, "orders": [{"orderId": i} for i in orders]}
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))):
            done = lt.manage_ladder_pending("XUSDT", pd, {"XUSDT": amt} if amt else {}, True)
        return done, calls, logs

    def test_zero_fills_cancels_entries_and_stop(self):
        orders = self._orders({
            1: {"status": "NEW", "executedQty": "0", "origQty": "10"},
            2: {"status": "NEW", "executedQty": "0", "origQty": "10"},
        })
        done, calls, logs = self.drive(orders, 0, 3, [_algo("STOP_MARKET", 9, "10", "90")])
        self.assertEqual(done, "drop")
        deleted = [c[2].get("orderId") for c in calls if c[0] == "DELETE" and c[1] == "/fapi/v1/order"]
        self.assertEqual(sorted(deleted), [1, 2])
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" for c in calls))
        self.assertTrue(any(k == "ladder_expire" and kw.get("cancel") == "stop" for k, kw in logs))
        self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_partial_fill_cancels_only_open_entries(self):
        orders = self._orders({
            1: {"status": "FILLED", "executedQty": "4", "origQty": "4"},
            2: {"status": "NEW", "executedQty": "0", "origQty": "6"},
        })
        protects = [_algo("STOP_MARKET", 9, "10", "90"), _algo("TAKE_PROFIT_MARKET", 8, "4", "120")]
        done, calls, logs = self.drive(orders, 4, 3, protects)
        self.assertFalse(done)
        deleted = [c[2].get("orderId") for c in calls if c[0] == "DELETE" and c[1] == "/fapi/v1/order"]
        self.assertEqual(deleted, [2])
        self.assertFalse(any(c[1] == "/fapi/v1/algoOrder" and c[0] == "DELETE" for c in calls))
        self.assertTrue(any(k == "ladder_expire" for k, _kw in logs))

    def test_full_fill_before_two_hours_is_kept(self):
        orders = self._orders({
            1: {"status": "FILLED", "executedQty": "10", "origQty": "10"},
        })
        done, calls, logs = self.drive(orders, 10, 0.5, [_algo("STOP_MARKET", 9, "10", "90")])
        self.assertEqual(done, "meta")
        self.assertFalse(any(k == "ladder_expire" for k, _kw in logs))
        self.assertFalse(any(c[0] == "DELETE" and c[1] == "/fapi/v1/order" for c in calls))

    def test_single_order_older_than_two_hours_is_not_cancelled(self):
        calls = []
        old_ms = int((time.time() - 5 * 3600) * 1000)

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if method == "GET" and path == "/fapi/v1/order":
                return {"orderId": 5, "status": "NEW", "executedQty": "0", "origQty": "1", "time": old_ms}
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            if method == "DELETE":
                raise AssertionError("單張不該被逾時撤掉")
            raise AssertionError((method, path))

        state = {"pending": {"XUSDT": {"orderId": 5, "stop": "1", "target": "2", "setup": "瘸師弟"}}, "meta": {}}
        with patch.object(lt, "req", fake_req), patch.object(lt, "log"), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state"):
            lt.cmd_manage(type("A", (), {"live": True})())
        self.assertTrue(any(c[1] == "/fapi/v1/order" and c[0] == "GET" for c in calls))
        self.assertFalse(any(c[0] == "DELETE" for c in calls))


class UserStreamTest(unittest.TestCase):
    def test_fill_event_triggers_manage_and_reconnects(self):
        managed = []
        connects = {"n": 0}

        class Sock(object):
            def __init__(self, messages, fail=False):
                self._messages = messages
                self.fail = fail

            def messages(self):
                if self.fail:
                    raise ConnectionError("drop")
                for msg in self._messages:
                    yield msg

        def connect():
            connects["n"] += 1
            if connects["n"] == 1:
                return Sock([], fail=True)
            return Sock([{"e": "ORDER_TRADE_UPDATE", "o": {"s": "XUSDT", "x": "TRADE", "X": "FILLED"}}])

        stopped = {"n": 0}

        def stop():
            return stopped["n"] >= 1

        def on_manage(msg):
            managed.append(msg)
            stopped["n"] += 1

        user_stream.run_loop(connect, stop, on_manage, lambda _s: None, lambda: None, lambda: None)
        self.assertEqual(connects["n"], 2)
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["o"]["X"], "FILLED")

    def test_polling_fallback_when_heartbeat_stale(self):
        path = user_stream.heartbeat_path()
        calls = []

        def fake_req(*_a, **_k):
            calls.append(1)
            raise AssertionError("心跳還新時不該輪詢")

        with open(path, "w") as handle:
            handle.write(str(time.time()))
        with patch.object(lt, "req", fake_req):
            lt.cmd_watch(type("A", (), {"live": True})())
        self.assertEqual(calls, [])

        with open(path, "w") as handle:
            handle.write(str(time.time() - 1000))
        state = {"pending": {}}

        def load():
            return json.loads(json.dumps(state))

        def save(s):
            state.clear()
            state.update(s)

        def fake_req_poll(*_a, **_k):
            calls.append("polled")
            raise AssertionError("空 pending 的備援不該打 API")

        calls.clear()
        with patch.object(lt, "req", fake_req_poll), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch.object(lt, "now_tpe", return_value=datetime(2026, 11, 14, 1, 2, tzinfo=lt.TPE)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                lt.cmd_watch(type("A", (), {"live": True})())
        self.assertIn("[watch] 無待成交限價單 01:02", buf.getvalue())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
