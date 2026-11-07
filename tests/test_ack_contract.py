"""處理 ACK。discover 可找到。不 POST。"""
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

try:
    from src.phase1.ack_contract import (
        KNOWN_TEST_EVENT, MAX_ACK_AGE, apply_processed_ack, load_ack, note_accepted, open_store, write_processed_file,
    )
    from src.phase1.ack_store import AckStore
except ImportError:
    from ack_contract import (
        KNOWN_TEST_EVENT, MAX_ACK_AGE, apply_processed_ack, load_ack, note_accepted, open_store, write_processed_file,
    )
    from ack_store import AckStore

SENT = "2026-11-07T05:08:13Z"
NOW = "2026-11-07T05:16:00Z"
EVAL = "s1"


def _processed(event_id=KNOWN_TEST_EVENT, when="2026-11-07T05:10:00Z", evaluation=EVAL):
    return {"kind": "processed", "event_id": event_id, "evaluation_id": evaluation, "processed_at": when}


def _store(tmp):
    store = open_store(tmp)
    store.state["outbox"]["cycle:" + EVAL] = {
        "status": "pending", "processing_ack": False, "event_class": "evaluation_cycle",
        "event_id": KNOWN_TEST_EVENT, "evaluation_id": EVAL,
    }
    store.save()
    return store


class AckContractTest(unittest.TestCase):
    def test_accepted_is_not_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            result = note_accepted(store, {"kind": "accepted", "event_id": KNOWN_TEST_EVENT, "schema_valid": True, "status": "accepted"})
            self.assertEqual(result["reason"], "accepted_is_not_processed")
            self.assertEqual(store.state["outbox"]["cycle:" + EVAL]["status"], "pending")
            with self.assertRaises(ValueError):
                write_processed_file(tmp, {"kind": "accepted", "event_id": KNOWN_TEST_EVENT, "schema_valid": True, "status": "accepted"}, processing_complete=True)

    def test_too_early_expired_wrong_id_and_binding(self):
        future = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)
        current = datetime(2026, 11, 7, 5, 16, tzinfo=timezone.utc)
        self.assertGreater(future, current + timedelta(seconds=60))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            cases = [
                (_processed(when="2026-11-07T05:00:00Z"), "too_early"),
                (_processed(when="2026-11-07T06:00:00Z"), "expired_or_future"),  # 未來時間，不是 15 分鐘過期
                (_processed(event_id="other-id"), "wrong_event"),
                (_processed(evaluation="missing"), "binding_mismatch"),
                ({"kind": "processed", "event_id": "../x", "evaluation_id": EVAL, "processed_at": "2026-11-07T05:10:00Z"}, "rejected_id"),
                ({"kind": "processed", "event_id": KNOWN_TEST_EVENT, "evaluation_id": "a/b", "processed_at": "2026-11-07T05:10:00Z"}, "rejected_id"),
            ]
            for document, reason in cases:
                result = apply_processed_ack(store, document, expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
                self.assertEqual(result["reason"], reason, document)
                self.assertTrue(result["pending"])
                self.assertEqual(store.state["outbox"]["cycle:" + EVAL]["status"], "pending")
                self.assertEqual(result["old_ai_poll"], "keep")

    def test_resend_restart_and_store_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store._fail = True
            accepted = note_accepted(store, {"kind": "accepted", "event_id": KNOWN_TEST_EVENT, "schema_valid": True, "status": "accepted"})
            self.assertEqual(accepted["reason"], "store_failed")
            self.assertNotIn(KNOWN_TEST_EVENT, AckStore(tmp).state["test_events"])
            failed = apply_processed_ack(store, _processed(), expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(failed["reason"], "store_failed")
            self.assertEqual(AckStore(tmp).state["outbox"]["cycle:" + EVAL]["status"], "pending")
            store = _store(tmp)
            first = apply_processed_ack(store, _processed(), expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(first["process_count"], 1)
            second = apply_processed_ack(store, _processed(), expected_event_id=KNOWN_TEST_EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(second["reason"], "duplicate")
            self.assertEqual(second["process_count"], 1)
            reloaded = AckStore(tmp)
            self.assertEqual(reloaded.state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)
            self.assertEqual(reloaded.state["outbox"]["cycle:" + EVAL]["status"], "acked")

    def test_partial_tmp_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            junk = os.path.join(tmp, "ack_state.json.tmp")
            with open(junk, "w", encoding="utf-8") as handle:
                handle.write("{")
            reloaded = AckStore(tmp)
            self.assertEqual(reloaded.state["outbox"]["cycle:" + EVAL]["status"], "pending")
            path = write_processed_file(tmp, _processed(), processing_complete=True)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(load_ack(path)["kind"], "processed")
            self.assertFalse(os.path.exists(path + ".tmp"))
            reloaded.state["outbox"]["cycle:" + EVAL]["marker"] = "saved-with-junk"
            reloaded.save()
            self.assertEqual(open(junk, encoding="utf-8").read(), "{")
            self.assertEqual(AckStore(tmp).state["outbox"]["cycle:" + EVAL]["marker"], "saved-with-junk")

    def test_older_than_15_minutes_stays_pending(self):
        current = datetime(2026, 11, 7, 5, 16, tzinfo=timezone.utc)
        expired_at = current - MAX_ACK_AGE - timedelta(seconds=1)
        self.assertGreater(current - expired_at, MAX_ACK_AGE)
        self.assertGreater(current - expired_at, timedelta(minutes=15))
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            result = apply_processed_ack(
                store, _processed(when=_z(expired_at)),
                expected_event_id=KNOWN_TEST_EVENT, sent_at="2026-11-07T04:00:00Z", now=_z(current),
            )
            self.assertEqual(result["reason"], "expired_or_future")
            self.assertTrue(result["pending"])
            self.assertEqual(result["old_ai_poll"], "keep")
            self.assertEqual(store.state["outbox"]["cycle:" + EVAL]["status"], "pending")
            self.assertNotIn(KNOWN_TEST_EVENT, store.state["test_events"])
            boundary = current - MAX_ACK_AGE
            self.assertEqual(current - boundary, timedelta(minutes=15))
            accepted = apply_processed_ack(
                AckStore(tmp), _processed(when=_z(boundary)),
                expected_event_id=KNOWN_TEST_EVENT, sent_at="2026-11-07T04:00:00Z", now=_z(current),
            )
            self.assertEqual(accepted["reason"], "processed")
            self.assertEqual(accepted["process_count"], 1)

    def test_crash_before_and_after_replace_can_save_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            original = open(store.path, encoding="utf-8").read()
            junk = store.path + ".tmp"
            with open(junk, "w", encoding="utf-8") as handle:
                handle.write("{")
            store._crash = "before_replace"
            store.state["outbox"]["cycle:" + EVAL]["marker"] = "not-yet"
            with self.assertRaises(OSError) as caught:
                store.save()
            self.assertEqual(str(caught.exception), "crash_before_replace")
            self.assertEqual(open(store.path, encoding="utf-8").read(), original)
            self.assertEqual(open(junk, encoding="utf-8").read(), "{")
            private = _private_tmps(tmp)
            self.assertEqual(len(private), 1)
            crashed = open(os.path.join(tmp, private[0]), encoding="utf-8").read()
            self.assertIn("not-yet", crashed)
            revived = AckStore(tmp)
            self.assertEqual(revived.state["outbox"]["cycle:" + EVAL]["status"], "pending")
            self.assertNotIn("marker", revived.state["outbox"]["cycle:" + EVAL])
            revived.state["outbox"]["cycle:" + EVAL]["marker"] = "after-crash"
            revived.save()
            self.assertEqual(AckStore(tmp).state["outbox"]["cycle:" + EVAL]["marker"], "after-crash")
            self.assertEqual(open(junk, encoding="utf-8").read(), "{")
            self.assertEqual(_private_tmps(tmp), private)
            revived.state["outbox"]["cycle:" + EVAL]["marker"] = "replaced"
            revived._crash = "after_replace"
            with self.assertRaises(OSError) as caught:
                revived.save()
            self.assertEqual(str(caught.exception), "crash_after_replace")
            landed = AckStore(tmp)
            self.assertEqual(landed.state["outbox"]["cycle:" + EVAL]["marker"], "replaced")
            landed.state["outbox"]["cycle:" + EVAL]["marker"] = "saved-again"
            landed.save()
            self.assertEqual(AckStore(tmp).state["outbox"]["cycle:" + EVAL]["marker"], "saved-again")
            self.assertEqual(open(junk, encoding="utf-8").read(), "{")

    def test_concurrent_updates_do_not_lose_or_double(self):
        with tempfile.TemporaryDirectory() as tmp:
            second = "t-second"
            _seed(tmp, [(EVAL, KNOWN_TEST_EVENT), ("s2", second)])
            distinct = _compete(tmp, [(KNOWN_TEST_EVENT, EVAL), (second, "s2")])
            self.assertEqual(sorted(item["reason"] for item in distinct), ["processed", "processed"])
            landed = AckStore(tmp)
            self.assertEqual(landed.state["outbox"]["cycle:" + EVAL]["status"], "acked")
            self.assertEqual(landed.state["outbox"]["cycle:s2"]["status"], "acked")
            self.assertEqual(landed.state["outbox"]["cycle:" + EVAL]["keep"], EVAL)
            self.assertEqual(landed.state["outbox"]["cycle:s2"]["keep"], "s2")
            self.assertEqual(landed.state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)
            self.assertEqual(landed.state["test_events"][second]["process_count"], 1)
            final = AckStore(tmp)
            self.assertEqual(final.state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)
            self.assertEqual(final.state["outbox"]["cycle:s2"]["status"], "acked")
            race = os.path.join(tmp, "race")
            _seed(race, [(EVAL, KNOWN_TEST_EVENT)])
            repeated = _compete(race, [(KNOWN_TEST_EVENT, EVAL), (KNOWN_TEST_EVENT, EVAL)])
            self.assertEqual(sorted(item["reason"] for item in repeated), ["duplicate", "processed"])
            self.assertTrue(all(item["process_count"] == 1 and item["old_ai_poll"] == "keep" for item in repeated))
            once = AckStore(race)
            self.assertEqual(once.state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)
            self.assertEqual(once.state["outbox"]["cycle:" + EVAL]["status"], "acked")
            self.assertEqual(once.state["outbox"]["cycle:" + EVAL]["keep"], EVAL)

    def test_consumer_then_producer_lands_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            inbox = os.path.join(tmp, "inbox")
            os.makedirs(inbox)
            _store(state)
            with self.assertRaises(ValueError) as blocked:
                write_processed_file(inbox, _processed(), processing_complete=False)
            self.assertEqual(str(blocked.exception), "processing_not_complete")
            self.assertEqual(os.listdir(inbox), [])
            with self.assertRaises(ValueError):
                write_processed_file(
                    inbox,
                    {"kind": "accepted", "event_id": KNOWN_TEST_EVENT, "schema_valid": True, "status": "accepted"},
                    processing_complete=True,
                )
            path = write_processed_file(inbox, _processed(), processing_complete=True)
            first = _apply_cli(state, path)
            self.assertEqual(first.returncode, 0, first.stderr)
            body = json.loads(first.stdout)
            self.assertEqual(body["reason"], "processed")
            self.assertEqual(body["process_count"], 1)
            self.assertEqual(body["old_ai_poll"], "keep")
            self.assertFalse(body["pending"])
            landed = AckStore(state)
            self.assertEqual(landed.state["outbox"]["cycle:" + EVAL]["status"], "acked")
            self.assertTrue(landed.state["outbox"]["cycle:" + EVAL]["processing_ack"])
            self.assertEqual(landed.state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)
            second = _apply_cli(state, path)
            self.assertEqual(second.returncode, 0, second.stderr)
            again = json.loads(second.stdout)
            self.assertEqual(again["reason"], "duplicate")
            self.assertEqual(again["process_count"], 1)
            self.assertEqual(AckStore(state).state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)
            accepted = os.path.join(inbox, "accepted.json")
            with open(accepted, "w", encoding="utf-8") as handle:
                json.dump({"kind": "accepted", "event_id": KNOWN_TEST_EVENT, "schema_valid": True, "status": "accepted"}, handle)
            rejected = _apply_cli(state, accepted)
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(json.loads(rejected.stdout)["reason"], "accepted_is_not_processed")
            self.assertEqual(json.loads(rejected.stdout)["old_ai_poll"], "keep")
            self.assertEqual(AckStore(state).state["outbox"]["cycle:" + EVAL]["status"], "acked")
            self.assertEqual(AckStore(state).state["test_events"][KNOWN_TEST_EVENT]["process_count"], 1)


def _z(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _private_tmps(directory):
    return sorted(
        name for name in os.listdir(directory)
        if name.startswith("ack_state.json.") and name.endswith(".tmp") and name != "ack_state.json.tmp"
    )


def _seed(directory, items):
    store = open_store(directory)
    for evaluation, event_id in items:
        store.state["outbox"]["cycle:" + evaluation] = {
            "status": "pending", "processing_ack": False, "event_class": "evaluation_cycle",
            "event_id": event_id, "evaluation_id": evaluation, "keep": evaluation,
        }
    store.save()
    return store


def _module_root():
    here = os.path.dirname(inspect.getfile(open_store))
    if os.path.basename(here) == "phase1":
        return os.path.dirname(os.path.dirname(here))
    return here


_WORKER = """
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
try:
    from src.phase1.ack_contract import apply_processed_ack, open_store
except ImportError:
    from ack_contract import apply_processed_ack, open_store
directory, event_id, evaluation, sent, now, gate, slot = sys.argv[2:9]
open(os.path.join(gate, slot), "w").close()
while not os.path.exists(os.path.join(gate, "go")):
    time.sleep(0.01)
store = open_store(directory)
document = {"kind": "processed", "event_id": event_id, "evaluation_id": evaluation, "processed_at": "2026-11-07T05:10:00Z"}
result = apply_processed_ack(store, document, expected_event_id=event_id, sent_at=sent, now=now)
sys.stdout.write(json.dumps(result))
"""


def _compete(directory, pairs):
    gate = os.path.join(directory, "gate-{0}".format(time.time_ns()))
    os.makedirs(gate)
    procs = []
    for index, (event_id, evaluation) in enumerate(pairs):
        slot = "p{0}".format(index)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _WORKER, _module_root(), directory, event_id, evaluation, SENT, NOW, gate, slot],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    deadline = time.time() + 5
    ready = False
    while time.time() < deadline:
        if all(os.path.exists(os.path.join(gate, "p{0}".format(index))) for index in range(len(pairs))):
            ready = True
            break
        time.sleep(0.01)
    if not ready:
        for proc in procs:
            proc.kill()
        raise AssertionError("workers did not reach the gate")
    open(os.path.join(gate, "go"), "w").close()
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=10)
        if proc.returncode != 0:
            raise AssertionError(err)
        results.append(json.loads(out))
    return results


def _apply_cli(directory, path):
    cli = os.path.join(os.path.dirname(inspect.getfile(open_store)), "ack_contract.py")
    return subprocess.run(
        [sys.executable, cli, "apply", "--state-dir", directory, "--file", path, "--expect", KNOWN_TEST_EVENT, "--sent-at", SENT, "--now", NOW],
        capture_output=True, text=True,
    )


if __name__ == "__main__":
    unittest.main()
