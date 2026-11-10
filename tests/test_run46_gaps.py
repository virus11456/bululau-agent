"""外部 CLI 結果封裝、啟動失敗、離線讀回、布林 schema。不發真 HTTP。"""
import hashlib
import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.formal_receive import receive_delivery, submit_parent_files, verify_formal_delivery
from src.phase1.orchestrator import run_orchestrator
from src.phase1.production_transport import build_envelope, wake_document
from src.phase1.runner import main
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
PY = sys.executable
EVENT = "c-" + "ef" * 16
ROOT = Path(__file__).resolve().parents[1]


def _write(path, body):
    Path(path).write_text(body, encoding="utf-8")


def _cli(path, payload):
    _write(path, "import json,sys\nsys.stdout.write(json.dumps(%r))\n" % (payload,))


class Run46GapTest(unittest.TestCase):
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

    def test_external_cli_success_uses_the_same_value_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            manage = os_path(tmp, "manage.py")
            status = os_path(tmp, "status.py")
            prefilter = os_path(tmp, "prefilter.py")
            _cli(manage, {"ok": True, "trusted": True, "halted": False})
            _cli(status, {
                "ok": True, "fresh": True, "status_trusted": True,
                "positions": 0, "open_orders": 0, "algo_orders": 0,
            })
            _cli(prefilter, {"ok": True, "candidates": [], "context_sufficient": True})
            calls = []
            result = run_orchestrator(
                Store(tmp), now=NOW, scan_id="s46quiet", deadline_seconds=5,
                callbacks={
                    "manage": {"argv": [PY, manage], "deadline_seconds": 5},
                    "status": {"argv": [PY, status], "deadline_seconds": 5},
                    "prefilter": {"argv": [PY, prefilter], "deadline_seconds": 5},
                    "fallback": lambda payload: calls.append("fallback") or {"ok": True},
                },
            )
        self.assertTrue(result["quiet"])
        self.assertEqual(calls, [])
        self.assertEqual(result["order"], ["manage", "status", "prefilter"])

    def test_spawn_failure_falls_back_instead_of_escaping(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            result = run_orchestrator(
                Store(tmp), now=NOW, scan_id="s46miss", deadline_seconds=5,
                callbacks={
                    "manage": {"argv": [os_path(tmp, "missing-bin")], "deadline_seconds": 5},
                    "status": lambda payload: {
                        "ok": True, "fresh": True, "status_trusted": True,
                        "positions": 0, "open_orders": 0, "algo_orders": 0,
                    },
                    "prefilter": lambda payload: {"ok": True, "candidates": [], "context_sufficient": True},
                    "fallback": lambda payload: calls.append("fallback") or {"ok": True},
                },
            )
        self.assertEqual(calls, ["fallback"])
        self.assertTrue(result["fallback_called"])
        self.assertFalse(result["quiet"])

    def test_readback_uses_store_files_and_missing_files_fail(self):
        envelope = build_envelope(
            {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
            {"candidates": []},
            {"valid": True, "status": "fresh", "age_seconds": 0},
            event_id=EVENT, evaluation_id="s46", sent_at=NOW, reason="not_quiet",
        )
        digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
        context = {
            "schema_version": 1, "event_type": "immutable_context", "event_id": EVENT, "evaluation_id": "s46",
            "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "ef" * 16, "envelope": envelope,
            "automatic_transport": False,
        }
        document = {"schema_version": 1, "event_type": "evaluation_delivery", "wake": wake_document(envelope), "context": context}
        judgment = {
            "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s46",
            "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": "not_quiet"},
            "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
        }
        trace = {"event_id": EVENT, "evaluation_id": "s46", "trace": "parent wrote this trace", "envelope_sha256": digest}
        with tempfile.TemporaryDirectory() as tmp:
            missing = main(["receive-readback", "--state-dir", tmp, "--event-id", EVENT])
            self.assertNotEqual(missing, 0)
            receive_delivery(Store(tmp), document, NOW)
            submit_parent_files(Store(tmp), EVENT, judgment, trace, NOW, isolated_test=True)
            code = main(["receive-readback", "--state-dir", tmp, "--event-id", EVENT])
            self.assertEqual(code, 0)

    def test_boolean_schema_version_is_rejected_and_package_has_no_trade_route(self):
        text = (ROOT / "docs" / "formal_receive_prompt_draft.md").read_text(encoding="utf-8")
        self.assertIn("不具交易路由資格", text)
        self.assertIn("receive-readback", text)
        self.assertNotIn("gateway_client put", text.split("不要用")[0])
        envelope = build_envelope(
            {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
            {"candidates": []},
            {"valid": True, "status": "fresh", "age_seconds": 0},
            event_id=EVENT, evaluation_id="s46b", sent_at=NOW, reason="not_quiet",
        )
        envelope["schema_version"] = True
        envelope["review_context"]["schema_version"] = True
        digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
        context = {
            "schema_version": True, "event_type": "immutable_context", "event_id": EVENT, "evaluation_id": "s46b",
            "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "ef" * 16, "envelope": envelope,
            "automatic_transport": False,
        }
        document = {
            "schema_version": True, "event_type": "evaluation_delivery",
            "wake": dict(wake_document(envelope), schema_version=True), "context": context,
        }
        with self.assertRaises(ValueError):
            verify_formal_delivery(document, NOW)
        document["schema_version"] = 1
        with self.assertRaises(ValueError):
            verify_formal_delivery(document, NOW)


def os_path(tmp, name):
    return str(Path(tmp) / name)
