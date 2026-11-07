"""處理 ACK 與重啟。不讀設定、不 POST。"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.phase1.ack_contract import KNOWN_TEST_EVENT, apply_processed_ack, load_ack, note_accepted, write_processed_file
from src.phase1.session import Store

SENT = "2026-11-07T05:08:13Z"
NOW = "2026-11-07T05:16:00Z"
EVAL = "cycle:s1"


def _processed(event_id=KNOWN_TEST_EVENT, when="2026-11-07T05:10:00Z", evaluation=EVAL):
    return {"kind": "processed", "event_id": event_id, "evaluation_id": evaluation, "processed_at": when}


def _store(tmp):
    store = Store(Path(tmp))
    store.state["outbox"]["cycle:" + EVAL] = {"status": "pending", "processing_ack": False, "event_class": "evaluation_cycle"}
    store.save()
    return store


class AckContractTest(unittest.TestCase):
    def test_known_accepted_receipt_is_not_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            result = note_accepted(store, {
                "kind": "accepted", "event_id": KNOWN_TEST_EVENT, "schema_valid": True, "status": "accepted",
            })
            self.assertFalse(result["applied"])
            self.assertEqual(result["reason"], "accepted_is_not_processed")
            self.assertEqual(store.state["outbox"]["cycle:" + EVAL]["status"], "pending")
            self.assertEqual(result["old_ai_poll"], "keep")

    def test_duplicate_crash_restart_and_bad_acks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            first = apply_processed_ack(store, _processed(), expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
            self.assertTrue(first["applied"])
            self.assertEqual(first["process_count"], 1)
            again = apply_processed_ack(store, _processed(), expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(again["reason"], "duplicate")
            self.assertEqual(again["process_count"], 1)
            reloaded = Store(Path(tmp))
            self.assertEqual(reloaded.state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)
            self.assertEqual(reloaded.state["outbox"]["cycle:" + EVAL]["status"], "acked")

    def test_fail_safe_rejects_without_clearing_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            cases = [
                (_processed(event_id="wrong"), "wrong_event"),
                (_processed(when="2026-11-07T05:00:00Z"), "too_early"),
                (_processed(when="2026-11-07T06:00:00Z"), "expired_or_future"),
                (_processed(evaluation="cycle:nope"), "unknown_event"),
                ({"kind": "processed", "event_id": KNOWN_TEST_EVENT}, "rejected_ack_fields"),
            ]
            for document, reason in cases:
                result = apply_processed_ack(store, document, expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
                self.assertEqual(result["reason"], reason)
                self.assertTrue(result["pending"])
                self.assertEqual(store.state["outbox"]["cycle:" + EVAL]["status"], "pending")
                self.assertEqual(result["old_ai_poll"], "keep")

    def test_atomic_file_survives_only_after_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = _processed()
            path = write_processed_file(tmp, document)
            loaded = load_ack(path)
            self.assertEqual(loaded["kind"], "processed")
            self.assertEqual(stat_mode(path), 0o600)
            partial = path + ".tmp"
            self.assertFalse(os.path.exists(partial))
            leftover = os.path.join(tmp, "processed-" + KNOWN_TEST_EVENT + ".json.tmp")
            with open(leftover, "w", encoding="utf-8") as handle:
                handle.write("{")
            self.assertFalse(os.path.exists(path + ".missing"))
            store = _store(os.path.join(tmp, "still"))
            self.assertEqual(store.state["outbox"]["cycle:" + EVAL]["status"], "pending")


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
