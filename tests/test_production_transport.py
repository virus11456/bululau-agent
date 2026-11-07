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
                result = send_wake(config, os.path.join(tmp, "state"), HttpsTransport(ssl_context=client))
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
                blocked = send_wake(config, os.path.join(tmp, "state"), HttpsTransport(ssl_context=client))
                write_transport_config(config, "https://127.0.0.1:{0}/redirect".format(port), "RawAuthValue")
                redirected = send_wake(config, os.path.join(tmp, "state"), HttpsTransport(ssl_context=client))
                self.assertFalse(redirected["delivered"])
                self.assertEqual(redirected["reason"], "redirect_blocked")
                self.assertFalse(redirected["processing_ack"])
            finally:
                httpd.shutdown()
                httpd.server_close()
            del blocked

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
                    "judgment_completed": False, "orders_placed": False, "decision": "no_trade", "completed_at": "2026-11-07T04:22:00Z",
                }, handle)
            proc = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", state],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["old_ai_poll"], "keep")
            self.assertFalse(os.path.exists(os.path.join(state, "ack-inbox", "processed-" + EVENT + ".json")))
            with open(os.path.join(state, "dry_run_judgment.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s1",
                    "judgment_completed": True, "orders_placed": False, "decision": "keep_original_ai", "completed_at": "2026-11-07T04:22:00Z",
                }, handle)
            done = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", state],
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


if __name__ == "__main__":
    unittest.main()
