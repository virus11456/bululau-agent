"""省 token 閘門。不跑全市場，不送 HTTP。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.phase1.session import Store
from src.phase1.token_switch import apply_prefilter

STARTED = "2026-11-07T10:55:00+08:00"


def _candidate(**extra):
    base = {
        "symbol": "SYNTHUSDT",
        "setup": "瘸師弟",
        "bar_open_ms": 1762484400000,
        "ladder": [100.0, 99.5],
    }
    base.update(extra)
    return base


class TokenSwitchTest(unittest.TestCase):
    def _apply(self, store, payload, scan_id):
        return apply_prefilter(
            store, payload, scan_id=scan_id, started_at=STARTED, completed_at=STARTED,
        )

    def test_empty_scan_does_not_wake_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            decision = self._apply(store, {"candidates": [], "call_ai": False}, "scan-empty")
            self.assertEqual(decision["exit_code"], 0)
            self.assertFalse(decision["ai_wake"])
            self.assertFalse(decision["http_sent"])
            self.assertEqual(store.state["ai_wake_count"], 0)
            self.assertEqual(store.state["snapshot"]["candidates"], [])

    def test_same_candidate_wakes_once(self):
        payload = {"candidates": [_candidate()], "call_ai": True}
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            first = self._apply(store, payload, "scan-1")
            second = self._apply(store, payload, "scan-2")
            self.assertEqual(first["exit_code"], 10)
            self.assertTrue(first["ai_wake"])
            self.assertEqual(first["notify"]["status"], "blocked")
            self.assertFalse(first["http_sent"])
            self.assertEqual(second["exit_code"], 0)
            self.assertFalse(second["ai_wake"])
            self.assertEqual(store.state["ai_wake_count"], 1)
            self.assertEqual(len(store.state["outbox"]), 1)
            self.assertEqual(next(iter(store.state["outbox"].values()))["status"], "pending")

    def test_new_bar_wakes_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self._apply(store, {"candidates": [_candidate()]}, "scan-1")
            decision = self._apply(
                store,
                {"candidates": [_candidate(bar_open_ms=1762485300000)]},
                "scan-2",
            )
            self.assertEqual(decision["exit_code"], 10)
            self.assertEqual(store.state["ai_wake_count"], 2)

    def test_failed_scan_does_not_look_like_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self._apply(store, {"candidates": [_candidate()]}, "scan-1")
            before = store.state["snapshot"]["scan_id"]
            decision = self._apply(store, {"error": "rate_limited", "candidates": []}, "scan-fail")
            self.assertEqual(decision["exit_code"], 2)
            self.assertFalse(decision["ai_wake"])
            self.assertEqual(store.state["snapshot"]["scan_id"], before)

    def test_blocked_or_failed_shape_is_not_a_candidate(self):
        payload = {"candidates": [
            {"symbol": "SYNTHUSDT", "setup": "布鹿流", "fail": "rr 1.1", "bar_open_ms": 1},
            {"symbol": "BTCUSDT", "setup": "瘸師弟", "blocked_by_btc_guard": True, "bar_open_ms": 1},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            decision = self._apply(store, payload, "scan-none")
            self.assertEqual(decision["exit_code"], 0)
            self.assertEqual(store.state["ai_wake_count"], 0)

    def test_missing_bar_time_is_not_a_silent_empty(self):
        payload = {"candidates": [{"symbol": "SYNTHUSDT", "setup": "瘸師弟", "ladder": [1]}]}
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self._apply(store, {"candidates": [_candidate()]}, "scan-1")
            before = store.state["snapshot"]["scan_id"]
            decision = self._apply(store, payload, "scan-bad")
            self.assertEqual(decision["exit_code"], 2)
            self.assertEqual(decision["reason"], "cannot_dedup_without_bar_time")
            self.assertEqual(store.state["snapshot"]["scan_id"], before)


if __name__ == "__main__":
    unittest.main()
