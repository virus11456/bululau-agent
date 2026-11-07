"""離線判斷回傳。不連 Vercel、不 POST 正式位址、不代填模型決定。"""
import hashlib
import json
import os
import tempfile
import threading
import unittest

from src.phase1.production_transport import bind_envelope, build_envelope
from src.phase1.return_adapter import (
    VERCEL_BLOB_ATOMIC_CREATE_PROVEN,
    FakeReturnHttp,
    InstallationStore,
    download_one,
    judgment_put,
    pending_event_ids,
    poll_pending,
)
from src.phase1.session import Store
from src.phase1.wake_gate import decide

NOW = "2026-11-07T04:22:00Z"
SENT = "2026-11-07T04:19:30Z"
EVENT = "c-" + "ab" * 16
INSTALL = "inst-local-1"


def _envelope():
    return build_envelope(
        {"status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": []},
        {"valid": True, "status": "fresh", "age_seconds": 1},
        event_id=EVENT, evaluation_id="s1", sent_at=SENT, reason="orders_open",
    )


def _judgment(envelope, decision="keep_original_ai"):
    raw = __import__("json").dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 1,
        "event_type": "dry_run_judgment",
        "event_id": envelope["event_id"],
        "evaluation_id": envelope["evaluation_id"],
        "envelope_sha256": hashlib.sha256(raw).hexdigest(),
        "decision": decision,
        "basis": {
            "reason": envelope["reason"],
            "as_of": envelope["snapshot"]["as_of"],
            "positions": envelope["snapshot"]["positions"],
            "orders": envelope["snapshot"]["orders"],
            "freshness_status": envelope["freshness"]["status"],
            "candidate_count": 0,
        },
        "completed_at": NOW,
        "orders_placed": False,
        "ai_execution_proven": False,
    }


def _seed(path, envelope):
    store = Store(path)
    store.state["outbox"]["cycle:s1"] = {"event_id": EVENT, "evaluation_id": "s1", "status": "pending", "processing_ack": False}
    store.save()
    bind_envelope(store, envelope)
    return store


class ReturnAdapterTest(unittest.TestCase):
    def test_exclusive_create_conflicts_without_get_then_put(self):
        self.assertFalse(VERCEL_BLOB_ATOMIC_CREATE_PROVEN)
        with tempfile.TemporaryDirectory() as tmp:
            store = InstallationStore(tmp)
            envelope = _envelope()
            body_a = judgment_put(INSTALL, EVENT, "s1", _judgment(envelope)["envelope_sha256"], _judgment(envelope))
            other = _judgment(envelope, "no_trade")
            body_b = judgment_put(INSTALL, EVENT, "s1", other["envelope_sha256"], other)
            results = []
            barrier = threading.Barrier(2)

            def worker(body):
                barrier.wait()
                try:
                    results.append(store.create(INSTALL, EVENT, body))
                except ValueError as exc:
                    results.append(str(exc))

            threads = [threading.Thread(target=worker, args=(body_a,)), threading.Thread(target=worker, args=(body_b,))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertIn("conflict", results)
            self.assertTrue(any(isinstance(item, dict) and item.get("created") for item in results))
            with open(store._path(INSTALL, EVENT), encoding="utf-8") as handle:
                saved = __import__("json").load(handle)
            duplicate = store.create(INSTALL, EVENT, saved)
            self.assertTrue(duplicate["duplicate"])
            self.assertFalse(duplicate["created"])
            huge = dict(saved)
            huge["pad"] = "x" * (70 * 1024)
            with self.assertRaises(ValueError) as caught:
                store.create(INSTALL, "c-" + "ef" * 16, huge)
            self.assertEqual(str(caught.exception), "too_large")

    def test_fake_http_scope_backoff_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            mac = os.path.join(tmp, "mac")
            envelope = _envelope()
            _seed(mac, envelope)
            judgment = _judgment(envelope)
            body = judgment_put(INSTALL, EVENT, "s1", judgment["envelope_sha256"], judgment)
            secrets = {"publisher": "pub-local", "reader": "read-local"}
            http = FakeReturnHttp(InstallationStore(os.path.join(tmp, "blob")), secrets, INSTALL)
            publisher = {"installation_id": INSTALL, "role": "publisher", "secret": "pub-local", "key_id": "k1"}
            reader = {"installation_id": INSTALL, "role": "reader", "secret": "read-local", "key_id": "k2"}
            other = {"installation_id": "inst-other", "role": "publisher", "secret": "pub-local", "key_id": "k1"}
            status, _headers, _raw = http.request("PUT", "/v1/installations/inst-other/judgments/" + EVENT, {}, __import__("json").dumps(body).encode(), other)
            self.assertEqual(status, 403)
            status, _headers, raw = http.request("PUT", "/v1/installations/{0}/judgments/{1}".format(INSTALL, EVENT), {}, __import__("json").dumps(body).encode(), publisher)
            self.assertEqual(status, 201)
            status, _headers, _raw = http.request("PUT", "/v1/installations/{0}/judgments/{1}".format(INSTALL, EVENT), {}, __import__("json").dumps(body).encode(), publisher)
            self.assertEqual(status, 200)
            self.assertIn(b"duplicate", raw)
            status, _headers, _raw = http.request("GET", "/v1/installations/{0}/judgments/{1}".format(INSTALL, EVENT), {}, None, publisher)
            self.assertEqual(status, 403)
            http.fail = "redirect"
            result = download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)
            self.assertEqual(result["reason"], "redirect_blocked")
            self.assertFalse(result["processed"])
            http.fail = "tls"
            self.assertEqual(download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)["reason"], "tls_rejected")
            http.fail = "timeout"
            self.assertEqual(download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)["reason"], "timeout")
            http.fail = "disconnect"
            self.assertEqual(download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)["reason"], "disconnect")
            http.fail = "backoff"
            backoff = download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)
            self.assertEqual(backoff["reason"], "backoff")
            self.assertEqual(backoff["retry_after"], "5")
            self.assertFalse(backoff["processed"])
            http.fail = "unauthorized"
            self.assertEqual(download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)["reason"], "auth_stopped")
            http.secrets_by_role = {"publisher": "pub-local", "reader": "nope"}
            stopped = download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)
            self.assertEqual(stopped["reason"], "auth_stopped")
            self.assertTrue(stopped["alert_once"])
            http.secrets_by_role = secrets
            missing = FakeReturnHttp(InstallationStore(os.path.join(tmp, "empty")), secrets, INSTALL)
            self.assertEqual(download_one(mac, missing, reader, INSTALL, EVENT, "s1", NOW, SENT)["reason"], "not_ready")
            self.assertEqual(Store(mac).state["outbox"]["cycle:s1"]["status"], "pending")
            applied = download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)
            self.assertEqual(applied["reason"], "processed")
            self.assertFalse(applied["vercel_blob_atomic_create_proven"])
            again = download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)
            self.assertEqual(again["reason"], "duplicate")
            self.assertEqual(Store(mac).state["outbox"]["cycle:s1"]["status"], "acked")
            self.assertEqual(pending_event_ids(mac), [])

    def test_mac_gate_suppresses_only_a_complete_flat_round(self):
        from pathlib import Path
        snap = {
            "status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 0,
            "open_orders": 0, "algo_orders": 0, "orders": 0, "manage_error": False,
            "status_trusted": True, "queries_ok": True,
            "queries": {"account": True, "open_orders": True, "open_algo_orders": True},
        }
        calls = []

        class Spy:
            def notify(self, event):
                calls.append(event)
                return {"status": "blocked", "delivered": False, "http_sent": False}

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            quiet = decide(store, snap, {"candidates": []}, now="2026-11-07T12:20:00+08:00", scan_id="q1", notifier=Spy(), mac_gate=True)
            self.assertEqual(quiet["reason"], "quiet_flat_book")
            self.assertEqual(calls, [])
            self.assertEqual(len(store.state["suppressed_quiet"]), 1)
            self.assertNotIn("totalWalletBalance", json.dumps(store.state["suppressed_quiet"]))
            again = decide(store, snap, {"candidates": []}, now="2026-11-07T12:20:00+08:00", scan_id="q1", notifier=Spy(), mac_gate=True)
            self.assertEqual(again["reason"], "quiet_flat_book")
            self.assertEqual(len(store.state["suppressed_quiet"]), 1)
            store.state["outbox"]["cycle:old"] = {"event_id": EVENT, "evaluation_id": "old", "status": "pending"}
            store.save()
            pending = decide(store, snap, {"candidates": []}, now="2026-11-07T12:20:00+08:00", scan_id="q2", notifier=Spy(), mac_gate=True)
            self.assertEqual(pending["reason"], "pending_return")
            self.assertTrue(pending["cycle_needs_ai"])
            self.assertEqual(pending["old_ai_poll"], "keep")
            unknown = decide(store, dict(snap, queries_ok=False), {"candidates": []}, now="2026-11-07T12:20:00+08:00", scan_id="q3", notifier=Spy(), mac_gate=True)
            self.assertEqual(unknown["reason"], "snapshot_unknown")
            self.assertEqual(unknown["old_ai_poll"], "keep")
            missing_algo = dict(snap)
            missing_algo.pop("algo_orders")
            hidden = decide(store, missing_algo, {"candidates": []}, now="2026-11-07T12:20:00+08:00", scan_id="q4", notifier=Spy(), mac_gate=True)
            self.assertEqual(hidden["reason"], "snapshot_unknown")
            skewed = decide(store, snap, {"candidates": []}, now="2026-11-07T12:00:00+08:00", scan_id="q5", notifier=Spy(), mac_gate=True)
            self.assertEqual(skewed["reason"], "snapshot_blocked_clock_skew")
            candidate = {"symbol": "ETHUSDT", "setup": "break", "bar_time": "2026-11-07T12:00:00+08:00"}
            woken = decide(store, snap, {"candidates": [candidate]}, now="2026-11-07T12:20:00+08:00", scan_id="q6", notifier=Spy(), mac_gate=True)
            self.assertEqual(woken["reason"], "candidates_present")
            self.assertTrue(woken["new_entry_ai"] if "new_entry_ai" in woken else woken["cycle_needs_ai"])
            self.assertTrue(calls)

    def test_poll_backoff_deadline_and_stage_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            mac = os.path.join(tmp, "mac")
            envelope = _envelope()
            _seed(mac, envelope)
            judgment = _judgment(envelope)
            body = judgment_put(INSTALL, EVENT, "s1", judgment["envelope_sha256"], judgment)
            secrets = {"publisher": "pub-local", "reader": "read-local"}
            http = FakeReturnHttp(InstallationStore(os.path.join(tmp, "blob")), secrets, INSTALL)
            publisher = {"installation_id": INSTALL, "role": "publisher", "secret": "pub-local", "key_id": "k1"}
            reader = {"installation_id": INSTALL, "role": "reader", "secret": "read-local", "key_id": "k2"}
            raw = json.dumps(body).encode("utf-8")
            other = dict(body)
            other["judgment"] = dict(judgment, decision="no_trade", completed_at=NOW)
            other["judgment_sha256"] = "0" * 64
            status, _headers, _payload = http.request("PUT", "/v1/installations/{0}/judgments/{1}".format(INSTALL, EVENT), {}, raw, reader)
            self.assertEqual(status, 403)
            status, _headers, _payload = http.request("PUT", "/v1/installations/{0}/judgments/{1}".format(INSTALL, EVENT), {}, raw, publisher)
            self.assertEqual(status, 201)
            conflict, _headers, _payload = http.request("PUT", "/v1/installations/{0}/judgments/{1}".format(INSTALL, EVENT), {}, json.dumps(other).encode("utf-8"), publisher)
            self.assertEqual(conflict, 409)
            http.failures = ["backoff", "backoff"]
            clock = {"now": 0.0}
            waits = []

            def sleep(seconds):
                waits.append(seconds)
                clock["now"] += seconds

            applied = poll_pending(
                mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT,
                sleep=sleep, clock=lambda: clock["now"], jitter=lambda _attempt: 0,
            )
            self.assertEqual(waits, [5, 10])
            self.assertEqual(applied["reason"], "processed")
            self.assertEqual(applied["process_count"], 1)
            self.assertFalse(applied["ai_execution_proven"])
            self.assertFalse(applied["vercel_blob_atomic_create_proven"])
            self.assertEqual(Store(mac).state["test_events"][EVENT]["process_count"], 1)
            self.assertNotIn("POST", http.calls)
            again = download_one(mac, http, reader, INSTALL, EVENT, "s1", NOW, SENT)
            self.assertEqual(again["reason"], "duplicate")
            self.assertEqual(again["process_count"], 1)

            fresh = os.path.join(tmp, "fresh")
            _seed(fresh, envelope)
            stalled = FakeReturnHttp(http.store, secrets, INSTALL)
            stalled.failures = ["unavailable"]
            clock["now"] = 0.0
            waits.clear()
            stopped = poll_pending(
                fresh, stalled, reader, INSTALL, EVENT, "s1", NOW, SENT,
                sleep=sleep, clock=lambda: clock["now"], jitter=lambda _attempt: 0, deadline_seconds=4,
            )
            self.assertEqual(stopped["reason"], "deadline")
            self.assertEqual(Store(fresh).state["outbox"]["cycle:s1"]["status"], "pending")
            self.assertEqual(waits, [])

            resumed = os.path.join(tmp, "resume")
            _seed(resumed, envelope)
            from src.phase1.return_adapter import _save_stage
            _save_stage(resumed, EVENT, {"phase": "downloaded", "event_id": EVENT, "body": body, "judgment_sha256": body["judgment_sha256"]})
            blind = FakeReturnHttp(InstallationStore(os.path.join(tmp, "unused")), secrets, INSTALL)
            blind.fail = "disconnect"
            recovered = download_one(resumed, blind, reader, INSTALL, EVENT, "s1", NOW, SENT)
            self.assertEqual(recovered["reason"], "processed")
            self.assertEqual(blind.calls, [])
            self.assertEqual(Store(resumed).state["outbox"]["cycle:s1"]["status"], "acked")
            replay = dict(json.load(open(os.path.join(resumed, "return-recovery", EVENT + ".json"), encoding="utf-8")))
            replay["phase"] = "committed"
            _save_stage(resumed, EVENT, replay)
            second = download_one(resumed, blind, reader, INSTALL, EVENT, "s1", NOW, SENT)
            self.assertEqual(second["reason"], "duplicate")
            self.assertEqual(second["process_count"], 1)
            self.assertEqual(json.load(open(os.path.join(resumed, "judgments", EVENT + ".json"), encoding="utf-8"))["completed_at"], NOW)


if __name__ == "__main__":
    unittest.main()
