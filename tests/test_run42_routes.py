"""路由缺欄、保護保留、halted、中斷恢復，以及未啟用的正式接收核對。不發真 HTTP。"""
import hashlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from src.phase1.canonical import canonical_json
from src.phase1.formal_receive import verify_formal_delivery
from src.phase1.orchestrator import load_orchestrator_config, run_orchestrator
from src.phase1.production_transport import CONTEXT_MAX_BYTES, build_envelope, wake_document
from src.phase1.session import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-07T12:20:00+08:00"
EVENT = "c-" + "ab" * 16


class Rec:
    def __init__(self):
        self.order = []

    def call(self, name, value):
        def fn(payload):
            self.order.append(name)
            if isinstance(value, BaseException):
                raise value
            return value
        return fn


def _cbs(rec, status, *, candidates=None, halted=False, manage_ok=True):
    return {
        "manage": rec.call("manage", {"ok": manage_ok, "trusted": True, "halted": halted}),
        "status": rec.call("status", status),
        "prefilter": rec.call("prefilter", {
            "ok": True,
            "candidates": [] if candidates is None else candidates,
            "context_sufficient": True,
        }),
        "protection": rec.call("protection", {"ok": True}),
        "candidate": rec.call("candidate", {"ok": True}),
        "fallback": rec.call("fallback", {"ok": True}),
    }


class Run42RouteTest(unittest.TestCase):
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

    def _run(self, callbacks, scan_id):
        with tempfile.TemporaryDirectory() as tmp:
            return run_orchestrator(Store(tmp), now=NOW, scan_id=scan_id, callbacks=callbacks)

    def test_unknown_counts_fallback_and_forbid_new_positions(self):
        rec = Rec()
        status = {"ok": True, "fresh": True, "status_trusted": True, "positions": False, "open_orders": 0}
        result = self._run(_cbs(rec, status, candidates=[{"symbol": "ETHUSDT", "setup": "布鹿流"}]), "s42bad")
        self.assertEqual(rec.order, ["manage", "status", "prefilter", "fallback"])
        self.assertNotIn("candidate", rec.order)
        self.assertTrue(result["fallback_called"])
        self.assertFalse(result["quiet"])
        self.assertNotEqual(result["reason"], "routed")

    def test_explicit_positive_count_keeps_protection_before_fallback(self):
        rec = Rec()
        partial = {"ok": True, "fresh": True, "status_trusted": True, "positions": 1, "open_orders": 0}
        first = self._run(_cbs(rec, partial), "s42part")
        self.assertEqual(rec.order, ["manage", "status", "prefilter", "protection", "fallback"])
        self.assertTrue(first["fallback_called"])
        self.assertIn("protection_ai", first["needs"])
        self.assertNotIn("candidate_ai", first["needs"])
        rec = Rec()
        failed = {"ok": False, "fresh": True, "status_trusted": True, "positions": 1, "open_orders": 0, "algo_orders": 0}
        second = self._run(_cbs(rec, failed), "s42stat")
        self.assertEqual(rec.order, ["manage", "status", "prefilter", "protection", "fallback"])
        self.assertTrue(second["fallback_called"])
        rec = Rec()
        complete = {"ok": True, "fresh": True, "status_trusted": True, "positions": 1, "open_orders": 0, "algo_orders": 0}
        third = self._run(_cbs(rec, complete), "s42full")
        self.assertEqual(rec.order, ["manage", "status", "prefilter", "protection"])
        self.assertFalse(third["fallback_called"])

    def test_halted_must_be_false_and_true_keeps_protection(self):
        rec = Rec()
        flat = {"ok": True, "fresh": True, "status_trusted": True, "positions": 0, "open_orders": 0, "algo_orders": 0}
        missing = _cbs(rec, flat)
        missing["manage"] = rec.call("manage", {"ok": True, "trusted": True})
        quiet = self._run(missing, "s42halt0")
        self.assertFalse(quiet["quiet"])
        self.assertEqual(rec.order, ["manage", "status", "prefilter", "fallback"])
        rec = Rec()
        held = {"ok": True, "fresh": True, "status_trusted": True, "positions": 1, "open_orders": 0, "algo_orders": 0}
        stopped = self._run(_cbs(rec, held, halted=True, candidates=[{"symbol": "ETHUSDT", "setup": "布鹿流"}]), "s42halt1")
        self.assertEqual(rec.order, ["manage", "status", "prefilter", "protection", "fallback"])
        self.assertNotIn("candidate", rec.order)
        self.assertEqual(stopped["reason"], "halt_fallback")
        self.assertFalse(stopped["quiet"])

    def test_incomplete_restart_fallbacks_without_rerunning_manage_and_live_claim_is_separate(self):
        script = (
            "import os, sys\n"
            "sys.path.insert(0, {root!r})\n"
            "from src.phase1.orchestrator import run_orchestrator\n"
            "from src.phase1.session import Store\n"
            "def manage(payload):\n"
            "    os._exit(3)\n"
            "run_orchestrator(Store({tmp!r}), now={now!r}, scan_id='s42dead', callbacks={{'manage': manage}})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            child = subprocess.run(
                [sys.executable, "-c", script.format(root=str(ROOT), tmp=tmp, now=NOW)],
                cwd=str(ROOT), check=False,
            )
            self.assertEqual(child.returncode, 3)
            rec = Rec()
            restarted = run_orchestrator(
                Store(tmp), now=NOW, scan_id="s42dead",
                callbacks={"fallback": rec.call("fallback", {"ok": True}), "manage": rec.call("manage", {"ok": True})},
            )
            self.assertEqual(rec.order, ["fallback"])
            self.assertEqual(restarted["reason"], "incomplete_restart")
            self.assertFalse(restarted["duplicate"])
            self.assertFalse(restarted["quiet"])
            again = run_orchestrator(Store(tmp), now=NOW, scan_id="s42dead", callbacks={"manage": rec.call("manage", {"ok": True})})
            self.assertTrue(again["duplicate"])
            self.assertEqual(rec.order, ["fallback"])
        gate = threading.Event()
        release = threading.Event()
        order = []

        def manage(payload):
            order.append("manage")
            gate.set()
            self.assertTrue(release.wait(3))
            return {"ok": True, "trusted": True, "halted": False}

        def status(payload):
            order.append("status")
            return {"ok": True, "fresh": True, "status_trusted": True, "positions": 0, "open_orders": 0, "algo_orders": 0}

        def prefilter(payload):
            order.append("prefilter")
            return {"ok": True, "candidates": [], "context_sufficient": True}

        held = {}
        with tempfile.TemporaryDirectory() as tmp:
            def second():
                gate.wait(3)
                held["result"] = run_orchestrator(Store(tmp), now=NOW, scan_id="s42live", callbacks={"fallback": lambda payload: order.append("fallback") or {"ok": True}})
                release.set()

            worker = threading.Thread(target=second)
            worker.start()
            first = run_orchestrator(Store(tmp), now=NOW, scan_id="s42live", callbacks={"manage": manage, "status": status, "prefilter": prefilter})
            worker.join(5)
        self.assertTrue(first["quiet"])
        self.assertTrue(held["result"]["claim_held"])
        self.assertEqual(held["result"]["order"], [])
        self.assertNotIn("fallback", order)

    def test_formal_handler_rejects_legacy_test_and_checks_delivery_offline(self):
        legacy = verify_formal_delivery({
            "schema_version": 1, "event_type": "trade_candidate_test", "event_id": "t-1",
            "test_only": True, "sent_at": "2026-11-07T04:20:00Z",
        }, NOW)
        self.assertFalse(legacy["formal_wake_authorized"])
        self.assertFalse(legacy["accepted"])
        self.assertFalse(legacy["budget_reusable"])
        self.assertFalse(legacy["layers"]["prompt_saved_by_platform"])
        self.assertFalse(legacy["layers"]["bounded_trial_approved"])
        self.assertTrue(legacy["layers"]["handler_verifiable"])
        envelope = build_envelope(
            {"status": "ok", "as_of": NOW, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
            {"candidates": []},
            {"valid": True, "status": "fresh", "age_seconds": 0},
            event_id=EVENT, evaluation_id="s42recv", sent_at=NOW, reason="not_quiet",
        )
        digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
        context = {
            "schema_version": 1, "event_type": "immutable_context", "event_id": EVENT, "evaluation_id": "s42recv",
            "envelope_sha256": digest, "sent_at": NOW, "producer_store_id": "ab" * 16, "envelope": envelope,
            "automatic_transport": False,
        }
        document = {"schema_version": 1, "event_type": "evaluation_delivery", "wake": wake_document(envelope), "context": context}
        verdict = verify_formal_delivery(document, NOW)
        self.assertTrue(verdict["accepted"])
        self.assertFalse(verdict["orders_placed"])
        self.assertFalse(verdict["http_sent"])
        self.assertFalse(verdict["http_200_is_ack"])
        self.assertFalse(verdict["program_fills_judgment"])
        self.assertFalse(verdict["formal_wake_authorized"])
        seen = {EVENT: digest}
        self.assertTrue(verify_formal_delivery(document, NOW, seen)["duplicate"])
        huge = dict(document, pad="x" * (CONTEXT_MAX_BYTES + 8))
        with self.assertRaises(ValueError):
            verify_formal_delivery(huge, NOW)
        with self.assertRaises(ValueError):
            load_orchestrator_config({
                "enabled": False, "execute": False, "execute_manage": False, "open_live_enabled": False,
                "live_callbacks": False, "live_http": False, "schedule_installed": False, "replaces_routine": False,
                "interval_seconds": 1800, "gateway_enabled": True, "notifier_verified": False,
            })
