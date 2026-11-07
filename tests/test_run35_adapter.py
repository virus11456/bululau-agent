"""巢狀 context 契約與 Mac req 離線接線。不重跑 run34，不發真 HTTP。"""
import hashlib
import json
import os
import tempfile
import unittest
import urllib.request

from src.phase1.canonical import canonical_json
from src.phase1.gateway_client import load_gateway_config, main as gateway_main, publisher_document
from src.phase1.market_adapter import (
    MAC_FLAT_SHA256,
    call_verified_req,
    kline_request,
    load_entry_config,
    load_verified_req,
    require_req_contract,
    run_review_entry,
)
from src.phase1.production_transport import build_envelope
from src.phase1.review_context import parent_package_ready
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
NOW_MS = 1762482000000
SENT = "2026-11-07T04:19:30Z"
EVENT = "c-" + "cd" * 16


def _package(envelope):
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    context = {
        "schema_version": 1,
        "event_type": "immutable_context",
        "event_id": EVENT,
        "evaluation_id": "s35",
        "envelope_sha256": digest,
        "sent_at": SENT,
        "producer_store_id": "ab" * 16,
        "envelope": envelope,
        "automatic_transport": False,
    }
    judgment = {
        "schema_version": 1,
        "event_type": "dry_run_judgment",
        "event_id": EVENT,
        "evaluation_id": "s35",
        "envelope_sha256": digest,
        "decision": "keep_original_ai",
        "basis": {"reason": envelope.get("reason")},
        "completed_at": "2026-11-07T04:22:00Z",
        "orders_placed": False,
        "ai_execution_proven": False,
    }
    trace = {
        "event_id": EVENT,
        "evaluation_id": "s35",
        "trace": "parent wrote this trace",
        "envelope_sha256": digest,
    }
    return context, judgment, trace


def _base_envelope():
    return build_envelope(
        {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": [{"symbol": "ETHUSDT", "setup": "瘸師弟", "bar_time": "t", "ladder": [1, 2], "plan": "ladder"}]},
        {"valid": True, "status": "fresh", "age_seconds": 1},
        event_id=EVENT, evaluation_id="s35", sent_at=SENT, reason="orders_open",
    )


def _config(tmp):
    return {
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
    }


class Run35AdapterTest(unittest.TestCase):
    def test_nested_schema_blocks_publish_even_when_digest_matches(self):
        context, judgment, trace = _package(_base_envelope())
        self.assertTrue(parent_package_ready(judgment, trace, context)["ready"])
        self.assertFalse(parent_package_ready(judgment, trace, context)["discretion_complete"])
        mutations = []
        snapshot_null = json.loads(json.dumps(_base_envelope()))
        snapshot_null["snapshot"] = None
        mutations.append(snapshot_null)
        freshness_text = json.loads(json.dumps(_base_envelope()))
        freshness_text["freshness"] = "fresh"
        mutations.append(freshness_text)
        candidates_object = json.loads(json.dumps(_base_envelope()))
        candidates_object["candidates"] = {"symbol": "ETHUSDT"}
        mutations.append(candidates_object)
        bad_count = json.loads(json.dumps(_base_envelope()))
        bad_count["snapshot"]["positions"] = "0"
        mutations.append(bad_count)
        bad_flag = json.loads(json.dumps(_base_envelope()))
        bad_flag["freshness"]["valid"] = 1
        mutations.append(bad_flag)
        bad_candidate = json.loads(json.dumps(_base_envelope()))
        bad_candidate["candidates"] = ["ETHUSDT"]
        mutations.append(bad_candidate)
        with tempfile.TemporaryDirectory() as tmp:
            config = load_gateway_config(_config(tmp))
            judgment_path = os.path.join(tmp, "judgment.json")
            trace_path = os.path.join(tmp, "trace.json")
            context_path = os.path.join(tmp, "context.json")
            for envelope in mutations:
                ctx, body, parent_trace = _package(envelope)
                ready = parent_package_ready(body, parent_trace, ctx)
                self.assertFalse(ready["ready"])
                self.assertEqual(ready["reason"], "rejected_context")
                self.assertNotEqual(ready["reason"], "digest_mismatch")
                with open(judgment_path, "w", encoding="utf-8") as handle:
                    json.dump(body, handle)
                with open(trace_path, "w", encoding="utf-8") as handle:
                    json.dump(parent_trace, handle)
                with open(context_path, "w", encoding="utf-8") as handle:
                    json.dump(ctx, handle)
                before = open(trace_path, encoding="utf-8").read()
                with self.assertRaises(ValueError) as caught:
                    publisher_document(config, EVENT, judgment_path, trace_path, context_path)
                self.assertEqual(str(caught.exception), "rejected_context")
                self.assertEqual(open(trace_path, encoding="utf-8").read(), before)
            import src.phase1.gateway_client as gateway_client

            def explode(*_args, **_kwargs):
                raise AssertionError("put sent")

            gateway_client.https_exchange = explode
            config_path = os.path.join(tmp, "client.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(_config(tmp), handle)
            code = gateway_main([
                "gateway_client", "put", "--send", "--config", config_path, "--event-id", EVENT,
                "--file", judgment_path, "--trace", trace_path, "--context", context_path,
            ])
            self.assertEqual(code, 1)

    def test_verified_req_contract_and_public_klines_stay_offline(self):
        sockets = []
        real_open = urllib.request.urlopen

        def blocked(*_args, **_kwargs):
            sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wrong = os.path.join(tmp, "live_trader.py")
                with open(wrong, "w", encoding="utf-8") as handle:
                    handle.write("def req(method, path, params=None, signed=False, retries=5):\n    return None\n")
                with self.assertRaises(ValueError) as mismatch:
                    load_verified_req(wrong)
                self.assertEqual(str(mismatch.exception), "module_sha_mismatch")
                entry = {
                    "enabled": True, "live_fetch": True, "replaces_scan_entry": False,
                    "outbound": False, "gateway_base_url": "", "mac_flat_path": wrong,
                }
                result = run_review_entry(
                    Store(tmp), {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0, "open_orders": 0, "algo_orders": 0,
                                 "manage_error": False, "status_trusted": True, "queries_ok": True,
                                 "queries": {"account": True, "open_orders": True, "open_algo_orders": True}},
                    {"call_ai": False, "n_candidates": 0, "candidates": [], "btc": {"last": 1}},
                    now=NOW, scan_id="m35", now_ms=NOW_MS, entry=entry,
                )
                self.assertEqual(result["reason"], "context_insufficient")
                self.assertEqual(result["old_ai_poll"], "keep")
                self.assertFalse(result["routine_replaced"])
                self.assertFalse(result["wakes_saved"])
            with self.assertRaises(ValueError):
                load_entry_config({
                    "enabled": False, "live_fetch": False, "replaces_scan_entry": False, "outbound": False,
                    "gateway_base_url": "", "mac_flat": {"sha256": "ab" * 32},
                })

            def req(method, path, params=None, signed=False, retries=5):
                req.calls.append((method, path, params, signed, retries))
                if path.endswith("account"):
                    return {"positions": []}
                if path.endswith("openAlgoOrders") and req.algo_none:
                    return None
                return []

            req.calls = []
            req.algo_none = False
            require_req_contract(req)
            self.assertEqual(call_verified_req(req, "/fapi/v2/account")["positions"], [])
            self.assertEqual(call_verified_req(req, "/fapi/v1/openOrders"), [])
            self.assertEqual(call_verified_req(req, "/fapi/v1/openAlgoOrders"), [])
            self.assertEqual(req.calls[0][0], "GET")
            self.assertTrue(all(item[3] is True for item in req.calls))
            self.assertNotIn("open_algo", [item[1] for item in req.calls])
            req.algo_none = True
            with self.assertRaises(ValueError):
                call_verified_req(req, "/fapi/v1/openAlgoOrders")
            path, params = kline_request("ETHUSDT")
            self.assertEqual(path, "/fapi/v1/klines")
            self.assertEqual(params, {"symbol": "ETHUSDT", "interval": "15m", "limit": 20})
            from src.phase1.market_adapter import fetch_market
            account = {"positions": [{"symbol": "ETHUSDT", "positionAmt": "1", "entryPrice": "10"}]}
            orders = [{"symbol": "ETHUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "9", "origQty": "1"}]

            def private(kind):
                def get(path):
                    if path.endswith("account"):
                        return None if kind == "account-none" else account
                    if path.endswith("openOrders"):
                        return orders
                    if path.endswith("openAlgoOrders"):
                        return None if kind == "algo-none" else []
                    raise ValueError("path")
                return get

            def bars(open_time):
                return [[open_time, "1", "2", "0.5", "1.5", "3"]]

            none_algo = fetch_market(private("algo-none"), lambda _symbol: bars(NOW_MS - 60000), [], NOW_MS)
            self.assertFalse(none_algo["context_sufficient"])
            self.assertEqual(none_algo["old_ai_poll"], "keep")
            self.assertIn("query_failed", none_algo["missing"])
            stale = fetch_market(private("ok"), lambda _symbol: bars(NOW_MS - 21 * 60 * 1000), [], NOW_MS)
            self.assertFalse(stale["context_sufficient"])
            self.assertTrue(any(item.startswith("klines:") for item in stale["missing"]))
            fresh = fetch_market(private("ok"), lambda _symbol: bars(NOW_MS - 60000), [], NOW_MS)
            self.assertTrue(fresh["context_sufficient"])
            self.assertEqual(MAC_FLAT_SHA256, "65f1d4526cd5d3b6c2115ca8eeebd102281b27a2fbd40d0ee07054ff4891459b")
        finally:
            urllib.request.urlopen = real_open
        self.assertEqual(sockets, [])


if __name__ == "__main__":
    unittest.main()
