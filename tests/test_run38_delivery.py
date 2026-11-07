"""K 線連續窗、正式 envelope 傳遞、context 上限。不發真 HTTP。"""
import hashlib
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.gateway_client import load_gateway_config, publisher_document
from src.phase1.market_adapter import fetch_strategy_market, run_review_entry
from src.phase1.production_transport import (
    CONTEXT_MAX_BYTES,
    build_context,
    build_delivery,
    build_envelope,
    context_limit,
    context_too_large,
)
from src.phase1.review_context import parent_package_ready
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
NOW_MS = 1762482000000
_STEP = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000}


def _raw(interval, count, now_ms):
    step = _STEP[interval]
    boundary = now_ms - (now_ms % step)
    start = boundary - step * count
    rows = []
    for index in range(count):
        opened = start + index * step
        rows.append([opened, "1", "2", "0.5", "1.5", "9", opened + step - 1])
    return rows


def _private():
    def get(path):
        if path.endswith("account"):
            return {"positions": []}
        return []
    return get


def _flat_snap():
    return {
        "status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0, "open_orders": 0, "algo_orders": 0,
        "manage_error": False, "manage_complete": True, "status_trusted": True, "queries_ok": True,
        "queries": {"account": True, "open_orders": True, "open_algo_orders": True},
    }


def _entry():
    return {
        "enabled": True, "live_fetch": True, "replaces_scan_entry": False, "outbound": False, "gateway_base_url": "",
    }


def _judgment(event_id, evaluation_id, digest):
    return {
        "schema_version": 1, "event_type": "dry_run_judgment", "event_id": event_id, "evaluation_id": evaluation_id,
        "envelope_sha256": digest, "decision": "keep_original_ai", "basis": {"reason": "candidates_present"},
        "completed_at": "2026-11-07T04:22:00Z", "orders_placed": False, "ai_execution_proven": False,
    }


def _trace(event_id, evaluation_id, digest):
    return {"event_id": event_id, "evaluation_id": evaluation_id, "trace": "parent wrote this trace", "envelope_sha256": digest}


def _wrapped(envelope):
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1, "event_type": "immutable_context", "event_id": envelope["event_id"],
        "evaluation_id": envelope["evaluation_id"], "envelope_sha256": digest, "sent_at": envelope["sent_at"],
        "producer_store_id": "ab" * 16, "envelope": envelope, "automatic_transport": False,
    }, digest


class Spy:
    def __init__(self):
        self.calls = []

    def notify(self, event):
        self.calls.append(event)
        return {"status": "blocked", "delivered": False, "http_sent": False}


class Run38DeliveryTest(unittest.TestCase):
    def test_duplicate_gap_and_order_do_not_pad_the_window(self):
        candidate = {"symbol": "ETHUSDT", "setup": "瘸師弟", "plan": "ladder", "stop": 1, "bar_time": NOW}
        good = fetch_strategy_market(
            _private(), lambda symbol, interval, limit: _raw(interval, limit, NOW_MS), [candidate], NOW_MS,
        )
        self.assertTrue(good["context_sufficient"], good["missing"])
        self.assertEqual(good["daily_ma_parameter"], "undecided")
        self.assertEqual(good["daily_ma_gate"], "not_evaluated")

        def repeated(symbol, interval, limit):
            latest = _raw(interval, limit, NOW_MS)[-1]
            return [latest for _ in range(limit)]

        dup = fetch_strategy_market(_private(), repeated, [candidate], NOW_MS)
        self.assertFalse(dup["context_sufficient"])
        self.assertEqual(len(dup["klines"]["ETHUSDT"]["15m"]), 1)

        def shuffled(symbol, interval, limit):
            return list(reversed(_raw(interval, limit, NOW_MS)))

        messy = fetch_strategy_market(_private(), shuffled, [candidate], NOW_MS)
        self.assertTrue(messy["context_sufficient"], messy["missing"])
        opens = [bar["open_time"] for bar in messy["klines"]["ETHUSDT"]["15m"]]
        self.assertEqual(opens, sorted(opens))

        def gapped(symbol, interval, limit):
            rows = _raw(interval, max(limit, 8), NOW_MS)
            hole = rows[:-3] + rows[-1:]
            step = _STEP[interval]
            oldest = rows[0][0]
            filler = []
            for index in range(10):
                opened = oldest - (index + 3) * step
                filler.append([opened, "1", "2", "0.5", "1.5", "9", opened + step - 1])
            return filler + hole

        gap = fetch_strategy_market(_private(), gapped, [candidate], NOW_MS)
        self.assertFalse(gap["context_sufficient"])
        self.assertTrue(any(item.startswith("klines:") for item in gap["missing"]))

        step = _STEP["15m"]
        start = NOW_MS - (NOW_MS % step) - step * 6
        times = [start, start + step, start + 3 * step, start + 4 * step, start + 5 * step]
        bars = [
            {"open_time": opened, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 9, "closed": True}
            for opened in times
        ]
        lied = build_envelope(
            _flat_snap(), {"candidates": [candidate]},
            {"valid": True, "status": "fresh", "age_seconds": 30},
            review={"context_sufficient": True, "klines": {"ETHUSDT": {"15m": bars}}, "missing": []},
            event_id="c-" + "cd" * 16, evaluation_id="s38gap", sent_at=NOW, reason="candidates_present",
        )
        context, digest = _wrapped(lied)
        verdict = parent_package_ready(_judgment("c-" + "cd" * 16, "s38gap", digest), _trace("c-" + "cd" * 16, "s38gap", digest), context)
        self.assertFalse(verdict["ready"])
        self.assertEqual(verdict["reason"], "rejected_context")

    def test_producer_file_reaches_publisher_and_oversize_keeps_ai(self):
        sockets = []
        real_open = urllib.request.urlopen

        def blocked(*_args, **_kwargs):
            sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked
        try:
            candidate = {"symbol": "ETHUSDT", "setup": "布鹿流", "plan": "structure", "stop": 1, "bar_time": NOW}
            prefilter = {"call_ai": True, "n_candidates": 1, "candidates": [candidate], "btc": {"last": 100000.0}}
            spy = Spy()
            with tempfile.TemporaryDirectory() as tmp:
                result = run_review_entry(
                    Store(tmp), _flat_snap(), prefilter, now=NOW, scan_id="s38", now_ms=NOW_MS,
                    entry=_entry(), notifier=spy, private_get=_private(),
                    public_get=lambda symbol, interval, limit: _raw(interval, limit, NOW_MS),
                )
                self.assertTrue(result["parent_received_klines"])
                self.assertFalse(result["trimmed"])
                self.assertEqual(result["old_ai_poll"], "keep")
                self.assertTrue(result["context_sufficient"])
                context = build_context(tmp, result["event_id"], "s38")
                review = context["envelope"]["review_context"]
                series = review["klines"]["ETHUSDT"]
                self.assertGreaterEqual(len(series["1h"]), 48)
                self.assertGreaterEqual(len(series["4h"]), 50)
                self.assertGreaterEqual(len(series["1d"]), 6)
                self.assertGreaterEqual(len(review["klines"]["BTCUSDT"]["15m"]), 24)
                self.assertEqual(review["event_id"], result["event_id"])
                self.assertEqual(review["evaluation_id"], "s38")
                self.assertEqual(review["daily_ma_parameter"], "undecided")
                self.assertEqual(review["daily_ma_gate"], "not_evaluated")
                self.assertTrue(review["context_sufficient"])
                digest = context["envelope_sha256"]
                self.assertTrue(parent_package_ready(_judgment(result["event_id"], "s38", digest), _trace(result["event_id"], "s38", digest), context)["ready"])
                judgment_path = os.path.join(tmp, "judgment.json")
                trace_path = os.path.join(tmp, "trace.json")
                context_path = os.path.join(tmp, "context.json")
                Path(judgment_path).write_text(json.dumps(_judgment(result["event_id"], "s38", digest)), encoding="utf-8")
                Path(trace_path).write_text(json.dumps(_trace(result["event_id"], "s38", digest)), encoding="utf-8")
                Path(context_path).write_text(json.dumps(context), encoding="utf-8")
                before = Path(trace_path).read_text(encoding="utf-8")
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
                document = publisher_document(config, result["event_id"], judgment_path, trace_path, context_path)
                self.assertEqual(document["judgment"]["envelope_sha256"], digest)
                reread = json.loads(Path(context_path).read_text(encoding="utf-8"))
                self.assertIn("1h", reread["envelope"]["review_context"]["klines"]["ETHUSDT"])
                self.assertEqual(Path(trace_path).read_text(encoding="utf-8"), before)
                _delivery, raw = build_delivery(tmp, result["event_id"], "s38")
                self.assertLessEqual(len(raw), CONTEXT_MAX_BYTES)
                self.assertGreater(len(raw), 12000)
                self.assertEqual(context_limit(None), CONTEXT_MAX_BYTES)
                self.assertEqual(context_limit(CONTEXT_MAX_BYTES * 5), CONTEXT_MAX_BYTES)
                with self.assertRaises(ValueError) as caught:
                    build_delivery(tmp, result["event_id"], "s38", max_bytes=len(raw) - 1)
                self.assertEqual(str(caught.exception), "context_too_large")
                self.assertGreaterEqual(len(json.loads(Path(context_path).read_text(encoding="utf-8"))["envelope"]["review_context"]["klines"]["ETHUSDT"]["1h"]), 48)
            huge = [
                {"symbol": "T{0}USDT".format(index), "setup": "布鹿流", "plan": "structure", "stop": 1, "bar_time": NOW}
                for index in range(20)
            ]
            seen = {}

            def counting(symbol, interval, limit):
                rows = _raw(interval, limit, NOW_MS)
                seen.setdefault(symbol, {})[interval] = len(rows)
                return rows

            spy = Spy()
            with tempfile.TemporaryDirectory() as tmp:
                result = run_review_entry(
                    Store(tmp), _flat_snap(),
                    {"call_ai": True, "n_candidates": 20, "candidates": huge, "btc": {"last": 100000.0}},
                    now=NOW, scan_id="s38big", now_ms=NOW_MS, entry=_entry(), notifier=spy,
                    private_get=_private(), public_get=counting,
                )
                self.assertEqual(result["reason"], "context_too_large")
                self.assertEqual(result["old_ai_poll"], "keep")
                self.assertFalse(result["context_sufficient"])
                self.assertFalse(result["trimmed"])
                self.assertEqual(result["bars_retained"], sum(sum(item.values()) for item in seen.values()))
                self.assertGreater(result["bars_retained"], 1000)
                self.assertEqual(spy.calls, [])
                self.assertFalse(result["parent_received_klines"])
                self.assertFalse(os.path.isdir(os.path.join(tmp, "envelopes")))
            blob = {"pad": "x" * (CONTEXT_MAX_BYTES + 8)}
            self.assertTrue(context_too_large(blob))
            self.assertTrue(context_too_large(blob, CONTEXT_MAX_BYTES * 5))
            master_calls = []

            def master_public(symbol, interval, limit):
                master_calls.append((symbol, interval, limit))
                return _raw(interval, limit, NOW_MS)

            spy = Spy()
            with tempfile.TemporaryDirectory() as tmp:
                result = run_review_entry(
                    Store(tmp), _flat_snap(),
                    {"call_ai": True, "n_candidates": 1, "candidates": [
                        {"symbol": "ETHUSDT", "setup": "大師兄", "plan": "ladder", "stop": 1, "bar_time": NOW},
                    ], "btc": {"last": 100000.0}},
                    now=NOW, scan_id="s38unk", now_ms=NOW_MS, entry=_entry(), notifier=spy,
                    private_get=_private(), public_get=master_public,
                )
            self.assertEqual(result["reason"], "context_insufficient")
            self.assertEqual(result["old_ai_poll"], "keep")
            self.assertFalse(result["context_sufficient"])
            self.assertEqual(spy.calls, [])
            self.assertEqual(master_calls, [])
            self.assertTrue(any(item.startswith("unknown_setup:") for item in result["missing"]))
        finally:
            urllib.request.urlopen = real_open
        self.assertEqual(sockets, [])
