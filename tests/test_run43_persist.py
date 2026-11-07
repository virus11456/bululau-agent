"""fallback 未完成不得封存；正式接收走完整 schema 與 Store。不發真 HTTP。"""
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.formal_receive import read_formal, receive_delivery, verify_formal_delivery
from src.phase1.orchestrator import run_orchestrator
from src.phase1.production_transport import build_envelope, wake_document
from src.phase1.runner import main
from src.phase1.session import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-07T12:20:00+08:00"
EVENT = "c-" + "cd" * 16


def _delivery(event_id=EVENT, evaluation_id="s43"):
    envelope = build_envelope(
        {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": []},
        {"valid": True, "status": "fresh", "age_seconds": 0},
        event_id=event_id, evaluation_id=evaluation_id, sent_at=NOW, reason="not_quiet",
    )
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    context = {
        "schema_version": 1, "event_type": "immutable_context", "event_id": event_id, "evaluation_id": evaluation_id,
        "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "cd" * 16, "envelope": envelope,
        "automatic_transport": False,
    }
    document = {"schema_version": 1, "event_type": "evaluation_delivery", "wake": wake_document(envelope), "context": context}
    return document, digest


class Run43PersistTest(unittest.TestCase):
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

    def test_unwired_restart_stays_pending_until_fallback_settles(self):
        script = (
            "import os, sys\n"
            "sys.path.insert(0, {root!r})\n"
            "from src.phase1.orchestrator import run_orchestrator\n"
            "from src.phase1.session import Store\n"
            "def manage(payload):\n"
            "    os._exit(3)\n"
            "run_orchestrator(Store({tmp!r}), now={now!r}, scan_id='s43dead', callbacks={{'manage': manage}})\n"
        )
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            child = subprocess.run([sys.executable, "-c", script.format(root=str(ROOT), tmp=tmp, now=NOW)], cwd=str(ROOT), check=False)
            self.assertEqual(child.returncode, 3)
            first = run_orchestrator(Store(tmp), now=NOW, scan_id="s43dead", callbacks={})
            self.assertEqual(first["reason"], "incomplete_restart")
            self.assertFalse(first["fallback_called"])
            self.assertEqual(Store(tmp).state["orchestrator_seen"]["s43dead"]["status"], "running")
            second = run_orchestrator(Store(tmp), now=NOW, scan_id="s43dead", callbacks={
                "fallback": lambda payload: calls.append("fallback") or {"ok": True},
                "manage": lambda payload: calls.append("manage") or {"ok": True},
            })
            self.assertEqual(calls, ["fallback"])
            self.assertTrue(second["fallback_called"])
            self.assertEqual(Store(tmp).state["orchestrator_seen"]["s43dead"]["status"], "recovered")
            third = run_orchestrator(Store(tmp), now=NOW, scan_id="s43dead", callbacks={
                "fallback": lambda payload: calls.append("fallback-again") or {"ok": True},
            })
            self.assertTrue(third["duplicate"])
            self.assertEqual(calls, ["fallback"])
            gate = threading.Event()
            release = threading.Event()

            def waiting(payload):
                calls.append("wait")
                gate.set()
                self.assertTrue(release.wait(3))
                return {"ok": True}

            def other():
                gate.wait(3)
                calls.append(run_orchestrator(Store(tmp), now=NOW, scan_id="s43live", callbacks={
                    "fallback": lambda payload: calls.append("other-fallback") or {"ok": True},
                })["reason"])
                release.set()

            # fresh crash-style running claim is created by the waiting fallback only after begin.
            # Hold the live claim on a new id by starting execute's manage.
            def manage_wait(payload):
                calls.append("live-manage")
                gate.set()
                self.assertTrue(release.wait(3))
                return {"ok": True, "trusted": True, "halted": False}

            worker = threading.Thread(target=other)
            worker.start()
            run_orchestrator(Store(tmp), now=NOW, scan_id="s43live", callbacks={
                "manage": manage_wait,
                "status": lambda payload: {"ok": True, "fresh": True, "status_trusted": True, "positions": 0, "open_orders": 0, "algo_orders": 0},
                "prefilter": lambda payload: {"ok": True, "candidates": [], "context_sufficient": True},
            })
            worker.join(5)
        self.assertIn("claim_held", calls)
        self.assertNotIn("other-fallback", calls)

    def test_snapshot_null_is_rejected_and_store_does_not_fake_ack(self):
        document, digest = _delivery()
        self.assertTrue(verify_formal_delivery(document, NOW)["accepted"])
        broken = json.loads(json.dumps(document))
        broken["context"]["envelope"]["snapshot"] = None
        broken["context"]["envelope_sha256"] = hashlib.sha256(
            canonical_json(broken["context"]["envelope"]).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(ValueError) as caught:
            verify_formal_delivery(broken, NOW)
        self.assertEqual(str(caught.exception), "rejected_context")
        wake = dict(document["wake"])
        wake["event_id"] = "c-" + "ef" * 16
        mismatched = dict(document, wake=wake)
        with self.assertRaises(ValueError):
            verify_formal_delivery(mismatched, NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "delivery.json")
            Path(path).write_text(json.dumps(document), encoding="utf-8")
            store = Store(tmp)
            first = receive_delivery(store, document, NOW)
            second = receive_delivery(store, document, NOW)
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["status"], "pending")
            self.assertFalse(first["ack"])
            self.assertFalse(second["parent_called"])
            self.assertEqual(read_formal(store, EVENT)["status"], "pending")
            code = main(["receive", "--state-dir", tmp, "--file", path, "--now", NOW])
            self.assertEqual(code, 0)
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "pending")
            calls = []

            def parent(payload):
                calls.append(payload["context"]["event_id"])
                body = {
                    "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s43",
                    "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": "not_quiet"},
                    "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
                }
                trace = {"event_id": EVENT, "evaluation_id": "s43", "trace": "parent wrote this trace", "envelope_sha256": digest}
                return {"judgment": body, "trace": trace}

            handed = receive_delivery(store, document, NOW, parent=parent)
            self.assertEqual(calls, [EVENT])
            self.assertEqual(read_formal(store, EVENT)["status"], "publish_ready")
            self.assertTrue(handed["ack"])
            self.assertFalse(handed["http_sent"])
            self.assertFalse(handed["program_fills_judgment"])
            self.assertFalse(handed["timeout_executor"])
            receive_delivery(store, document, NOW, parent=parent)
            self.assertEqual(calls, [EVENT])
