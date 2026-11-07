"""cycle 產出的 outbox 由同一個 store 吃 processed。不 POST。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.phase1.ack_contract import write_processed_file
from src.phase1.cycle import ack_cycle, migrate_legacy_cycles, rollback_legacy_migration, run_cycle
from src.phase1.session import Store

ROOT = Path(run_cycle.__code__.co_filename).resolve().parents[2]
SENT = "2026-11-07T04:19:30Z"
NOW = "2026-11-07T04:28:00Z"
WHEN = "2026-11-07T04:22:00Z"
AS_OF = "2026-11-07T12:19:30+08:00"


def _snap(**extra):
    base = {
        "status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0,
        "manage_error": False, "manage_complete": True, "limit_older_than_2h": False,
    }
    base.update(extra)
    return base


def _apply(directory, path, event_id):
    return subprocess.run(
        [
            sys.executable, "-m", "src.phase1.cycle", "apply",
            "--state-dir", directory, "--file", path, "--expect", event_id,
            "--sent-at", SENT, "--now", NOW,
        ],
        cwd=str(ROOT), capture_output=True, text=True,
    )


class CycleAckTest(unittest.TestCase):
    def test_run_cycle_then_cli_lands_on_same_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = os.path.join(tmp, "inbox")
            state = os.path.join(tmp, "state")
            os.makedirs(inbox)
            store = Store(state)
            decision = run_cycle(
                store, _snap(positions=1), {"candidates": []},
                now="2026-11-07T12:20:00+08:00", scan_id="s1", manage_exit=0,
            )
            self.assertTrue(decision["cycle_needs_ai"])
            self.assertEqual(decision["ai_suggestion"], "keep")
            self.assertEqual(decision["old_ai_poll"], "keep")
            self.assertFalse(decision["routine_stopped"])
            self.assertEqual(decision["evaluation_id"], "s1")
            key = "cycle:s1"
            item = store.state["outbox"][key]
            self.assertEqual(item["event_id"], decision["event_id"])
            self.assertEqual(item["status"], "pending")
            self.assertFalse(item["processing_ack"])
            document = {
                "kind": "processed", "event_id": decision["event_id"],
                "evaluation_id": "s1", "processed_at": WHEN,
            }
            with self.assertRaises(ValueError):
                write_processed_file(inbox, document, processing_complete=False)
            self.assertEqual(os.listdir(inbox), [])
            path = write_processed_file(inbox, document, processing_complete=True)
            first = _apply(state, path, decision["event_id"])
            self.assertEqual(first.returncode, 0, first.stderr)
            body = json.loads(first.stdout)
            self.assertEqual(body["reason"], "processed")
            self.assertEqual(body["process_count"], 1)
            self.assertEqual(body["old_ai_poll"], "keep")
            landed = Store(state)
            self.assertEqual(landed.state["outbox"][key]["status"], "acked")
            self.assertTrue(landed.state["outbox"][key]["processing_ack"])
            self.assertEqual(landed.state["test_events"][decision["event_id"]]["process_count"], 1)
            second = _apply(state, path, decision["event_id"])
            self.assertEqual(second.returncode, 0, second.stderr)
            again = json.loads(second.stdout)
            self.assertEqual(again["reason"], "duplicate")
            self.assertEqual(again["process_count"], 1)
            self.assertEqual(Store(state).state["test_events"][decision["event_id"]]["process_count"], 1)
            rerun = run_cycle(
                Store(state), _snap(positions=1), {"candidates": []},
                now="2026-11-07T12:21:00+08:00", scan_id="s1", manage_exit=0,
            )
            self.assertTrue(rerun["cycle_needs_ai"])
            self.assertEqual(rerun["old_ai_poll"], "keep")
            self.assertFalse(rerun["routine_stopped"])
            self.assertEqual(Store(state).state["outbox"][key]["status"], "acked")

    def test_mismatch_accepted_and_quiet_do_not_stop_routine(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = os.path.join(tmp, "inbox")
            state = os.path.join(tmp, "state")
            os.makedirs(inbox)
            store = Store(state)
            decision = run_cycle(
                store, _snap(orders=1), {"candidates": []},
                now="2026-11-07T12:20:00+08:00", scan_id="s1", manage_exit=0,
            )
            self.assertTrue(decision["cycle_needs_ai"])
            self.assertEqual(decision["old_ai_poll"], "keep")
            wrong = write_processed_file(inbox, {
                "kind": "processed", "event_id": "c-other",
                "evaluation_id": "s1", "processed_at": WHEN,
            }, processing_complete=True)
            refused = _apply(state, wrong, decision["event_id"])
            self.assertEqual(refused.returncode, 1)
            self.assertEqual(json.loads(refused.stdout)["reason"], "wrong_event")
            self.assertEqual(Store(state).state["outbox"]["cycle:s1"]["status"], "pending")
            mismatch = write_processed_file(inbox, {
                "kind": "processed", "event_id": decision["event_id"],
                "evaluation_id": "other", "processed_at": WHEN,
            }, processing_complete=True)
            bound = _apply(state, mismatch, decision["event_id"])
            self.assertEqual(json.loads(bound.stdout)["reason"], "binding_mismatch")
            self.assertEqual(Store(state).state["outbox"]["cycle:s1"]["status"], "pending")
            accepted = os.path.join(inbox, "accepted.json")
            with open(accepted, "w", encoding="utf-8") as handle:
                json.dump({
                    "kind": "accepted", "event_id": decision["event_id"],
                    "schema_valid": True, "status": "accepted",
                }, handle)
            note = _apply(state, accepted, decision["event_id"])
            self.assertEqual(note.returncode, 1)
            self.assertEqual(json.loads(note.stdout)["reason"], "accepted_is_not_processed")
            self.assertEqual(json.loads(note.stdout)["old_ai_poll"], "keep")
            self.assertEqual(Store(state).state["outbox"]["cycle:s1"]["status"], "pending")
            self.assertFalse(ack_cycle(store, "s1", processing_ack=True))
            quiet = run_cycle(
                Store(os.path.join(tmp, "quiet")), _snap(), {"candidates": []},
                now="2026-11-07T12:20:00+08:00", scan_id="quiet1", manage_exit=0,
            )
            self.assertFalse(quiet["cycle_needs_ai"])
            self.assertEqual(quiet["ai_suggestion"], "skip")
            self.assertEqual(quiet["old_ai_poll"], "keep")
            self.assertFalse(quiet["routine_stopped"])
            self.assertIsNone(quiet["event_id"])
            failed = run_cycle(
                Store(os.path.join(tmp, "bad")), _snap(manage_complete=False),
                {"error": "timeout", "candidates": []},
                now="2026-11-07T12:20:00+08:00", scan_id="a/b", manage_exit=1,
            )
            self.assertTrue(failed["cycle_needs_ai"])
            self.assertEqual(failed["ai_suggestion"], "keep")
            self.assertFalse(failed["registered"])
            self.assertEqual(Store(os.path.join(tmp, "bad")).state["outbox"], {})

    def test_legacy_pending_is_copied_and_rollback_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phase1_state.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "outbox": {"cycle:cycle:s1": {
                        "event_class": "evaluation_cycle", "evaluation_id": "cycle:s1",
                        "status": "pending", "processing_ack": False,
                    }},
                    "keep_me": "still-pending",
                }, handle)
            store = Store(tmp)
            self.assertEqual(store.state["keep_me"], "still-pending")
            self.assertEqual(store.state["outbox"]["cycle:cycle:s1"]["status"], "pending")
            report = migrate_legacy_cycles(store)
            self.assertEqual(report[0]["action"], "copy")
            self.assertEqual(store.state["outbox"]["cycle:cycle:s1"]["status"], "pending")
            self.assertEqual(store.state["outbox"]["cycle:s1"]["evaluation_id"], "s1")
            self.assertEqual(store.state["outbox"]["cycle:s1"]["status"], "pending")
            self.assertEqual(store.state["keep_me"], "still-pending")
            rolled = rollback_legacy_migration(Store(tmp))
            self.assertEqual(rolled[0]["action"], "removed_copy")
            revived = Store(tmp)
            self.assertNotIn("cycle:s1", revived.state["outbox"])
            self.assertEqual(revived.state["outbox"]["cycle:cycle:s1"]["status"], "pending")
            self.assertEqual(revived.state["keep_me"], "still-pending")

    def test_foreign_ack_dir_and_junk_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            foreign = os.path.join(tmp, "foreign")
            os.makedirs(foreign)
            ack_path = os.path.join(foreign, "ack_state.json")
            with open(ack_path, "w", encoding="utf-8") as handle:
                handle.write('{"keep":true}\n')
            missing = os.path.join(tmp, "missing.json")
            with open(missing, "w", encoding="utf-8") as handle:
                json.dump({"kind": "processed", "event_id": "c-abc", "evaluation_id": "s1", "processed_at": WHEN}, handle)
            refused = _apply(foreign, missing, "c-abc")
            self.assertEqual(refused.returncode, 1)
            self.assertEqual(json.loads(refused.stdout)["reason"], "refused_foreign_state_dir")
            with open(ack_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), '{"keep":true}\n')
            self.assertFalse(os.path.exists(os.path.join(foreign, "phase1_state.json")))
            state = os.path.join(tmp, "state")
            store = Store(state)
            junk = os.path.join(state, "phase1_state.json.tmp")
            with open(junk, "w", encoding="utf-8") as handle:
                handle.write("{")
            store.state["keep_me"] = "saved"
            store.save()
            with open(junk, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "{")
            self.assertEqual(Store(state).state["keep_me"], "saved")


if __name__ == "__main__":
    unittest.main()
