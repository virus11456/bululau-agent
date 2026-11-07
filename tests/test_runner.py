"""整條 runner 用 subprocess 假執行檔。不讀金鑰、不 POST。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-07T12:20:00+08:00"
PYTHON = sys.executable
WORKER = r"""
import json, sys
mode = open(sys.argv[1], encoding="utf-8").read().strip()
role = sys.argv[2]
if role == "scanner":
    if mode == "timeout":
        json.dump({"error": "timeout", "candidates": []}, sys.stdout)
    elif mode == "429":
        json.dump({"error": "429", "candidates": []}, sys.stdout)
    elif mode == "empty":
        pass
    elif mode == "bad":
        sys.stdout.write("{")
    elif mode == "missing":
        json.dump({}, sys.stdout)
    elif mode == "quiet":
        json.dump({"candidates": []}, sys.stdout)
    elif mode == "manage_fail":
        json.dump({"candidates": []}, sys.stdout)
    else:
        json.dump({"candidates": []}, sys.stdout)
elif role == "manage":
    if mode == "manage_fail":
        sys.exit(1)
elif role == "status":
    positions = 1 if mode == "position" else 0
    as_of = "2026-11-07T12:00:00+08:00" if mode == "stale" else "2026-11-07T12:19:30+08:00"
    if mode == "status_bad":
        sys.stdout.write("nope")
    else:
        json.dump({"status": "ok", "as_of": as_of, "positions": positions, "orders": 0}, sys.stdout)
elif role == "notifier":
    with open(sys.argv[3], "a", encoding="utf-8") as handle:
        handle.write("called\n")
    if mode == "notify_fail":
        sys.exit(1)
    json.dump({"status": "ok"}, sys.stdout)
"""


class RunnerChainTest(unittest.TestCase):
    def _layout(self, tmp, mode, notifier=False):
        script = os.path.join(tmp, "worker.py")
        mode_path = os.path.join(tmp, "mode.txt")
        state = os.path.join(tmp, "state")
        os.makedirs(state)
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(WORKER)
        with open(mode_path, "w", encoding="utf-8") as handle:
            handle.write(mode)
        config = {
            "shadow": True,
            "timeout_seconds": 10,
            "scanner": [PYTHON, script, mode_path, "scanner"],
            "manage": [PYTHON, script, mode_path, "manage"],
            "status": [PYTHON, script, mode_path, "status"],
        }
        if notifier:
            config["notifier"] = {"verified": True, "argv": [PYTHON, script, mode_path, "notifier", os.path.join(tmp, "notified.txt")]}
        config_path = os.path.join(state, "runner.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return state, config_path

    def _run(self, state, config, scan_id):
        return subprocess.run(
            [PYTHON, "-m", "src.phase1.runner", "run", "--state-dir", state, "--config", config, "--now", NOW, "--scan-id", scan_id, "--shadow"],
            cwd=str(ROOT), capture_output=True, text=True,
        )

    def test_errors_never_skip_through_subprocess(self):
        modes = ("timeout", "429", "empty", "bad", "missing", "manage_fail", "status_bad", "stale")
        with tempfile.TemporaryDirectory() as tmp:
            for mode in modes:
                state, config = self._layout(os.path.join(tmp, mode), mode)
                proc = self._run(state, config, "s-" + mode)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                body = json.loads(proc.stdout)
                self.assertTrue(body["cycle_needs_ai"], mode)
                self.assertNotEqual(body["reason"], "quiet_flat_book", mode)
                self.assertEqual(body["ai_suggestion"], "keep", mode)
                self.assertFalse(body["protection_complete"], mode)
                self.assertFalse(body["ready"], mode)
                self.assertFalse(body["routine_stopped"], mode)
                self.assertEqual(body["old_ai_poll"], "keep", mode)
                self.assertFalse(body["executed_live"], mode)
                self.assertFalse(body["formal_channel"], mode)

    def test_quiet_suggestion_does_not_stop_routine(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, config = self._layout(tmp, "quiet")
            proc = self._run(state, config, "quiet1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads(proc.stdout)
            self.assertFalse(body["cycle_needs_ai"])
            self.assertEqual(body["ai_suggestion"], "skip")
            self.assertEqual(body["reason"], "quiet_flat_book")
            self.assertTrue(body["protection_complete"])
            self.assertFalse(body["ready"])
            self.assertFalse(body["routine_stopped"])
            self.assertEqual(body["old_ai_poll"], "keep")

    def test_notifier_failure_keeps_pending_on_same_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, config = self._layout(tmp, "notify_fail", notifier=True)
            status = os.path.join(tmp, "worker.py")
            mode = os.path.join(tmp, "mode.txt")
            with open(mode, "w", encoding="utf-8") as handle:
                handle.write("position")
            # position mode still exits notifier non-zero only when mode is notify_fail.
            # Use notify_fail for the worker mode and force a position via a dedicated status file.
            with open(mode, "w", encoding="utf-8") as handle:
                handle.write("notify_fail")
            config_path = config
            with open(config_path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            loaded["status"] = [PYTHON, status, mode, "status"]
            # status role with notify_fail falls through to positions 0. Rewrite worker call by mode position
            # after first line was overwritten. Set mode to a custom path pair instead.
            self._position_notifier(tmp, state, config_path, status, mode)
            first = self._run(state, config_path, "hold1")
            self.assertEqual(first.returncode, 0, first.stderr)
            body = json.loads(first.stdout)
            self.assertTrue(body["cycle_needs_ai"])
            self.assertEqual(body["old_ai_poll"], "keep")
            saved = json.loads(Path(state, "phase1_state.json").read_text(encoding="utf-8"))
            item = saved["outbox"]["cycle:hold1"]
            self.assertEqual(item["status"], "pending")
            self.assertFalse(item["processing_ack"])
            second = self._run(state, config_path, "hold1")
            self.assertEqual(second.returncode, 0, second.stderr)
            again = json.loads(Path(state, "phase1_state.json").read_text(encoding="utf-8"))
            self.assertEqual(again["outbox"]["cycle:hold1"]["status"], "pending")
            self.assertEqual(again["outbox"]["cycle:hold1"]["event_id"], item["event_id"])
            self.assertNotEqual(again["outbox"]["cycle:hold1"].get("status"), "acked")
            self.assertGreaterEqual(Path(tmp, "notified.txt").read_text(encoding="utf-8").count("called"), 2)

    def _position_notifier(self, tmp, state, config_path, status, mode):
        text = WORKER.replace(
            'positions = 1 if mode == "position" else 0',
            'positions = 1 if mode in ("position", "notify_fail") else 0',
        )
        with open(status, "w", encoding="utf-8") as handle:
            handle.write(text)
        with open(mode, "w", encoding="utf-8") as handle:
            handle.write("notify_fail")
        with open(config_path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        loaded["scanner"][0:3] = [PYTHON, status, mode]
        loaded["manage"][0:3] = [PYTHON, status, mode]
        loaded["status"] = [PYTHON, status, mode, "status"]
        loaded["notifier"] = {"verified": True, "argv": [PYTHON, status, mode, "notifier", os.path.join(tmp, "notified.txt")]}
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(loaded, handle)
        del state

    def test_live_command_is_refused_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, config = self._layout(tmp, "quiet")
            touched = os.path.join(tmp, "live_trader.py")
            with open(touched, "w", encoding="utf-8") as handle:
                handle.write("open(r'{0}', 'w', encoding='utf-8').write('ran')\n".format(os.path.join(tmp, "ran.txt")))
            with open(config, encoding="utf-8") as handle:
                loaded = json.load(handle)
            loaded["manage"] = [PYTHON, touched, "--live"]
            with open(config, "w", encoding="utf-8") as handle:
                json.dump(loaded, handle)
            proc = self._run(state, config, "s1")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            body = json.loads(proc.stdout)
            self.assertEqual(body["reason"], "refused_unverified_live_command")
            self.assertFalse(body["ready"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "ran.txt")))

    def test_shadow_schedule_is_disabled_and_rollback_removes_only_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            dest = os.path.join(tmp, "schedule")
            os.makedirs(state)
            proc = subprocess.run(
                [PYTHON, "-m", "src.phase1.runner", "install-schedule", "--dest", dest, "--state-dir", state, "--repo", str(ROOT), "--python", PYTHON],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plist = Path(dest, "bululau-cycle-shadow.plist").read_text(encoding="utf-8")
            self.assertIn("<key>Disabled</key><true/>", plist)
            self.assertIn("<key>RunAtLoad</key><false/>", plist)
            self.assertNotIn("--live", plist)
            self.assertNotIn("sender_setup", plist)
            kept = os.path.join(dest, "other.txt")
            with open(kept, "w", encoding="utf-8") as handle:
                handle.write("keep")
            rolled = subprocess.run(
                [PYTHON, "-m", "src.phase1.runner", "rollback-schedule", "--dest", dest],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(rolled.returncode, 0, rolled.stderr)
            self.assertFalse(os.path.exists(os.path.join(dest, "bululau-cycle-shadow.plist")))
            self.assertEqual(Path(kept).read_text(encoding="utf-8"), "keep")
            launch = subprocess.run(
                [PYTHON, "-m", "src.phase1.runner", "install-schedule", "--dest", os.path.join(tmp, "LaunchAgents"), "--state-dir", state, "--repo", str(ROOT), "--python", PYTHON],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(launch.returncode, 1)
            self.assertEqual(json.loads(launch.stdout)["reason"], "refused_schedule_dest")


PREFILTER = r"""
import json, os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
mode = open(os.path.join(root, "mode.txt"), encoding="utf-8").read().strip()
if "--quiet" not in sys.argv or "--nolog" not in sys.argv:
    sys.exit(9)
with open(os.path.join(root, "spawned.txt"), "a", encoding="utf-8") as handle:
    handle.write("prefilter\n")
if mode == "candidates":
    json.dump({"time": "2026-11-07T04:19:00Z", "call_ai": True, "n_candidates": 1, "candidates": [{"symbol": "ETHUSDT", "setup": "布鹿流", "bar_open_ms": 1762484400000, "ladder": ["1"]}]}, sys.stdout)
    sys.exit(10)
if mode == "rate":
    json.dump({"time": "2026-11-07T04:19:00Z", "call_ai": False, "error": "rate_limited: HTTP 429", "candidates": []}, sys.stdout)
    sys.exit(2)
if mode == "contradict":
    json.dump({"call_ai": True, "candidates": []}, sys.stdout)
    sys.exit(0)
json.dump({"time": "2026-11-07T04:19:00Z", "call_ai": False, "n_candidates": 0, "candidates": []}, sys.stdout)
sys.exit(0)
"""

TRADER = r"""
import json, os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
mode = open(os.path.join(root, "mode.txt"), encoding="utf-8").read().strip()
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "manage":
    if sys.argv[2:] != ["--live"]:
        sys.exit(9)
    with open(os.path.join(root, "spawned.txt"), "a", encoding="utf-8") as handle:
        handle.write("manage\n")
    sys.exit(1 if mode == "manage_fail" else (10 if mode == "manage10" else 0))
if cmd == "status-json":
    if len(sys.argv) != 2:
        sys.exit(9)
    with open(os.path.join(root, "spawned.txt"), "a", encoding="utf-8") as handle:
        handle.write("status\n")
    queries = {"account": True, "open_orders": True, "open_algo_orders": True}
    body = {"schema_version": 1, "command": "status-json", "ok": True, "as_of": "2026-11-07T12:19:30+08:00", "positions": 0, "open_orders": 0, "algo_orders": 0, "queries": queries, "limit_older_than_2h_implemented": False}
    code = 0
    if mode == "position":
        body["positions"] = 2
    elif mode == "order":
        body["open_orders"] = 1
    elif mode == "algo":
        body["algo_orders"] = 1
    elif mode == "status10":
        code = 10
    elif mode == "status_fail":
        body = {"schema_version": 1, "command": "status-json", "ok": False, "error": "query_failed", "queries": {"account": False, "open_orders": False, "open_algo_orders": False}}
        code = 2
    elif mode == "ok_false":
        body["ok"] = False
        body["error"] = "query_failed"
    elif mode == "algo_hidden":
        body["queries"] = {"account": True, "open_orders": True, "open_algo_orders": False}
    elif mode == "error_text":
        json.dump(body, sys.stdout)
        sys.stdout.write("\n查詢失敗 HTTP 429\n")
        sys.exit(0)
    elif mode == "broken":
        sys.stdout.write("時間 2026-11-07 12:19 台北\n持倉 0/5\n一般掛單 0  條件單 0\n")
        sys.exit(0)
    json.dump(body, sys.stdout)
    sys.exit(code)
sys.exit(9)
"""

NOTIFIER = r"""
import json, sys
raw = json.load(sys.stdin)
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(raw, ensure_ascii=False))
if raw.get("schema_version") != 1 or raw.get("event_type") != "evaluation_wake":
    sys.exit(2)
if raw.get("test_channel") or raw.get("position_channel"):
    sys.exit(2)
json.dump({"schema_version": 1, "delivered": True}, sys.stdout)
"""


class ProductionAdapterTest(unittest.TestCase):
    def _tree(self, tmp, mode, notifier=None):
        root = os.path.join(tmp, "quantboy")
        os.makedirs(os.path.join(root, "src"))
        with open(os.path.join(root, "src", "prefilter.py"), "w", encoding="utf-8") as handle:
            handle.write(PREFILTER)
        with open(os.path.join(root, "src", "live_trader.py"), "w", encoding="utf-8") as handle:
            handle.write(TRADER)
        with open(os.path.join(root, "mode.txt"), "w", encoding="utf-8") as handle:
            handle.write(mode)
        state = os.path.join(tmp, "state")
        os.makedirs(state)
        config = {"mode": "production", "quantboy_root": root, "python": PYTHON, "interval_seconds": 180, "timeout_seconds": 10, "account_verified": True, "ready": True, "switch_allowed": True}
        if notifier is not None:
            config["notifier"] = notifier
        path = os.path.join(state, "runner.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return root, state, path

    def _run(self, state, config, scan_id="s1", now=NOW, extra=None):
        argv = [PYTHON, "-m", "src.phase1.runner", "run", "--state-dir", state, "--config", config]
        if now is not None:
            argv.extend(["--now", now, "--scan-id", scan_id])
        if extra:
            argv.extend(extra)
        return subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True)

    def test_exit_contracts_and_status_text(self):
        expect = {
            "candidates": {"needs": True, "blocked": False, "reason": "candidates_present"},
            "quiet": {"needs": False, "blocked": False, "reason": "quiet_flat_book"},
            "rate": {"needs": True, "blocked": True},
            "position": {"needs": True, "blocked": False, "reason": "positions_open"},
            "order": {"needs": True, "blocked": False, "reason": "orders_open"},
            "algo": {"needs": True, "blocked": False, "reason": "orders_open"},
            "manage_fail": {"needs": True, "blocked": False},
            "manage10": {"needs": True, "blocked": False},
            "status10": {"needs": True, "blocked": False},
            "status_fail": {"needs": True, "blocked": False},
            "broken": {"needs": True, "blocked": False},
            "contradict": {"needs": True, "blocked": True},
            "error_text": {"needs": True, "blocked": False},
            "algo_hidden": {"needs": True, "blocked": False},
            "ok_false": {"needs": True, "blocked": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            for mode, wanted in expect.items():
                root, state, config = self._tree(os.path.join(tmp, mode), mode)
                proc = self._run(state, config, "p" + mode[:6])
                self.assertEqual(proc.returncode, 0, mode + proc.stderr)
                body = json.loads(proc.stdout)
                self.assertEqual(body["cycle_needs_ai"], wanted["needs"], mode)
                self.assertEqual(body["scan_blocked"], wanted["blocked"], mode)
                if "reason" in wanted:
                    self.assertEqual(body["reason"], wanted["reason"], mode)
                self.assertNotEqual(body["reason"], "quiet_flat_book" if mode != "quiet" else "positions_open", mode)
                self.assertEqual(body["old_ai_poll"], "keep", mode)
                self.assertFalse(body["switch_allowed"], mode)
                self.assertFalse(body["processing_ack"], mode)
                self.assertFalse(body["account_verified"], mode)
                self.assertEqual(body["mode"], "production")
                self.assertTrue(body["executed_live"], mode)
                spawned = Path(root, "spawned.txt").read_text(encoding="utf-8")
                self.assertIn("manage\n", spawned, mode)
                if mode == "quiet":
                    self.assertEqual(body["ai_suggestion"], "skip")
                    self.assertTrue(body["protection_complete"])
                else:
                    self.assertEqual(body["ai_suggestion"], "keep", mode)
                if mode in ("rate", "manage_fail", "manage10", "status10", "status_fail", "broken", "contradict", "error_text", "algo_hidden", "ok_false"):
                    self.assertFalse(body["protection_complete"], mode)
                if mode == "status10":
                    self.assertNotEqual(body["reason"], "candidates_present")

    def test_verified_notifier_delivers_without_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = os.path.join(tmp, "transport.py")
            seen = os.path.join(tmp, "seen.json")
            with open(note, "w", encoding="utf-8") as handle:
                handle.write(NOTIFIER)
            root, state, config = self._tree(tmp, "position", {"verified": True, "argv": [PYTHON, note, seen]})
            proc = self._run(state, config, "hold1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads(proc.stdout)
            self.assertTrue(body["delivered"])
            self.assertFalse(body["processing_ack"])
            self.assertFalse(body["formal_channel"])
            self.assertEqual(body["old_ai_poll"], "keep")
            saved = json.loads(Path(state, "phase1_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["outbox"]["cycle:hold1"]["status"], "pending")
            payload = json.loads(Path(seen).read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["event_type"], "evaluation_wake")
            self.assertEqual(payload["evaluation_id"], "hold1")
            self.assertTrue(payload["event_id"])
            self.assertTrue(payload["sent_at"])
            self.assertFalse(payload["test_channel"])
            self.assertFalse(payload["position_channel"])
            unverified = dict(json.loads(Path(config).read_text(encoding="utf-8")))
            unverified["notifier"] = {"verified": False, "argv": [PYTHON, note, seen + ".no"]}
            with open(config, "w", encoding="utf-8") as handle:
                json.dump(unverified, handle)
            blocked = self._run(os.path.join(tmp, "state2"), config, "hold2")
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertFalse(json.loads(blocked.stdout)["delivered"])
            self.assertFalse(os.path.exists(seen + ".no"))

    def test_generated_scan_id_and_runnable_disabled_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, config = self._tree(tmp, "quiet")
            first = self._run(state, config, now=None)
            second = self._run(state, config, now=None)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            one = json.loads(first.stdout)["evaluation_id"]
            two = json.loads(second.stdout)["evaluation_id"]
            self.assertRegex(one, r"^c\d{8}T\d{6}Z[0-9a-f]{8}$")
            self.assertNotEqual(one, two)
            dest = os.path.join(tmp, "schedule")
            installed = subprocess.run(
                [PYTHON, "-m", "src.phase1.runner", "install-schedule", "--dest", dest, "--state-dir", state, "--repo", str(ROOT), "--python", PYTHON, "--config", config],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            plist = Path(dest, "bululau-cycle-shadow.plist").read_text(encoding="utf-8")
            self.assertIn("<key>Disabled</key><true/>", plist)
            self.assertIn("<key>StartInterval</key><integer>180</integer>", plist)
            self.assertNotIn("--now", plist)
            self.assertNotIn("--scan-id", plist)
            matched = __import__("re").search(r"<key>ProgramArguments</key><array>(.*)</array>", plist)
            args = __import__("re").findall(r"<string>(.*?)</string>", matched.group(1))
            self.assertNotIn("--shadow", args)
            ran = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertTrue(json.loads(ran.stdout)["executed_live"])
            self.assertFalse(json.loads(ran.stdout)["switch_allowed"])
            self.assertIn("manage\n", Path(root, "spawned.txt").read_text(encoding="utf-8"))
            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            checked = subprocess.run(
                [PYTHON, "-m", "src.phase1.runner", "check", "--config", config, "--state-dir", empty],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            report = json.loads(checked.stdout)
            self.assertFalse(report["spawned"])
            self.assertFalse(report["account_verified"])
            self.assertFalse(report["switch_allowed"])
            self.assertEqual(report["commands"]["manage"][-2:], ["manage", "--live"])
            self.assertEqual(report["commands"]["status"][-1], "status-json")
            self.assertEqual(report["commands"]["scanner"][-2:], ["--quiet", "--nolog"])
            self.assertEqual(os.listdir(os.path.join(tmp, "empty")), [])

    def test_shadow_flag_does_not_run_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, state, config = self._tree(tmp, "quiet")
            proc = self._run(state, config, extra=["--shadow"])
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["reason"], "shadow_refuses_production")
            self.assertFalse(os.path.exists(os.path.join(root, "spawned.txt")))


if __name__ == "__main__":
    unittest.main()
