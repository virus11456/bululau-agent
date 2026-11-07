"""未知 setup 不能靠已知子集通過。路由 mock 不宣稱等效。不發真 HTTP。"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.gateway_client import load_gateway_config, publisher_document
from src.phase1.market_adapter import fetch_strategy_market, run_review_entry
from src.phase1.production_transport import build_context
from src.phase1.review_context import parent_package_ready
from src.phase1.route_contract import route_round
from src.phase1.runner import _now_ms
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
NOW_MS = _now_ms(NOW)
ROOT = Path(__file__).resolve().parents[1]
_STEP = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000}


def _raw(interval, count):
    step = _STEP[interval]
    boundary = NOW_MS - (NOW_MS % step)
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


def _snap():
    return {
        "status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0, "open_orders": 0, "algo_orders": 0,
        "manage_error": False, "manage_complete": True, "status_trusted": True, "queries_ok": True,
        "queries": {"account": True, "open_orders": True, "open_algo_orders": True},
    }


def _entry():
    return {"enabled": True, "live_fetch": True, "replaces_scan_entry": False, "outbound": False, "gateway_base_url": ""}


def _bulu():
    return {"symbol": "ETHUSDT", "setup": "布鹿流", "plan": "structure", "stop": 1, "bar_time": NOW}


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


class Run40MixedTest(unittest.TestCase):
    def test_unknown_setup_beside_known_bars_is_not_sufficient(self):
        sockets = []
        real_open = urllib.request.urlopen

        def blocked(*_args, **_kwargs):
            sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked
        try:
            asked = []

            def public(symbol, interval, limit):
                asked.append((symbol, interval, limit))
                return _raw(interval, limit)

            with tempfile.TemporaryDirectory() as tmp:
                result = run_review_entry(
                    Store(tmp), _snap(),
                    {"call_ai": True, "n_candidates": 1, "candidates": [_bulu()], "btc": {"last": 100000.0}},
                    now=NOW, scan_id="s40", now_ms=NOW_MS, entry=_entry(), notifier=Spy(),
                    private_get=_private(), public_get=public,
                )
                context = build_context(tmp, result["event_id"], "s40")
                self.assertNotIn("SOLUSDT", context["envelope"]["review_context"]["klines"])
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
                extras = (
                    {"symbol": "ETHUSDT", "setup": "大師兄", "bar_time": NOW, "ladder": [], "plan": "unchanged", "stop": 1},
                    {"symbol": "SOLUSDT", "setup": "大師兄", "bar_time": NOW, "ladder": [], "plan": "unchanged", "stop": 1},
                )
                for extra in extras:
                    copied = copy.deepcopy(context)
                    copied["envelope"]["candidates"].append(extra)
                    self.assertEqual(copied["envelope"]["review_context"]["context_sufficient"], True)
                    self.assertEqual(len(copied["envelope"]["candidates"]), 2)
                    digest = hashlib.sha256(canonical_json(copied["envelope"]).encode("utf-8")).hexdigest()
                    copied["envelope_sha256"] = digest
                    verdict = parent_package_ready(
                        _judgment(result["event_id"], "s40", digest), _trace(result["event_id"], "s40", digest), copied,
                    )
                    self.assertFalse(verdict["ready"])
                    self.assertEqual(verdict["reason"], "rejected_context")
                    judgment_path = os.path.join(tmp, "judgment.json")
                    trace_path = os.path.join(tmp, "trace.json")
                    context_path = os.path.join(tmp, "context.json")
                    Path(judgment_path).write_text(json.dumps(_judgment(result["event_id"], "s40", digest)), encoding="utf-8")
                    Path(trace_path).write_text(json.dumps(_trace(result["event_id"], "s40", digest)), encoding="utf-8")
                    Path(context_path).write_text(json.dumps(copied), encoding="utf-8")
                    before = Path(trace_path).read_text(encoding="utf-8")
                    with self.assertRaises(ValueError) as caught:
                        publisher_document(config, result["event_id"], judgment_path, trace_path, context_path)
                    self.assertEqual(str(caught.exception), "rejected_context")
                    self.assertEqual(Path(trace_path).read_text(encoding="utf-8"), before)
                    saved = json.loads(Path(context_path).read_text(encoding="utf-8"))
                    self.assertEqual(len(saved["envelope"]["candidates"]), 2)
            mixed = [_bulu(), {"symbol": "ETHUSDT", "setup": "大師兄", "plan": "unchanged", "stop": 1, "bar_time": NOW}]
            other = [_bulu(), {"symbol": "SOLUSDT", "setup": "大師兄", "plan": "unchanged", "stop": 1, "bar_time": NOW}]
            asked[:] = []
            same = fetch_strategy_market(_private(), public, mixed, NOW_MS)
            self.assertFalse(same["context_sufficient"])
            self.assertIn("unknown_setup:ETHUSDT:大師兄", same["missing"])
            self.assertEqual(same["old_ai_poll"], "keep")
            self.assertEqual(len(mixed), 2)
            self.assertNotIn("SOLUSDT", asked)
            asked[:] = []
            sol = fetch_strategy_market(_private(), public, other, NOW_MS)
            self.assertFalse(sol["context_sufficient"])
            self.assertIn("unknown_setup:SOLUSDT:大師兄", sol["missing"])
            self.assertEqual(len(other), 2)
            self.assertFalse(any(symbol == "SOLUSDT" for symbol, _interval, _limit in asked))
            self.assertNotIn("大師兄", str(asked))
            spy = Spy()
            with tempfile.TemporaryDirectory() as tmp:
                produced = run_review_entry(
                    Store(tmp), _snap(),
                    {"call_ai": True, "n_candidates": 2, "candidates": mixed, "btc": {"last": 100000.0}},
                    now=NOW, scan_id="s40mix", now_ms=NOW_MS, entry=_entry(), notifier=spy,
                    private_get=_private(), public_get=public,
                )
            self.assertFalse(produced["context_sufficient"])
            self.assertEqual(produced["old_ai_poll"], "keep")
            self.assertEqual(produced["reason"], "context_insufficient")
            self.assertEqual(spy.calls, [])
            self.assertIn("unknown_setup:ETHUSDT:大師兄", produced["missing"])
        finally:
            urllib.request.urlopen = real_open
        self.assertEqual(sockets, [])

    def test_route_mock_keeps_original_ai_and_lists_missing_joints(self):
        flat = {
            "confirmed": True, "status": "ok", "status_trusted": True, "fresh": True,
            "manage_error": False, "manage_complete": True,
            "positions": 0, "open_orders": 0, "algo_orders": 0, "candidates": [],
        }
        quiet = route_round(manage={"ok": True}, status=flat)
        self.assertTrue(quiet["quiet"])
        self.assertEqual(quiet["trace"], ["manage", "status", "quiet"])
        self.assertFalse(quiet["review_round_equivalent"])
        self.assertFalse(quiet["orders_placed"])
        self.assertIn("original_executor_unknown", quiet["missing_joints"])
        held = dict(flat, positions=1, open_orders=0, algo_orders=1)
        failed = route_round(manage={"ok": True}, status=held, protection={"ok": False, "timed_out": True})
        self.assertEqual(failed["reason"], "protection_ai_failed")
        self.assertEqual(failed["old_ai_poll"], "keep")
        self.assertFalse(failed["quiet"])
        self.assertEqual(failed["trace"], ["manage", "status", "protection_ai"])
        kept = route_round(manage={"ok": True}, status=held, protection={"ok": True})
        self.assertEqual(kept["reason"], "protection_ai_kept_original")
        self.assertFalse(kept["quiet"])
        both = dict(flat, candidates=[{"symbol": "ETHUSDT", "setup": "布鹿流"}])
        timed = route_round(manage={"ok": True}, status=both, candidate={"ok": False, "timed_out": True})
        self.assertEqual(timed["reason"], "candidate_ai_failed")
        self.assertEqual(timed["trace"], ["manage", "status", "candidate_ai"])
        manage_down = route_round(manage={"ok": False, "timed_out": True}, status=flat)
        self.assertEqual(manage_down["trace"], ["manage"])
        self.assertEqual(manage_down["old_ai_poll"], "keep")
        completed = subprocess.run(
            [sys.executable, "-m", "src.phase1.route_contract"],
            cwd=str(ROOT), check=True, capture_output=True, text=True,
        )
        body = json.loads(completed.stdout)
        self.assertFalse(body["review_round_equivalent"])
        self.assertFalse(body["orders_placed"])
        self.assertIn("original_prompt_not_in_package", body["missing_joints"])
        self.assertIn("thirty_minute_routine_not_switched", body["missing_joints"])
