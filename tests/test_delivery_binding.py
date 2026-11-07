"""wake 與 envelope 逐欄綁定。篡改任一欄都拒絕，不改既有事件的時間或摘要。"""
import copy
import hashlib
import json
import unittest

from src.phase1.box_receiver import accept_delivery
from datetime import datetime, timedelta, timezone

from src.phase1.ack_contract import MAX_ACK_AGE
from src.phase1.production_transport import WAKE_FIELDS, _outside_window, build_envelope, wake_document

NOW = "2026-11-07T04:22:00Z"
SENT = "2026-11-07T04:19:30Z"


def _context_from_envelope(envelope):
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "event_type": "immutable_context",
        "event_id": envelope["event_id"],
        "evaluation_id": envelope["evaluation_id"],
        "envelope_sha256": hashlib.sha256(raw).hexdigest(),
        "sent_at": envelope["sent_at"],
        "producer_store_id": "ab" * 16,
        "envelope": envelope,
        "automatic_transport": False,
    }


def _delivery(envelope, wake):
    return {"schema_version": 1, "event_type": "evaluation_delivery", "wake": wake, "context": _context_from_envelope(envelope)}


class DeliveryBindingTest(unittest.TestCase):
    def setUp(self):
        self.envelope = build_envelope(
            {"status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
            {"candidates": []},
            {"valid": True, "status": "fresh", "age_seconds": 1},
            event_id="c-" + "cd" * 16, evaluation_id="synthetic-local", sent_at=SENT, reason="orders_open",
        )

    def test_dispatch_copies_channel_flags_instead_of_forcing_false(self):
        marked = copy.deepcopy(self.envelope)
        marked["test_channel"] = True
        wake = wake_document(marked)
        self.assertEqual(set(wake), set(WAKE_FIELDS))
        self.assertIs(wake["test_channel"], True)
        self.assertIs(wake["position_channel"], False)

    def test_receiver_rejects_wake_false_when_envelope_says_true(self):
        marked = copy.deepcopy(self.envelope)
        marked["test_channel"] = True
        wake = wake_document(self.envelope)
        self.assertIs(wake["test_channel"], False)
        with self.assertRaises(ValueError) as caught:
            accept_delivery(_delivery(marked, wake), NOW, "/tmp/does-not-write")
        self.assertEqual(str(caught.exception), "binding_mismatch")

    def test_each_wake_field_tamper_is_rejected(self):
        wake = wake_document(self.envelope)
        document = _delivery(self.envelope, wake)
        replacements = {
            "schema_version": 2,
            "event_type": "evaluation_wake_extra",
            "event_id": "c-" + "ef" * 16,
            "evaluation_id": "other-eval",
            "sent_at": "2026-11-07T04:21:00Z",
            "reason": "positions_open",
            "test_channel": True,
            "position_channel": True,
        }
        for key, value in replacements.items():
            tampered = copy.deepcopy(document)
            tampered["wake"][key] = value
            with self.assertRaises(ValueError) as caught:
                accept_delivery(tampered, NOW, "/tmp/does-not-write")
            self.assertIn(str(caught.exception), ("binding_mismatch", "rejected_wake_fields"), key)
        extra = copy.deepcopy(document)
        extra["wake"]["ninth"] = False
        with self.assertRaises(ValueError) as caught:
            accept_delivery(extra, NOW, "/tmp/does-not-write")
        self.assertEqual(str(caught.exception), "rejected_wake_fields")

    def test_missing_channel_is_not_treated_as_false(self):
        unknown = copy.deepcopy(self.envelope)
        unknown.pop("test_channel")
        with self.assertRaises(ValueError) as caught:
            wake_document(unknown)
        self.assertEqual(str(caught.exception), "rejected_channel")

    def test_ack_window_is_fifteen_minutes(self):
        self.assertEqual(MAX_ACK_AGE, timedelta(minutes=15))
        sent = datetime(2026, 11, 7, 4, 19, 30, tzinfo=timezone.utc)
        self.assertFalse(_outside_window(sent, "2026-11-07T04:34:30Z"))
        self.assertTrue(_outside_window(sent, "2026-11-07T04:34:31Z"))
        self.assertFalse(_outside_window(sent, "2026-11-07T04:18:30Z"))
        self.assertTrue(_outside_window(sent, "2026-11-07T04:18:29Z"))

    def test_candidate_review_fields_survive_and_balances_do_not(self):
        envelope = build_envelope(
            {"status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
            {"candidates": [{
                "symbol": "ETHUSDT", "setup": "布鹿流", "bar_open_ms": 1762484400000,
                "ladder": [100.0, 99.5], "stop": 97.5, "target": 110.0, "plan": "exit 1.5R",
                "totalWalletBalance": "1029.79",
            }]},
            {"valid": True, "status": "fresh", "age_seconds": 1},
            event_id="c-" + "ef" * 16, evaluation_id="review-local", sent_at=SENT, reason="candidates_present",
        )
        row = envelope["candidates"][0]
        self.assertEqual(row["stop"], 97.5)
        self.assertEqual(row["target"], 110.0)
        self.assertEqual(row["plan"], "exit 1.5R")
        self.assertNotIn("totalWalletBalance", row)
        self.assertEqual(envelope["snapshot"]["positions"], 0)
        self.assertNotIn("symbol", envelope["snapshot"])

    def test_build_delivery_matches_its_own_envelope(self):
        # 這個測試不讀 Mac 上那筆舊事件，也不改它的 sent_at。
        self.assertNotEqual(self.envelope["event_id"], "c-15a60b2092e54c0a92e7fc28423b8559")


if __name__ == "__main__":
    unittest.main()
