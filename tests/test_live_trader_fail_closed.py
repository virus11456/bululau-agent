"""live_trader 的 status／孤兒條件單。只換掉 req，不連網、不讀金鑰。"""
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

# 在 import live_trader 之前把資料目錄指到暫存，避免碰到專案 data/。
_DATA = tempfile.mkdtemp(prefix="live-trader-fail-closed-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import gate  # noqa: E402
import live_trader  # noqa: E402


ACC = {
    "totalWalletBalance": "700",
    "totalUnrealizedProfit": "0",
    "availableBalance": "700",
    "positions": [],
}
DAY = {"day_start_equity": 700.0, "slice_margin": 35.0, "halted_new": False, "killed": False}


class LiveTraderFailClosedTest(unittest.TestCase):
    def test_marker_matches_gate(self):
        self.assertEqual(live_trader.ALGO_ORDERS_UNREADABLE, gate.STATUS_ALGO_UNREADABLE)

    def test_open_algo_default_still_swallows_for_manage_watch_flatten(self):
        def boom(*_a, **_k):
            raise live_trader.BinanceError(500, -1000, "algo down")

        with patch.object(live_trader, "req", boom), patch.object(live_trader, "log"):
            self.assertEqual(live_trader.open_algo(), [])
            with self.assertRaises(live_trader.BinanceError):
                live_trader.open_algo(fail_closed=True)

    def test_status_algo_error_does_not_claim_zero_orders(self):
        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/openOrders":
                return []
            if path == "/fapi/v1/openAlgoOrders":
                raise live_trader.BinanceError(500, -1000, "algo endpoint down")
            raise AssertionError((method, path))

        buf = io.StringIO()
        with patch.object(live_trader, "req", fake_req), \
                patch.object(live_trader, "log"), \
                patch.object(live_trader, "account", return_value=ACC), \
                patch.object(live_trader, "day_state", return_value=dict(DAY)), \
                patch.object(live_trader, "load_api_keys", side_effect=AssertionError("read keys")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with redirect_stdout(buf):
                with self.assertRaises(live_trader.BinanceError):
                    live_trader.cmd_status(object())
        text = buf.getvalue()
        self.assertIn(live_trader.ALGO_ORDERS_UNREADABLE, text)
        self.assertNotIn("條件單 0", text)
        self.assertNotIn("一般掛單 0", text)

    def test_status_process_exits_nonzero_on_algo_error(self):
        data = tempfile.mkdtemp(prefix="status-exit-")
        script = textwrap.dedent("""\
            import os, sys
            os.environ["QUANTBOY_DATA_DIR"] = sys.argv[1]
            os.environ.pop("BINANCE_ENV_FILE", None)
            os.environ.pop("BINANCE_API_KEY", None)
            os.environ.pop("BINANCE_API_SECRET", None)
            sys.path.insert(0, sys.argv[2])
            import live_trader

            def fake_req(method, path, params=None, signed=False, retries=5):
                if path == "/fapi/v2/account":
                    return {"totalWalletBalance": "700", "totalUnrealizedProfit": "0",
                            "availableBalance": "700", "positions": []}
                if path == "/fapi/v1/openOrders":
                    return []
                if path == "/fapi/v1/openAlgoOrders":
                    raise live_trader.BinanceError(500, -1000, "algo endpoint down")
                raise SystemExit("unexpected " + method + " " + path)

            def no_keys():
                raise AssertionError("read keys")

            live_trader.req = fake_req
            live_trader.log = lambda *a, **k: None
            live_trader.load_api_keys = no_keys
            sys.argv = ["live_trader.py", "status"]
            live_trader.main()
        """)
        proc = subprocess.run(
            [sys.executable, "-c", script, data, _SRC],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(live_trader.ALGO_ORDERS_UNREADABLE, proc.stdout)
        self.assertNotIn("條件單 0", proc.stdout)
        self.assertNotIn("BINANCE_API", proc.stdout + proc.stderr)
        self.assertNotIn("read keys", proc.stdout + proc.stderr)

    def test_non_list_algo_payload_is_not_a_zero_count(self):
        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/openOrders":
                return []
            if path == "/fapi/v1/openAlgoOrders":
                return {"msg": "not a list"}
            raise AssertionError((method, path))

        buf = io.StringIO()
        with patch.object(live_trader, "req", fake_req), \
                patch.object(live_trader, "log"), \
                patch.object(live_trader, "account", return_value=ACC), \
                patch.object(live_trader, "day_state", return_value=dict(DAY)), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with redirect_stdout(buf):
                with self.assertRaises(live_trader.BinanceError):
                    live_trader.cmd_status(object())
        self.assertIn(live_trader.ALGO_ORDERS_UNREADABLE, buf.getvalue())
        self.assertNotIn("條件單 0", buf.getvalue())

    def test_flat_orphan_cancel_prints_a_gate_wake_word(self):
        deleted = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return []
            if method == "GET" and path == "/fapi/v1/openAlgoOrders":
                return [{"symbol": "BTCUSDT", "algoId": 99, "orderType": "STOP_MARKET", "side": "SELL"}]
            if method == "GET" and path == "/fapi/v1/openOrders":
                return [{
                    "symbol": "ETHUSDT", "orderId": 7, "type": "STOP_MARKET", "side": "SELL",
                    "price": "0", "stopPrice": "1", "time": 0,
                }]
            if method == "DELETE" and path in ("/fapi/v1/algoOrder", "/fapi/v1/order"):
                deleted.append((path, params))
                return {}
            raise AssertionError((method, path, params))

        buf = io.StringIO()
        with patch.object(live_trader, "req", fake_req), \
                patch.object(live_trader, "log"), \
                patch.object(live_trader, "account", return_value=ACC), \
                patch.object(live_trader, "day_state", return_value=dict(DAY)), \
                patch.object(live_trader, "load_state", return_value={"pending": {}, "meta": {}}), \
                patch.object(live_trader, "save_state"), \
                patch.object(live_trader, "load_api_keys", side_effect=AssertionError("read keys")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with redirect_stdout(buf):
                live_trader.cmd_manage(type("A", (), {"live": True})())
        text = buf.getvalue()
        self.assertIn("孤兒條件單 BTCUSDT 99", text)
        self.assertIn("孤兒條件單(舊端點) ETHUSDT 7", text)
        self.assertTrue(any(word in text for word in gate.MANAGE_ALERT_WORDS), text)
        self.assertEqual(
            sorted(path for path, _params in deleted),
            ["/fapi/v1/algoOrder", "/fapi/v1/order"],
        )
        quiet, reasons = gate.decide(
            {"code": 0, "stdout": text, "stderr": ""},
            {"code": 0, "stdout": "", "stderr": ""},
            None,
            {"code": 0, "stdout": "", "stderr": ""},
            None,
            {"_absent": True},
            "2026-11-10",
            self_now(),
        )
        self.assertFalse(quiet)
        self.assertIn("manage_reported_action", reasons)


def self_now():
    from datetime import datetime
    return datetime.now(gate.TPE)


if __name__ == "__main__":
    unittest.main()
