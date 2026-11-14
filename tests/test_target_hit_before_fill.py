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
            return [{"symbol": "PIEVERSEUSDT", "positionAmt": self.amt, "markPrice": self.mark}]
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
            return {}
        raise AssertionError((method, path, params))

    def watch(self, live):
        def load():
            return json.loads(json.dumps(self.store))

        def save(state):
            self.store.clear()
            self.store.update(state)

        self.managed = []
        buf = io.StringIO()
        with patch.object(lt, "req", self.fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch.object(lt, "_user_stream_fresh", return_value=False), \
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
        self.assertEqual(self.managed, [])

    def test_pieverse_dry_run_cancels_without_delete(self):
        text = self.watch(False)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertNotIn("PIEVERSEUSDT", self.store.get("pending") or {})
        self.assertIn("[dry-run] 取消進場單", text)
        self.assertIn("[dry-run] 取消保護單", text)
        self.assertTrue(any(kind == "target_hit_before_fill" for kind, _kw in self.logs))

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


if __name__ == "__main__":
    unittest.main()
