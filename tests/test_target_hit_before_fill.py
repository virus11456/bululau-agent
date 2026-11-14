"""未成交但標記價已到止盈就整筆撤。不連網、不讀金鑰。"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="target-hit-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402

LEVELS = ("1.7276", "1.7098", "1.692")
TARGET = "1.8003"


class TargetHitTest(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.calls = []
        self.store = {"pending": {"PIEVERSEUSDT": {
            "ladder": True, "side": "BUY", "target": TARGET, "stop": "1.6000", "setup": "瘸師弟",
            "orders": [{"orderId": 1}, {"orderId": 2}, {"orderId": 3}],
        }}}
        self.orders = {
            1: {"orderId": 1, "status": "NEW", "executedQty": "0", "price": LEVELS[0]},
            2: {"orderId": 2, "status": "NEW", "executedQty": "0", "price": LEVELS[1]},
            3: {"orderId": 3, "status": "NEW", "executedQty": "0", "price": LEVELS[2]},
        }
        self.mark = "1.823"
        self.amt = "0"
        self.fail_order = None
        self.fail_algo = False

    def fake_req(self, method, path, params=None, signed=False, retries=5):
        params = dict(params or {})
        self.calls.append((method, path, params))
        if method == "GET" and path == "/fapi/v1/order":
            if self.fail_order is not None and params.get("orderId") == self.fail_order:
                raise lt.BinanceError(500, -1000, "order down")
            return dict(self.orders[params["orderId"]])
        if path == "/fapi/v2/positionRisk":
            self.risk_retries.append(retries)
            if getattr(self, "fill_after_cancel", False) and any(c[0] == "DELETE" for c in self.calls):
                amt = "2"
            else:
                amt = self.amt
            return [{"symbol": "PIEVERSEUSDT", "positionAmt": amt, "markPrice": self.mark}]
        if path == "/fapi/v1/openAlgoOrders":
            if self.fail_algo:
                raise lt.BinanceError(500, -1000, "algo down")
            return [
                {"symbol": "PIEVERSEUSDT", "orderType": "STOP_MARKET", "algoId": 11,
                 "quantity": "10", "closePosition": "false", "triggerPrice": "1.6000"},
                {"symbol": "PIEVERSEUSDT", "orderType": "TAKE_PROFIT_MARKET", "algoId": 12,
                 "quantity": "10", "closePosition": "true", "triggerPrice": TARGET},
            ]
        if path == "/fapi/v1/openOrders":
            return []
        if method == "DELETE":
            if getattr(self, "cancel_missing", False) and path == "/fapi/v1/order":
                raise lt.BinanceError(400, -2011, "unknown order")
            return {}
        raise AssertionError((method, path, params))

    def watch(self, live, fresh=False):
        def load():
            return json.loads(json.dumps(self.store))

        def save(state):
            self.store.clear()
            self.store.update(state)
            self.saved.append(json.loads(json.dumps(state)))

        self.managed = []
        self.saved = []
        self.risk_retries = []
        buf = io.StringIO()
        with patch.object(lt, "req", self.fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch.object(lt, "_user_stream_fresh", return_value=fresh), \
                patch.object(lt, "cmd_manage", side_effect=lambda a: self.managed.append(1)):
            with redirect_stdout(buf):
                lt.cmd_watch(type("A", (), {"live": live})())
        return buf.getvalue()

    def test_pieverse_long_unfilled_at_1_823_cancels_everything(self):
        text = self.watch(True)
        deleted_orders = sorted(c[2].get("orderId") for c in self.calls
                                if c[0] == "DELETE" and c[1] == "/fapi/v1/order")
        deleted_algos = sorted(c[2].get("algoId") for c in self.calls
                               if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder")
        self.assertEqual(deleted_orders, [1, 2, 3])
        self.assertEqual(deleted_algos, [11, 12])
        self.assertNotIn("PIEVERSEUSDT", self.store.get("pending") or {})
        self.assertTrue(any(kind == "target_hit_before_fill" and kw.get("event") == "target_hit_before_fill"
                            and kw.get("mark") == "1.823" and kw.get("target") == TARGET
                            for kind, kw in self.logs))
        self.assertIn("未成交但標記價 1.823 已到止盈 1.8003", text)
        self.assertEqual(self.risk_retries, [2, 2])
        self.assertEqual(self.managed, [])

    def test_pieverse_dry_run_does_not_write_state(self):
        text = self.watch(False)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertIn("PIEVERSEUSDT", self.store["pending"])
        self.assertEqual(self.saved, [])
        self.assertIn("[dry-run] 取消進場單", text)
        self.assertIn("[dry-run] 取消保護單", text)
        self.assertTrue(any(kind == "target_hit_before_fill" for kind, _kw in self.logs))

    def test_fresh_user_stream_still_cancels_unfilled_target(self):
        text = self.watch(True, fresh=True)
        self.assertIn("未成交但標記價 1.823 已到止盈 1.8003", text)
        self.assertNotIn("PIEVERSEUSDT", self.store.get("pending") or {})
        self.assertEqual(self.managed, [])

    def test_fill_during_cancel_keeps_stop_and_calls_manage(self):
        self.cancel_missing = True
        self.fill_after_cancel = True
        self.watch(True)
        self.assertFalse(any(c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" for c in self.calls))
        self.assertIn("PIEVERSEUSDT", self.store["pending"])
        self.assertEqual(self.managed, [1])
        self.assertFalse(any(kind == "target_hit_before_fill" for kind, _kw in self.logs))
        self.assertTrue(any("保留停損" in str(kw.get("msg")) for _k, kw in self.logs))

    def test_already_cancelled_orders_are_not_target_hits(self):
        for row in self.orders.values():
            row["status"] = "CANCELED"
        self.watch(True)
        self.assertFalse(any(c[1] == "/fapi/v2/positionRisk" for c in self.calls))
        self.assertFalse(any(kind == "target_hit_before_fill" for kind, _kw in self.logs))
        self.assertIn("PIEVERSEUSDT", self.store["pending"])

    def test_partial_fill_is_not_cancelled(self):
        self.orders[1] = {"orderId": 1, "status": "FILLED", "executedQty": "2", "price": LEVELS[0]}
        self.watch(True)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertFalse(any(c[1] == "/fapi/v2/positionRisk" for c in self.calls))
        self.assertIn("PIEVERSEUSDT", self.store["pending"])

    def test_order_read_failure_cancels_nothing(self):
        self.fail_order = 2
        self.watch(True)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertIn("PIEVERSEUSDT", self.store["pending"])

    def test_protect_read_failure_cancels_nothing(self):
        self.fail_algo = True
        self.watch(True)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertIn("PIEVERSEUSDT", self.store["pending"])

    def test_short_cancels_only_when_mark_is_at_or_below_target(self):
        self.store["pending"]["PIEVERSEUSDT"]["side"] = "SELL"
        self.mark = "1.9"
        self.watch(True)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertIn("PIEVERSEUSDT", self.store["pending"])
        self.calls.clear()
        self.mark = "1.7"
        self.watch(True)
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/order" for c in self.calls))
        self.assertNotIn("PIEVERSEUSDT", self.store.get("pending") or {})


class TargetPassedAtFillTest(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.calls = []
        self.saved = []
        self.state = {"pending": {}, "meta": {"XUSDT": {
            "setup": "瘸師弟", "stop": "90", "target": "120", "side": "BUY",
        }}}
        self.amt = "1"
        self.mark = "100"
        self.tp_error = None
        self.close_error = None
        self.entry = None

    def manage(self, live):
        def load():
            return json.loads(json.dumps(self.state))

        def save(state):
            self.state.clear()
            self.state.update(json.loads(json.dumps(state)))
            self.saved.append(json.loads(json.dumps(state)))

        def fake_req(method, path, params=None, signed=False, retries=5):
            self.calls.append((method, path, dict(params or {})))
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "XUSDT", "positionAmt": self.amt, "markPrice": self.mark}]
            if path == "/fapi/v1/openAlgoOrders":
                return [{"symbol": "XUSDT", "orderType": "STOP_MARKET", "algoId": 9,
                         "quantity": self.amt.lstrip("-"), "closePosition": "false", "triggerPrice": "90"}]
            if path == "/fapi/v1/openOrders":
                return [self.entry] if self.entry else []
            if method == "GET" and path == "/fapi/v1/order":
                return {"orderId": (params or {}).get("orderId"), "status": "PARTIALLY_FILLED",
                        "executedQty": "1", "origQty": "3"}
            if path in ("/fapi/v1/userTrades", "/fapi/v1/income"):
                return []
            if method == "POST" and path == "/fapi/v1/algoOrder":
                if self.tp_error:
                    raise self.tp_error
                return {"algoId": 21}
            if method == "POST" and path == "/fapi/v1/order":
                if self.close_error:
                    raise self.close_error
                return {"orderId": 50}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path, params))

        acc = {
            "totalWalletBalance": "1000", "totalUnrealizedProfit": "0", "availableBalance": "1000",
            "positions": [{"symbol": "XUSDT", "positionAmt": self.amt, "positionSide": "BOTH"}],
        }
        day = {"day": "2026-11-14", "day_start_equity": 1000.0, "slice_margin": 50.0,
               "halted_new": False, "killed": False}
        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "day_state", return_value=day), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with redirect_stdout(buf):
                lt.cmd_manage(type("A", (), {"live": live})())
        return buf.getvalue()

    def _market_orders(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]

    def test_minus_2021_closes_and_records(self):
        self.tp_error = lt.BinanceError(400, -2021, "would immediately trigger")
        text = self.manage(True)
        markets = self._market_orders()
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0][2]["type"], "MARKET")
        self.assertEqual(markets[0][2]["reduceOnly"], "true")
        self.assertEqual(markets[0][2]["side"], "SELL")
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" for c in self.calls))
        self.assertNotIn("XUSDT", self.state.get("meta") or {})
        closed = [kw for kind, kw in self.logs if kind == "position_closed"]
        self.assertEqual(closed[0]["reason"], "target_passed_at_fill")
        self.assertTrue(any(kind == "target_passed_at_fill" for kind, _kw in self.logs))
        self.assertIn("觸發=價已過止盈", text)
        algo_posts = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        self.assertEqual(len(algo_posts), 1)

    def test_mark_past_target_closes_long_without_placing_tp(self):
        self.mark = "120"
        self.manage(True)
        self.assertFalse(any(c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" for c in self.calls))
        markets = self._market_orders()
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0][2]["side"], "SELL")
        self.assertEqual(markets[0][2]["reduceOnly"], "true")
        self.assertNotIn("XUSDT", self.state.get("meta") or {})

    def test_mark_past_target_closes_short(self):
        self.amt = "-2"
        self.mark = "120"
        self.state["meta"]["XUSDT"]["side"] = "SELL"
        self.manage(True)
        markets = self._market_orders()
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0][2]["side"], "BUY")
        self.assertEqual(markets[0][2]["reduceOnly"], "true")
        self.assertNotIn("XUSDT", self.state.get("meta") or {})

    def test_market_close_failure_keeps_stop(self):
        self.mark = "130"
        self.close_error = lt.BinanceError(400, -2010, "close rejected")
        self.manage(True)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertIn("XUSDT", self.state["meta"])
        self.assertFalse(any(kind == "position_closed" for kind, _kw in self.logs))
        self.assertTrue(any("保留停損" in str(kw.get("msg")) for _k, kw in self.logs))

    def test_dry_run_prints_and_does_not_order(self):
        self.mark = "130"
        text = self.manage(False)
        self.assertIn("會市價平倉", text)
        self.assertFalse(any(c[0] == "POST" for c in self.calls))
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertEqual(self.saved, [])
        self.assertIn("XUSDT", self.state["meta"])

    def test_partial_fill_closes_position_and_cancels_resting_entry(self):
        self.mark = "121"
        self.state = {"meta": {}, "pending": {"XUSDT": {
            "orderId": 77, "side": "BUY", "target": "120", "stop": "90", "setup": "小師弟",
        }}}
        self.entry = {"symbol": "XUSDT", "orderId": 77, "type": "LIMIT", "status": "PARTIALLY_FILLED"}
        self.manage(True)
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/order" and c[2].get("orderId") == 77
                            for c in self.calls))
        self.assertEqual(len(self._market_orders()), 1)
        self.assertNotIn("XUSDT", self.state.get("pending") or {})


if __name__ == "__main__":
    unittest.main()
