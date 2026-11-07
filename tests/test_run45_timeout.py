"""argv callback 的 process group 超時。不發真 HTTP，不執行 live 命令。"""
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.phase1.orchestrator import run_isolated, run_orchestrator
from src.phase1.session import Store

NOW = "2026-11-07T12:20:00+08:00"
PY = sys.executable


def _script(path, body):
    Path(path).write_text(body, encoding="utf-8")


SLOW = """import os, sys, time, pathlib
marker = pathlib.Path(sys.argv[1])
marker.write_text("started {0}".format(os.getpid()))
time.sleep(30)
marker.write_text("finished")
sys.stdout.write('{"ok": true}')
"""
FAST = """import json, sys
sys.stdout.write(json.dumps({"ok": True}))
"""
TOUCH = """import pathlib, sys
pathlib.Path(sys.argv[1]).write_text("ran")
sys.stdout.write('{"ok": true}')
"""


class Run45TimeoutTest(unittest.TestCase):
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

    def test_protection_timeout_kills_the_process_and_skips_new_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            slow = os.path.join(tmp, "slow.py")
            fast = os.path.join(tmp, "fast.py")
            touch = os.path.join(tmp, "touch.py")
            marker = os.path.join(tmp, "protection.txt")
            opened = os.path.join(tmp, "candidate.txt")
            _script(slow, SLOW)
            _script(fast, FAST)
            _script(touch, TOUCH)
            result = run_orchestrator(
                Store(tmp), now=NOW, scan_id="s45kill", deadline_seconds=1,
                callbacks={
                    "manage": lambda payload: {"ok": True, "trusted": True, "halted": False},
                    "status": lambda payload: {
                        "ok": True, "fresh": True, "status_trusted": True,
                        "positions": 1, "open_orders": 0, "algo_orders": 0,
                    },
                    "prefilter": lambda payload: {
                        "ok": True, "context_sufficient": True,
                        "candidates": [{"symbol": "ETHUSDT", "setup": "布鹿流"}],
                    },
                    "protection": {"argv": [PY, slow, marker], "deadline_seconds": 1},
                    "candidate": {"argv": [PY, touch, opened], "deadline_seconds": 1},
                    "fallback": {"argv": [PY, fast], "deadline_seconds": 1},
                },
            )
            text = Path(marker).read_text(encoding="utf-8")
            pid = int(text.split()[1])
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertFalse(text.startswith("finished"))
            self.assertFalse(os.path.exists(opened))
            self.assertEqual(result["order"], ["manage", "status", "prefilter", "protection", "fallback"])
            self.assertNotIn("candidate", result["order"])
            self.assertTrue(result["fallback_called"])
            self.assertFalse(result["orders_placed"])
            self.assertIn("timeout_kills_the_process_group", result["isolation_limits"])
            self.assertIn("pickle_not_used", result["isolation_limits"])
            with self.assertRaises(ValueError):
                run_isolated([PY, touch, opened, "--live"], 1, {})
            self.assertFalse(os.path.exists(opened))

    def test_fallback_timeout_stays_pending_and_a_later_fallback_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            slow = os.path.join(tmp, "slow.py")
            fast = os.path.join(tmp, "fast.py")
            manage_script = os.path.join(tmp, "manage.py")
            manage_mark = os.path.join(tmp, "manage-ran.txt")
            _script(slow, SLOW)
            _script(fast, FAST)
            _script(manage_script, TOUCH)
            first = run_orchestrator(
                Store(tmp), now=NOW, scan_id="s45fb", deadline_seconds=1,
                callbacks={
                    "manage": lambda payload: {"ok": True, "trusted": True},
                    "status": lambda payload: {
                        "ok": True, "fresh": True, "status_trusted": True,
                        "positions": 0, "open_orders": 0, "algo_orders": 0,
                    },
                    "prefilter": lambda payload: {"ok": True, "candidates": [], "context_sufficient": True},
                    "fallback": {"argv": [PY, slow, os.path.join(tmp, "fallback.txt")], "deadline_seconds": 1},
                },
            )
            self.assertTrue(first["fallback_called"])
            self.assertFalse(first["fallback_settled"])
            self.assertEqual(Store(tmp).state["orchestrator_seen"]["s45fb"]["status"], "running")
            second = run_orchestrator(
                Store(tmp), now=NOW, scan_id="s45fb", deadline_seconds=1,
                callbacks={
                    "manage": {"argv": [PY, manage_script, manage_mark], "deadline_seconds": 1},
                    "fallback": {"argv": [PY, fast], "deadline_seconds": 1},
                },
            )
            self.assertEqual(second["order"], ["fallback"])
            self.assertTrue(second["fallback_settled"])
            self.assertEqual(Store(tmp).state["orchestrator_seen"]["s45fb"]["status"], "recovered")
            self.assertFalse(os.path.exists(manage_mark))
            self.assertIn("platform_ai_timeout_not_connected", second["gaps"])
