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


def rising_daily(n=40, step=1.0):
    now = 1_700_000_000_000
    rows = []
    px = 100.0
    for i in range(n):
        px += step
        t = now + i * 86_400_000
        rows.append([t, px - step, px, px - step, px, "0", t + 86_400_000 - 1, "1", 0, 0, 0, "0"])
    return rows


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
       "halted_new": False, "killed": False, "capital_flow_last_ms": 1, "capital_flow_seen": []}


class Args:
    def __init__(self, **kw):
        self.live = kw.get("live", True)
        self.symbol = kw.get("symbol", "ETHUSDT")
        self.side = kw.get("side", "BUY")
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
        self.avg = "100"
        self.filled = "10"
        self.mark = "100"
        self.info = info()
        self.algos = []
        lt.STOP_READBACK_SLEEP_S = 0

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
            if path == "/fapi/v1/klines":
                if getattr(self, "btc_error", None):
                    raise self.btc_error
                return self.btc_rows if getattr(self, "btc_rows", None) is not None else rising_daily()
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": self.mark}
            if path in ("/fapi/v1/order/test", "/fapi/v1/marginType", "/fapi/v1/leverage"):
                return {}
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": args.symbol, "positionAmt": "10", "markPrice": "100"}]
            if path == "/fapi/v1/openAlgoOrders":
                return list(self.algos)
            if path in ("/fapi/v1/openOrders", "/fapi/v1/userTrades", "/fapi/v1/income"):
                return []
            if method == "POST" and path == "/fapi/v1/order":
                return {"orderId": 7, "avgPrice": self.avg, "executedQty": self.filled}
            if method == "POST" and path == "/fapi/v1/algoOrder":
                algo_n["n"] += 1
                if self.fail_stop and algo_n["n"] == 1:
                    raise lt.BinanceError(400, -2021, "stop rejected")
                algo_id = 30 + algo_n["n"]
                self.algos.append({"algoId": algo_id, "symbol": (params or {}).get("symbol"),
                                   "algoStatus": "NEW", "orderType": (params or {}).get("type")})
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
                patch.object(lt, "sym_info", return_value=self.info), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with redirect_stdout(buf):
                lt.cmd_open(args)
        return buf.getvalue()

    def test_ioc_entry_places_stop_and_target_from_fill(self):
        text = self.run_open(Args())
        entry = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
        self.assertEqual(entry[0][2]["type"], "LIMIT")
        self.assertEqual(entry[0][2]["timeInForce"], "IOC")
        self.assertEqual(entry[0][2]["price"], "100.3")
        self.assertEqual(entry[0][2]["quantity"], "9.708")
        self.assertLess(lt.Decimal(entry[0][2]["quantity"]), lt.Decimal("10"))
        loss = (lt.Decimal(entry[0][2]["price"]) - lt.Decimal("90")) * lt.Decimal(entry[0][2]["quantity"])
        self.assertLessEqual(loss, lt.Decimal("100"))
        self.assertEqual(entry[0][2]["side"], "BUY")
        protects = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        self.assertEqual([c[2]["type"] for c in protects], ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        self.assertEqual(protects[1][2]["triggerPrice"], "120")
        self.assertEqual(self.state["meta"]["ETHUSDT"]["target"], "120")
        self.assertEqual(self.state["meta"]["ETHUSDT"]["stop"], "90")
        self.assertFalse(any(kind == "xiao_risk_over_1pct" for kind, _kw in self.logs))
        self.assertIn("已進場並掛好保護單", text)

    def test_long_and_short_caps_round_conservatively(self):
        self.assertEqual(lt.xiao_worst_price("BUY", "100", "0.003", "0.01"), lt.Decimal("100.3"))
        self.assertEqual(lt.xiao_worst_price("SELL", "100", "0.003", "0.01"), lt.Decimal("99.7"))
        self.assertEqual(lt.xiao_ioc_limit("BUY", "10", "90", "100", "0.01"), lt.Decimal("100"))
        self.assertEqual(lt.xiao_ioc_limit("SELL", "10", "110", "100", "0.01"), lt.Decimal("100"))
        text = self.run_open(Args(side="SELL", stop="110", target="80", symbol="ETHUSDT"))
        entry = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
        self.assertEqual(entry[0][2]["side"], "SELL")
        self.assertEqual(entry[0][2]["timeInForce"], "IOC")
        self.assertEqual(entry[0][2]["price"], "99.7")
        self.assertEqual(entry[0][2]["quantity"], "9.708")
        loss = (lt.Decimal("110") - lt.Decimal("99.7")) * lt.Decimal("9.708")
        self.assertLessEqual(loss, lt.Decimal("100"))

    def test_ioc_unfilled_is_abandoned(self):
        self.filled = "0"
        text = self.run_open(Args())
        self.assertIn("沒成交", text)
        self.assertTrue(any(kind == "xiao_ioc_unfilled" for kind, _kw in self.logs))
        self.assertFalse(any(c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" for c in self.calls))
        self.assertEqual(self.saved, [])
        self.assertEqual(self.state["meta"], {})

    def test_partial_fill_keeps_filled_qty_and_places_protects(self):
        self.filled = "4"
        self.avg = "99"
        self.run_open(Args())
        entry = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
        self.assertEqual(entry[0][2]["quantity"], "9.708")
        protects = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        self.assertEqual(len(protects), 2)
        self.assertEqual(self.state["meta"]["ETHUSDT"]["target"], "117")

    def test_estimated_risk_over_one_percent_does_not_order(self):
        self.mark = "90.2"
        self.info = info()
        self.info["tick"] = Decimal("1")
        text = self.run_open(Args(stop="90"))
        self.assertIn("放棄不下單", text)
        self.assertTrue(any(kind == "xiao_risk_over_1pct" for kind, _kw in self.logs))
        self.assertFalse(any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in self.calls))
        self.assertEqual(self.saved, [])

    def test_stop_place_failure_market_closes(self):
        self.fail_stop = True
        with self.assertRaises(SystemExit):
            self.run_open(Args())
        markets = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("type") == "MARKET"]
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0][2]["reduceOnly"], "true")
        self.assertEqual(markets[0][2]["side"], "SELL")
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
        self.assertEqual(lt.resolve_entry_type("小師弟", "limit"), "ioc")
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

    def test_da_btc_bear_records_and_does_not_order(self):
        self.btc_rows = rising_daily(step=-1.0)
        text = self.run_open(Args(live=True, setup="大師兄", entry_type="limit",
                                  symbol="BTCUSDT", target_3r="130"))
        self.assertIn("不下單", text)
        paused = [kw for kind, kw in self.logs if kind == "da_paused_btc_daily_bear"]
        self.assertEqual(paused[0]["side"], "BUY")
        self.assertEqual(paused[0]["entry"], "100")
        self.assertEqual(paused[0]["stop"], "90")
        self.assertEqual(paused[0]["target"], "120")
        self.assertEqual(paused[0]["target_3r"], "130")
        self.assertFalse(any(c[0] == "POST" for c in self.calls))
        self.assertEqual(self.saved, [])

    def test_da_btc_bull_orders(self):
        text = self.run_open(Args(live=False, setup="大師兄", entry_type="limit",
                                  symbol="BTCUSDT", target_3r="130"))
        self.assertIn("2R 半倉", text)
        self.assertFalse(any(kind == "da_paused_btc_daily_bear" for kind, _kw in self.logs))
        self.assertTrue(any(c[1] == "/fapi/v1/klines" for c in self.calls))

    def test_da_btc_unread_still_orders(self):
        self.btc_error = lt.BinanceError(500, -1000, "daily down")
        text = self.run_open(Args(live=False, setup="大師兄", entry_type="limit",
                                  symbol="BTCUSDT", target_3r="130"))
        self.assertIn("2R 半倉", text)
        self.assertTrue(any("仍下單" in str(kw.get("msg")) for _k, kw in self.logs))
        self.assertFalse(any(kind == "da_paused_btc_daily_bear" for kind, _kw in self.logs))

    def test_da_btc_short_history_pauses(self):
        self.btc_rows = rising_daily(n=10)
        text = self.run_open(Args(setup="大師兄", entry_type="limit", symbol="BTCUSDT", target_3r="130"))
        self.assertIn("不下單", text)
        paused = [kw for kind, kw in self.logs if kind == "da_paused_btc_daily_bear"]
        self.assertEqual(paused[0]["why"], "日K資料不足")
        self.assertFalse(any(c[0] == "POST" for c in self.calls))

    def test_xiao_and_kuai_ignore_btc_daily(self):
        self.run_open(Args(live=False, setup="小師弟"))
        self.assertFalse(any(c[1] == "/fapi/v1/klines" for c in self.calls))
        self.calls.clear()
        self.logs.clear()
        self.run_open(Args(live=False, setup="瘸師弟", entry_type="limit", symbol="BTCUSDT"))
        self.assertFalse(any(c[1] == "/fapi/v1/klines" for c in self.calls))
        self.assertFalse(any(kind == "da_paused_btc_daily_bear" for kind, _kw in self.logs))

    def test_bad_daily_rows_still_order(self):
        self.btc_rows = [["壞掉"]]
        text = self.run_open(Args(live=False, setup="大師兄", entry_type="limit",
                                  symbol="BTCUSDT", target_3r="130"))
        self.assertIn("2R 半倉", text)
        self.assertTrue(any("仍下單" in str(kw.get("msg")) for _k, kw in self.logs))
        self.assertFalse(any(kind == "da_paused_btc_daily_bear" for kind, _kw in self.logs))

    def test_rate_limit_does_not_order(self):
        self.btc_error = lt.BinanceError(429, -1003, "limit")
        with self.assertRaises(SystemExit) as caught:
            self.run_open(Args(setup="大師兄", entry_type="limit", symbol="BTCUSDT", target_3r="130"))
        self.assertIn("限流", str(caught.exception))
        self.assertFalse(any(c[0] == "POST" for c in self.calls))
        self.assertFalse(any(kind == "da_paused_btc_daily_bear" for kind, _kw in self.logs))

    def test_daily_constants_match_hardfilter(self):
        import hardfilter as hf
        self.assertEqual((lt.DAILY_FAST, lt.DAILY_SLOW, lt.DAILY_MIN_BARS, lt.DAILY_KLINE_LIMIT),
                         (hf.DAILY_FAST, hf.DAILY_SLOW, hf.DAILY_MIN_BARS, hf.DAILY_KLINE_LIMIT))
        closes = [100 + i * 0.4 for i in range(40)]
        self.assertEqual(lt.ma_at(closes, 7), hf.ma_at(closes, 7))
        self.assertEqual(lt.ma_at(closes, 25, 3), hf.ma_at(closes, 25, 3))
        for rows in (rising_daily(), rising_daily(step=-1.0)):
            parsed = lt._closed_daily(rows, 10 ** 15)
            cs = [bar["c"] for bar in parsed]
            embedded = None if cs[-1] > lt.ma_at(cs, lt.DAILY_FAST) > lt.ma_at(cs, lt.DAILY_SLOW) else "pause"
            theirs = None if cs[-1] > hf.ma_at(cs, hf.DAILY_FAST) > hf.ma_at(cs, hf.DAILY_SLOW) else "pause"
            self.assertEqual(embedded, theirs)
            self.assertEqual(lt.da_btc_pause_reason(rows, now_ms=10 ** 15) is None, embedded is None)
        src = open(lt.__file__).read()
        self.assertNotIn("import hardfilter", src)
        self.assertNotIn("from hardfilter", src)
