"""pending 補交、claim 衝突與完整 publisher 契約。不發真 HTTP。"""
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.formal_receive import (
    _formal_paths,
    _offline_publisher_config,
    read_formal,
    read_parent_files,
    receive_delivery,
    submit_parent_files,
)
from src.phase1.gateway_client import publisher_document
from src.phase1.production_transport import build_envelope, wake_document
from src.phase1.runner import main
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
EVENT = "c-" + "ab" * 16


def _delivery(evaluation_id="s44"):
    envelope = build_envelope(
        {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": []},
        {"valid": True, "status": "fresh", "age_seconds": 0},
        event_id=EVENT, evaluation_id=evaluation_id, sent_at=NOW, reason="not_quiet",
    )
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    context = {
        "schema_version": 1, "event_type": "immutable_context", "event_id": EVENT, "evaluation_id": evaluation_id,
        "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "ab" * 16, "envelope": envelope,
        "automatic_transport": False,
    }
    document = {"schema_version": 1, "event_type": "evaluation_delivery", "wake": wake_document(envelope), "context": context}
    return document, digest


def _full_pair(digest, evaluation_id="s44"):
    judgment = {
        "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": evaluation_id,
        "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": "not_quiet"},
        "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
    }
    trace = {"event_id": EVENT, "evaluation_id": evaluation_id, "trace": "parent wrote this trace", "envelope_sha256": digest}
    return judgment, trace


class Run44SubmitTest(unittest.TestCase):
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

    def test_empty_parent_stays_pending_until_file_submit(self):
        document, digest = _delivery()
        calls = []

        def parent(payload):
            calls.append("parent")
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            first = receive_delivery(store, document, NOW, parent=parent)
            self.assertEqual(calls, ["parent"])
            self.assertEqual(first["status"], "pending")
            self.assertFalse(first["ack"])
            self.assertTrue(read_formal(store, EVENT)["parent_invoked"])
            again = receive_delivery(Store(tmp), document, NOW, parent=parent)
            self.assertEqual(calls, ["parent"])
            self.assertTrue(again["duplicate"])
            self.assertEqual(again["status"], "pending")
            judgment, trace = _full_pair(digest)
            judgment_path = Path(tmp) / "parent-judgment.json"
            trace_path = Path(tmp) / "parent-trace.json"
            judgment_path.write_text(json.dumps(judgment), encoding="utf-8")
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            # run50 起正式 CLI 要先有持久 claim。舊 parent 已呼叫過，claim 只登記 owner，不再喚 parent。
            self.assertEqual(main(["receive-claim", "--state-dir", tmp, "--event-id", EVENT, "--owner", "parent-44"]), 0)
            code = main([
                "receive-submit", "--state-dir", tmp, "--event-id", EVENT,
                "--judgment", str(judgment_path), "--trace", str(trace_path), "--now", NOW,
                "--owner", "parent-44",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(calls, ["parent"])
            reopened = Store(tmp)
            self.assertEqual(read_formal(reopened, EVENT)["status"], "publish_ready")
            files = read_parent_files(reopened, EVENT)
            self.assertEqual(files["judgment"]["decision"], "keep_original_ai")
            self.assertEqual(files["trace"]["trace"], "parent wrote this trace")
            paths = _formal_paths(reopened.root, EVENT)
            put = publisher_document(_offline_publisher_config(), EVENT, paths["judgment"], paths["trace"], paths["context"])
            self.assertEqual(put["event_type"], "judgment_put")
            self.assertEqual(put["event_id"], EVENT)
            self.assertFalse(put.get("http_sent") is True)

    def test_busy_claim_rejects_a_different_binding(self):
        document, _digest = _delivery("s44")
        other, _other_digest = _delivery("s44other")
        started = threading.Event()
        release = threading.Event()

        def parent(payload):
            started.set()
            self.assertTrue(release.wait(3))
            return {}

        conflict = {}
        with tempfile.TemporaryDirectory() as tmp:
            def second():
                started.wait(3)
                try:
                    receive_delivery(Store(tmp), other, NOW)
                    conflict["raised"] = False
                except ValueError as exc:
                    conflict["raised"] = str(exc)
                release.set()

            worker = threading.Thread(target=second)
            worker.start()
            receive_delivery(Store(tmp), document, NOW, parent=parent)
            worker.join(5)
            with self.assertRaises(ValueError) as caught:
                receive_delivery(Store(tmp), other, NOW)
        self.assertEqual(conflict["raised"], "immutable_conflict")
        self.assertEqual(str(caught.exception), "immutable_conflict")

    def test_thin_parent_fixture_is_not_ready_and_is_not_stored(self):
        document, digest = _delivery()
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            receive_delivery(store, document, NOW)
            thin = {
                "event_id": EVENT, "evaluation_id": "s44", "envelope_sha256": digest,
                "decision": "keep_original_ai", "orders_placed": False, "ai_execution_proven": False,
            }
            trace = {"event_id": EVENT, "evaluation_id": "s44", "trace": "parent wrote this trace", "envelope_sha256": digest}
            result = submit_parent_files(store, EVENT, thin, trace, NOW, isolated_test=True)
            self.assertFalse(result["ack"])
            self.assertEqual(read_formal(store, EVENT)["status"], "pending")
            self.assertIsNone(read_parent_files(Store(tmp), EVENT))
