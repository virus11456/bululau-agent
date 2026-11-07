"""父判斷截止、活躍鎖與 payload 時鐘。不發真 HTTP。"""
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.formal_receive import (
    PARENT_JUDGMENT_DEADLINE,
    PLATFORM_PARENT_AI,
    expire_parent,
    formal_status,
    read_formal,
    receive_delivery,
    submit_parent_files,
)
from src.phase1.production_transport import build_envelope, wake_document
from src.phase1.runner import main
from src.phase1.session import Store
from src.phase1.timeparse import parse_instant

NOW = "2026-11-07T12:20:00+08:00"
T0 = "2026-11-07T04:20:00Z"
EVENT = "c-" + "47" * 16
ROOT = Path(__file__).resolve().parents[1]


def _shift(text, delta):
    moment = parse_instant(text) + delta
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _delivery():
    envelope = build_envelope(
        {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": []},
        {"valid": True, "status": "fresh", "age_seconds": 0},
        event_id=EVENT, evaluation_id="s47", sent_at=NOW, reason="not_quiet",
    )
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    context = {
        "schema_version": 1, "event_type": "immutable_context", "event_id": EVENT, "evaluation_id": "s47",
        "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "47" * 16, "envelope": envelope,
        "automatic_transport": False,
    }
    document = {"schema_version": 1, "event_type": "evaluation_delivery", "wake": wake_document(envelope), "context": context}
    return document, digest


def _pair(digest, note="parent wrote this trace"):
    judgment = {
        "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s47",
        "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": "not_quiet"},
        "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
    }
    trace = {"event_id": EVENT, "evaluation_id": "s47", "trace": note, "envelope_sha256": digest}
    return judgment, trace


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return code, json.loads(lines[-1]) if lines else None


class Run47ParentDeadlineTest(unittest.TestCase):
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

    def test_no_result_expires_without_extending_or_rewaking(self):
        document, digest = _delivery()
        calls = []
        late = _shift(T0, PARENT_JUDGMENT_DEADLINE + timedelta(seconds=1))
        later = _shift(late, timedelta(seconds=30))

        def parent(_payload):
            calls.append("parent")
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            first = receive_delivery(store, document, NOW, parent=parent, judgment_clock=T0)
            deadline = read_formal(store, EVENT)["judgment_deadline"]
            self.assertEqual(first["status"], "pending")
            self.assertEqual(deadline, _shift(T0, PARENT_JUDGMENT_DEADLINE))
            self.assertEqual(calls, ["parent"])
            looked = formal_status(store, EVENT, judgment_clock=late)
            self.assertTrue(looked["past_deadline"])
            self.assertFalse(looked["mutated"])
            self.assertFalse(looked["scheduler_present"])
            self.assertTrue(looked["expiry_requires_platform_trigger"])
            self.assertEqual(read_formal(store, EVENT)["status"], "pending")
            self.assertFalse(PLATFORM_PARENT_AI["wake_connected"])
            self.assertFalse(PLATFORM_PARENT_AI["original_ai_fallback_connected"])
            expired = expire_parent(store, EVENT, judgment_clock=late)
            self.assertEqual(expired["status"], "parent_deadline_expired")
            self.assertTrue(expired["mutated"])
            self.assertFalse(expired["platform_ai_process_killed"])
            self.assertFalse(expired["platform_parent_ai"]["desensitized_alert_delivery_connected"])
            self.assertFalse(expired["platform_parent_ai"]["bounded_deadline_trigger_connected"])
            again = expire_parent(Store(tmp), EVENT, judgment_clock=later)
            self.assertFalse(again["mutated"])
            self.assertEqual(read_formal(store, EVENT)["expired_at"], late)
            self.assertEqual(read_formal(store, EVENT)["judgment_deadline"], deadline)
            receive_delivery(Store(tmp), document, NOW, parent=parent, judgment_clock=later)
            self.assertEqual(calls, ["parent"])
            self.assertEqual(read_formal(Store(tmp), EVENT)["judgment_deadline"], deadline)
            judgment, trace = _pair(digest)
            with self.assertRaises(ValueError) as caught:
                submit_parent_files(Store(tmp), EVENT, judgment, trace, "2099-01-01T00:00:00Z", judgment_clock=later)
            self.assertEqual(str(caught.exception), "parent_deadline_expired")
            alert = json.loads(Path(tmp, "formal-alerts", EVENT + ".json").read_text(encoding="utf-8"))
            self.assertEqual(alert["alert_delivered"], False)
            self.assertEqual(alert["original_ai_fallback_called"], False)
            self.assertEqual(alert["platform_ai_process_killed"], False)
            self.assertEqual(alert["contains_context"], False)
            self.assertEqual(alert["contains_judgment"], False)
            self.assertEqual(alert["contains_secret"], False)
            self.assertNotIn("envelope", json.dumps(alert))
            self.assertNotIn(digest, json.dumps(alert))
            reopened = read_formal(Store(tmp), EVENT)
            self.assertEqual(reopened["status"], "parent_deadline_expired")
            self.assertEqual(reopened["judgment_deadline"], deadline)

    def test_active_lock_and_saved_result_stay_put(self):
        document, digest = _delivery()
        calls = []
        started = threading.Event()
        release = threading.Event()
        late = _shift(T0, PARENT_JUDGMENT_DEADLINE + timedelta(seconds=5))

        def parent(_payload):
            calls.append("parent")
            started.set()
            self.assertTrue(release.wait(3))
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            def other():
                started.wait(3)
                busy = receive_delivery(Store(tmp), document, NOW, parent=parent, judgment_clock=late)
                held = expire_parent(Store(tmp), EVENT, judgment_clock=late)
                other.busy = busy
                other.held = held
                release.set()

            worker = threading.Thread(target=other)
            worker.start()
            receive_delivery(Store(tmp), document, NOW, parent=parent, judgment_clock=T0)
            worker.join(5)
            self.assertTrue(other.busy["claim_held"])
            self.assertEqual(other.held["reason"], "claim_held")
            self.assertFalse(other.held["mutated"])
            self.assertEqual(calls, ["parent"])
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "pending")
            self.assertFalse(Path(tmp, "formal-alerts", EVENT + ".json").exists())
            judgment, trace = _pair(digest)
            early = _shift(T0, timedelta(seconds=1))
            submitted = submit_parent_files(Store(tmp), EVENT, judgment, trace, "2099-01-01T00:00:00Z", judgment_clock=early)
            self.assertEqual(submitted["status"], "publish_ready")
            path = Path(tmp, "formal-files", EVENT, "judgment.json")
            body = path.read_bytes()
            expire_parent(Store(tmp), EVENT, judgment_clock=late)
            with self.assertRaises(ValueError):
                submit_parent_files(
                    Store(tmp), EVENT, {"replaced": True}, {"trace": "other"}, "2000-01-01T00:00:00Z", judgment_clock=late,
                )
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "publish_ready")

    def test_exact_deadline_still_accepts_and_the_next_second_expires(self):
        document, digest = _delivery()
        judgment, trace = _pair(digest)
        edge = _shift(T0, PARENT_JUDGMENT_DEADLINE)
        past = _shift(T0, PARENT_JUDGMENT_DEADLINE + timedelta(seconds=1))
        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            result = submit_parent_files(Store(tmp), EVENT, judgment, trace, edge, judgment_clock=edge)
            self.assertEqual(result["status"], "publish_ready")
        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            with self.assertRaises(ValueError) as caught:
                submit_parent_files(Store(tmp), EVENT, judgment, trace, edge, judgment_clock=past)
            self.assertEqual(str(caught.exception), "parent_deadline_expired")
            self.assertEqual(read_formal(Store(tmp), EVENT)["judgment_deadline"], edge)

    def test_production_cli_uses_system_clock_and_rejects_payload_now(self):
        document, digest = _delivery()
        judgment, trace = _pair(digest)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "delivery.json")
            Path(path).write_text(json.dumps(document), encoding="utf-8")
            code, _body = _run(["receive", "--state-dir", tmp, "--file", path, "--now", NOW])
            self.assertEqual(code, 0)
            row = read_formal(Store(tmp), EVENT)
            self.assertEqual(row["judgment_clock_source"], "system")
            self.assertNotEqual(row["judgment_deadline"], _shift(T0, PARENT_JUDGMENT_DEADLINE))
            skew = abs((parse_instant(row["judgment_deadline"]) - (datetime.now(timezone.utc) + PARENT_JUDGMENT_DEADLINE)).total_seconds())
            self.assertLess(skew, 5)
            code, body = _run(["receive-expire", "--state-dir", tmp, "--event-id", EVENT, "--now", "2099-01-01T00:00:00Z"])
            self.assertEqual(code, 1)
            self.assertEqual(body["reason"], "payload_clock_rejected")
            self.assertFalse(body["mutated"])
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "pending")
            code, body = _run(["receive-status", "--state-dir", tmp, "--event-id", EVENT, "--now", T0])
            self.assertEqual(code, 1)
            self.assertEqual(body["reason"], "payload_clock_rejected")
            judgment_path = Path(tmp) / "judgment.json"
            trace_path = Path(tmp) / "trace.json"
            judgment_path.write_text(json.dumps(judgment), encoding="utf-8")
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            code, body = _run([
                "receive-submit", "--state-dir", tmp, "--event-id", EVENT,
                "--judgment", str(judgment_path), "--trace", str(trace_path),
                "--now", "2099-01-01T00:00:00Z",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(body["status"], "publish_ready")
            self.assertEqual(read_formal(Store(tmp), EVENT)["judgment_deadline"], row["judgment_deadline"])
        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=_shift(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), timedelta(minutes=-16)))
            deadline = read_formal(Store(tmp), EVENT)["judgment_deadline"]
            judgment_path = Path(tmp) / "judgment.json"
            trace_path = Path(tmp) / "trace.json"
            judgment_path.write_text(json.dumps(judgment), encoding="utf-8")
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            code, body = _run([
                "receive-submit", "--state-dir", tmp, "--event-id", EVENT,
                "--judgment", str(judgment_path), "--trace", str(trace_path),
                "--now", T0,
            ])
            self.assertEqual(code, 1)
            self.assertEqual(body["reason"], "parent_deadline_expired")
            self.assertEqual(read_formal(Store(tmp), EVENT)["judgment_deadline"], deadline)
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "parent_deadline_expired")

    def test_prompt_states_the_unconnected_parent_branches(self):
        text = (ROOT / "docs" / "formal_receive_prompt_draft.md").read_text(encoding="utf-8")
        self.assertIn("不是平台父 AI", text)
        self.assertIn("本地 argv", text)
        self.assertIn("活躍 flock", text)
        self.assertIn("parent_deadline_expired", text)
        self.assertIn("receive-expire", text)
        self.assertIn("receive-status", text)
        self.assertIn("不影響舊的 manage", text)
        self.assertIn("60 秒 watch", text)
        self.assertIn("沒有 scheduler", text)
        self.assertIn("原 AI 回退", text)
        self.assertNotIn("https://", text.split("## 下一步未接的平台父 AI")[1])
