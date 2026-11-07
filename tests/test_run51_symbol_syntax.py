"""零倉也要先過 symbol 語法。不發真 HTTP，不把語法當上市證明。"""
import unittest
import urllib.request
from pathlib import Path

from src.phase1.mac_query_map import map_queries

ROOT = Path(__file__).resolve().parents[1]


def _doc(rows, orders=None, algo=None):
    return {
        "schema_version": 1,
        "event_type": "offline_query_details",
        "account": {"positions": rows},
        "open_orders": [] if orders is None else orders,
        "open_algo_orders": [] if algo is None else algo,
    }


class Run51SymbolSyntaxTest(unittest.TestCase):
    def setUp(self):
        self.sockets = []
        self.real_open = urllib.request.urlopen

        def blocked(*_args, **_kwargs):
            self.sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked

    def tearDown(self):
        urllib.request.urlopen = self.real_open
        self.assertEqual(self.sockets, [])

    def test_zero_amount_still_rejects_a_bad_symbol(self):
        for symbol in (False, "ETHBTC", "../USDT", ""):
            mapped = map_queries(_doc([{"symbol": symbol, "positionAmt": "0"}]))
            self.assertFalse(mapped["detail_proven"], symbol)
            self.assertFalse(mapped["new_positions_allowed"], symbol)
            self.assertFalse(mapped["protection_required"], symbol)
            self.assertEqual(mapped["queries"]["account"]["state"], "unknown")
            self.assertFalse(mapped["symbol_listing_verified"])
        dotted = map_queries(_doc([{"symbol": "../USDT", "positionAmt": "1.5"}]))
        self.assertFalse(dotted["detail_proven"])
        self.assertFalse(dotted["new_positions_allowed"])
        self.assertTrue(dotted["protection_required"])
        self.assertEqual(dotted["queries"]["account"]["state"], "unknown")
        order = map_queries(_doc(
            [{"symbol": "ETHUSDT", "positionAmt": "0"}],
            orders=[{"symbol": "../USDT", "side": "BUY", "origQty": "0"}],
        ))
        self.assertFalse(order["new_positions_allowed"])
        self.assertFalse(order["protection_required"])
        self.assertEqual(order["queries"]["open_orders"]["state"], "unknown")
        live_order = map_queries(_doc(
            [{"symbol": "ETHUSDT", "positionAmt": "0"}],
            algo=[{"symbol": "ETHBTC", "side": "SELL", "origQty": "2"}],
        ))
        self.assertTrue(live_order["protection_required"])
        self.assertFalse(live_order["new_positions_allowed"])
        self.assertEqual(live_order["queries"]["open_algo_orders"]["state"], "unknown")
        flat = map_queries(_doc([{"symbol": "ETHUSDT", "positionAmt": "0"}]))
        self.assertTrue(flat["detail_proven"])
        self.assertTrue(flat["new_positions_allowed"])
        self.assertFalse(flat["symbol_listing_verified"])
        self.assertEqual(flat["symbol_check"], "market_adapter._symbol_ok")
        text = (ROOT / "docs" / "formal_receive_prompt_draft.md").read_text(encoding="utf-8")
        self.assertIn("receive-claim", text)
        self.assertIn("--owner", text)
        self.assertIn("--role expiry-supervisor", text)
        self.assertIn("不是 `exchangeInfo` 核對已上市", text)
