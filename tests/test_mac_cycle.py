"""Mac 週期：空倉不叫保護 AI；有候選仍要新倉審核；未知不當空倉。不打幣安、不 POST。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.phase1.account_status import query_status
from src.phase1.legacy_snapshot import snapshot_file
from src.phase1.runner import HOURLY_SCAN_SECONDS, _load_config, deploy_report, run_configured
from src.phase1.session import Store
from src.phase1.status_contract import parse_status_json

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
NOW = "2026-11-07T07:21:55Z"


def _script(path, body):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)


class MacCycleTest(unittest.TestCase):
    def _config(self, tmp, scanner, status):
        return {
            "mode": "production",
            "role": "mac",
            "timeout_seconds": 10,
            "interval_seconds": HOURLY_SCAN_SECONDS,
            "commands": {
                "scanner": [PYTHON, scanner],
                "manage": None,
                "status": [PYTHON, status],
            },
        }

    def test_flat_book_skips_protection_ai_but_candidates_still_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "manage-ran")
            scanner = os.path.join(tmp, "scan.py")
            status = os.path.join(tmp, "status.py")
            _script(scanner, "import json,sys\njson.dump({'call_ai':False,'n_candidates':0,'candidates':[],'btc':{'last':100000.0}}, sys.stdout)\n")
            _script(status, (
                "import json,sys\n"
                "json.dump({'schema_version':1,'command':'status-json','ok':True,"
                "'as_of':'2026-11-07T15:21:55+08:00','positions':0,'open_orders':0,'algo_orders':0,"
                "'queries':{'account':True,'open_orders':True,'open_algo_orders':True}}, sys.stdout)\n"
            ))
            store = Store(os.path.join(tmp, "state"))
            decision = run_configured(store, self._config(tmp, scanner, status), now=NOW, scan_id="flat1")
            self.assertFalse(decision["cycle_needs_ai"])
            self.assertTrue(decision["protection_ai_skipped"])
            self.assertFalse(decision["new_entry_ai"])
            self.assertFalse(decision["manage_ran"])
            self.assertFalse(decision["unknown_treated_as_flat"])
            self.assertEqual(decision["old_ai_poll"], "keep")
            self.assertFalse(os.path.exists(marker))
            _script(scanner, (
                "import json,sys\n"
                "json.dump({'call_ai':True,'n_candidates':1,'btc':{'last':100000.0},'candidates':[{'symbol':'ETHUSDT','setup':'布鹿流',"
                "'bar_open_ms':1762484400000,'ladder':['1']}]}, sys.stdout)\n"
                "sys.exit(10)\n"
            ))
            woken = run_configured(Store(os.path.join(tmp, "state2")), self._config(tmp, scanner, status), now=NOW, scan_id="flat2")
            self.assertTrue(woken["cycle_needs_ai"])
            self.assertTrue(woken["new_entry_ai"])
            self.assertFalse(woken["protection_ai_skipped"])
            self.assertEqual(woken["reason"], "candidates_present")

    def test_unknown_status_is_not_an_empty_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = os.path.join(tmp, "scan.py")
            status = os.path.join(tmp, "status.py")
            _script(scanner, "import json,sys\njson.dump({'call_ai':False,'n_candidates':0,'candidates':[],'btc':{'last':100000.0}}, sys.stdout)\n")
            _script(status, "import json,sys\njson.dump({'schema_version':1,'command':'status-json','ok':False,'error':'query_failed','queries':{'account':False,'open_orders':False,'open_algo_orders':False}}, sys.stdout)\nsys.exit(2)\n")
            decision = run_configured(Store(os.path.join(tmp, "state")), self._config(tmp, scanner, status), now=NOW, scan_id="bad1")
            self.assertTrue(decision["cycle_needs_ai"])
            self.assertNotEqual(decision["reason"], "quiet_flat_book")
            self.assertFalse(decision["protection_ai_skipped"])
            self.assertFalse(decision["unknown_treated_as_flat"])
            self.assertEqual(decision["old_ai_poll"], "keep")

    def test_state_snapshot_is_not_the_three_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"day": "2026-11-07", "pending": {}, "meta": {}, "halted_new": False, "killed": False}, handle)
            snap = snapshot_file(path, NOW)
            trusted = parse_status_json(json.dumps(snap), 0, "")
            self.assertFalse(trusted["trusted"])
            self.assertIsNone(snap["positions"])

    def test_three_queries_reject_null_and_keep_a_real_empty_list(self):
        def getter(path):
            if path.endswith("account"):
                return {"positions": [], "totalWalletBalance": "1029.79"}
            return []
        document, code = query_status(NOW, getter)
        self.assertEqual(code, 0)
        self.assertNotIn("1029.79", json.dumps(document))
        self.assertEqual(document["positions"], 0)
        def bad(path):
            return None
        failed, failed_code = query_status(NOW, bad)
        self.assertEqual(failed_code, 2)
        self.assertFalse(failed["ok"])
        self.assertNotIn("positions", failed)

    def test_mac_role_refuses_a_fifteen_minute_scan_and_a_second_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            fast = os.path.join(tmp, "fast.json")
            with open(fast, "w", encoding="utf-8") as handle:
                json.dump({"mode": "production", "role": "mac", "interval_seconds": 900, "python": PYTHON}, handle)
            with self.assertRaises(ValueError) as caught:
                _load_config(fast)
            self.assertEqual(str(caught.exception), "scan_interval_too_fast")
            hourly = os.path.join(tmp, "hour.json")
            with open(hourly, "w", encoding="utf-8") as handle:
                json.dump({"mode": "production", "role": "mac", "python": PYTHON}, handle)
            loaded = _load_config(hourly)
            self.assertEqual(loaded["interval_seconds"], 1800)
            self.assertIsNone(loaded["commands"]["manage"])
            self.assertTrue(loaded["commands"]["status"][-1].endswith("account_status"))
            proc = subprocess.run(
                [PYTHON, "-m", "src.phase1.runner", "install-schedule", "--dest", os.path.join(tmp, "sched"), "--state-dir", os.path.join(tmp, "state"), "--repo", str(ROOT), "--python", PYTHON, "--config", hourly],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["reason"], "mac_schedule_not_installed")
            report = deploy_report(hourly)
            self.assertEqual(report["scan_interval_seconds"], 1800)
            self.assertFalse(report["reverts_to_15m"])
            self.assertFalse(report["this_package_spawns_manage"])
            self.assertFalse(report["judgment_opens_position"])
            self.assertEqual(report["context_positions"], "count_only")
            self.assertFalse(report["unattended_transport"])
            self.assertEqual(report["old_ai_poll"], "keep")


if __name__ == "__main__":
    unittest.main()
