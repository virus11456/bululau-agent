"""正式 submit 要 claim，終止狀態不再開始 AI，明細蓋過 count。不發真 HTTP。"""
import hashlib
import io
import json
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.formal_receive import (
    PARENT_JUDGMENT_DEADLINE,
    claim_parent_run,
    expire_parent,
    read_formal,
    receive_delivery,
    submit_parent_files,
)
from src.phase1.mac_query_map import map_queries
from src.phase1.production_transport import build_envelope, wake_document
from src.phase1.runner import main
from src.phase1.session import Store
from src.phase1.timeparse import parse_instant

NOW = "2026-11-07T12:20:00+08:00"
T0 = "2026-11-07T04:20:00Z"
EVENT = "c-" + "50" * 16
OWNER = "ef5f0556-2d5c-49a4-8b61-a4c5eef57780"
ROOT = Path(__file__).resolve().parents[1]


def _shift(text, delta):
    moment = parse_instant(text) + delta
    return moment.astimezone(parse_instant(T0).tzinfo).strftime("%Y-%m-%dT%H:%M:%SZ")


def _delivery():
    envelope = build_envelope(
        {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": []},
        {"valid": True, "status": "fresh", "age_seconds": 0},
        event_id=EVENT, evaluation_id="s50", sent_at=NOW, reason="not_quiet",
    )
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    context = {
        "schema_version": 1, "event_type": "immutable_context", "event_id": EVENT, "evaluation_id": "s50",
        "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "50" * 16, "envelope": envelope,
        "automatic_transport": False,
    }
    document = {
        "schema_version": 1, "event_type": "evaluation_delivery",
        "wake": wake_document(envelope), "context": context,
    }
    return document, digest


def _pair(digest):
    judgment = {
        "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s50",
        "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": "not_quiet"},
        "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
    }
    trace = {"event_id": EVENT, "evaluation_id": "s50", "trace": "parent wrote this trace", "envelope_sha256": digest}
    return judgment, trace


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return code, json.loads(lines[-1]) if lines else {}


class Run50ClaimRequiredTest(unittest.TestCase):
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

    def test_formal_cli_submit_without_claim_is_rejected(self):
        document, digest = _delivery()
        judgment, trace = _pair(digest)
        with tempfile.TemporaryDirectory() as tmp:
            delivery = Path(tmp) / "delivery.json"
            delivery.write_text(json.dumps(document), encoding="utf-8")
            code, body = _run(["receive", "--state-dir", tmp, "--file", str(delivery), "--now", NOW])
            self.assertEqual(code, 0)
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "pending")
            self.assertTrue(read_formal(Store(tmp), EVENT)["claim_required"])
            judgment_path = Path(tmp) / "judgment.json"
            trace_path = Path(tmp) / "trace.json"
            judgment["isolated_test"] = True
            judgment_path.write_text(json.dumps(judgment), encoding="utf-8")
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            code, body = _run([
                "receive-submit", "--state-dir", tmp, "--event-id", EVENT,
                "--judgment", str(judgment_path), "--trace", str(trace_path), "--now", NOW,
            ])
            self.assertNotEqual(code, 0)
            self.assertEqual(body["reason"], "claim_required")
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "pending")
            code, body = _run([
                "receive-submit", "--state-dir", tmp, "--event-id", EVENT,
                "--judgment", str(judgment_path), "--trace", str(trace_path), "--now", NOW,
                "--owner", OWNER,
            ])
            self.assertEqual(code, 1)
            self.assertEqual(body["reason"], "claim_required")
            self.assertNotEqual(read_formal(Store(tmp), EVENT)["status"], "publish_ready")

    def test_ready_and_expired_cannot_start_ai(self):
        document, digest = _delivery()
        judgment, trace = _pair(digest)
        late = _shift(T0, PARENT_JUDGMENT_DEADLINE + timedelta(seconds=1))
        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            ready = submit_parent_files(
                Store(tmp), EVENT, judgment, trace, T0, judgment_clock=T0, isolated_test=True,
            )
            self.assertEqual(ready["status"], "publish_ready")
            body = Path(tmp, "formal-files", EVENT, "judgment.json").read_bytes()
            claimed = claim_parent_run(Store(tmp), EVENT, OWNER, judgment_clock=T0)
            self.assertFalse(claimed["ai_start_allowed"])
            self.assertEqual(claimed["reason"], "status_blocks_ai")
            self.assertFalse(claimed["owner_is_authentication"])
            self.assertIsNone(read_formal(Store(tmp), EVENT).get("parent_claim"))
            self.assertEqual(Path(tmp, "formal-files", EVENT, "judgment.json").read_bytes(), body)
            self.assertEqual(read_formal(Store(tmp), EVENT)["status"], "publish_ready")
        with tempfile.TemporaryDirectory() as tmp:
            receive_delivery(Store(tmp), document, NOW, judgment_clock=T0)
            expire_parent(Store(tmp), EVENT, judgment_clock=late)
            claimed = claim_parent_run(Store(tmp), EVENT, OWNER, judgment_clock=late)
            self.assertFalse(claimed["ai_start_allowed"])
            self.assertEqual(claimed["status"], "parent_deadline_expired")
            self.assertIsNone(read_formal(Store(tmp), EVENT).get("parent_claim"))

    def test_details_override_counts_and_bad_schema_blocks_new_positions(self):
        rejected = map_queries({
            "schema_version": True,
            "event_type": "not_the_detail_type",
            "account": {"state": "success", "positions": 0},
            "open_orders": {"state": "success", "count": 0},
            "open_algo_orders": {"state": "success", "count": 0},
        })
        self.assertFalse(rejected["new_positions_allowed"])
        self.assertEqual(rejected["reason"], "schema_rejected")
        self.assertFalse(rejected["flock_taken"])
        hidden = map_queries({
            "schema_version": 1,
            "event_type": "offline_query_details",
            "account": {
                "state": "success",
                "positions": [{"symbol": "ETHBTC", "positionAmt": "1.5"}],
                "count": 0,
            },
            "open_orders": [],
            "open_algo_orders": [],
        })
        self.assertTrue(hidden["protection_required"])
        self.assertFalse(hidden["new_positions_allowed"])
        self.assertEqual(hidden["queries"]["account"]["state"], "unknown")
        self.assertFalse(hidden["detail_proven"])
        flat = map_queries({
            "schema_version": 1,
            "event_type": "offline_query_details",
            "account": {"positions": [{"symbol": "ETHUSDT", "positionAmt": "0"}]},
            "open_orders": [],
            "open_algo_orders": [],
        })
        self.assertTrue(flat["detail_proven"])
        self.assertTrue(flat["new_positions_allowed"])
        self.assertFalse(flat["protection_required"])
        self.assertFalse(flat["live_equivalent"])
        self.assertFalse(flat["mac_executed"])
        self.assertIn("mac_lock_not_taken", flat["not_equivalent"])
        counted = map_queries({
            "account": {"state": "success", "positions": 0},
            "open_orders": {"state": "success", "count": 0},
            "open_algo_orders": {"state": "success", "count": 0},
        })
        self.assertTrue(counted["count_only"])
        self.assertFalse(counted["new_positions_allowed"])
        self.assertFalse(counted["detail_proven"])
        text = (ROOT / "docs" / "formal_receive_prompt_draft.md").read_text(encoding="utf-8")
        self.assertIn("不是已驗證的身分邊界", text)
        self.assertIn("取消能力這輪沒有驗證", text)
        self.assertNotIn("沒有取消 API", text)
