"""publisher 重驗 setup 契約，notifier 前量完整 delivery 的 UTF-8。不發真 HTTP。"""
import copy
import hashlib
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.gateway_client import load_gateway_config, publisher_document
from src.phase1.market_adapter import fetch_strategy_market, review_covers_setup, run_review_entry
from src.phase1.production_transport import CONTEXT_MAX_BYTES, build_context, build_envelope, projected_delivery_bytes
from src.phase1.review_context import parent_package_ready
from src.phase1.runner import _now_ms
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
NOW_MS = _now_ms(NOW)
_STEP = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000}


def _raw(interval, count, now_ms):
    step = _STEP[interval]
    boundary = now_ms - (now_ms % step)
    start = boundary - step * count
    return [
        [start + index * step, "1", "2", "0.5", "1.5", "9", start + index * step + step - 1]
        for index in range(count)
    ]


def _private():
    def get(path):
        if path.endswith("account"):
            return {"positions": []}
        return []
    return get


def _public(symbol, interval, limit):
    return _raw(interval, limit, NOW_MS)


def _snap():
    return {
        "status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0, "open_orders": 0, "algo_orders": 0,
        "manage_error": False, "manage_complete": True, "status_trusted": True, "queries_ok": True,
        "queries": {"account": True, "open_orders": True, "open_algo_orders": True},
    }


def _entry():
    return {"enabled": True, "live_fetch": True, "replaces_scan_entry": False, "outbound": False, "gateway_base_url": ""}


def _candidate(symbol, setup):
    return {"symbol": symbol, "setup": setup, "plan": "structure", "stop": 1, "bar_time": NOW}


def _judgment(event_id, evaluation_id, digest):
    return {
        "schema_version": 1, "event_type": "dry_run_judgment", "event_id": event_id, "evaluation_id": evaluation_id,
        "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": "candidates_present"},
        "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
    }


def _trace(event_id, evaluation_id, digest):
    return {"event_id": event_id, "evaluation_id": evaluation_id, "trace": "parent wrote this trace", "envelope_sha256": digest}


class Spy:
    def __init__(self):
        self.calls = []

    def notify(self, event):
        self.calls.append(event)
        return {"status": "blocked", "delivered": False, "http_sent": False}


class Run39ContractTest(unittest.TestCase):
    def test_publisher_rechecks_setup_contract_on_a_real_product(self):
        sockets = []
        real_open = urllib.request.urlopen
        def blocked(*_args, **_kwargs):
            sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked
        try:
            candidate = _candidate("ETHUSDT", "布鹿流")
            prefilter = {"call_ai": True, "n_candidates": 1, "candidates": [candidate], "btc": {"last": 100000.0}}
            with tempfile.TemporaryDirectory() as tmp:
                result = run_review_entry(
                    Store(tmp), _snap(), prefilter, now=NOW, scan_id="s39", now_ms=NOW_MS,
                    entry=_entry(), notifier=Spy(), private_get=_private(), public_get=_public,
                )
                context = build_context(tmp, result["event_id"], "s39")
                digest = context["envelope_sha256"]
                self.assertTrue(parent_package_ready(
                    _judgment(result["event_id"], "s39", digest), _trace(result["event_id"], "s39", digest), context,
                )["ready"])
                self.assertGreaterEqual(len(context["envelope"]["review_context"]["klines"]["ETHUSDT"]["1h"]), 48)

                def drop_1h(envelope):
                    del envelope["review_context"]["klines"]["ETHUSDT"]["1h"]

                def one_1h_bar(envelope):
                    envelope["review_context"]["klines"]["ETHUSDT"]["1h"] = (
                        envelope["review_context"]["klines"]["ETHUSDT"]["1h"][:1]
                    )

                def drop_close(envelope):
                    bar = dict(envelope["review_context"]["klines"]["ETHUSDT"]["1h"][0])
                    del bar["close"]
                    envelope["review_context"]["klines"]["ETHUSDT"]["1h"][0] = bar

                def illegal_high(envelope):
                    bar = dict(envelope["review_context"]["klines"]["ETHUSDT"]["4h"][0])
                    bar["high"] = bar["low"] - 1
                    envelope["review_context"]["klines"]["ETHUSDT"]["4h"][0] = bar

                config = load_gateway_config({
                    "enabled": True, "live_send": True,
                    "base_url": "https://bululau-return-isolated-8qi96wkvs-virus11456s-projects.vercel.app",
                    "installation_id": "inst-local-1", "key_id": "pub-key", "role": "publisher",
                    "ledger_path": os.path.join(tmp, "budget.json"),
                    "trial": {
                        "approved": True, "dry_run_only": True, "event_id": result["event_id"], "approved_at": 1000,
                        "new_ids": 1, "max_wake_posts": 2, "max_judgment_puts": 1, "max_gets": 6, "deadline_seconds": 600,
                    },
                })
                for change in (drop_1h, one_1h_bar, drop_close, illegal_high):
                    copied = copy.deepcopy(context)
                    change(copied["envelope"])
                    digest = hashlib.sha256(canonical_json(copied["envelope"]).encode("utf-8")).hexdigest()
                    copied["envelope_sha256"] = digest
                    self.assertEqual(copied["envelope"]["review_context"]["context_sufficient"], True)
                    verdict = parent_package_ready(
                        _judgment(result["event_id"], "s39", digest), _trace(result["event_id"], "s39", digest), copied,
                    )
                    self.assertFalse(verdict["ready"])
                    self.assertEqual(verdict["reason"], "rejected_context")
                    judgment_path = os.path.join(tmp, "judgment.json")
                    trace_path = os.path.join(tmp, "trace.json")
                    context_path = os.path.join(tmp, "context.json")
                    Path(judgment_path).write_text(json.dumps(_judgment(result["event_id"], "s39", digest)), encoding="utf-8")
                    Path(trace_path).write_text(json.dumps(_trace(result["event_id"], "s39", digest)), encoding="utf-8")
                    Path(context_path).write_text(json.dumps(copied), encoding="utf-8")
                    before = Path(trace_path).read_text(encoding="utf-8")
                    with self.assertRaises(ValueError) as caught:
                        publisher_document(config, result["event_id"], judgment_path, trace_path, context_path)
                    self.assertEqual(str(caught.exception), "rejected_context")
                    self.assertEqual(Path(trace_path).read_text(encoding="utf-8"), before)
            junior = _candidate("ETHUSDT", "瘸師弟")
            review = fetch_strategy_market(_private(), _public, [junior], NOW_MS)
            self.assertTrue(review["context_sufficient"], review["missing"])
            broken = copy.deepcopy(review["klines"])
            del broken["ETHUSDT"]["15m"]
            self.assertFalse(review_covers_setup([junior], broken, NOW_MS))
            short = copy.deepcopy(review["klines"])
            short["ETHUSDT"]["4h"] = short["ETHUSDT"]["4h"][:1]
            self.assertFalse(review_covers_setup([junior], short, NOW_MS))
        finally:
            urllib.request.urlopen = real_open
        self.assertEqual(sockets, [])

    def test_notifier_sees_delivery_utf8_not_the_envelope(self):
        sockets = []
        real_open = urllib.request.urlopen
        def blocked(*_args, **_kwargs):
            sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked
        try:
            plan = "漢" * 90
            huge = [
                {"symbol": "T{0}USDT".format(index), "setup": "布鹿流", "plan": plan, "stop": 1, "bar_time": NOW}
                for index in range(6)
            ]
            seen = {}

            def counting(symbol, interval, limit):
                rows = _raw(interval, limit, NOW_MS)
                seen.setdefault(symbol, {})[interval] = len(rows)
                return rows

            prefilter = {"call_ai": True, "n_candidates": 6, "candidates": huge, "btc": {"last": 100000.0}}
            envelope = build_envelope(
                _snap(), prefilter, {"valid": True, "status": "fresh", "age_seconds": 30},
                review=fetch_strategy_market(_private(), counting, huge, NOW_MS),
                event_id="c-" + "ab" * 16, evaluation_id="s39size", sent_at=NOW, reason="candidates_present",
            )
            envelope_bytes = len(canonical_json(envelope).encode("utf-8"))
            delivered = projected_delivery_bytes(envelope, "ab" * 16)
            self.assertLessEqual(envelope_bytes, CONTEXT_MAX_BYTES)
            self.assertGreater(len(delivered), CONTEXT_MAX_BYTES)
            self.assertLess(len(delivered.decode("utf-8")), CONTEXT_MAX_BYTES)
            spy = Spy()
            seen.clear()
            with tempfile.TemporaryDirectory() as tmp:
                result = run_review_entry(
                    Store(tmp), _snap(), prefilter, now=NOW, scan_id="s39size", now_ms=NOW_MS,
                    entry=_entry(), notifier=spy, private_get=_private(), public_get=counting,
                )
                self.assertEqual(result["reason"], "context_too_large")
                self.assertFalse(result["context_sufficient"])
                self.assertEqual(result["old_ai_poll"], "keep")
                self.assertFalse(result["trimmed"])
                self.assertEqual(result["bars_retained"], sum(sum(item.values()) for item in seen.values()))
                self.assertEqual(spy.calls, [])
                self.assertFalse(result["parent_received_klines"])
                self.assertFalse(os.path.isdir(os.path.join(tmp, "envelopes")))
        finally:
            urllib.request.urlopen = real_open
        self.assertEqual(sockets, [])
