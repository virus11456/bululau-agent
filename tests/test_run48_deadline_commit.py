"""告警補建與提交前再核時。不發真 HTTP。"""
import hashlib
import json
import tempfile
import unittest
import urllib.request
from datetime import timedelta
from pathlib import Path

import src.phase1.formal_receive as formal
from src.phase1.canonical import canonical_json
from src.phase1.formal_receive import (
    PARENT_JUDGMENT_DEADLINE,
    expire_parent,
    read_formal,
    read_parent_files,
    receive_delivery,
    submit_parent_files,
)
from src.phase1.production_transport import build_envelope, wake_document
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
T0 = "2026-11-07T04:20:00Z"
EVENT = "c-" + "48" * 16
ROOT = Path(__file__).resolve().parents[1]


def _shift(text, delta):
    from src.phase1.timeparse import parse_instant
    from datetime import timezone
    moment = parse_instant(text) + delta
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _delivery():
    envelope = build_envelope(
        {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": []},
        {"valid": True, "status": "fresh", "age_seconds": 0},
        event_id=EVENT, evaluation_id="s48", sent_at=NOW, reason="not_quiet",
    )
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    context = {
        "schema_version": 1, "event_type": "immutable_context", "event_id": EVENT, "evaluation_id": "s48",
        "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "48" * 16, "envelope": envelope,
        "automatic_transport": False,
    }
    document = {
        "schema_version": 1, "event_type": "evaluation_delivery",
        "wake": wake_document(envelope), "context": context,
    }
    return document, digest


def _pair(digest, note="parent wrote this trace"):
    judgment = {
        "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s48",
        "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": note},
        "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
    }
    trace = {"event_id": EVENT, "evaluation_id": "s48", "trace": note, "envelope_sha256": digest}
    return judgment, trace


class Run48DeadlineCommitTest(unittest.TestCase):
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

    def test_expired_row_rebuilds_a_local_alert_after_a_failed_write(self):
        document, _digest = _delivery()
        late = _shift(T0, PARENT_JUDGMENT_DEADLINE + timedelta(seconds=1))
        real_write = formal._write_json

        def fail_alert(path, body):
            if "formal-alerts" in path:
                raise OSError("alert_write_failed")
            return real_write(path, body)

        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            formal._write_json = fail_alert
            try:
                with self.assertRaises(OSError):
                    expire_parent(Store(tmp), EVENT, judgment_clock=late)
            finally:
                formal._write_json = real_write
            row = read_formal(Store(tmp), EVENT)
            self.assertEqual(row["status"], "parent_deadline_expired")
            self.assertIsNone(row.get("local_alert"))
            self.assertFalse(Path(tmp, "formal-alerts", EVENT + ".json").exists())
            expire_parent(Store(tmp), EVENT, judgment_clock=late)
            repaired = read_formal(Store(tmp), EVENT)
            alert_path = Path(tmp, "formal-alerts", EVENT + ".json")
            self.assertEqual(repaired["local_alert"], str(Path("formal-alerts") / (EVENT + ".json")))
            self.assertTrue(alert_path.is_file())
            first = alert_path.read_bytes()
            body = json.loads(first.decode("utf-8"))
            self.assertIs(body["alert_delivered"], False)
            self.assertIs(body["original_ai_fallback_called"], False)
            self.assertIs(body["platform_ai_process_killed"], False)
            self.assertIs(body["scheduler_present"], False)
            self.assertIs(body["http_sent"], False)
            expire_parent(Store(tmp), EVENT, judgment_clock=_shift(late, timedelta(seconds=30)))
            self.assertEqual(alert_path.read_bytes(), first)
            self.assertEqual(read_formal(Store(tmp), EVENT)["expired_at"], late)

    def test_clock_is_sampled_again_before_the_ready_commit(self):
        document, digest = _delivery()
        edge = _shift(T0, PARENT_JUDGMENT_DEADLINE)
        past = _shift(edge, timedelta(seconds=1))
        judgment, trace = _pair(digest)
        ticks = [edge, past]

        def clock():
            return ticks.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            with self.assertRaises(ValueError) as caught:
                submit_parent_files(Store(tmp), EVENT, judgment, trace, "2099-01-01T00:00:00Z", judgment_clock=clock, isolated_test=True)
            self.assertEqual(str(caught.exception), "parent_deadline_expired")
            self.assertEqual(ticks, [])
            stored = read_formal(Store(tmp), EVENT)
            self.assertEqual(stored["status"], "parent_deadline_expired")
            self.assertNotEqual(stored["status"], "publish_ready")
            self.assertIsNone(read_parent_files(Store(tmp), EVENT))
            self.assertTrue(Path(tmp, "formal-files", EVENT, "context.json").is_file())
            self.assertFalse(Path(tmp, "formal-files", EVENT, "judgment.json").exists())
            self.assertFalse(Path(tmp, "formal-files", EVENT, "trace.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            ready = submit_parent_files(Store(tmp), EVENT, judgment, trace, edge, judgment_clock=edge, isolated_test=True)
            self.assertEqual(ready["status"], "publish_ready")
            path = Path(tmp, "formal-files", EVENT, "judgment.json")
            body = path.read_bytes()
            other, other_trace = _pair(digest, "replaced-body")
            kept = submit_parent_files(Store(tmp), EVENT, other, other_trace, past, judgment_clock=edge, isolated_test=True)
            self.assertEqual(kept["status"], "publish_ready")
            self.assertEqual(path.read_bytes(), body)
            self.assertNotIn(b"replaced-body", body)
        text = (ROOT / "docs" / "formal_receive_prompt_draft.md").read_text(encoding="utf-8")
        self.assertIn("不可再補", text)
        self.assertIn("publish_ready` 之前", text)
        self.assertIn("不能當可發布結果", text)
        self.assertIn("耗時為零", text)
        self.assertIn("alert_delivered", text)
