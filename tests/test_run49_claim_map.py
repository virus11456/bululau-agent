"""離線查詢映射與跨行程 claim。不發真 HTTP，不碰 Mac。"""
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.formal_receive import (
    EXPIRY_SUPERVISOR_ROLE,
    PARENT_JUDGMENT_DEADLINE,
    claim_parent_run,
    expire_parent,
    read_formal,
    receive_delivery,
    submit_parent_files,
)
from src.phase1.legacy_root import DISPATCH, KNOWN_LIVE_FLAT_SHA
from src.phase1.mac_query_map import map_queries
from src.phase1.production_transport import build_envelope, wake_document
from src.phase1.runner import main
from src.phase1.session import Store
from src.phase1.timeparse import parse_instant

NOW = "2026-11-07T12:20:00+08:00"
T0 = "2026-11-07T04:20:00Z"
EVENT = "c-" + "49" * 16
OWNER = "ef5f0556-2d5c-49a4-8b61-a4c5eef57780"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "mac_query_map"


def _shift(text, delta):
    moment = parse_instant(text) + delta
    return moment.astimezone(parse_instant(T0).tzinfo).strftime("%Y-%m-%dT%H:%M:%SZ")


def _delivery():
    envelope = build_envelope(
        {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": []},
        {"valid": True, "status": "fresh", "age_seconds": 0},
        event_id=EVENT, evaluation_id="s49", sent_at=NOW, reason="not_quiet",
    )
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    context = {
        "schema_version": 1, "event_type": "immutable_context", "event_id": EVENT, "evaluation_id": "s49",
        "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "49" * 16, "envelope": envelope,
        "automatic_transport": False,
    }
    document = {
        "schema_version": 1, "event_type": "evaluation_delivery",
        "wake": wake_document(envelope), "context": context,
    }
    return document, digest


def _pair(digest):
    judgment = {
        "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s49",
        "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": "not_quiet"},
        "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
    }
    trace = {"event_id": EVENT, "evaluation_id": "s49", "trace": "parent wrote this trace", "envelope_sha256": digest}
    return judgment, trace


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return code, json.loads(lines[-1]) if lines else {}, buf.getvalue()


class Run49ClaimMapTest(unittest.TestCase):
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

    def test_query_fixtures_protect_exposure_and_block_unknown(self):
        exposure = json.loads((FIXTURES / "exposure_unknown.json").read_text(encoding="utf-8"))
        mapped = map_queries(exposure)
        self.assertTrue(mapped["protection_required"])
        self.assertFalse(mapped["new_positions_allowed"])
        self.assertEqual(mapped["queries"]["account"]["state"], "success")
        self.assertEqual(mapped["queries"]["open_orders"]["state"], "unknown")
        self.assertEqual(mapped["queries"]["open_algo_orders"]["state"], "failure")
        self.assertFalse(mapped["live_equivalent"])
        self.assertFalse(mapped["mac_executed"])
        self.assertFalse(mapped["flock_taken"])
        self.assertFalse(mapped["state_bytes_read"])
        self.assertFalse(mapped["known_flat_sha_reverified"])
        self.assertEqual(mapped["known_flat_sha"], KNOWN_LIVE_FLAT_SHA)
        self.assertEqual(mapped["lock_boundary"]["covers"], list(DISPATCH))
        self.assertIn("manage", mapped["lock_boundary"]["covers"])
        self.assertFalse(mapped["quiet"])
        self.assertIn("live_trader_not_executed", mapped["not_equivalent"])
        unknown = map_queries(json.loads((FIXTURES / "all_unknown.json").read_text(encoding="utf-8")))
        self.assertFalse(unknown["new_positions_allowed"])
        self.assertFalse(unknown["protection_required"])
        flat = map_queries(json.loads((FIXTURES / "all_flat.json").read_text(encoding="utf-8")))
        self.assertTrue(flat["new_positions_allowed"])
        self.assertFalse(flat["candidate_allowed"])
        self.assertFalse(flat["quiet"])
        self.assertFalse(flat["live_equivalent"])
        fuzzy = map_queries({"account": {"state": "success", "positions": True}, "open_orders": {"state": "success", "count": 0}, "open_algo_orders": {"state": "success", "count": 0}})
        self.assertEqual(fuzzy["queries"]["account"]["state"], "unknown")
        self.assertFalse(fuzzy["new_positions_allowed"])
        with tempfile.TemporaryDirectory() as tmp:
            path = str(FIXTURES / "exposure_unknown.json")
            code, body, text = _run(["map-queries", "--file", path])
            self.assertEqual(code, 0)
            self.assertTrue(body["protection_required"])
            self.assertFalse(body["mac_executed"])
            secret = str(Path(tmp) / "secret.json")
            Path(secret).write_text(json.dumps({"api_key": "not-a-real-secret", "account": {"state": "unknown"}}), encoding="utf-8")
            code, body, text = _run(["map-queries", "--file", secret])
            self.assertEqual(code, 1)
            self.assertEqual(body["reason"], "secret_rejected")
            self.assertNotIn("not-a-real-secret", text)
        snap = (ROOT / "docs" / "current_live_routine_snapshot.md").read_text(encoding="utf-8")
        start = snap.index("```text\n") + len("```text\n")
        end = snap.index("\n```", start)
        digest = hashlib.sha256(snap[start:end].encode("utf-8")).hexdigest()
        self.assertEqual(digest, "db9366fd031789d95191090b2504f494469c03da29c92def64f6e46959080e61")

    def test_one_claim_starts_ai_and_other_owners_cannot_mutate(self):
        document, digest = _delivery()
        late = _shift(T0, PARENT_JUDGMENT_DEADLINE + timedelta(seconds=1))
        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            barrier = threading.Barrier(2)
            found = []

            def worker():
                barrier.wait(3)
                found.append(claim_parent_run(Store(tmp), EVENT, OWNER, judgment_clock=T0))

            threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertEqual(sum(item["ai_start_allowed"] is True for item in found), 1)
            self.assertTrue(all(item["owner_is_authentication"] is False for item in found))
            self.assertTrue(all(item["parent_called"] is False for item in found))
            again = claim_parent_run(Store(tmp), EVENT, OWNER, judgment_clock=T0)
            self.assertFalse(again["ai_start_allowed"])
            self.assertEqual(again["reason"], "restart_no_new_ai")
            self.assertTrue(again["may_submit_existing"])
            self.assertNotIn("context_path", again)
            with self.assertRaises(ValueError) as mismatch:
                claim_parent_run(Store(tmp), EVENT, OTHER, judgment_clock=T0)
            self.assertEqual(str(mismatch.exception), "owner_mismatch")
            judgment, trace = _pair(digest)
            submitted = submit_parent_files(
                Store(tmp), EVENT, judgment, trace, "2099-01-01T00:00:00Z", judgment_clock=T0, owner=OWNER,
            )
            self.assertEqual(submitted["status"], "publish_ready")
            body = Path(tmp, "formal-files", EVENT, "judgment.json").read_bytes()
            with self.assertRaises(ValueError) as wrong:
                submit_parent_files(Store(tmp), EVENT, {"replaced": True}, {"trace": "x"}, T0, judgment_clock=T0, owner=OTHER)
            self.assertEqual(str(wrong.exception), "owner_mismatch")
            self.assertEqual(Path(tmp, "formal-files", EVENT, "judgment.json").read_bytes(), body)
            Path(tmp, "j.json").write_text(json.dumps(judgment), encoding="utf-8")
            Path(tmp, "t.json").write_text(json.dumps(trace), encoding="utf-8")
            code, payload, _text = _run([
                "receive-submit", "--state-dir", tmp, "--event-id", EVENT,
                "--judgment", str(Path(tmp) / "j.json"), "--trace", str(Path(tmp) / "t.json"), "--now", T0,
            ])
            self.assertEqual(code, 1)
            self.assertEqual(payload["reason"], "owner_required")
            with self.assertRaises(ValueError) as early:
                expire_parent(Store(tmp), EVENT, judgment_clock=late)
            self.assertEqual(str(early.exception), "supervisor_required")
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "publish_ready")
            self.assertEqual(read_formal(Store(tmp), EVENT)["parent_claim"]["owner"], OWNER)

    def test_supervisor_expiry_does_not_regrant_ai_and_cli_requires_owner(self):
        document, _digest = _delivery()
        late = _shift(T0, PARENT_JUDGMENT_DEADLINE + timedelta(seconds=1))
        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            code, body, _text = _run(["receive-claim", "--state-dir", tmp, "--event-id", EVENT])
            self.assertEqual(code, 2)
            self.assertEqual(body["reason"], "owner_required")
            self.assertFalse(body["ai_start_allowed"])
            granted = claim_parent_run(Store(tmp), EVENT, OWNER, judgment_clock=T0)
            self.assertTrue(granted["ai_start_allowed"])
            self.assertTrue(granted["context_path"].endswith("context.json"))
            with self.assertRaises(ValueError):
                expire_parent(Store(tmp), EVENT, judgment_clock=late, role=OTHER)
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "pending")
            expired = expire_parent(Store(tmp), EVENT, judgment_clock=late, role=EXPIRY_SUPERVISOR_ROLE)
            self.assertEqual(expired["status"], "parent_deadline_expired")
            self.assertEqual(read_formal(Store(tmp), EVENT)["parent_claim"]["owner"], OWNER)
            follow = claim_parent_run(Store(tmp), EVENT, OWNER, judgment_clock=late)
            self.assertFalse(follow["ai_start_allowed"])
            self.assertFalse(follow["may_submit_existing"])
            code, gap, _text = _run(["fallback-gap", "--state-dir", tmp, "--event-id", EVENT])
            self.assertEqual(code, 0)
            self.assertIs(gap["fallback_ran"], False)
            self.assertIs(gap["alert_delivered"], False)
            self.assertIs(gap["cancel_api_used"], False)
            self.assertIs(gap["original_ai_invoked"], False)
        text = (ROOT / "docs" / "formal_receive_prompt_draft.md").read_text(encoding="utf-8")
        self.assertIn("receive-claim", text)
        self.assertIn("不是身分驗證", text)
        self.assertIn("automation-1794058224037", text)
        self.assertIn("不因這次拒絕被刪", text)
        self.assertIn("沒有取消 API", text)
        self.assertIn("expiry-supervisor", text)
        self.assertIn("paused", text)
