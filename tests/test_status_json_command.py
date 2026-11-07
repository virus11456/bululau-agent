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


if __name__ == "__main__":
    unittest.main()
