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
