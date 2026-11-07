"""一輪整合驗收。不連交易所、不 POST。"""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from src.phase1.cycle import ack_cycle, run_cycle
from src.phase1.session import Store

TPE = timezone(timedelta(hours=8))
NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"


def _snap(**extra):
    base = {
        "status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0,
        "manage_error": False, "manage_complete": True, "limit_older_than_2h": False,
    }
    base.update(extra)
    return base


def _cand(ms=1762484400000):
    return {"symbol": "ETHUSDT", "setup": "布鹿流", "bar_open_ms": ms, "ladder": ["1"]}


class Http200:
    def notify(self, event):
        return {"status": "ok", "delivered": True, "http_status": 200, "processing_ack": True, "http_sent": True}


class Boom:
    def notify(self, event):
        raise RuntimeError("consumer down")


class CycleTest(unittest.TestCase):
    def _run(self, store, snap, prefilter, scan_id, manage_exit=0, notifier=None):
        return run_cycle(
            store, snap, prefilter, now=NOW, scan_id=scan_id,
            manage_exit=manage_exit, notifier=notifier,
        )

    def test_fresh_flat_and_no_new_candidate_skips_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            first = self._run(store, _snap(), {"candidates": [_cand()]}, "s1")
            self.assertTrue(first["cycle_needs_ai"])
            second = self._run(store, _snap(), {"candidates": [_cand()]}, "s2")
            self.assertFalse(second["cycle_needs_ai"])
            self.assertEqual(second["reason"], "quiet_flat_book")
            self.assertEqual(second["manage_argv"][-2:], ["manage", "--live"])
            self.assertNotIn("candidates", " ".join(second["manage_argv"]))

    def test_position_order_stale_and_failure_keep_ai(self):
        stale = (datetime.fromisoformat(NOW) - timedelta(hours=2)).isoformat()
        cases = [
            _snap(positions=1),
            _snap(orders=1),
            _snap(limit_older_than_2h=True),
            _snap(as_of=stale),
            _snap(status="unknown"),
            _snap(manage_complete=False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for index, snap in enumerate(cases):
                decision = self._run(store, snap, {"candidates": [_cand()]}, "k" + str(index))
                self.assertTrue(decision["cycle_needs_ai"], decision["reason"])
                self.assertEqual(decision["old_ai_poll"], "keep")
                self.assertFalse(decision["formal_channel"])

    def test_http_200_does_not_ack_and_failure_keeps_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            decision = self._run(store, _snap(positions=1), {"candidates": []}, "s1", notifier=Http200())
            key = "cycle:" + decision["evaluation_id"]
            self.assertEqual(store.state["outbox"][key]["status"], "pending")
            self.assertFalse(decision["sender"]["processing_ack"])
            self.assertFalse(ack_cycle(store, decision["evaluation_id"], processing_ack=False))
            self.assertEqual(store.state["outbox"][key]["status"], "pending")
            again = self._run(store, _snap(positions=1), {"candidates": []}, "s1", notifier=Boom())
            self.assertTrue(again["cycle_needs_ai"])
            self.assertEqual(store.state["outbox"][key]["status"], "pending")
            self.assertTrue(ack_cycle(store, decision["evaluation_id"], processing_ack=True))
            self.assertEqual(store.state["outbox"][key]["status"], "acked")

    def test_disconnect_does_not_claim_protection_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            decision = self._run(store, _snap(manage_complete=False), {"error": "timeout", "candidates": []}, "s1", manage_exit=1)
            self.assertTrue(decision["cycle_needs_ai"])
            self.assertFalse(decision["protection_complete"])


if __name__ == "__main__":
    unittest.main()
