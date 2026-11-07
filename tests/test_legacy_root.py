"""舊平鋪 root 的安裝前檢查。不讀 state、不 flock、不碰 ~/quantboy-live。"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
SOURCE = textwrap.dedent("""\
    HOME = os.path.expanduser('~/quantboy-live')
    STATE = os.path.join(HOME, 'state.json')
    EVENTS = os.path.join(HOME, 'events.json')
    HALT_FLAG = os.path.join(HOME, 'HALT_REPORT_USER.flag')
    LOCK = os.path.join(HOME, '.lock')

    class Lock:
        def __enter__(self):
            self.f = open(LOCK, 'w')
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit('另一個 live_trader 正在執行（檔案鎖），稍後再試')
            return self
        def __exit__(self, *a):
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()

    def main():
        with Lock():
            try:
                {'status': 0, 'open': 0, 'manage': 0, 'watch': 0, 'flatten': 0}[cmd]()
            except BinanceError:
                raise SystemExit('幣安錯誤')
    """)


class LegacyRootTest(unittest.TestCase):
    def _write(self, root, source=SOURCE):
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "live_trader.py"), "w", encoding="utf-8") as handle:
            handle.write(source)
        for name in ("state.json", ".lock", "events.json"):
            with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                handle.write("secret-state-bytes\n")

    def test_matching_source_still_blocks_manage_without_schema(self):
        from src.phase1.legacy_root import check_legacy_install
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp)
            report = check_legacy_install(tmp, home=tmp)
            self.assertTrue(report["path_is_legacy_home"])
            self.assertTrue(report["bindings_match"])
            self.assertTrue(report["lock_protocol_match"])
            self.assertFalse(report["known_sha_match"])
            self.assertFalse(report["legacy_source_accepted"])
            self.assertEqual(report["reason"], "sha_mismatch")
            self.assertFalse(report["state_schema_proven"])
            self.assertFalse(report["share_live_state"])
            self.assertFalse(report["manage_allowed"])
            self.assertFalse(report["flock_taken"])
            self.assertFalse(report["state_bytes_read"])
            self.assertEqual(report["files"]["state.json"]["kind"], "file")
            self.assertNotIn("secret-state-bytes", json.dumps(report))

    def test_unknown_path_is_rejected_even_when_the_text_matches(self):
        from src.phase1.legacy_root import check_legacy_install, production_block_reason
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "elsewhere")
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            self._write(root)
            report = check_legacy_install(root, home=home)
            self.assertFalse(report["path_is_legacy_home"])
            self.assertTrue(report["lock_protocol_match"])
            self.assertEqual(report["reason"], "unknown_path")
            self.assertEqual(report["files"]["state.json"]["kind"], "not_checked")
            self.assertEqual(production_block_reason(report), "unknown_path")

    def test_env_override_is_not_the_known_binding(self):
        from src.phase1.legacy_root import bindings_match
        text = SOURCE.replace(
            "HOME = os.path.expanduser('~/quantboy-live')",
            "HOME = os.environ['QUANTBOY_DATA_DIR']",
        )
        self.assertFalse(bindings_match(text))

    def test_production_run_does_not_spawn_a_flat_unknown_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "flat")
            os.makedirs(root)
            marker = os.path.join(root, "spawned.txt")
            with open(os.path.join(root, "live_trader.py"), "w", encoding="utf-8") as handle:
                handle.write("import pathlib,sys\npathlib.Path(sys.argv[1] if False else %r).write_text('ran')\n" % marker)
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            config = os.path.join(state, "runner.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump({"mode": "production", "quantboy_root": root, "python": PYTHON, "interval_seconds": 180}, handle)
            proc = subprocess.run(
                [PYTHON, "-m", "src.phase1.runner", "run", "--state-dir", state, "--config", config, "--now", "2026-11-07T04:20:00Z", "--scan-id", "s1"],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["reason"], "unknown_path")
            self.assertFalse(os.path.exists(marker))
            checked = subprocess.run(
                [PYTHON, "-m", "src.phase1.runner", "check-install", "--quantboy-root", root],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(checked.returncode, 1)
            body = json.loads(checked.stdout)
            self.assertFalse(body["manage_allowed"])
            self.assertFalse(body["share_live_state"])
            self.assertFalse(body["package_data_shared"])


if __name__ == "__main__":
    unittest.main()
