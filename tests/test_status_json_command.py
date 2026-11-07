"""呼叫 repo 裡的 cmd_status_json。查詢用假物件，不讀金鑰、不打幣安。"""
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    os.environ["QUANTBOY_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("BINANCE_API_KEY", None)
    os.environ.pop("BINANCE_API_SECRET", None)
    os.environ.pop("BINANCE_ENV_FILE", None)
    sys.path.insert(0, str(ROOT / "src"))
    name = "live_trader"
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


class StatusJsonCommandTest(unittest.TestCase):
    def test_three_queries_emit_whitelist_without_balance(self):
        lt = _load()
        lt.account = lambda: {"positions": [{"positionAmt": "1.0"}], "totalWalletBalance": "999.99", "totalUnrealizedProfit": "1"}
        lt.open_orders = lambda symbol=None: [{"symbol": "ETHUSDT", "type": "LIMIT"}]
        lt.req = lambda method, path, params=None, signed=False: [{"symbol": "BTCUSDT"}] if path == "/fapi/v1/openAlgoOrders" else []
        buf = io.StringIO()
        with redirect_stdout(buf):
            lt.cmd_status_json(None)
        text = buf.getvalue()
        self.assertNotIn("999.99", text)
        self.assertNotIn("totalWalletBalance", text)
        body = json.loads(text)
        self.assertTrue(body["ok"])
        self.assertEqual(body["queries"], {"account": True, "open_orders": True, "open_algo_orders": True})
        self.assertEqual(body["positions"], 1)
        self.assertEqual(body["open_orders"], 1)
        self.assertEqual(body["algo_orders"], 1)
        self.assertFalse(body["limit_older_than_2h_implemented"])

    def test_algo_failure_exits_nonzero_without_zero_book(self):
        lt = _load()
        lt.account = lambda: {"positions": [], "totalWalletBalance": "999.99", "totalUnrealizedProfit": "0"}
        lt.open_orders = lambda symbol=None: []

        def fail(method, path, params=None, signed=False):
            raise lt.BinanceError(429, -1003, "HTTP 429 token=secret")

        lt.req = fail
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as caught:
                lt.cmd_status_json(None)
        self.assertEqual(caught.exception.code, 2)
        text = buf.getvalue()
        self.assertNotIn("999.99", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("positions", text)
        body = json.loads(text)
        self.assertFalse(body["ok"])
        self.assertTrue(body["queries"]["account"])
        self.assertTrue(body["queries"]["open_orders"])
        self.assertFalse(body["queries"]["open_algo_orders"])

    def test_null_and_similar_values_are_not_empty_books(self):
        lt = _load()
        bad_orders = (None, False, 0, "", {}, "[]", [None], ["x"])
        bad_accounts = (None, False, 0, "", [], {"positions": None}, {"positions": False}, {"positions": 0}, {"positions": ""}, {"positions": {}}, {"positions": [None]}, {"positions": [{"positionAmt": None}]}, {"positions": [{"positionAmt": False}]}, {"positions": [{"positionAmt": ""}]})
        for value in bad_orders:
            lt.account = lambda: {"positions": [], "totalWalletBalance": "999.99", "totalUnrealizedProfit": "0"}
            lt.open_orders = lambda symbol=None: []
            lt.req = lambda method, path, params=None, signed=False, found=value: found
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as caught:
                    lt.cmd_status_json(None)
            self.assertEqual(caught.exception.code, 2, value)
            text = buf.getvalue()
            self.assertNotIn("999.99", text)
            body = json.loads(text)
            self.assertFalse(body["ok"])
            self.assertNotIn("positions", body)
            self.assertNotIn("algo_orders", body)
        for value in bad_accounts:
            lt.account = lambda found=value: found
            lt.open_orders = lambda symbol=None: []
            lt.req = lambda method, path, params=None, signed=False: []
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as caught:
                    lt.cmd_status_json(None)
            self.assertEqual(caught.exception.code, 2, value)
            body = json.loads(buf.getvalue())
            self.assertFalse(body["ok"], value)
            self.assertFalse(body["queries"]["account"], value)
        lt.account = lambda: {"positions": [], "totalWalletBalance": "999.99", "totalUnrealizedProfit": "0"}
        for value in bad_orders:
            lt.open_orders = lambda symbol=None, found=value: found
            lt.req = lambda method, path, params=None, signed=False: []
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as caught:
                    lt.cmd_status_json(None)
            self.assertEqual(caught.exception.code, 2, value)
            body = json.loads(buf.getvalue())
            self.assertTrue(body["queries"]["account"])
            self.assertFalse(body["queries"]["open_orders"])
            self.assertNotIn("algo_orders", body)
        lt.open_orders = lambda symbol=None: []
        lt.req = lambda method, path, params=None, signed=False: []
        buf = io.StringIO()
        with redirect_stdout(buf):
            lt.cmd_status_json(None)
        body = json.loads(buf.getvalue())
        self.assertTrue(body["ok"])
        self.assertEqual(body["positions"], 0)
        self.assertEqual(body["open_orders"], 0)
        self.assertEqual(body["algo_orders"], 0)

    def test_check_root_is_not_completed_by_pointing_at_a_directory(self):
        from src.phase1.runner import inspect_quantboy_root
        report = inspect_quantboy_root(str(ROOT))
        self.assertTrue(report["prefilter_present"])
        self.assertTrue(report["root_supports_status_json"])
        self.assertFalse(report["deploy_complete"])
        self.assertFalse(report["account_verified"])
        self.assertFalse(report["spawned"])
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "src"))
            with open(os.path.join(tmp, "src", "prefilter.py"), "w", encoding="utf-8") as handle:
                handle.write("# old scanner\n")
            with open(os.path.join(tmp, "src", "live_trader.py"), "w", encoding="utf-8") as handle:
                handle.write("def cmd_status():\n    return None\n")
            old = inspect_quantboy_root(tmp)
            self.assertTrue(old["prefilter_present"])
            self.assertFalse(old["root_supports_status_json"])
            self.assertFalse(old["deploy_complete"])


if __name__ == "__main__":
    unittest.main()
