"""整鏈只走 mock。不連 gateway、不產生判斷、不重跑舊的 12 項 gateway 測試。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from src.phase1.ack_contract import apply_processed_ack, load_ack
from src.phase1.gateway_client import authorized_exchange, load_gateway_config, provision_ledger
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
            "event_id": EVENT,
            "approved_at": 0,
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
        calls = []

        def transport(method, path, headers, body):
            calls.append((method, path))
            if method == "GET":
                raise TimeoutError("timeout")
            if method == "PUT":
                raise ValueError("SYNTHETIC_SECRET_MARKER")
            return 401, {"Cache-Control": "private, no-store"}, b'{"reason":"unauthorized"}'

        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "budget.json")
            publisher_doc = _trial()
            publisher_doc["ledger_path"] = ledger
            config = load_gateway_config(publisher_doc)
            provision_ledger(config)
            denied = authorized_exchange(config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="publisher-secret", ledger_path=ledger, now_epoch=100)
            self.assertNotIn("SYNTHETIC_SECRET_MARKER", json.dumps(denied))
            self.assertEqual(denied["reason"], "transport_failed")
            self.assertEqual(authorized_exchange(config, transport, "PUT", EVENT, {}, secret="publisher-secret", ledger_path=ledger, now_epoch=100)["reason"], "put_budget_exhausted")
            self.assertEqual(authorized_exchange(config, transport, "PUT", "c-" + "cd" * 16, {}, secret="publisher-secret", ledger_path=ledger, now_epoch=100)["reason"], "event_not_approved")
            self.assertEqual(len(calls), 1)
        with tempfile.TemporaryDirectory() as tmp:
            reader_doc = _trial()
            reader_doc["role"] = "reader"
            reader_doc["ledger_path"] = os.path.join(tmp, "gets.json")
            reader = load_gateway_config(reader_doc)
            provision_ledger(reader)
            timed = authorized_exchange(reader, transport, "GET", EVENT, secret="reader-secret", ledger_path=reader["ledger_path"], now_epoch=100)
        self.assertEqual(timed["reason"], "timeout")
        self.assertEqual(timed["old_ai_poll"], "keep")
        self.assertNotIn("SYNTHETIC_SECRET_MARKER", json.dumps(timed))

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
        trace = {"event_id": EVENT, "evaluation_id": "s1", "trace": "parent read the context and chose keep"}
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

    def test_budget_role_schema_and_get_does_not_apply_the_wrong_event(self):
        from src.phase1.gateway_client import apply_gateway_get, canonical_json
        from src.phase1.review_context import operate_round, read_parent_package
        bad = _trial()
        bad["ledger_path"] = "budget.json"
        bad["trial"]["max_judgment_puts"] = True
        with self.assertRaises(ValueError):
            load_gateway_config(bad)
        calls = []

        def transport(method, path, headers, body):
            calls.append(path)
            if path.endswith("huge"):
                return 200, {}, b"x" * 70000
            return 200, {}, b'{"qty": NaN}'

        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "budget.json")
            publisher_doc = _trial()
            publisher_doc["ledger_path"] = ledger
            config = load_gateway_config(publisher_doc)
            provision_ledger(config)
            rejected = authorized_exchange(config, transport, "PUT", "../other?x=y", {}, secret="s", ledger_path=ledger, now_epoch=50)
            self.assertEqual(rejected["reason"], "rejected_id")
            self.assertEqual(calls, [])
            huge_id = EVENT
            # NaN response is a failed attempt and consumes the only PUT.
            poisoned = authorized_exchange(config, transport, "PUT", huge_id, {"event_id": huge_id}, secret="s", ledger_path=ledger, now_epoch=50)
            self.assertEqual(poisoned["reason"], "non_finite")
            self.assertNotIn("NaN", json.dumps(poisoned))
            self.assertEqual(authorized_exchange(config, transport, "PUT", huge_id, {}, secret="s", ledger_path=ledger, now_epoch=50)["reason"], "put_budget_exhausted")
        empty = {"decision": "keep_original_ai", "ai_execution_proven": False, "orders_placed": False}
        self.assertFalse(parent_package_ready(empty, {"trace": "x"})["ready"])
        self.assertFalse(parent_package_ready({"event_id": None}, {"event_id": None, "trace": "x"})["ready"])
        account = {"positions": [{"symbol": "ETHUSDT", "positionAmt": "bad"}]}
        self.assertIn("bad_position", build_review(account, [], [], {}, [], NOW_MS)["missing"])
        queried = fetch_review(lambda _path: None, lambda _path: [], [], NOW_MS)
        self.assertFalse(queried["context_sufficient"])
        self.assertIn("query_failed", queried["missing"])
        sample = {"basis": {"qty": 1.0}, "event_id": EVENT}
        script = (
            "import { canonical, sha256Hex } from './gateway/lib/contract.js';"
            "let raw=''; process.stdin.on('data', (chunk) => raw += chunk);"
            "process.stdin.on('end', () => process.stdout.write(sha256Hex(canonical(JSON.parse(raw)))));"
        )
        proc = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(sample), capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout, hashlib.sha256(canonical_json(sample).encode("utf-8")).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            spy = Spy()
            quiet = operate_round(store, _snap(), _flat_scan(), now=NOW, scan_id="op1", notifier=spy, now_ms=NOW_MS)
            self.assertTrue(quiet["quiet"])
            self.assertTrue(quiet["fetch_review_called"])
            self.assertFalse(quiet["external_send"])
            self.assertFalse(quiet["wakes_saved"])
            self.assertEqual(spy.calls, [])
            snap_path = os.path.join(tmp, "snap.json")
            prefilter_path = os.path.join(tmp, "prefilter.json")
            with open(snap_path, "w", encoding="utf-8") as handle:
                json.dump(_snap(), handle)
            with open(prefilter_path, "w", encoding="utf-8") as handle:
                json.dump(_flat_scan(), handle)
            proc = subprocess.run(
                [sys.executable, "-m", "src.phase1.runner", "review-round", "--state-dir", tmp, "--now", NOW, "--scan-id", "op2", "--snapshot", snap_path, "--prefilter", prefilter_path],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads(proc.stdout)
            self.assertTrue(body["quiet"])
            self.assertFalse(body["external_send"])
            self.assertFalse(body["routine_stopped"])
            self.assertFalse(body["wakes_saved"])
        with tempfile.TemporaryDirectory() as tmp:
            producer = os.path.join(tmp, "producer")
            os.makedirs(producer)
            store = Store(producer)
            envelope = build_envelope(
                {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
                {"candidates": []},
                {"valid": True, "status": "fresh", "age_seconds": 1},
                event_id=EVENT, evaluation_id="s1", sent_at=SENT, reason="orders_open",
            )
            store.state["outbox"]["cycle:s1"] = {"event_id": EVENT, "evaluation_id": "s1", "status": "pending", "processing_ack": False}
            store.save()
            bind_envelope(store, envelope)
            wrong = "c-" + "ff" * 16
            payload = {"schema_version": 1, "event_type": "judgment_put", "event_id": wrong, "evaluation_id": "s1", "judgment": {"event_id": wrong, "evaluation_id": "s1"}}
            reader_doc = _trial()
            reader_doc["role"] = "reader"
            reader_doc["ledger_path"] = os.path.join(tmp, "gets.json")
            reader = load_gateway_config(reader_doc)
            provision_ledger(reader)

            def ok_wrong(method, path, headers, body):
                return 200, {"Cache-Control": "private, no-store"}, json.dumps(payload).encode("utf-8")

            result = apply_gateway_get(
                reader, ok_wrong, EVENT, producer, "s1", SENT, "2026-11-07T04:22:00Z",
                secret="reader-secret", ledger_path=reader["ledger_path"], now_epoch=100,
            )
            self.assertFalse(result["applied"])
            self.assertFalse(result["written"])
            self.assertEqual(Store(producer).state["outbox"]["cycle:s1"]["status"], "pending")
            self.assertFalse(os.path.exists(os.path.join(producer, "judgments", EVENT + ".json")))

    def test_parallel_puts_share_one_durable_slot(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "budget.json")
            publisher_doc = _trial()
            publisher_doc["ledger_path"] = ledger
            config = load_gateway_config(publisher_doc)
            provision_ledger(config)
            results = []

            def transport(method, path, headers, body):
                return 500, {}, b'{"reason":"no"}'

            def worker():
                results.append(authorized_exchange(
                    config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", ledger_path=ledger, now_epoch=10,
                ))

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(1 for item in results if item.get("sent")), 1)
            self.assertEqual(sum(1 for item in results if item.get("reason") == "put_budget_exhausted"), 2)
            again = authorized_exchange(config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", ledger_path=ledger, now_epoch=10)
            self.assertEqual(again["reason"], "put_budget_exhausted")


if __name__ == "__main__":
    unittest.main()
