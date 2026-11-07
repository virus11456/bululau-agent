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
            config["notifier"] = [PYTHON, script, mode_path, "notifier", os.path.join(tmp, "notified.txt")]
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
        loaded["notifier"] = [PYTHON, status, mode, "notifier", os.path.join(tmp, "notified.txt")]
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


if __name__ == "__main__":
    unittest.main()
