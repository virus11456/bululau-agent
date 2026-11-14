"""小師弟市價進場。不連網、不讀金鑰。"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="xiao-market-")
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
    "totalWalletBalance": "10000",
    "totalUnrealizedProfit": "0",
    "availableBalance": "10000",
    "positions": [],
}
DAY = {"day": "2026-11-14", "day_start_equity": 10000.0, "slice_margin": 500.0,
       "halted_new": False, "killed": False}


class Args:
    def __init__(self, **kw):
        self.live = kw.get("live", True)
        self.symbol = kw.get("symbol", "ETHUSDT")
        self.side = "BUY"
        self.entry_type = kw.get("entry_type", "limit")
        self.price = kw.get("price", "100")
        self.stop = kw.get("stop", "90")
        self.target = kw.get("target", "120")
        self.setup = kw.get("setup", "小師弟")
        self.target_3r = kw.get("target_3r")
        self.leverage = None
        self.portion = 1.0


class XiaoMarketEntryTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.logs = []
        self.saved = []
        self.state = {"pending": {}, "meta": {}}
        self.fail_stop = False
        self.avg = "101"

    def run_open(self, args):
        def load():
            return json.loads(json.dumps(self.state))

        def save(state):
            self.state.clear()
            self.state.update(json.loads(json.dumps(state)))
            self.saved.append(json.loads(json.dumps(state)))

        algo_n = {"n": 0}

        def fake_req(method, path, params=None, signed=False, retries=5):
            self.calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "100"}
            if path in ("/fapi/v1/order/test", "/fapi/v1/marginType", "/fapi/v1/leverage"):
                return {}
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": args.symbol, "positionAmt": "10", "markPrice": "100"}]
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders", "/fapi/v1/userTrades", "/fapi/v1/income"):
                return []
            if method == "POST" and path == "/fapi/v1/order":
                return {"orderId": 7, "avgPrice": self.avg, "executedQty": "10"}
            if method == "POST" and path == "/fapi/v1/algoOrder":
                algo_n["n"] += 1
                if self.fail_stop and algo_n["n"] == 1:
                    raise lt.BinanceError(400, -2021, "stop rejected")
                return {"algoId": 30 + algo_n["n"]}
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
                lt.cmd_open(args)
        return buf.getvalue()

    def test_market_entry_places_stop_and_target_from_fill(self):
        text = self.run_open(Args())
        entry = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
        self.assertEqual(entry[0][2]["type"], "MARKET")
        self.assertNotIn("price", entry[0][2])
        protects = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        self.assertEqual([c[2]["type"] for c in protects], ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        self.assertEqual(protects[1][2]["triggerPrice"], "123")
        self.assertEqual(entry[0][2]["quantity"], protects[0][2].get("quantity") or entry[0][2]["quantity"])
        self.assertEqual(self.state["meta"]["ETHUSDT"]["target"], "123")
        self.assertEqual(self.state["meta"]["ETHUSDT"]["stop"], "90")
        self.assertTrue(any("未確認" in str(kw.get("msg")) for _k, kw in self.logs))
        self.assertIn("已進場並掛好保護單", text)

    def test_stop_place_failure_market_closes(self):
        self.fail_stop = True
        with self.assertRaises(SystemExit):
            self.run_open(Args())
        markets = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("type") == "MARKET"]
        self.assertEqual(len(markets), 2)
        self.assertEqual(markets[1][2]["reduceOnly"], "true")
        self.assertEqual(markets[1][2]["side"], "SELL")
        protects = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        self.assertEqual(len(protects), 1)
        self.assertNotIn("ETHUSDT", self.state.get("meta") or {})

    def test_dry_run_does_not_send_orders(self):
        text = self.run_open(Args(live=False))
        self.assertIn("不下單", text)
        self.assertIn("STOP_MARKET", text)
        self.assertIn("TAKE_PROFIT_MARKET", text)
        self.assertFalse(any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in self.calls))
        self.assertFalse(any(c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" for c in self.calls))
        self.assertEqual(self.saved, [])
        self.assertEqual(self.state["meta"], {})

    def test_other_setups_stay_limit(self):
        self.assertEqual(lt.resolve_entry_type("小師弟", "limit"), "market")
        self.assertEqual(lt.resolve_entry_type("大師兄", "limit"), "limit")
        self.assertEqual(lt.resolve_entry_type("瘸師弟", "limit"), "limit")
        self.assertEqual(lt.resolve_entry_type("布鹿流", "limit"), "limit")
        text = self.run_open(Args(live=False, setup="布鹿流", entry_type="limit", symbol="BTCUSDT"))
        posted = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order/test"]
        self.assertEqual(posted[0][2]["type"], "LIMIT")
        self.assertIn("止盈不預掛", text)

    def test_da_open_refuses_without_3r_and_dry_run_shows_two_targets(self):
        with self.assertRaises(SystemExit) as caught:
            lt.cmd_open(Args(setup="大師兄", entry_type="limit"))
        self.assertIn("target-3r", str(caught.exception))
        with self.assertRaises(SystemExit):
            lt.cmd_open(Args(setup="大師兄", entry_type="market", target_3r="130"))
        text = self.run_open(Args(live=False, setup="大師兄", entry_type="limit",
                                  symbol="BTCUSDT", target_3r="130"))
        self.assertIn("2R 半倉", text)
        self.assertIn("3R closePosition", text)
        self.assertFalse(any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in self.calls))
        self.assertTrue(lt._da_limit_expired({"time": 1}))
        self.assertFalse(lt._da_limit_expired({"time": int(__import__("time").time() * 1000)}))
        self.assertEqual(lt.DA_LIMIT_EXPIRE_4H, 2)
