"""run34 三個新卡點。不重跑已過差異，不發真 HTTP。"""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import urllib.request

from src.phase1.canonical import canonical_json
from src.phase1.gateway_client import load_gateway_config, main as gateway_main, publisher_document
from src.phase1.market_adapter import _load_public_get, _real_public, run_review_entry
from src.phase1.production_transport import build_envelope
from src.phase1.review_context import parent_package_ready
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
NOW_MS = 1794025200000  # 2026-11-07T12:20:00+08:00，與 NOW 同一刻
SENT = "2026-11-07T04:19:30Z"
EVENT = "c-" + "ab" * 16


def _flat():
    return {
        "status": "ok", "as_of": AS_OF, "positions": 0, "open_orders": 0, "algo_orders": 0, "orders": 0,
        "manage_error": False, "status_trusted": True, "queries_ok": True,
        "queries": {"account": True, "open_orders": True, "open_algo_orders": True},
    }


def _scan():
    return {"call_ai": False, "n_candidates": 0, "candidates": [], "btc": {"last": 100000.0}}


def _entry():
    return {
        "enabled": True,
        "replaces_scan_entry": False,
        "live_fetch": True,
        "outbound": False,
        "gateway_base_url": "",
    }


class Run34GapTest(unittest.TestCase):
    def test_repo_root_public_import_and_enabled_entry_keeps_ai(self):
        os.environ["QUANTBOY_DATA_DIR"] = tempfile.mkdtemp()
        loaded = _load_public_get()
        import src.common as common
        import src.config as config
        self.assertIs(loaded, common.get)
        self.assertTrue(common.DATA_DIR.startswith(os.environ["QUANTBOY_DATA_DIR"]))
        self.assertEqual(config.data_dir.__module__, "src.config")
        calls = []

        def fake_get(path, params=None, retries=2):
            calls.append((path, params))
            return []

        common.get = fake_get
        self.assertEqual(_real_public("BTCUSDT"), [])
        self.assertEqual(calls[0][0], "/fapi/v1/klines")
        self.assertEqual(calls[0][1]["symbol"], "BTCUSDT")
        sockets = []
        real_open = urllib.request.urlopen

        def blocked(*_args, **_kwargs):
            sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = run_review_entry(
                    Store(tmp), _flat(), _scan(), now=NOW, scan_id="r34", now_ms=NOW_MS, entry=_entry(),
                )
        finally:
            urllib.request.urlopen = real_open
        self.assertEqual(sockets, [])
        self.assertFalse(result["context_sufficient"])
        self.assertEqual(result["reason"], "context_insufficient")
        self.assertEqual(result["old_ai_poll"], "keep")
        self.assertFalse(result["quiet"])
        self.assertFalse(result["wakes_saved"])
        self.assertFalse(result["routine_replaced"])
        self.assertFalse(result["live_fetch"])

    def test_publisher_recomputes_envelope_and_cli_stops(self):
        envelope = build_envelope(
            {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
            {"candidates": []},
            {"valid": True, "status": "fresh", "age_seconds": 1},
            event_id=EVENT, evaluation_id="s1", sent_at=SENT, reason="orders_open",
        )
        digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
        context = {
            "schema_version": 1,
            "event_type": "immutable_context",
            "event_id": EVENT,
            "evaluation_id": "s1",
            "envelope_sha256": digest,
            "sent_at": SENT,
            "producer_store_id": "cd" * 16,
            "envelope": envelope,
            "automatic_transport": False,
        }
        judgment = {
            "schema_version": 1,
            "event_type": "dry_run_judgment",
            "event_id": EVENT,
            "evaluation_id": "s1",
            "envelope_sha256": digest,
            "decision": "keep_original_ai",
            "basis": {"reason": "orders_open"},
            "completed_at": "2026-11-07T04:22:00Z",
            "orders_placed": False,
            "ai_execution_proven": False,
        }
        trace = {
            "event_id": EVENT,
            "evaluation_id": "s1",
            "trace": "parent wrote this trace",
            "envelope_sha256": digest,
        }
        self.assertTrue(parent_package_ready(judgment, trace, context)["ready"])
        self.assertFalse(parent_package_ready(judgment, trace, context)["discretion_complete"])
        other = json.loads(json.dumps(context))
        other["envelope"]["event_id"] = "OTHER"
        # envelope 與外層 id 不一致，現在在完整 context 驗證就被擋下（rejected_context），比 binding 檢查更早。
        self.assertFalse(parent_package_ready(judgment, trace, other)["ready"])
        self.assertEqual(parent_package_ready(judgment, trace, other)["reason"], "rejected_context")
        tampered = json.loads(json.dumps(context))
        tampered["envelope"]["snapshot"]["positions"] = 9
        self.assertEqual(parent_package_ready(judgment, trace, tampered)["reason"], "digest_mismatch")
        with tempfile.TemporaryDirectory() as tmp:
            judgment_path = os.path.join(tmp, "judgment.json")
            trace_path = os.path.join(tmp, "trace.json")
            context_path = os.path.join(tmp, "context.json")
            with open(judgment_path, "w", encoding="utf-8") as handle:
                json.dump(judgment, handle)
            with open(trace_path, "w", encoding="utf-8") as handle:
                json.dump(trace, handle)
            with open(context_path, "w", encoding="utf-8") as handle:
                json.dump(other, handle)
            before = open(trace_path, encoding="utf-8").read()
            config = load_gateway_config({
                "enabled": True,
                "live_send": True,
                "base_url": "https://bululau-return-isolated-8qi96wkvs-virus11456s-projects.vercel.app",
                "installation_id": "inst-local-1",
                "key_id": "pub-key",
                "role": "publisher",
                "ledger_path": os.path.join(tmp, "budget.json"),
                "trial": {
                    "approved": True, "dry_run_only": True, "event_id": EVENT, "approved_at": 1000,
                    "new_ids": 1, "max_wake_posts": 2, "max_judgment_puts": 1, "max_gets": 6,
                    "deadline_seconds": 600,
                },
            })
            with self.assertRaises(ValueError):
                publisher_document(config, EVENT, judgment_path, trace_path, context_path)
            self.assertEqual(open(trace_path, encoding="utf-8").read(), before)
            config_path = os.path.join(tmp, "client.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "enabled": True, "live_send": True,
                    "base_url": "https://bululau-return-isolated-8qi96wkvs-virus11456s-projects.vercel.app",
                    "installation_id": "inst-local-1", "key_id": "pub-key", "role": "publisher",
                    "ledger_path": os.path.join(tmp, "budget.json"),
                    "trial": {
                        "approved": True, "dry_run_only": True, "event_id": EVENT, "approved_at": 1000,
                        "new_ids": 1, "max_wake_posts": 2, "max_judgment_puts": 1, "max_gets": 6,
                        "deadline_seconds": 600,
                    },
                }, handle)
            import src.phase1.gateway_client as gateway_client

            def explode(*_args, **_kwargs):
                raise AssertionError("put sent")

            gateway_client.https_exchange = explode
            code = gateway_main([
                "gateway_client", "put", "--send", "--config", config_path, "--event-id", EVENT,
                "--file", judgment_path, "--trace", trace_path, "--context", context_path,
            ])
            self.assertEqual(code, 1)
            self.assertEqual(open(trace_path, encoding="utf-8").read(), before)
            names = set(os.listdir(tmp))
            self.assertNotIn(EVENT + ".trace.json", names)

    def test_line_separators_match_node_stringify(self):
        payload = {"note": "a\u2028b\u2029c", "\u2028": 1, "\u2029": 2, "b": 3}
        script = (
            "import { canonical } from './gateway/lib/contract.js';"
            "let raw=''; process.stdin.on('data', (chunk) => raw += chunk);"
            "process.stdin.on('end', () => process.stdout.write(canonical(JSON.parse(raw))));"
        )
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True, check=True,
        )
        self.assertEqual(canonical_json(payload), proc.stdout)
        self.assertIn("\u2028", canonical_json(payload))
        self.assertIn("\u2029", canonical_json(payload))
        self.assertNotIn("\\u2028", canonical_json(payload))
        self.assertNotIn("\\u2029", canonical_json(payload))


if __name__ == "__main__":
    unittest.main()
