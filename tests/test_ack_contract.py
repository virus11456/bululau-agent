"""處理 ACK。discover 可找到。不 POST。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from src.phase1.ack_contract import KNOWN_TEST_EVENT, apply_processed_ack, load_ack, note_accepted, open_store, write_processed_file
from src.phase1.ack_store import AckStore

SENT = "2026-11-07T05:08:13Z"
NOW = "2026-11-07T05:16:00Z"
EVAL = "s1"


def _processed(event_id=KNOWN_TEST_EVENT, when="2026-11-07T05:10:00Z", evaluation=EVAL):
    return {"kind": "processed", "event_id": event_id, "evaluation_id": evaluation, "processed_at": when}


def _store(tmp):
    store = open_store(tmp)
    store.state["outbox"]["cycle:" + EVAL] = {
        "status": "pending", "processing_ack": False, "event_class": "evaluation_cycle",
        "event_id": KNOWN_TEST_EVENT, "evaluation_id": EVAL,
    }
    store.save()
    return store


class AckContractTest(unittest.TestCase):
    def test_accepted_is_not_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            result = note_accepted(store, {"kind": "accepted", "event_id": KNOWN_TEST_EVENT, "schema_valid": True, "status": "accepted"})
            self.assertEqual(result["reason"], "accepted_is_not_processed")
            self.assertEqual(store.state["outbox"]["cycle:" + EVAL]["status"], "pending")
            with self.assertRaises(ValueError):
                write_processed_file(tmp, {"kind": "accepted", "event_id": KNOWN_TEST_EVENT, "schema_valid": True, "status": "accepted"})

    def test_too_early_expired_wrong_id_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            cases = [
                (_processed(when="2026-11-07T05:00:00Z"), "too_early"),
                (_processed(when="2026-11-07T06:00:00Z"), "expired_or_future"),
                (_processed(event_id="other-id"), "wrong_event"),
                (_processed(evaluation="missing"), "binding_mismatch"),
                ({"kind": "processed", "event_id": "../x", "evaluation_id": EVAL, "processed_at": "2026-11-07T05:10:00Z"}, "rejected_id"),
                ({"kind": "processed", "event_id": KNOWN_TEST_EVENT, "evaluation_id": "a/b", "processed_at": "2026-11-07T05:10:00Z"}, "rejected_id"),
            ]
            for document, reason in cases:
                result = apply_processed_ack(store, document, expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
                self.assertEqual(result["reason"], reason, document)
                self.assertTrue(result["pending"])
                self.assertEqual(store.state["outbox"]["cycle:" + EVAL]["status"], "pending")
                self.assertEqual(result["old_ai_poll"], "keep")

    def test_resend_restart_and_store_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store._fail = True
            failed = apply_processed_ack(store, _processed(), expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(failed["reason"], "store_failed")
            self.assertEqual(AckStore(tmp).state["outbox"]["cycle:" + EVAL]["status"], "pending")
            store = _store(tmp)
            first = apply_processed_ack(store, _processed(), expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(first["process_count"], 1)
            second = apply_processed_ack(store, _processed(), expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(second["reason"], "duplicate")
            self.assertEqual(second["process_count"], 1)
            reloaded = AckStore(tmp)
            self.assertEqual(reloaded.state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)
            self.assertEqual(reloaded.state["outbox"]["cycle:" + EVAL]["status"], "acked")

    def test_partial_tmp_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            junk = os.path.join(tmp, "ack_state.json.tmp")
            with open(junk, "w", encoding="utf-8") as handle:
                handle.write("{")
            reloaded = AckStore(tmp)
            self.assertEqual(reloaded.state["outbox"]["cycle:" + EVAL]["status"], "pending")
            path = write_processed_file(tmp, _processed())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(load_ack(path)["kind"], "processed")
            self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
