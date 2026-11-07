"""受監督搬檔與本機 webhook 迴路。不打 GM，不代填模型決定。"""
import json
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.phase1.ack_contract import apply_processed_ack, load_ack
from src.phase1.box_receiver import accept_delivery, envelope_for_model
from src.phase1.production_transport import (
    HttpsTransport,
    WAKE_FIELDS,
    bind_envelope,
    build_delivery,
    build_envelope,
    commit_judgment,
    hold_dispatch,
    import_staged,
    send_delivery,
    write_transport_config,
)
from src.phase1.session import Store

EVENT = "c-" + "ab" * 16
SENT = "2026-11-07T04:19:30Z"
NOW = "2026-11-07T04:22:00Z"
AUTH = "loopback-auth"


def _seed(path):
    store = Store(path)
    store.state["outbox"]["cycle:s1"] = {"event_id": EVENT, "evaluation_id": "s1", "status": "pending", "processing_ack": False}
    store.save()
    bind_envelope(store, build_envelope(
        {"status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 0, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
        {"candidates": [{"symbol": "ETHUSDT", "setup": "布鹿流", "bar_open_ms": 1762484400000, "ladder": ["1"]}]},
        {"valid": True, "status": "fresh", "age_seconds": 30},
        event_id=EVENT, evaluation_id="s1", sent_at=SENT, reason="orders_open",
    ))
    return store


def _model_judgment(envelope, decision):
    """測試裡這份字典代表模型已經回答。程式自己不會產生它。"""
    import hashlib
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
            "candidate_count": len(envelope["candidates"]),
        },
        "completed_at": NOW,
        "orders_placed": False,
        "ai_execution_proven": False,
    }


class _HTTP(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.posts.append(body)
        if self.headers.get("Authorization") != AUTH:
            self.send_response(401)
            self.end_headers()
            return
        try:
            accept_delivery(json.loads(body.decode("utf-8")), NOW, self.server.inbox)
            self.send_response(200)
        except ValueError:
            self.send_response(400)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class SupervisedDeliveryTest(unittest.TestCase):
    def test_wake_stays_eight_fields_and_oversize_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp)
            document, raw = build_delivery(tmp, EVENT, "s1")
            self.assertEqual(set(document["wake"]), set(WAKE_FIELDS))
            self.assertEqual(len(document["wake"]), 8)
            self.assertFalse(document["context"]["automatic_transport"])
            with self.assertRaises(ValueError) as caught:
                build_delivery(tmp, EVENT, "s1", max_bytes=40)
            self.assertEqual(str(caught.exception), "context_too_large")
            self.assertGreater(len(raw), 40)

    def test_hold_survives_restart_without_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp)
            dest = os.path.join(tmp, "to-box.json")
            first = hold_dispatch(tmp, EVENT, "s1", dest)
            second = hold_dispatch(tmp, EVENT, "s1", dest)
            self.assertEqual(first["status"], "held")
            self.assertFalse(first["http_sent"])
            self.assertFalse(first["unattended"])
            self.assertEqual(second["status"], "held")
            saved = json.loads(open(os.path.join(tmp, "dispatch", EVENT + ".json"), encoding="utf-8").read())
            self.assertEqual(saved["attempts"], 0)
            body = json.loads(open(dest, encoding="utf-8").read())
            self.assertEqual(set(body["wake"]), set(WAKE_FIELDS))
            self.assertFalse(os.path.isdir(os.path.join(tmp, "judgments")))

    def test_loopback_posts_wrapper_once_and_staged_import_applies_on_the_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            mac = os.path.join(tmp, "mac")
            box = os.path.join(tmp, "box")
            _seed(mac)
            cert = os.path.join(tmp, "cert.pem")
            key = os.path.join(tmp, "key.pem")
            subprocess.check_call([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", cert,
                "-days", "1", "-nodes", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HTTP)
            httpd.posts = []
            httpd.inbox = box
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            port = httpd.server_address[1]
            client = HttpsTransport(ssl_context=ssl.create_default_context(cafile=cert))
            config = os.path.join(tmp, "cfg")
            try:
                write_transport_config(config, "https://127.0.0.1:{0}/webhook".format(port), AUTH, ca_file=cert, post_approved=True)
                first = send_delivery(config, mac, EVENT, "s1", client)
                second = send_delivery(config, mac, EVENT, "s1", client)
                self.assertTrue(first["http_sent"])
                self.assertFalse(first["live_connected"])
                self.assertFalse(second["http_sent"])
                self.assertTrue(second["duplicate"])
                self.assertEqual(len(httpd.posts), 1)
                posted = json.loads(httpd.posts[0].decode("utf-8"))
                self.assertEqual(set(posted["wake"]), set(WAKE_FIELDS))
                self.assertEqual(Store(mac).state["outbox"]["cycle:s1"]["status"], "pending")
                shown = envelope_for_model(box, EVENT)
                staged = os.path.join(tmp, "staged.json")
                with open(staged, "w", encoding="utf-8") as handle:
                    json.dump(_model_judgment(shown, "no_trade"), handle)
                bad = json.loads(open(staged, encoding="utf-8").read())
                bad["envelope_sha256"] = "0" * 64
                bad_path = os.path.join(tmp, "bad.json")
                with open(bad_path, "w", encoding="utf-8") as handle:
                    json.dump(bad, handle)
                with self.assertRaises(ValueError) as mismatch:
                    import_staged(mac, bad_path, NOW)
                self.assertEqual(str(mismatch.exception), "digest_mismatch")
                with self.assertRaises(ValueError):
                    import_staged(mac, staged, "2026-11-07T08:00:00Z")
                import_staged(mac, staged, NOW)
                committed = commit_judgment(mac, EVENT, "s1", NOW)
                applied = apply_processed_ack(Store(mac), load_ack(committed), expected_event_id=EVENT, sent_at=SENT, now=NOW)
                self.assertEqual(applied["reason"], "processed")
                self.assertEqual(Store(mac).state["outbox"]["cycle:s1"]["status"], "acked")
                again = apply_processed_ack(Store(mac), load_ack(committed), expected_event_id=EVENT, sent_at=SENT, now=NOW)
                self.assertEqual(again["reason"], "duplicate")
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
