"""停損送出後回查。不連網、不讀金鑰。"""
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="stop-readback-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402


def info():
    return dict(tick=Decimal("0.01"), step=Decimal("0.001"), mstep=Decimal("0.001"),
                minqty=Decimal("0.001"), maxmqty=Decimal("1000000"), minnot=Decimal("5"),
                status="TRADING", ctype="PERPETUAL")


ACC = {
    "totalWalletBalance": "10000", "totalUnrealizedProfit": "0", "availableBalance": "10000",
    "positions": [],
}
DAY = {"day": "2026-11-14", "day_start_equity": 10000.0, "slice_margin": 500.0,
       "halted_new": False, "killed": False}


class Args:
    def __init__(self, **kw):
        self.live = kw.get("live", True)
        self.symbol = "BTCUSDT"
        self.side = "BUY"
        self.entry_type = "limit"
        self.price = "100"
        self.stop = "90"
        self.target = "120"
        self.setup = "布鹿流"
        self.target_3r = None
        self.leverage = None
        self.portion = 1.0


class StopReadbackTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.logs = []
        self.saved = []
        self.state = {"pending": {}, "meta": {}}
        self.algos = []
        self.read_error = None
        self.next_id = 40
        lt.STOP_READBACK_SLEEP_S = 0
        limit = os.path.join(lt.HOME, lt.RATELIMIT_NAME)
        if os.path.exists(limit):
            os.remove(limit)

    def run_open(self):
        def load():
            return {"pending": {}, "meta": {}}

        def save(state):
            self.saved.append(state)

        def fake_req(method, path, params=None, signed=False, retries=5):
            params = dict(params or {})
            self.calls.append((method, path, params))
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "100"}
            if path in ("/fapi/v1/order/test", "/fapi/v1/marginType", "/fapi/v1/leverage"):
                return {}
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "BTCUSDT", "positionAmt": "0", "markPrice": "100"}]
            if path == "/fapi/v1/openOrders":
                return []
            if path == "/fapi/v1/openAlgoOrders":
                if self.read_error:
                    raise self.read_error
                return list(self.algos)
            if method == "POST" and path == "/fapi/v1/order":
                return {"orderId": 7, "avgPrice": "100", "executedQty": "0"}
            if method == "POST" and path == "/fapi/v1/algoOrder":
                self.next_id += 1
                algo_id = self.next_id
                if getattr(self, "remember", True):
                    self.algos.append({"algoId": algo_id, "symbol": params.get("symbol"),
                                       "algoStatus": "NEW", "orderType": params.get("type")})
                return {"algoId": algo_id}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path, params))

        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "upcoming_event", return_value=None), \
                patch.object(lt, "sym_info", return_value=info()), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with redirect_stdout(buf):
                lt.cmd_open(Args(live=self.live if hasattr(self, "live") else True))
        return buf.getvalue()

    def _opens(self):
        return [c for c in self.calls if c[0] == "GET" and c[1] == "/fapi/v1/openAlgoOrders"]

    def _stops(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"
                and c[2].get("type") == "STOP_MARKET"]

    def test_readback_succeeds_on_first_try(self):
        self.live = True
        text = self.run_open()
        self.assertEqual(len(self._stops()), 1)
        self.assertIn("已掛上", text)
        reads = [kw for kind, kw in self.logs if kind == "stop_readback"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["attempt"], 1)
        self.assertTrue(reads[0]["ok"])
        self.assertEqual(reads[0]["symbol"], "BTCUSDT")
        self.assertEqual(reads[0]["algoId"], 41)

    def test_missing_then_replace_succeeds(self):
        self.live = True
        text = self._run_with_late_visibility()
        self.assertEqual(len(self._stops()), 2)
        reads = [kw for kind, kw in self.logs if kind == "stop_readback"]
        self.assertEqual([kw["ok"] for kw in reads], [False, True])
        self.assertEqual([kw["attempt"] for kw in reads], [1, 2])
        self.assertIn("已掛上", text)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))

    def _run_with_late_visibility(self):
        reads = {"n": 0}

        def fake_req(method, path, params=None, signed=False, retries=5):
            params = dict(params or {})
            self.calls.append((method, path, params))
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "100"}
            if path in ("/fapi/v1/order/test", "/fapi/v1/marginType", "/fapi/v1/leverage"):
                return {}
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "BTCUSDT", "positionAmt": "0", "markPrice": "100"}]
            if path == "/fapi/v1/openOrders":
                return []
            if path == "/fapi/v1/openAlgoOrders":
                if not any(c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
                           for c in self.calls[:-1]):
                    return []
                reads["n"] += 1
                if reads["n"] == 1:
                    return []
                return list(self.algos)
            if method == "POST" and path == "/fapi/v1/order":
                return {"orderId": 7, "avgPrice": "100", "executedQty": "0"}
            if method == "POST" and path == "/fapi/v1/algoOrder":
                self.next_id += 1
                self.algos = [{"algoId": self.next_id, "symbol": params.get("symbol"),
                               "algoStatus": "NEW", "orderType": params.get("type")}]
                return {"algoId": self.next_id}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path, params))

        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "upcoming_event", return_value=None), \
                patch.object(lt, "sym_info", return_value=info()), \
                patch.object(lt, "load_state", return_value={"pending": {}, "meta": {}}), \
                patch.object(lt, "save_state", side_effect=lambda state: self.saved.append(state)), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with redirect_stdout(buf):
                lt.cmd_open(Args())
        return buf.getvalue()

    def test_both_reads_fail_cancels_entry(self):
        self.live = True
        self.remember = False
        with self.assertRaises(SystemExit) as caught:
            self._run_empty_reads()
        self.assertIn("停損失敗", str(caught.exception))
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/order" and c[2].get("orderId") == 7
                            for c in self.calls))
        self.assertEqual(len(self._stops()), 2)
        reads = [kw for kind, kw in self.logs if kind == "stop_readback"]
        self.assertEqual([kw["ok"] for kw in reads], [False, False])
        self.assertNotIn("已掛上", str(caught.exception))

    def _run_empty_reads(self):
        def fake_req(method, path, params=None, signed=False, retries=5):
            params = dict(params or {})
            self.calls.append((method, path, params))
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "100"}
            if path in ("/fapi/v1/order/test", "/fapi/v1/marginType", "/fapi/v1/leverage"):
                return {}
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "BTCUSDT", "positionAmt": "0", "markPrice": "100"}]
            if path == "/fapi/v1/openOrders":
                return []
            if path == "/fapi/v1/openAlgoOrders":
                if self.read_error:
                    raise self.read_error
                return []
            if method == "POST" and path == "/fapi/v1/order":
                return {"orderId": 7, "avgPrice": "100", "executedQty": "0"}
            if method == "POST" and path == "/fapi/v1/algoOrder":
                self.next_id += 1
                return {"algoId": self.next_id}
            if method == "DELETE":
                return {}
            if path == "/fapi/v2/account":
                return ACC
            raise AssertionError((method, path, params))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "upcoming_event", return_value=None), \
                patch.object(lt, "sym_info", return_value=info()), \
                patch.object(lt, "load_state", return_value={"pending": {}, "meta": {}}), \
                patch.object(lt, "save_state", side_effect=lambda state: self.saved.append(state)), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            lt.cmd_open(Args())

    def test_429_on_readback_is_a_failure(self):
        self.read_error = lt.BinanceError(429, -1003, "too many")
        with self.assertRaises(SystemExit) as caught:
            self._run_empty_reads()
        self.assertIn("停損失敗", str(caught.exception))
        reads = [kw for kind, kw in self.logs if kind == "stop_readback"]
        self.assertEqual([kw["ok"] for kw in reads], [False, False])
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/order" for c in self.calls))

    def test_active_backoff_cancels_without_another_read(self):
        lt.write_ratelimit(429, time.time() + 60)
        with self.assertRaises(SystemExit) as caught:
            self._run_empty_reads()
        self.assertIn("停損失敗", str(caught.exception))
        self.assertEqual(len(self._stops()), 1)
        after = self.calls[self.calls.index(self._stops()[0]) + 1:]
        self.assertFalse(any(c[1] == "/fapi/v1/openAlgoOrders" for c in after))
        reads = [kw for kind, kw in self.logs if kind == "stop_readback"]
        self.assertEqual(len(reads), 1)
        self.assertFalse(reads[0]["ok"])
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/order" for c in self.calls))

    def test_dry_run_skips_readback(self):
        buf = io.StringIO()
        with patch.object(lt, "req", side_effect=AssertionError("should not read")), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "upcoming_event", return_value=None), \
                patch.object(lt, "sym_info", return_value=info()), \
                patch.object(lt, "load_state", return_value={"pending": {}, "meta": {}}), \
                patch.object(lt, "save_state", side_effect=AssertionError("no state")), \
                patch.object(lt, "log"), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with redirect_stdout(buf):
                # dry-run 仍會打 /order/test。這裡只確認 place_protect 自己的略過句。
                lt.place_protect("BTCUSDT", "SELL", "STOP_MARKET", "90", False, qty="1")
        text = buf.getvalue()
        self.assertIn("停損回查略過", text)
        self.assertNotIn("已掛上", text)


if __name__ == "__main__":
    unittest.main()
