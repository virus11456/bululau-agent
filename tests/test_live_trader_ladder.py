"""瘸師弟三檔限價。不連網、不讀金鑰、不簽章。"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="live-trader-ladder-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402


def info(minqty="0.001", minnot="5", step="0.001", tick="0.01"):
    return dict(tick=Decimal(tick), step=Decimal(step), mstep=Decimal(step), minqty=Decimal(minqty),
                maxmqty=Decimal("1000000"), minnot=Decimal(minnot), status="TRADING", ctype="PERPETUAL")


ACC = {
    "totalWalletBalance": "10000",
    "totalUnrealizedProfit": "0",
    "availableBalance": "10000",
    "positions": [],
}
DAY = {"day": "2026-11-13", "day_start_equity": 10000.0, "slice_margin": 500.0,
       "halted_new": False, "killed": False}


class Args:
    def __init__(self, **kw):
        self.live = False
        self.symbol = "PIEVERSEUSDT"
        self.side = "BUY"
        self.entry_type = "limit"
        self.price = "97"
        self.ladder = None
        self.stop = "92.15"
        self.target = "1.5R"
        self.setup = "瘸師弟"
        self.leverage = 5
        self.portion = 1.0
        for k, v in kw.items():
            setattr(self, k, v)


class SplitAndRrTest(unittest.TestCase):
    def test_split_keeps_total_and_weights(self):
        step = Decimal("0.001")
        total = Decimal("10")
        parts = lt.split_ladder_qty(total, step)
        self.assertEqual(parts, [Decimal("2"), Decimal("3"), Decimal("5")])
        self.assertEqual(sum(parts), total)
        # 11 個 step：2.2、3.3、5.5。餘數 1 個 step 補給餘數最大的最深一檔。
        parts = lt.split_ladder_qty(Decimal("11") * step, step)
        self.assertEqual(parts, [Decimal("0.002"), Decimal("0.003"), Decimal("0.006")])
        self.assertEqual(sum(parts), Decimal("0.011"))

    def test_weighted_rr_versus_level1(self):
        prices = [Decimal("97"), Decimal("96"), Decimal("95")]
        avg = lt.weighted_entry(prices)
        self.assertEqual(avg, Decimal("95.7"))
        stop = Decimal("92.15")
        target = lt.ladder_target("1.5R", avg, stop)
        self.assertEqual(target, Decimal("101.025"))
        self.assertEqual(lt.ladder_rr(avg, stop, target), Decimal("1.5"))
        level1 = lt.ladder_rr(prices[0], stop, target)
        self.assertAlmostEqual(float(level1), 0.83, places=2)
        self.assertLess(level1, Decimal("1.5"))

    def test_min_notional_reasons_reject_whole_ladder(self):
        prices = [Decimal("97"), Decimal("96"), Decimal("95")]
        qtys = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]
        reasons = lt.check_ladder_levels(prices, qtys, Decimal("0.001"), Decimal("5"))
        self.assertEqual(len(reasons), 3)
        self.assertIn("最小名目", reasons[0])

    def test_order_size_matches_single_order_formula(self):
        # cmd_open 的式子：dist、1% 風險、份額×槓桿，三者取較小名目後用 step 下取整。
        entry, stop = 95.7, 92.15
        qty, risk, margin = lt.order_size(entry, stop, 10000, 500, 1, 5, Decimal("0.001"))
        dist = abs(entry - stop) / entry
        notional = min(10000 * 0.01 / dist, 500 * 5)
        expect = lt.rnd(notional / entry, Decimal("0.001"))
        self.assertEqual(qty, expect)
        self.assertEqual(risk, 100)
        self.assertEqual(margin, 500)


class OpenPathTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake_req(self, method, path, params=None, signed=False, retries=5):
        self.calls.append((method, path, dict(params or {})))
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        if path == "/fapi/v1/premiumIndex":
            return {"markPrice": "100"}
        if path in ("/fapi/v1/order/test", "/fapi/v1/marginType", "/fapi/v1/leverage"):
            return {}
        if method == "POST" and path == "/fapi/v1/order":
            return {"orderId": len(self.calls), "avgPrice": "0", "executedQty": "0"}
        if method == "POST" and path == "/fapi/v1/algoOrder":
            return {"algoId": len(self.calls)}
        if method == "DELETE":
            return {}
        if method == "GET" and path == "/fapi/v1/order":
            return {"status": "NEW", "executedQty": "0", "origQty": "1", "orderId": params.get("orderId")}
        if path == "/fapi/v1/openOrders":
            return []
        if path == "/fapi/v1/openAlgoOrders":
            return []
        raise AssertionError((method, path, params))

    def patches(self, sym=None):
        return [
            patch.object(lt, "req", self.fake_req),
            patch.object(lt, "log"),
            patch.object(lt, "account", return_value=ACC),
            patch.object(lt, "day_state", return_value=dict(DAY)),
            patch.object(lt, "upcoming_event", return_value=None),
            patch.object(lt, "sym_info", return_value=sym or info()),
            patch("urllib.request.urlopen", side_effect=AssertionError("network")),
            patch.object(lt, "load_api_keys", side_effect=AssertionError("keys")),
        ]

    def enter(self, stack):
        for p in self.patches():
            stack.enter_context(p)

    def test_single_order_still_one_limit(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.enter(stack)
            buf = io.StringIO()
            with redirect_stdout(buf):
                lt.cmd_open(Args(ladder=None, price="100", stop="90", target="120", setup="布鹿流", symbol="XUSDT"))
        posts = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order/test"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["type"], "LIMIT")
        self.assertEqual(posts[0][2]["price"], "100")
        self.assertNotIn("ladder", buf.getvalue())
        live_posts = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
        self.assertEqual(live_posts, [])

    def test_ladder_dry_run_three_payloads_and_rr(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.enter(stack)
            buf = io.StringIO()
            with redirect_stdout(buf):
                plan = lt.open_ladder(Args(ladder="97,96,95", stop="92.15", target="1.5R"))
        posts = [c for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order/test"]
        self.assertEqual(len(posts), 3)
        self.assertEqual([c[2]["price"] for c in posts], ["97", "96", "95"])
        qtys = [Decimal(c[2]["quantity"]) for c in posts]
        self.assertEqual(sum(qtys), Decimal(plan["qty"]))
        weights = [q / sum(qtys) for q in qtys]
        self.assertAlmostEqual(float(weights[0]), 0.2, places=2)
        self.assertAlmostEqual(float(weights[1]), 0.3, places=2)
        self.assertAlmostEqual(float(weights[2]), 0.5, places=2)
        self.assertGreaterEqual(plan["rr"], 1.5)
        self.assertLess(plan["level1_rr"], 1.5)
        self.assertAlmostEqual(plan["level1_rr"], 0.83, places=2)
        self.assertEqual([c for c in self.calls if c[1] == "/fapi/v1/order"], [])
        text = buf.getvalue()
        self.assertIn("[dry-run] POST /fapi/v1/order", text)
        self.assertEqual(text.count("'type': 'LIMIT'"), 3)
        self.assertIn("STOP_MARKET", text)
        self.assertNotIn("TAKE_PROFIT_MARKET", text)
        self.assertIn("止盈不預掛", text)

    def test_level_below_min_qty_sends_nothing(self):
        from contextlib import ExitStack
        qty, _, _ = lt.order_size(Decimal("95.7"), Decimal("92.15"), 10000, 500, 1, 5, Decimal("0.001"))
        parts = lt.split_ladder_qty(qty, Decimal("0.001"))
        sym = info(minqty=str(parts[0] + Decimal("0.001")), minnot="0.01")
        with ExitStack() as stack:
            self.enter(stack)
            stack.enter_context(patch.object(lt, "sym_info", return_value=sym))
            with self.assertRaises(SystemExit) as caught:
                lt.open_ladder(Args(ladder="97,96,95", stop="92.15", target="1.5R"))
        self.assertIn("整筆不送出", str(caught.exception))
        self.assertEqual([c for c in self.calls if c[1] in ("/fapi/v1/order", "/fapi/v1/order/test")], [])

    def test_low_rr_target_rejects(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.enter(stack)
            with self.assertRaises(SystemExit) as caught:
                lt.open_ladder(Args(ladder="97,96,95", stop="92.15", target="98"))
        self.assertIn("加權進場 RR", str(caught.exception))
        self.assertEqual([c for c in self.calls if c[1] == "/fapi/v1/order/test"], [])

    def test_live_second_level_failure_cancels_the_first(self):
        from contextlib import ExitStack
        def flaky(method, path, params=None, signed=False, retries=5):
            self.calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/order/test":
                return {}
            if path in ("/fapi/v1/marginType", "/fapi/v1/leverage"):
                return {}
            if method == "POST" and path == "/fapi/v1/order":
                n = sum(1 for c in self.calls if c[0] == "POST" and c[1] == "/fapi/v1/order")
                if n >= 2:
                    raise lt.BinanceError(400, -1, "reject")
                return {"orderId": 11}
            if method == "DELETE":
                return {}
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
                return []
            raise AssertionError((method, path))
        with ExitStack() as stack:
            self.enter(stack)
            stack.enter_context(patch.object(lt, "req", flaky))
            with self.assertRaises(SystemExit) as caught:
                lt.open_ladder(Args(live=True, ladder="97,96,95", stop="92.15", target="1.5R"))
        self.assertIn("沒有全部掛上", str(caught.exception))
        deletes = [c for c in self.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/order"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][2]["orderId"], 11)


class ManageLadderTest(unittest.TestCase):
    def test_partial_fill_resizes_protect_to_position_and_keeps_resting(self):
        calls = []
        orders = {
            1: {"orderId": 1, "status": "FILLED", "executedQty": "2", "origQty": "2"},
            2: {"orderId": 2, "status": "NEW", "executedQty": "0", "origQty": "3"},
            3: {"orderId": 3, "status": "NEW", "executedQty": "0", "origQty": "5"},
        }
        protects = [{
            "t": "STOP_MARKET", "algo": True, "id": 9, "qty": "10", "cp": False, "trig": "92.15",
        }, {
            "t": "TAKE_PROFIT_MARKET", "algo": True, "id": 8, "qty": "10", "cp": False, "trig": "101.02",
        }]

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/order" and method == "GET":
                return dict(orders[params["orderId"]])
            if path == "/fapi/v1/openAlgoOrders":
                return [{
                    "symbol": "PIEVERSEUSDT", "orderType": p["t"], "algoId": p["id"],
                    "quantity": p["qty"], "closePosition": "false", "triggerPrice": p["trig"],
                } for p in protects]
            if path == "/fapi/v1/openOrders":
                return []
            if path == "/fapi/v1/algoOrder" and method == "POST":
                return {"algoId": 100}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path, params))

        pd = {"ladder": True, "stop": "92.15", "target": "101.02", "setup": "瘸師弟",
              "orders": [{"orderId": 1}, {"orderId": 2}, {"orderId": 3}]}
        with patch.object(lt, "req", fake_req), patch.object(lt, "log"), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            done = lt.manage_ladder_pending("PIEVERSEUSDT", pd, {"PIEVERSEUSDT": 2.0}, True)
        self.assertFalse(done)
        posted = [c for c in calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        # 停損原本就是計畫總量 10，不縮小。止盈才改成實際持倉 2，觸發價不變。
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][2]["type"], "TAKE_PROFIT_MARKET")
        self.assertEqual(posted[0][2]["quantity"], "2")
        self.assertEqual(posted[0][2]["triggerPrice"], "101.02")
        self.assertEqual(posted[0][2]["reduceOnly"], "true")
        self.assertNotIn("closePosition", posted[0][2])

    def test_flat_after_stop_cancels_resting_levels(self):
        calls = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/order" and method == "GET":
                oid = params["orderId"]
                if oid == 1:
                    return {"orderId": 1, "status": "FILLED", "executedQty": "2", "origQty": "2"}
                return {"orderId": oid, "status": "NEW", "executedQty": "0", "origQty": "3"}
            if path == "/fapi/v1/openAlgoOrders":
                # 停損已不在，只剩止盈。
                return [{"symbol": "PIEVERSEUSDT", "orderType": "TAKE_PROFIT_MARKET", "algoId": 8,
                         "quantity": "10", "closePosition": "false", "triggerPrice": "101.02"}]
            if path == "/fapi/v1/openOrders":
                return []
            if method == "DELETE":
                return {}
            raise AssertionError((method, path))

        pd = {"ladder": True, "orders": [{"orderId": 1}, {"orderId": 2}, {"orderId": 3}]}
        with patch.object(lt, "req", fake_req), patch.object(lt, "log"), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            done = lt.manage_ladder_pending("PIEVERSEUSDT", pd, {}, True)
        self.assertEqual(done, "drop")
        deleted_entries = [c[2]["orderId"] for c in calls if c[0] == "DELETE" and c[1] == "/fapi/v1/order"]
        self.assertEqual(sorted(deleted_entries), [2, 3])

    def test_unfilled_resting_is_left_alone(self):
        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/order" and method == "GET":
                return {"orderId": params["orderId"], "status": "NEW", "executedQty": "0", "origQty": "1"}
            raise AssertionError("不該動保護單")

        pd = {"ladder": True, "orders": [{"orderId": 1}, {"orderId": 2}, {"orderId": 3}]}
        with patch.object(lt, "req", fake_req), patch.object(lt, "log"):
            self.assertFalse(lt.manage_ladder_pending("PIEVERSEUSDT", pd, {}, False))

    def test_flat_snapshot_with_fills_and_unread_orders_does_nothing(self):
        calls = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path))
            if method == "GET" and path == "/fapi/v1/order":
                oid = params["orderId"]
                if oid == 1:
                    return {"orderId": 1, "status": "FILLED", "executedQty": "2", "origQty": "2"}
                return {"orderId": oid, "status": "NEW", "executedQty": "0", "origQty": "3"}
            if path == "/fapi/v1/openAlgoOrders":
                raise lt.BinanceError(500, -1000, "algo down")
            if method == "DELETE" or method == "POST":
                raise AssertionError("讀不到保護單時不能撤單或下單")
            raise AssertionError((method, path))

        pd = {"ladder": True, "orders": [{"orderId": 1}, {"orderId": 2}]}
        with patch.object(lt, "req", fake_req), patch.object(lt, "log"):
            done = lt.manage_ladder_pending("XUSDT", pd, {}, True)
        self.assertFalse(done)
        self.assertFalse(any(m == "DELETE" for m, _p in calls))


if __name__ == "__main__":
    unittest.main()
