"""狀態感知喚醒。不連交易所、不送 HTTP。"""
import unittest
from pathlib import Path
import tempfile

from src.phase1.session import Store
from src.phase1.wake_gate import decide

NOW = "2026-11-07T12:15:00+08:00"
AS_OF = "2026-11-07T12:14:30+08:00"


def _snap(**extra):
    base = {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0, "manage_error": False}
    base.update(extra)
    return base


def _cand():
    return {"symbol": "ETHUSDT", "setup": "布鹿流", "bar_open_ms": 1762484400000, "ladder": [1]}


class WakeGateTest(unittest.TestCase):
    def _run(self, store, snap, prefilter, scan_id, **kw):
        return decide(store, snap, prefilter, now=NOW, scan_id=scan_id, **kw)

    def test_only_fresh_flat_empty_skips_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            decision = self._run(store, _snap(), {"candidates": []}, "s1")
            self.assertFalse(decision["cycle_needs_ai"])
            self.assertEqual(decision["reason"], "quiet_flat_book")
            self.assertEqual(store.state.get("cycle_wake_count", 0), 0)
            self.assertTrue(decision["manage_independent"])
            self.assertEqual(decision["old_ai_poll"], "keep")
            self.assertFalse(decision["sender"]["http_sent"])

    def test_position_recheck_is_not_swallowed_by_same_candle(self):
        payload = {"candidates": [_cand()]}
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            first = self._run(store, _snap(positions=1), payload, "s1")
            second = self._run(store, _snap(positions=1), payload, "s2")
            self.assertTrue(first["cycle_needs_ai"])
            self.assertTrue(second["cycle_needs_ai"])
            self.assertNotEqual(first["evaluation_id"], second["evaluation_id"])
            self.assertEqual(first["candidate_fingerprints"], second["candidate_fingerprints"])
            self.assertEqual(store.state["cycle_wake_count"], 2)
            cycle_keys = [key for key in store.state["outbox"] if key.startswith("cycle:")]
            self.assertEqual(len(cycle_keys), 2)

    def test_any_order_or_stale_or_error_keeps_ai(self):
        cases = [
            _snap(orders=1),
            _snap(status="unknown", as_of=None),
            _snap(as_of="2026-11-07T10:00:00+08:00"),
            _snap(manage_error=True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for index, snap in enumerate(cases):
                decision = self._run(store, snap, {"candidates": []}, "e" + str(index))
                self.assertTrue(decision["cycle_needs_ai"], decision["reason"])
                self.assertEqual(decision["sender"]["status"], "blocked")
                self.assertEqual(decision["old_ai_poll"], "keep")

    def test_non_finite_market_numbers_keep_ai(self):
        from src.phase1.runner import adapt_prefilter
        snap = {
            "status": "ok", "as_of": AS_OF, "positions": 0, "open_orders": 0, "algo_orders": 0, "orders": 0,
            "manage_error": False, "status_trusted": True, "queries_ok": True,
            "queries": {"account": True, "open_orders": True, "open_algo_orders": True},
        }

        def book(last, **btc):
            payload = {"last": last}
            payload.update(btc)
            return {"call_ai": False, "n_candidates": 0, "candidates": [], "btc": payload}

        calls = []

        class Spy:
            def notify(self, event):
                calls.append(event)
                return {"status": "blocked", "delivered": False, "http_sent": False}

        rejected = (
            float("nan"),
            float("inf"),
            float("-inf"),
            0,
            -1,
            True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for index, last in enumerate(rejected):
                decision = self._run(store, snap, book(last), "nan" + str(index), notifier=Spy(), mac_gate=True)
                self.assertTrue(decision["cycle_needs_ai"], last)
                self.assertNotEqual(decision["reason"], "quiet_flat_book", last)
                self.assertEqual(decision["old_ai_poll"], "keep")
            sibling = self._run(store, snap, book(100000.0, chg24=float("nan")), "chg", notifier=Spy(), mac_gate=True)
            self.assertNotEqual(sibling["reason"], "quiet_flat_book")
            self.assertTrue(sibling["cycle_needs_ai"])
            self.assertEqual(len(calls), len(rejected) + 1)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            before = len(calls)
            kept = self._run(store, snap, book(100000.0, chg24=-0.4), "ok", notifier=Spy(), mac_gate=True)
            self.assertEqual(kept["reason"], "quiet_flat_book")
            self.assertFalse(kept["cycle_needs_ai"])
            self.assertEqual(len(calls), before)
            self.assertEqual(store.state["suppressed_quiet"][0]["evaluation_id"], "ok")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for token in ("NaN", "Infinity", "-Infinity"):
                text = '{"call_ai": false, "n_candidates": 0, "candidates": [], "btc": {"last": ' + token + '}}'
                parsed = adapt_prefilter(text, 0, None)
                self.assertTrue(parsed.get("error"), token)
                decision = self._run(store, snap, parsed, "json-" + token, notifier=Spy(), mac_gate=True)
                self.assertNotEqual(decision["reason"], "quiet_flat_book", token)
                self.assertTrue(decision["cycle_needs_ai"])
            overflow = adapt_prefilter('{"call_ai": false, "n_candidates": 0, "candidates": [], "btc": {"last": 1e309}}', 0, None)
            decision = self._run(store, snap, overflow, "overflow", notifier=Spy(), mac_gate=True)
            self.assertNotEqual(decision["reason"], "quiet_flat_book")
            self.assertTrue(decision["cycle_needs_ai"])

    def test_ack_without_recovery_does_not_stop_old_poll(self):
        class Ack:
            def notify(self, event):
                return {"status": "ok", "delivered": True, "processing_ack": True, "http_sent": False}

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            decision = self._run(store, _snap(positions=1), {"candidates": []}, "s1", notifier=Ack(), recovery_verified=False)
            self.assertEqual(decision["old_ai_poll"], "keep")
            self.assertTrue(decision["sender_blocked"])


if __name__ == "__main__":
    unittest.main()
