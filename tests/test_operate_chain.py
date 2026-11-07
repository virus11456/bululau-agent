"""整鏈只走 mock。不連 gateway、不產生判斷、不重跑舊的 12 項 gateway 測試。"""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

from src.phase1.ack_contract import apply_processed_ack, load_ack
from src.phase1.gateway_client import authorized_exchange, load_gateway_config
from src.phase1.production_transport import bind_envelope, build_envelope, commit_judgment, import_staged
from src.phase1.return_adapter import judgment_put
from src.phase1.review_context import build_review, fetch_review, parent_package_ready, plan_round
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
NOW_MS = 1762482000000
SENT = "2026-11-07T04:19:30Z"
EVENT = "c-" + "ab" * 16


def _snap(**extra):
    base = {
        "status": "ok", "as_of": AS_OF, "positions": 0, "open_orders": 0, "algo_orders": 0, "orders": 0,
        "manage_error": False, "status_trusted": True, "queries_ok": True,
        "queries": {"account": True, "open_orders": True, "open_algo_orders": True},
    }
    base.update(extra)
    return base


def _flat_scan():
    return {"call_ai": False, "n_candidates": 0, "candidates": [], "btc": {"last": 100000.0}}


class Spy:
    def __init__(self):
        self.calls = []

    def notify(self, event):
        self.calls.append(event)
        return {"status": "blocked", "delivered": False, "http_sent": False}


def _trial(approved=True):
    return {
        "enabled": True,
        "live_send": True,
        "base_url": "https://gm-provided.example",
        "installation_id": "inst-local-1",
        "key_id": "pub-key",
        "role": "publisher",
        "trial": {
            "approved": approved,
            "dry_run_only": True,
            "new_ids": 1,
            "max_wake_posts": 2,
            "max_judgment_puts": 1,
            "max_gets": 6,
            "deadline_seconds": 600,
        },
    }


class OperateChainTest(unittest.TestCase):
    def test_quiet_sends_no_wake_and_candidates_or_unknown_keep_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            spy = Spy()
            quiet = plan_round(store, _snap(), _flat_scan(), build_review(None, [], [], {}, [], NOW_MS), now=NOW, scan_id="q1", notifier=spy)
            self.assertTrue(quiet["quiet"])
            self.assertFalse(quiet["notifier_called"])
            self.assertEqual(spy.calls, [])
            self.assertIsNone(quiet["event_id"])
            self.assertFalse(quiet["discretion_complete"])
            self.assertFalse(quiet["second_schedule"])
            candidate = {"symbol": "ETHUSDT", "setup": "break", "bar_time": "2026-11-07T12:00:00+08:00", "plan": "watch", "stop": 1}
            review = build_review(None, [], [], {}, [candidate], NOW_MS)
            woken = plan_round(store, _snap(), dict(_flat_scan(), call_ai=True, n_candidates=1, candidates=[candidate]), review, now=NOW, scan_id="q2", notifier=spy)
            self.assertEqual(woken["reason"], "candidates_present")
            self.assertTrue(woken["notifier_called"])
            self.assertFalse(woken["context_sufficient"])
            self.assertEqual(woken["old_ai_poll"], "keep")
            self.assertEqual(len(spy.calls), 1)
            unknown = plan_round(store, _snap(status="unknown", queries_ok=False), _flat_scan(), review, now=NOW, scan_id="q3", notifier=spy)
            self.assertNotEqual(unknown["reason"], "quiet_flat_book")
            self.assertEqual(unknown["old_ai_poll"], "keep")
            held = {"positions": [{"symbol": "ETHUSDT", "positionAmt": "1", "entryPrice": "100", "totalWalletBalance": "9"}]}
            bare = build_review(held, [], [], {}, [], NOW_MS)
            self.assertFalse(bare["context_sufficient"])
            self.assertTrue(any(item.startswith("stop:") for item in bare["missing"]))
            self.assertNotIn("totalWalletBalance", json.dumps(bare))
            self.assertFalse(bare["balance_included"])
            self.assertEqual(fetch_review(None, None, [], NOW_MS)["missing"], ["fetcher_missing"])

    def test_disabled_gateway_does_not_send_and_authorized_mock_is_bounded(self):
        with open("gateway/client.example.json", encoding="utf-8") as handle:
            disabled = load_gateway_config(json.load(handle))
        self.assertFalse(disabled["enabled"])

        def boom(*_args):
            raise AssertionError("disabled transport was called")

        self.assertEqual(authorized_exchange(disabled, boom, "PUT", EVENT, {}, secret="x")["reason"], "gateway_disabled")
        config = load_gateway_config(_trial())
        calls = []

        def transport(method, path, headers, body):
            calls.append(method)
            if method == "GET":
                raise TimeoutError("timeout")
            return 401, {"Cache-Control": "private, no-store"}, b'{"reason":"unauthorized"}'

        denied = authorized_exchange(config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="publisher-secret")
        self.assertEqual(denied["status"], 401)
        self.assertNotIn("publisher-secret", json.dumps(denied))
        self.assertEqual(authorized_exchange(config, transport, "PUT", EVENT, {}, sent_count=1, secret="publisher-secret")["reason"], "put_budget_exhausted")
        self.assertEqual(len(calls), 1)
        timed = authorized_exchange(config, transport, "GET", EVENT, secret="reader-secret")
        self.assertEqual(timed["reason"], "timeout")
        self.assertEqual(timed["old_ai_poll"], "keep")

    def test_cross_language_digest_and_original_store_replay(self):
        envelope = build_envelope(
            {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
            {"candidates": []},
            {"valid": True, "status": "fresh", "age_seconds": 1},
            event_id=EVENT, evaluation_id="s1", sent_at=SENT, reason="orders_open",
        )
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        judgment = {
            "schema_version": 1,
            "event_type": "dry_run_judgment",
            "event_id": EVENT,
            "evaluation_id": "s1",
            "envelope_sha256": hashlib.sha256(raw).hexdigest(),
            "decision": "keep_original_ai",
            "basis": {
                "reason": "orders_open",
                "as_of": AS_OF,
                "positions": 0,
                "orders": 1,
                "freshness_status": "fresh",
                "candidate_count": 0,
            },
            "completed_at": "2026-11-07T04:22:00Z",
            "orders_placed": False,
            "ai_execution_proven": False,
        }
        wrapped = judgment_put("inst-local-1", EVENT, "s1", judgment["envelope_sha256"], judgment)
        script = (
            "import { canonical, sha256Hex } from './gateway/lib/contract.js';"
            "let raw=''; process.stdin.on('data', (c) => raw += c);"
            "process.stdin.on('end', () => { process.stdout.write(sha256Hex(canonical(JSON.parse(raw)))); });"
        )
        proc = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(judgment), capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout, wrapped["judgment_sha256"])
        trace = {"event_id": EVENT, "trace": "parent read the context and chose keep"}
        ready = parent_package_ready(judgment, trace)
        self.assertTrue(ready["ready"])
        self.assertFalse(ready["discretion_complete"])
        self.assertTrue(ready["keep_original_ai_is_not_a_fill"])
        self.assertEqual(parent_package_ready(judgment, None)["reason"], "parent_trace_missing")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.state["outbox"]["cycle:s1"] = {"event_id": EVENT, "evaluation_id": "s1", "status": "pending", "processing_ack": False}
            store.save()
            bind_envelope(store, envelope)
            path = os.path.join(tmp, "parent-judgment.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(judgment, handle)
            import_staged(tmp, path, "2026-11-07T04:22:00Z")
            processed = commit_judgment(tmp, EVENT, "s1", "2026-11-07T04:22:00Z")
            first = apply_processed_ack(Store(tmp), load_ack(processed), expected_event_id=EVENT, sent_at=SENT, now="2026-11-07T04:22:00Z")
            second = apply_processed_ack(Store(tmp), load_ack(processed), expected_event_id=EVENT, sent_at=SENT, now="2026-11-07T04:22:00Z")
            self.assertEqual(first["reason"], "processed")
            self.assertEqual(first["process_count"], 1)
            self.assertEqual(second["reason"], "duplicate")
            self.assertEqual(second["process_count"], 1)
            saved = Store(tmp)
            self.assertEqual(saved.state["outbox"]["cycle:s1"]["status"], "acked")
            self.assertFalse(os.path.exists(os.path.join(tmp, "judgments-in", EVENT + ".json")))


if __name__ == "__main__":
    unittest.main()
