"""run33 新失敗。不重跑已通過的舊套件。不連真 gateway。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from src.phase1.canonical import canonical_json
from src.phase1.gateway_client import (
    LEDGER_LIMITS,
    apply_gateway_get,
    authorized_exchange,
    gateway_exchange,
    load_gateway_config,
    main as gateway_main,
    provision_ledger,
)
from src.phase1.market_adapter import load_entry_config, run_review_entry, source_gap
from src.phase1.production_transport import bind_envelope, build_envelope
from src.phase1.return_adapter import judgment_put
from src.phase1.review_context import parent_package_ready, read_parent_package
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
NOW_MS = 1762482000000
SENT = "2026-11-07T04:19:30Z"
EVENT = "c-" + "ab" * 16
INSTALL = "inst-local-1"


def _trial(role="publisher", ledger_path=""):
    return {
        "enabled": True,
        "live_send": True,
        "base_url": "https://bululau-return-isolated-8qi96wkvs-virus11456s-projects.vercel.app",
        "installation_id": INSTALL,
        "key_id": "pub-key",
        "role": role,
        "ledger_path": ledger_path,
        "trial": {
            "approved": True,
            "dry_run_only": True,
            "event_id": EVENT,
            "approved_at": 1_000,
            "new_ids": 1,
            "max_wake_posts": 2,
            "max_judgment_puts": 1,
            "max_gets": 6,
            "deadline_seconds": 600,
        },
    }


def _config(root, name, role="publisher"):
    doc = _trial(role, os.path.join(root, name))
    config = load_gateway_config(doc)
    provision_ledger(config)
    return config


def _bars(open_time):
    return [[open_time, "1", "2", "0.5", "1.5", "9"]]


class NodeCanon:
    def digest(self, document):
        script = (
            "import { canonical, sha256Hex } from './gateway/lib/contract.js';"
            "let raw=''; process.stdin.on('data', (chunk) => raw += chunk);"
            "process.stdin.on('end', () => process.stdout.write(sha256Hex(canonical(JSON.parse(raw)))));"
        )
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=json.dumps(document), capture_output=True, text=True, check=True,
        )
        return proc.stdout

    def text(self, document):
        script = (
            "import { canonical } from './gateway/lib/contract.js';"
            "let raw=''; process.stdin.on('data', (chunk) => raw += chunk);"
            "process.stdin.on('end', () => process.stdout.write(canonical(JSON.parse(raw))));"
        )
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=json.dumps(document), capture_output=True, text=True, check=True,
        )
        return proc.stdout


class Run33GateTest(unittest.TestCase):
    def test_ledger_path_stage_empty_and_crash_do_not_restart(self):
        self.assertIn("disk_admin_delete_then_provision_resets_counts", LEDGER_LIMITS)

        def transport(method, path, headers, body):
            return 500, {}, b'{"reason":"no"}'

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, "budget.json")
            other = os.path.join(tmp, "other.json")
            moved = authorized_exchange(
                config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", ledger_path=other, now_epoch=1_010,
            )
            self.assertEqual(moved["reason"], "ledger_path_rejected")
            self.assertFalse(os.path.exists(other))
            with open(config["ledger_path"], encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["judgment_put"], 0)
            staged = authorized_exchange(
                config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", now_epoch=1_010, stage="get",
            )
            self.assertEqual(staged["reason"], "stage_rejected")
            with open(config["ledger_path"], encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["get"], 0)
            self.assertEqual(state["judgment_put"], 0)
            self.assertEqual(state["approved_at"], 1_000)
            with open(config["ledger_path"], "wb") as handle:
                handle.write(b"")
            empty = authorized_exchange(config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", now_epoch=1_010)
            self.assertEqual(empty["reason"], "ledger_corrupt")
            self.assertEqual(open(config["ledger_path"], "rb").read(), b"")
            os.remove(config["ledger_path"])
            missing = authorized_exchange(config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", now_epoch=1_900)
            self.assertEqual(missing["reason"], "ledger_missing")
            self.assertFalse(os.path.exists(config["ledger_path"]))

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, "budget.json")
            original = open(config["ledger_path"], "rb").read()
            import src.phase1.gateway_client as gateway_client
            real_replace = os.replace

            def boom(src, dst):
                raise OSError("interrupted")

            os.replace = boom
            try:
                crashed = authorized_exchange(
                    config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", now_epoch=1_010,
                )
            finally:
                os.replace = real_replace
            self.assertEqual(crashed["reason"], "timeout")
            self.assertEqual(open(config["ledger_path"], "rb").read(), original)
            expired = authorized_exchange(
                config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", now_epoch=1_000 + 601,
            )
            self.assertEqual(expired["reason"], "deadline_exceeded")
            with open(config["ledger_path"], encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["approved_at"], 1_000)

    def test_canonical_matches_node_and_judgment_put_accepts_qty_float(self):
        node = NodeCanon()
        sample = {"a": 1.0, "b": 1e-7, "c": 1e-6, "d": 1e20, "e": 1e21, "f": 65537}
        self.assertEqual(canonical_json(sample), node.text(sample))
        self.assertRaises(ValueError, canonical_json, {"n": 2 ** 53 + 1})
        keyed = {"\uffff": 1, "\U0001F600": 2, "b": 3}
        self.assertEqual(canonical_json(keyed), node.text(keyed))
        judgment = {
            "schema_version": 1,
            "event_type": "dry_run_judgment",
            "event_id": EVENT,
            "evaluation_id": "s1",
            "envelope_sha256": "ab" * 32,
            "decision": "keep_original_ai",
            "basis": {"qty": 1.0, "reason": "orders_open"},
            "completed_at": "2026-11-07T04:22:00Z",
            "orders_placed": False,
            "ai_execution_proven": False,
        }
        wrapped = judgment_put(INSTALL, EVENT, "s1", judgment["envelope_sha256"], judgment)
        self.assertEqual(wrapped["judgment_sha256"], node.digest(judgment))
        self.assertNotIn("1.0", canonical_json(judgment))

        def transport(method, path, headers, body):
            return 201, {"Cache-Control": "private, no-store"}, body

        result = gateway_exchange(transport, "PUT", INSTALL, EVENT, "pub-key", "secret", wrapped)
        self.assertEqual(result["status"], 201)

    def test_outer_wrapper_must_match_before_store_and_written_follows_ack(self):
        envelope = build_envelope(
            {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
            {"candidates": []},
            {"valid": True, "status": "fresh", "age_seconds": 1},
            event_id=EVENT, evaluation_id="s1", sent_at=SENT, reason="orders_open",
        )
        from src.phase1.production_transport import _canonical
        judgment = {
            "schema_version": 1,
            "event_type": "dry_run_judgment",
            "event_id": EVENT,
            "evaluation_id": "s1",
            "envelope_sha256": __import__("hashlib").sha256(_canonical(envelope)).hexdigest(),
            "decision": "keep_original_ai",
            "basis": {
                "reason": "orders_open", "as_of": AS_OF, "positions": 0, "orders": 1,
                "freshness_status": "fresh", "candidate_count": 0,
            },
            "completed_at": "2026-11-07T04:22:00Z",
            "orders_placed": False,
            "ai_execution_proven": False,
        }
        good = judgment_put(INSTALL, EVENT, "s1", judgment["envelope_sha256"], judgment)
        bad = dict(good)
        bad["installation_id"] = "OTHER"
        bad["event_id"] = "OTHER"
        with tempfile.TemporaryDirectory() as tmp:
            producer = os.path.join(tmp, "producer")
            os.makedirs(producer)
            store = Store(producer)
            store.state["outbox"]["cycle:s1"] = {
                "event_id": EVENT, "evaluation_id": "s1", "status": "pending", "processing_ack": False,
            }
            store.save()
            bind_envelope(store, envelope)
            reader = _config(tmp, "gets.json", role="reader")

            def wrong(method, path, headers, body):
                self.assertIn("/" + INSTALL + "/", path)
                self.assertTrue(path.endswith("/" + EVENT))
                return 200, {"Cache-Control": "private, no-store"}, json.dumps(bad).encode("utf-8")

            rejected = apply_gateway_get(
                reader, wrong, EVENT, producer, "s1", SENT, "2026-11-07T04:22:00Z",
                secret="reader-secret", now_epoch=1_010,
            )
            self.assertFalse(rejected["applied"])
            self.assertFalse(rejected["written"])
            self.assertEqual(rejected["reason"], "binding_mismatch")
            self.assertEqual(Store(producer).state["outbox"]["cycle:s1"]["status"], "pending")

            def ok(method, path, headers, body):
                return 200, {"Cache-Control": "private, no-store"}, json.dumps(good).encode("utf-8")

            applied = apply_gateway_get(
                reader, ok, EVENT, producer, "s1", SENT, "2026-11-07T04:22:00Z",
                secret="reader-secret", now_epoch=1_020,
            )
            self.assertTrue(applied["applied"])
            self.assertTrue(applied["written"])
            self.assertEqual(applied["process_count"], 1)
            self.assertEqual(Store(producer).state["outbox"]["cycle:s1"]["status"], "acked")

    def test_trace_digest_and_publisher_gate_block_send(self):
        judgment = {
            "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s1",
            "envelope_sha256": "cd" * 32, "decision": "keep_original_ai", "basis": {"reason": "orders_open"},
            "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
        }
        trace = {"event_id": EVENT, "evaluation_id": "s1", "trace": "parent wrote this", "envelope_sha256": "00" * 32}
        self.assertFalse(parent_package_ready(judgment, trace)["ready"])
        context = {"event_id": EVENT, "evaluation_id": "s1", "envelope_sha256": judgment["envelope_sha256"], "envelope": {"event_id": EVENT}}
        self.assertEqual(parent_package_ready(judgment, trace, context)["reason"], "digest_mismatch")
        with tempfile.TemporaryDirectory() as tmp:
            judgment_path = os.path.join(tmp, "judgment.json")
            trace_path = os.path.join(tmp, "trace.json")
            context_path = os.path.join(tmp, "context.json")
            with open(judgment_path, "w", encoding="utf-8") as handle:
                json.dump(judgment, handle)
            with open(trace_path, "w", encoding="utf-8") as handle:
                json.dump(trace, handle)
            with open(context_path, "w", encoding="utf-8") as handle:
                json.dump(context, handle)
            self.assertFalse(read_parent_package(context, judgment_path, trace_path)["ready"])
            config_path = os.path.join(tmp, "client.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(_trial("publisher", os.path.join(tmp, "budget.json")), handle)
            import src.phase1.gateway_client as gateway_client

            def explode(*_args):
                raise AssertionError("publisher sent before the parent gate")

            gateway_client.https_exchange = explode
            code = gateway_main([
                "gateway_client", "put", "--send", "--config", config_path, "--event-id", EVENT,
                "--file", judgment_path, "--trace", trace_path, "--context", context_path,
            ])
            self.assertEqual(code, 1)

    def test_overflow_number_is_not_sent(self):
        calls = []

        def transport(method, path, headers, body):
            calls.append(path)
            return 200, {"Cache-Control": "private, no-store"}, b'{"x":1e309}'

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, "budget.json")
            result = authorized_exchange(config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", now_epoch=1_010)
            self.assertFalse(result["sent"])
            self.assertEqual(result["reason"], "non_finite")
            self.assertNotIn("Infinity", json.dumps(result))
            again = authorized_exchange(config, transport, "PUT", EVENT, {"event_id": EVENT}, secret="s", now_epoch=1_011)
            self.assertEqual(again["reason"], "put_budget_exhausted")
            self.assertEqual(len(calls), 1)

    def test_review_round_uses_real_clock_and_mock_adapter_keeps_ai(self):
        with open("gateway/client.example.json", encoding="utf-8") as handle:
            example = json.load(handle)
        loaded = load_gateway_config(example)
        self.assertFalse(loaded["enabled"])
        self.assertFalse(loaded["trial"]["approved"])
        self.assertEqual(loaded["base_url"], "https://bululau-return-isolated-8qi96wkvs-virus11456s-projects.vercel.app")
        self.assertEqual(loaded["installation_id"], "")
        gap = source_gap()
        self.assertFalse(gap["mac_flat_source_absent"]["wired"])
        self.assertIn("_signed_get", gap["repo_private"])
        with open("config/review_entry.example.json", encoding="utf-8") as handle:
            entry = load_entry_config(json.load(handle))
        self.assertFalse(entry["enabled"])
        self.assertFalse(entry["live_fetch"])
        self.assertFalse(entry["replaces_scan_entry"])
        with tempfile.TemporaryDirectory() as tmp:
            snap = {
                "status": "ok", "as_of": AS_OF, "positions": 0, "open_orders": 0, "algo_orders": 0, "orders": 0,
                "manage_error": False, "status_trusted": True, "queries_ok": True,
                "queries": {"account": True, "open_orders": True, "open_algo_orders": True},
            }
            scan = {"call_ai": False, "n_candidates": 0, "candidates": [], "btc": {"last": 100000.0}}
            snap_path = os.path.join(tmp, "snap.json")
            scan_path = os.path.join(tmp, "scan.json")
            with open(snap_path, "w", encoding="utf-8") as handle:
                json.dump(snap, handle)
            with open(scan_path, "w", encoding="utf-8") as handle:
                json.dump(scan, handle)
            proc = subprocess.run(
                [sys.executable, "-m", "src.phase1.runner", "review-round", "--state-dir", tmp, "--now", NOW,
                 "--scan-id", "op-clock", "--snapshot", snap_path, "--prefilter", scan_path],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads(proc.stdout)
            self.assertTrue(body["quiet"])
            self.assertNotEqual(body["now_ms"], 0)
            self.assertGreater(body["now_ms"], 1_000_000_000_000)
            self.assertFalse(body["wakes_saved"])
            self.assertFalse(body["routine_replaced"])
            self.assertFalse(body["live_fetch"])
            self.assertFalse(body["entry_enabled"])
            store = Store(tmp)
            calls = []

            def private(path):
                calls.append(path)
                if path.endswith("account"):
                    return {"positions": [{"symbol": "ETHUSDT", "positionAmt": "1", "entryPrice": "10"}]}
                if path.endswith("openOrders"):
                    return [{"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "9", "origQty": "1"}]
                return []

            def public(symbol):
                calls.append(symbol)
                if symbol == "SOLUSDT":
                    return [[NOW_MS - 60_000, "1", "2"]]
                return _bars(NOW_MS - 60_000)

            candidate = {"symbol": "SOLUSDT", "setup": "瘸師弟", "plan": "ladder", "stop": 1, "entry": 2, "bar_time": NOW}
            held = dict(scan, call_ai=True, n_candidates=1, candidates=[candidate])
            failed = run_review_entry(
                store, snap, held, now=NOW, scan_id="adapt-fail", now_ms=NOW_MS, entry=dict(entry, enabled=True),
                private_get=private, public_get=public,
            )
            self.assertFalse(failed["quiet"])
            self.assertFalse(failed["notifier_called"])
            self.assertEqual(failed["old_ai_poll"], "keep")
            self.assertFalse(failed["wakes_saved"])
            self.assertFalse(failed["discretion_complete"])
            self.assertIn("/fapi/v2/account", calls)
            self.assertIn("/fapi/v1/openOrders", calls)
            self.assertIn("/fapi/v1/openAlgoOrders", calls)
            self.assertIn("ETHUSDT", calls)
            self.assertIn("BTCUSDT", calls)
            self.assertIn("SOLUSDT", calls)
            self.assertIn("klines:SOLUSDT", failed["missing"])

            def public_ok(symbol):
                return _bars(NOW_MS - 60_000)

            none_query = run_review_entry(
                Store(os.path.join(tmp, "none")), dict(snap, positions=1, orders=1), held, now=NOW, scan_id="adapt-none",
                now_ms=NOW_MS, entry=dict(entry, enabled=True),
                private_get=lambda _path: None, public_get=public_ok,
            )
            self.assertFalse(none_query["context_sufficient"])
            self.assertFalse(none_query["quiet"])
            self.assertEqual(none_query["old_ai_poll"], "keep")


if __name__ == "__main__":
    unittest.main()
