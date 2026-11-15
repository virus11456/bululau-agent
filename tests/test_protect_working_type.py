"""保護單觸發價改為合約最新價。不連網、不讀金鑰。"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="protect-working-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402


class ProtectWorkingTypeTest(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.algo_status = "NEW"
        self.legacy = False
        lt.STOP_READBACK_SLEEP_S = 0
        limit = lt._ratelimit_path()
        if os.path.exists(limit):
            os.remove(limit)

    def tearDown(self):
        limit = lt._ratelimit_path()
        if os.path.exists(limit):
            os.remove(limit)

    def fake_req(self, method, path, params=None, signed=False, retries=5):
        params = dict(params or {})
        if method == "POST" and path == "/fapi/v1/algoOrder":
            if self.legacy:
                raise lt.BinanceError(404, None, "no algo endpoint")
            self.posts.append(params)
            return {"algoId": len(self.posts)}
        if method == "POST" and path == "/fapi/v1/order":
            self.posts.append(params)
            return {"orderId": len(self.posts)}
        if path == "/fapi/v1/openAlgoOrders":
            if not self.posts:
                return []
            return [{"algoId": len(self.posts), "algoStatus": self.algo_status}]
        raise AssertionError((method, path, params))

    def place(self, otype, qty, live=True):
        buf = io.StringIO()
        with patch.object(lt, "req", self.fake_req), patch.object(lt, "log"), \
                redirect_stdout(buf):
            lt.place_protect("BTCUSDT", "SELL", otype, "90", live, qty=qty)
        return buf.getvalue()

    def test_source_has_no_mark_price_working_type(self):
        src = open(os.path.join(_SRC, "live_trader.py")).read()
        self.assertEqual(lt.PROTECT_WORKING_TYPE, "CONTRACT_PRICE")
        self.assertNotIn("MARK_PRICE", src)
        self.assertEqual(src.count('priceProtect="TRUE"'), 3)
        self.assertNotIn('priceProtect="FALSE"', src)

    def test_live_stop_and_take_profit_use_contract_price(self):
        cases = (
            ("STOP_MARKET", None, "true"),
            ("STOP_MARKET", "1.5", None),
            ("TAKE_PROFIT_MARKET", None, "true"),
            ("TAKE_PROFIT_MARKET", "2", None),
        )
        for otype, qty, close_position in cases:
            with self.subTest(otype=otype, qty=qty):
                self.posts = []
                self.place(otype, qty)
                self.assertEqual(len(self.posts), 1)
                order = self.posts[0]
                self.assertEqual(order["workingType"], "CONTRACT_PRICE")
                self.assertEqual(order["priceProtect"], "TRUE")
                self.assertEqual(order["type"], otype)
                if close_position:
                    self.assertEqual(order["closePosition"], close_position)
                    self.assertNotIn("quantity", order)
                else:
                    self.assertEqual(order["quantity"], qty)
                    self.assertEqual(order["reduceOnly"], "true")

    def test_dry_run_prints_contract_price_and_does_not_post(self):
        text = self.place("STOP_MARKET", "1", live=False)
        self.assertEqual(self.posts, [])
        self.assertIn("CONTRACT_PRICE", text)
        self.assertIn("priceProtect", text)
        self.assertIn("TRUE", text)
        self.assertIn("停損回查略過", text)
        text = self.place("TAKE_PROFIT_MARKET", None, live=False)
        self.assertIn("CONTRACT_PRICE", text)
        self.assertNotIn("停損回查略過", text)

    def test_legacy_fallback_keeps_contract_price_and_price_protect(self):
        self.legacy = True
        with patch.object(lt, "req", self.fake_req), patch.object(lt, "log"):
            lt._post_protect("BTCUSDT", "SELL", "STOP_MARKET", "90", None)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["workingType"], "CONTRACT_PRICE")
        self.assertEqual(self.posts[0]["priceProtect"], "TRUE")
        self.assertEqual(self.posts[0]["stopPrice"], "90")

    def test_readback_replace_uses_contract_price(self):
        seen = {"n": 0}

        def fake_req(method, path, params=None, signed=False, retries=5):
            params = dict(params or {})
            if method == "POST" and path == "/fapi/v1/algoOrder":
                self.posts.append(params)
                return {"algoId": len(self.posts)}
            if path == "/fapi/v1/openAlgoOrders":
                seen["n"] += 1
                if seen["n"] == 1:
                    return []
                return [{"algoId": len(self.posts), "algoStatus": "NEW"}]
            raise AssertionError((method, path))

        with patch.object(lt, "req", fake_req), patch.object(lt, "log"), \
                redirect_stdout(io.StringIO()):
            lt.place_protect("BTCUSDT", "SELL", "STOP_MARKET", "90", True, qty="3")
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(all(p["workingType"] == "CONTRACT_PRICE" and p["priceProtect"] == "TRUE"
                            for p in self.posts))
        self.assertTrue(all(p["reduceOnly"] == "true" and p["quantity"] == "3" for p in self.posts))


if __name__ == "__main__":
    unittest.main()
