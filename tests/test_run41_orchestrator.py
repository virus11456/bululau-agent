"""orchestrator 真的呼叫注入函式。不執行 manage --live，不發真 HTTP。"""
import json
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.phase1.orchestrator import disabled_mac_contract, load_orchestrator_config, run_disabled_manage, run_orchestrator
from src.phase1.runner import main
from src.phase1.session import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-07T12:20:00+08:00"


class Recorder:
    def __init__(self):
        self.order = []
        self.events = []

    def call(self, name, value):
        def fn(payload):
            self.order.append(name)
            self.events.append(payload["event_id"])
            if isinstance(value, Exception):
                raise value
            return value
        return fn


def _book(recorder, *, positions=0, open_orders=0, algo_orders=0, candidates=None, manage=None, prefilter_error=None):
    candidates = [] if candidates is None else candidates
    manage_value = manage or {"ok": True, "trusted": True, "halted": False}
    prefilter_value = {"ok": True, "candidates": candidates, "context_sufficient": True}
    return {
        "manage": recorder.call("manage", manage_value),
        "status": recorder.call("status", {
            "ok": True, "fresh": True, "status_trusted": True,
            "positions": positions, "open_orders": open_orders, "algo_orders": algo_orders,
        }),
        "prefilter": recorder.call("prefilter", prefilter_error or prefilter_value),
        "protection": recorder.call("protection", {"ok": True}),
        "candidate": recorder.call("candidate", {"ok": True}),
        "fallback": recorder.call("fallback", {"ok": True}),
    }


class Run41OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.sockets = []
        self.real_open = urllib.request.urlopen
        self.real_run = subprocess.run

        def blocked(*_args, **_kwargs):
            self.sockets.append(True)
            raise AssertionError("socket")

        urllib.request.urlopen = blocked
        subprocess.run = blocked

    def tearDown(self):
        urllib.request.urlopen = self.real_open
        subprocess.run = self.real_run
        self.assertEqual(self.sockets, [])

    def test_empty_book_is_quiet_and_live_manage_is_not_executed(self):
        recorder = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_orchestrator(
                Store(tmp), now=NOW, scan_id="s41quiet", callbacks=_book(recorder),
            )
        self.assertEqual(recorder.order, ["manage", "status", "prefilter"])
        self.assertTrue(result["quiet"])
        self.assertFalse(result["fallback_called"])
        self.assertFalse(result["orders_placed"])
        self.assertFalse(result["platform_ai_executed"])
        self.assertFalse(result["wakes_saved"])
        self.assertIn("original_ai_invoke_absent", result["gaps"])
        called = {"ran": False}

        def runner(_argv):
            called["ran"] = True
            return 0

        blocked = run_disabled_manage(disabled_mac_contract(), runner)
        self.assertFalse(blocked["executed"])
        self.assertFalse(blocked["called"])
        self.assertFalse(called["ran"])
        with self.assertRaises(ValueError):
            load_orchestrator_config({"enabled": True, "execute": False, "execute_manage": False, "interval_seconds": 1800, "schedule_installed": False, "replaces_routine": False, "live_callbacks": False, "live_http": False, "open_live_enabled": False})

    def test_exposure_protection_and_candidate_share_one_event(self):
        recorder = Recorder()
        callbacks = _book(recorder, positions=1, open_orders=1, algo_orders=1, candidates=[
            {"symbol": "ETHUSDT", "setup": "布鹿流"},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_orchestrator(Store(tmp), now=NOW, scan_id="s41both", callbacks=callbacks)
        self.assertEqual(recorder.order, ["manage", "status", "prefilter", "protection", "candidate"])
        self.assertEqual(len(set(recorder.events)), 1)
        self.assertEqual(result["needs"], ["protection_ai", "candidate_ai"])
        self.assertFalse(result["fallback_called"])
        self.assertFalse(result["orders_placed"])
        self.assertFalse(result["quiet"])

    def test_failure_still_calls_protection_then_fallback_and_duplicate_does_not_rerun(self):
        recorder = Recorder()
        callbacks = _book(
            recorder, positions=0, open_orders=2, algo_orders=3,
            prefilter_error=TimeoutError(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            failed = run_orchestrator(store, now=NOW, scan_id="s41fail", callbacks=callbacks)
            again = run_orchestrator(store, now=NOW, scan_id="s41fail", callbacks=callbacks)
        self.assertEqual(recorder.order, ["manage", "status", "prefilter", "protection", "fallback"])
        self.assertTrue(failed["fallback_called"])
        self.assertFalse(failed["platform_ai_executed"])
        self.assertFalse(failed["quiet"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["order"], [])
        self.assertEqual(recorder.order, ["manage", "status", "prefilter", "protection", "fallback"])

    def test_unknown_setup_calls_fallback_not_a_fabricated_candidate_ai(self):
        recorder = Recorder()
        callbacks = _book(recorder, candidates=[{"symbol": "ETHUSDT", "setup": "大師兄"}])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_orchestrator(Store(tmp), now=NOW, scan_id="s41unk", callbacks=callbacks)
        self.assertEqual(recorder.order, ["manage", "status", "prefilter", "fallback"])
        self.assertNotIn("candidate", recorder.order)
        self.assertTrue(result["fallback_called"])
        self.assertEqual(result["reason"], "unknown_setup_fallback")
        self.assertIn("platform_ai_not_executable", result["gaps"])

    def test_unwired_cli_exposes_the_gap_and_does_not_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main([
                "orchestrate", "--state-dir", tmp, "--config", str(ROOT / "config" / "orchestrator.example.json"),
                "--now", NOW, "--scan-id", "s41cli",
            ])
        self.assertEqual(code, 0)
        unwired = Recorder()
        callbacks = _book(unwired)
        del callbacks["fallback"]
        with tempfile.TemporaryDirectory() as tmp:
            result = run_orchestrator(
                Store(tmp), now=NOW, scan_id="s41gap",
                callbacks={
                    "manage": unwired.call("manage", TimeoutError()),
                    "status": callbacks["status"],
                    "prefilter": callbacks["prefilter"],
                    "protection": callbacks["protection"],
                    "candidate": callbacks["candidate"],
                },
            )
        self.assertFalse(result["fallback_called"])
        self.assertIn("original_ai_callback_unwired", result["gaps"])
        self.assertIn("fallback_absent", result["gaps"])
        self.assertFalse(result["quiet"])
        self.assertFalse(result["wakes_saved"])
