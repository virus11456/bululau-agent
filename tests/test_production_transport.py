"""正式傳輸。本地 HTTPS，不打外網，不讀測試 sender 或金鑰。"""
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.phase1.production_transport import (
    HttpsTransport,
    bind_envelope,
    build_envelope,
    commit_judgment,
    send_wake,
    wake_document,
    write_transport_config,
)
from src.phase1.session import Store

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
EVENT = "c-" + "ab" * 16
SENT = "2026-11-07T04:19:30Z"
NOW = "2026-11-07T04:28:00Z"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.seen = {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://127.0.0.1/elsewhere")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def _judgment(envelope, **overrides):
    import hashlib
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = {
        "schema_version": 1,
        "event_type": "dry_run_judgment",
        "event_id": envelope["event_id"],
        "evaluation_id": envelope["evaluation_id"],
        "envelope_sha256": hashlib.sha256(raw).hexdigest(),
        "decision": "keep_original_ai",
        "basis": {
            "reason": envelope["reason"],
            "as_of": envelope["snapshot"]["as_of"],
            "positions": envelope["snapshot"]["positions"],
            "orders": envelope["snapshot"]["orders"],
            "freshness_status": envelope["freshness"]["status"],
            "candidate_count": len(envelope["candidates"]),
        },
        "completed_at": "2026-11-07T04:22:00Z",
        "orders_placed": False,
        "ai_execution_proven": False,
    }
    body.update(overrides)
    return body


def _envelope():
    return build_envelope(
        {"status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 1, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": [{"symbol": "ETHUSDT", "setup": "布鹿流", "bar_open_ms": 1762484400000, "ladder": ["1"], "totalWalletBalance": "secret"}]},
        {"valid": True, "status": "fresh", "age_seconds": 30},
        event_id=EVENT, evaluation_id="s1", sent_at=SENT, reason="positions_open",
    )


def _seed(tmp):
    store = Store(tmp)
    store.state["outbox"]["cycle:s1"] = {"event_id": EVENT, "evaluation_id": "s1", "status": "pending", "processing_ack": False}
    store.save()
    bind_envelope(store, _envelope())
    return store


def _server(tmp):
    cert = os.path.join(tmp, "cert.pem")
    key = os.path.join(tmp, "key.pem")
    subprocess.check_call([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", cert, "-days", "1", "-nodes",
        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return cert, httpd, httpd.server_address[1]


class ProductionTransportTest(unittest.TestCase):
    def test_local_https_posts_eight_fields_and_raw_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = os.path.join(tmp, "cert.pem")
            key = os.path.join(tmp, "key.pem")
            subprocess.check_call([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", cert,
                "-days", "1", "-nodes", "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_address[1]
            try:
                _seed(os.path.join(tmp, "state"))
                config = os.path.join(tmp, "transport-config")
                write_transport_config(config, "https://127.0.0.1:{0}/wake".format(port), "RawAuthValue")
                client = ssl.create_default_context(cafile=cert)
                result = send_wake(config, os.path.join(tmp, "state"), HttpsTransport(ssl_context=client), EVENT, "s1", NOW)
                self.assertTrue(result["delivered"])
                self.assertFalse(result["processing_ack"])
                self.assertTrue(result["pending"])
                seen = httpd.seen
                self.assertEqual(seen["auth"], "RawAuthValue")
                self.assertNotIn("Bearer ", seen["auth"])
                body = json.loads(seen["body"].decode("utf-8"))
                self.assertEqual(set(body), set(wake_document(_envelope())))
                self.assertEqual(body["event_type"], "evaluation_wake")
                self.assertNotIn("candidates", body)
                self.assertNotIn("snapshot", body)
                self.assertFalse(body["test_channel"])
                write_transport_config(config, "https://127.0.0.1:{0}/redirect".format(port), "RawAuthValue")
                redirected = send_wake(config, os.path.join(tmp, "state"), HttpsTransport(ssl_context=client), EVENT, "s1", NOW)
                self.assertFalse(redirected["delivered"])
                self.assertEqual(redirected["reason"], "redirect_blocked")
                self.assertFalse(redirected["processing_ack"])
            finally:
                httpd.shutdown()
                httpd.server_close()
    def test_refuses_test_sender_dir_and_judgment_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = os.path.join(tmp, "bululau-sender-test")
            os.makedirs(sender)
            with self.assertRaises(ValueError):
                write_transport_config(sender, "https://receiver.example/wake", "RawAuthValue")
            state = os.path.join(tmp, "state")
            _seed(state)
            with open(os.path.join(state, "dry_run_judgment.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s1",
                    "judgment_completed": True, "orders_placed": False, "decision": "no_trade", "completed_at": "2026-11-07T04:22:00Z",
                }, handle)
            proc = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", state, "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["old_ai_poll"], "keep")
            self.assertFalse(os.path.exists(os.path.join(state, "ack-inbox", "processed-" + EVENT + ".json")))
            envelope = json.loads(Path(state, "envelopes", EVENT + ".json").read_text(encoding="utf-8"))
            with open(os.path.join(state, "dry_run_judgment.json"), "w", encoding="utf-8") as handle:
                json.dump(_judgment(envelope), handle)
            done = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", state, "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            applied = subprocess.run(
                [PYTHON, "-m", "src.phase1.cycle", "apply", "--state-dir", state, "--file", os.path.join(state, "ack-inbox", "processed-" + EVENT + ".json"), "--expect", EVENT, "--sent-at", SENT, "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(json.loads(applied.stdout)["reason"], "processed")
            self.assertEqual(json.loads(applied.stdout)["old_ai_poll"], "keep")
            again = subprocess.run(
                [PYTHON, "-m", "src.phase1.cycle", "apply", "--state-dir", state, "--file", os.path.join(state, "ack-inbox", "processed-" + EVENT + ".json"), "--expect", EVENT, "--sent-at", SENT, "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(json.loads(again.stdout)["reason"], "duplicate")
            self.assertEqual(json.loads(again.stdout)["process_count"], 1)

    def test_envelope_omits_balance_and_read_is_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp)
            proc = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "read-envelope", "--state-dir", tmp, "--event-id", EVENT, "--evaluation-id", "s1"],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            body = json.loads(proc.stdout)
            self.assertNotIn("totalWalletBalance", json.dumps(body))
            self.assertFalse(body["snapshot"]["limit_older_than_2h_implemented"])
            self.assertFalse(body["orders_allowed"])
            mismatch = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "read-envelope", "--state-dir", tmp, "--event-id", "c-other", "--evaluation-id", "s1"],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(mismatch.returncode, 1)
            self.assertEqual(json.loads(mismatch.stdout)["reason"], "binding_mismatch")
            self.assertEqual(json.loads(mismatch.stdout)["old_ai_poll"], "keep")

    def test_runner_notifier_stdin_posts_wake_and_leaves_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert, httpd, port = _server(tmp)
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            quant = os.path.join(tmp, "quantboy", "src")
            os.makedirs(quant)
            with open(os.path.join(quant, "prefilter.py"), "w", encoding="utf-8") as handle:
                handle.write("import json,sys\njson.dump({'call_ai':False,'candidates':[]}, sys.stdout)\n")
            with open(os.path.join(quant, "live_trader.py"), "w", encoding="utf-8") as handle:
                handle.write("import json,sys\ncmd=sys.argv[1]\n"
                             "assert cmd in ('manage','status-json')\n"
                             "if cmd=='status-json':\n"
                             " json.dump({'schema_version':1,'command':'status-json','ok':True,'as_of':'2026-11-07T12:19:30+08:00','positions':1,'open_orders':0,'algo_orders':0,'queries':{'account':True,'open_orders':True,'open_algo_orders':True}}, sys.stdout)\n")
            config_dir = os.path.join(tmp, "cfg")
            write_transport_config(config_dir, "https://127.0.0.1:{0}/wake".format(port), "RawAuthValue", ca_file=cert)
            runner_cfg = os.path.join(state, "runner.json")
            with open(runner_cfg, "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "production", "quantboy_root": os.path.join(tmp, "quantboy"), "python": PYTHON,
                    "interval_seconds": 900, "timeout_seconds": 20,
                    "notifier": {"verified": True, "argv": [PYTHON, "-m", "src.phase1.production_transport", "send-wake-stdin", "--config-dir", config_dir, "--state-dir", state]},
                }, handle)
            try:
                proc = subprocess.run(
                    [PYTHON, "-m", "src.phase1.runner", "run", "--state-dir", state, "--config", runner_cfg],
                    cwd=str(ROOT), capture_output=True, text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                body = json.loads(proc.stdout)
                self.assertTrue(body["delivered"], proc.stdout)
                self.assertFalse(body["processing_ack"])
                self.assertEqual(body["old_ai_poll"], "keep")
                self.assertFalse(body["switch_allowed"])
                saved = json.loads(Path(state, "phase1_state.json").read_text(encoding="utf-8"))
                item = saved["outbox"]["cycle:" + body["evaluation_id"]]
                self.assertEqual(item["status"], "pending")
                self.assertTrue(os.path.isfile(os.path.join(state, "envelopes", item["event_id"] + ".json")))
                self.assertEqual(json.loads(httpd.seen["body"].decode("utf-8"))["event_type"], "evaluation_wake")
                self.assertNotIn("RawAuthValue", proc.stdout + proc.stderr)
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_older_pending_survives_newer_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _seed(tmp)
            second_id = "c-" + "cd" * 16
            store.state["outbox"]["cycle:s2"] = {"event_id": second_id, "evaluation_id": "s2", "status": "pending", "processing_ack": False}
            store.save()
            second = build_envelope(
                {"status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
                {"candidates": []},
                {"valid": True, "status": "fresh", "age_seconds": 10},
                event_id=second_id, evaluation_id="s2", sent_at=SENT, reason="orders_open",
            )
            bind_envelope(Store(tmp), second)
            first = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "read-envelope", "--state-dir", tmp, "--event-id", EVENT, "--evaluation-id", "s1"],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["reason"], "positions_open")
            self.assertEqual(json.loads(first.stdout)["snapshot"]["positions"], 1)
            later = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "read-envelope", "--state-dir", tmp, "--event-id", second_id, "--evaluation-id", "s2"],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(json.loads(later.stdout)["reason"], "orders_open")
            stale = _judgment(json.loads(first.stdout), completed_at="2026-11-07T02:00:00Z")
            with open(os.path.join(tmp, "dry_run_judgment.json"), "w", encoding="utf-8") as handle:
                json.dump(stale, handle)
            refused = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", tmp, "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(json.loads(refused.stdout)["reason"], "expired")
            self.assertFalse(os.path.exists(os.path.join(tmp, "ack-inbox")))

    def test_stdin_wake_to_local_https_stays_pending_and_redacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = os.path.join(tmp, "cert.pem")
            key = os.path.join(tmp, "key.pem")
            subprocess.check_call([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", cert, "-days", "1", "-nodes",
                "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_address[1]
            state = os.path.join(tmp, "state")
            _seed(state)
            secret = "super-secret-token"
            config = os.path.join(tmp, "cfg")
            os.makedirs(config)
            try:
                with open(os.path.join(config, "transport.json"), "w", encoding="utf-8") as handle:
                    json.dump({"webhook_url": "https://127.0.0.1:{0}/wake?token={1}".format(port, secret), "authorization": "RawAuthValue"}, handle)
                leaked = subprocess.run(
                    [PYTHON, "-m", "src.phase1.production_transport", "send-wake", "--config-dir", config, "--state-dir", state, "--event-id", EVENT, "--evaluation-id", "s1", "--now", NOW],
                    cwd=str(ROOT), capture_output=True, text=True,
                )
                self.assertEqual(json.loads(leaked.stdout)["reason"], "rejected_url")
                self.assertNotIn(secret, leaked.stdout + leaked.stderr)
                write_transport_config(config, "https://127.0.0.1:{0}/wake".format(port), "RawAuthValue", ca_file=cert)
                envelope = json.loads(Path(state, "envelopes", EVENT + ".json").read_text(encoding="utf-8"))
                wake = wake_document(envelope)
                sent = subprocess.run(
                    [PYTHON, "-m", "src.phase1.production_transport", "send-wake-stdin", "--config-dir", config, "--state-dir", state, "--now", NOW],
                    cwd=str(ROOT), capture_output=True, text=True, input=json.dumps(wake),
                )
                self.assertEqual(sent.returncode, 0, sent.stderr)
                body = json.loads(sent.stdout)
                self.assertTrue(body["delivered"])
                self.assertFalse(body["processing_ack"])
                self.assertTrue(body["pending"])
                self.assertNotIn("RawAuthValue", sent.stdout + sent.stderr)
                saved = json.loads(Path(state, "phase1_state.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["outbox"]["cycle:s1"]["status"], "pending")
                again = subprocess.run(
                    [PYTHON, "-m", "src.phase1.production_transport", "send-wake-stdin", "--config-dir", config, "--state-dir", state, "--now", NOW],
                    cwd=str(ROOT), capture_output=True, text=True, input=json.dumps(wake),
                )
                self.assertTrue(json.loads(again.stdout)["delivered"])
                self.assertEqual(json.loads(Path(state, "phase1_state.json").read_text(encoding="utf-8"))["outbox"]["cycle:s1"]["status"], "pending")
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
