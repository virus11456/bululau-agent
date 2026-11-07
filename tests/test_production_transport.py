"""正式傳輸。本地 HTTPS，不打外網，不讀測試 sender 或金鑰。"""
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.phase1.ack_contract import cycle_key
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
            os.makedirs(os.path.join(state, "judgments"))
            with open(os.path.join(state, "judgments", EVENT + ".json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "schema_version": 1, "event_type": "dry_run_judgment", "event_id": EVENT, "evaluation_id": "s1",
                    "judgment_completed": True, "orders_placed": False, "decision": "no_trade", "completed_at": "2026-11-07T04:22:00Z",
                }, handle)
            proc = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", state, "--event-id", EVENT, "--evaluation-id", "s1", "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["old_ai_poll"], "keep")
            self.assertFalse(os.path.exists(os.path.join(state, "ack-inbox", "processed-" + EVENT + ".json")))
            envelope = json.loads(Path(state, "envelopes", EVENT + ".json").read_text(encoding="utf-8"))
            with open(os.path.join(state, "judgments", EVENT + ".json"), "w", encoding="utf-8") as handle:
                json.dump(_judgment(envelope), handle)
            done = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", state, "--event-id", EVENT, "--evaluation-id", "s1", "--now", NOW],
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
            os.makedirs(os.path.join(tmp, "judgments"))
            with open(os.path.join(tmp, "judgments", EVENT + ".json"), "w", encoding="utf-8") as handle:
                json.dump(stale, handle)
            refused = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", tmp, "--event-id", EVENT, "--evaluation-id", "s1", "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(json.loads(refused.stdout)["reason"], "expired")
            self.assertFalse(os.path.exists(os.path.join(tmp, "ack-inbox")))

    def test_interrupt_before_replace_is_not_a_trusted_envelope(self):
        """行程在換檔前中斷。不是斷電測試。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _seed(tmp)
            path = os.path.join(tmp, "envelopes", EVENT + ".json")
            original = Path(path).read_bytes()
            other = build_envelope(
                {"status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 9, "orders": 9, "manage_error": False, "manage_complete": True, "status_trusted": True},
                {"candidates": []},
                {"valid": True, "status": "fresh", "age_seconds": 1},
                event_id=EVENT, evaluation_id="s1", sent_at=SENT, reason="positions_open",
            )
            with self.assertRaises(OSError):
                bind_envelope(Store(tmp), other, crash="before_replace")
            self.assertEqual(Path(path).read_bytes(), original)
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8"))["snapshot"]["positions"], 1)
            fresh = Store(tmp)
            self.assertEqual(fresh.state["outbox"]["cycle:s1"]["envelope_sha256"], store.state["outbox"]["cycle:s1"]["envelope_sha256"])
            read = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "read-envelope", "--state-dir", tmp, "--event-id", EVENT, "--evaluation-id", "s1"],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(json.loads(read.stdout)["snapshot"]["positions"], 1)

    def test_two_judgments_do_not_drop_the_other_pending(self):
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
            os.makedirs(os.path.join(tmp, "judgments"))
            first_env = json.loads(Path(tmp, "envelopes", EVENT + ".json").read_text(encoding="utf-8"))
            second_env = json.loads(Path(tmp, "envelopes", second_id + ".json").read_text(encoding="utf-8"))
            with open(os.path.join(tmp, "judgments", EVENT + ".json"), "w", encoding="utf-8") as handle:
                json.dump(_judgment(first_env), handle)
            with open(os.path.join(tmp, "judgments", second_id + ".json"), "w", encoding="utf-8") as handle:
                json.dump(_judgment(second_env, decision="no_trade"), handle)
            one = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", tmp, "--event-id", EVENT, "--evaluation-id", "s1", "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(one.returncode, 0, one.stderr)
            saved = json.loads(Path(tmp, "phase1_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["outbox"]["cycle:s2"]["status"], "pending")
            self.assertTrue(os.path.isfile(os.path.join(tmp, "envelopes", second_id + ".json")))
            swapped = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", tmp, "--event-id", second_id, "--evaluation-id", "s1", "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(swapped.returncode, 1)
            self.assertIn(json.loads(swapped.stdout)["reason"], ("binding_mismatch", "digest_mismatch"))
            two = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", tmp, "--event-id", second_id, "--evaluation-id", "s2", "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(two.returncode, 0, two.stderr)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "ack-inbox", "processed-" + EVENT + ".json")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "ack-inbox", "processed-" + second_id + ".json")))

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

    def _pending(self, state, event_id, evaluation_id):
        store = Store(state)
        store.state["outbox"][cycle_key(evaluation_id)] = {
            "event_id": event_id, "evaluation_id": evaluation_id, "status": "pending", "processing_ack": False,
        }
        store.save()
        return store

    def _race(self, state, event_id, evaluation_id, positions, ready, result, go):
        worker = (
            "import json,os,sys,time\n"
            "sys.path.insert(0, os.environ['BIND_ROOT'])\n"
            "open(os.environ['BIND_READY'], 'w').close()\n"
            "while not os.path.exists(os.environ['BIND_GO']):\n"
            "    time.sleep(0.01)\n"
            "from src.phase1.production_transport import bind_envelope, build_envelope\n"
            "from src.phase1.session import Store\n"
            "envelope = build_envelope(\n"
            "    {'status':'ok','as_of':'2026-11-07T12:19:30+08:00','positions':int(os.environ['BIND_POS']),'orders':0,'manage_error':False,'manage_complete':True,'status_trusted':True},\n"
            "    {'candidates':[]}, {'valid':True,'status':'fresh','age_seconds':1},\n"
            "    event_id=os.environ['BIND_EVENT'], evaluation_id=os.environ['BIND_EVAL'], sent_at=os.environ['BIND_SENT'], reason='positions_open')\n"
            "try:\n"
            "    digest = bind_envelope(Store(os.environ['BIND_STATE']), envelope)\n"
            "    body = {'ok': True, 'digest': digest}\n"
            "except ValueError as exc:\n"
            "    body = {'ok': False, 'reason': str(exc)}\n"
            "with open(os.environ['BIND_RESULT'], 'w', encoding='utf-8') as handle:\n"
            "    json.dump(body, handle)\n"
        )
        env = os.environ.copy()
        env.update({
            "BIND_ROOT": str(ROOT), "BIND_READY": ready, "BIND_GO": go, "BIND_RESULT": result,
            "BIND_STATE": state, "BIND_EVENT": event_id, "BIND_EVAL": evaluation_id,
            "BIND_POS": str(positions), "BIND_SENT": SENT,
        })
        proc = subprocess.Popen([PYTHON, "-c", worker], cwd=str(ROOT), env=env)
        return proc

    def test_two_processes_one_envelope_cannot_both_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._pending(tmp, EVENT, "s1")
            ready_a = os.path.join(tmp, "ready-a")
            ready_b = os.path.join(tmp, "ready-b")
            go = os.path.join(tmp, "go")
            left = self._race(tmp, EVENT, "s1", 1, ready_a, os.path.join(tmp, "result-a"), go)
            right = self._race(tmp, EVENT, "s1", 7, ready_b, os.path.join(tmp, "result-b"), go)
            for path in (ready_a, ready_b):
                for _ in range(200):
                    if os.path.exists(path):
                        break
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(os.path.join(tmp, "result-a")))
            open(go, "w").close()
            self.assertEqual(left.wait(timeout=15), 0)
            self.assertEqual(right.wait(timeout=15), 0)
            results = [json.loads(Path(tmp, name).read_text(encoding="utf-8")) for name in ("result-a", "result-b")]
            self.assertEqual(sorted(item["ok"] for item in results), [False, True])
            self.assertEqual([item for item in results if not item["ok"]][0]["reason"], "immutable_conflict")
            winner = [item for item in results if item["ok"]][0]["digest"]
            raw = Path(tmp, "envelopes", EVENT + ".json").read_bytes().strip()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), winner)
            fresh = Store(tmp)
            self.assertEqual(fresh.state["outbox"]["cycle:s1"]["envelope_sha256"], winner)
            read = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "read-envelope", "--state-dir", tmp, "--event-id", EVENT, "--evaluation-id", "s1"],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(read.returncode, 0, read.stderr)
            body = json.loads(read.stdout)
            self.assertEqual(hashlib.sha256(_canonical_dump(body)).hexdigest(), winner)
            self.assertIn(body["snapshot"]["positions"], (1, 7))

    def test_same_content_is_idempotent_and_other_events_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            second = "c-" + "cd" * 16
            self._pending(tmp, EVENT, "s1")
            self._pending(tmp, second, "s2")
            ready_a = os.path.join(tmp, "ready-a")
            ready_b = os.path.join(tmp, "ready-b")
            go = os.path.join(tmp, "go")
            left = self._race(tmp, EVENT, "s1", 1, ready_a, os.path.join(tmp, "result-a"), go)
            right = self._race(tmp, EVENT, "s1", 1, ready_b, os.path.join(tmp, "result-b"), go)
            for path in (ready_a, ready_b):
                for _ in range(200):
                    if os.path.exists(path):
                        break
                    time.sleep(0.01)
            open(go, "w").close()
            self.assertEqual(left.wait(timeout=15), 0)
            self.assertEqual(right.wait(timeout=15), 0)
            results = [json.loads(Path(tmp, name).read_text(encoding="utf-8")) for name in ("result-a", "result-b")]
            self.assertTrue(all(item["ok"] for item in results))
            self.assertEqual(results[0]["digest"], results[1]["digest"])
            other = build_envelope(
                {"status": "ok", "as_of": "2026-11-07T12:19:30+08:00", "positions": 2, "orders": 1, "manage_error": False, "manage_complete": True, "status_trusted": True},
                {"candidates": []}, {"valid": True, "status": "fresh", "age_seconds": 1},
                event_id=second, evaluation_id="s2", sent_at=SENT, reason="orders_open",
            )
            bind_envelope(Store(tmp), other)
            saved = Store(tmp).state["outbox"]
            self.assertEqual(saved["cycle:s1"]["envelope_sha256"], results[0]["digest"])
            self.assertTrue(saved["cycle:s2"]["envelope_sha256"])
            self.assertTrue(os.path.isfile(os.path.join(tmp, "envelopes", EVENT + ".json")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "envelopes", second + ".json")))

    def test_killed_lock_holder_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._pending(tmp, EVENT, "s1")
            directory = os.path.join(tmp, "envelopes")
            os.makedirs(directory)
            held = os.path.join(tmp, "held")
            holder = subprocess.Popen([PYTHON, "-c", (
                "import os,sys,time\n"
                "sys.path.insert(0, os.environ['BIND_ROOT'])\n"
                "from src.phase1.ack_store import directory_lock\n"
                "with directory_lock(os.environ['BIND_DIR'], 'publish.lock'):\n"
                "    open(os.environ['BIND_HELD'], 'w').close()\n"
                "    time.sleep(30)\n"
            )], cwd=str(ROOT), env=dict(os.environ, BIND_ROOT=str(ROOT), BIND_DIR=directory, BIND_HELD=held))
            for _ in range(200):
                if os.path.exists(held):
                    break
                time.sleep(0.01)
            self.assertTrue(os.path.exists(held))
            holder.kill()
            holder.wait(timeout=5)
            digest = bind_envelope(Store(tmp), _envelope())
            self.assertTrue(digest)
            self.assertEqual(Store(tmp).state["outbox"]["cycle:s1"]["envelope_sha256"], digest)

    def test_context_export_is_not_a_shared_directory_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = os.path.join(tmp, "mac", "CYCLE_DIR")
            right = os.path.join(tmp, "box", "CYCLE_DIR")
            os.makedirs(left)
            self._pending(left, EVENT, "s1")
            bind_envelope(Store(left), _envelope())
            exported = os.path.join(tmp, "context.json")
            proc = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "export-context", "--state-dir", left, "--event-id", EVENT, "--evaluation-id", "s1", "--out", exported],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            text = Path(exported).read_text(encoding="utf-8")
            self.assertNotIn("authorization", text)
            self.assertNotIn("BINANCE_API", text)
            self.assertNotIn("automatic_transport\":true", text.replace(" ", ""))
            checked = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "verify-context", "--file", exported, "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertFalse(json.loads(checked.stdout)["same_store"])
            self.assertFalse(json.loads(checked.stdout)["ai_execution_proven"])
            same = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "verify-same-store", "--state-dir", left, "--file", exported],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(same.returncode, 0, same.stderr)
            same_body = json.loads(same.stdout)
            self.assertTrue(same_body["logical_id_match"])
            self.assertFalse(same_body["physical_store_proven"])
            self.assertFalse(same_body["same_store"])
            self.assertTrue(same_body["ack_requires_original_producer_apply"])
            os.makedirs(right)
            Store(right)
            other = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "verify-same-store", "--state-dir", right, "--file", exported],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(other.returncode, 1)
            self.assertEqual(json.loads(other.stdout)["reason"], "different_store")
            self.assertFalse(json.loads(other.stdout)["physical_store_proven"])
            copied = os.path.join(tmp, "copied-producer")
            shutil.copytree(left, copied)
            copied_check = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "verify-same-store", "--state-dir", copied, "--file", exported],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            copied_body = json.loads(copied_check.stdout)
            self.assertTrue(copied_body["logical_id_match"])
            self.assertFalse(copied_body["physical_store_proven"])
            self.assertFalse(copied_body["same_store"])
            from src.phase1.ack_contract import apply_processed_ack, load_ack, write_processed_file
            inbox = os.path.join(copied, "ack-inbox")
            os.makedirs(inbox)
            processed = write_processed_file(inbox, {
                "kind": "processed", "event_id": EVENT, "evaluation_id": "s1", "processed_at": "2026-11-07T04:22:00Z",
            }, processing_complete=True)
            apply_processed_ack(Store(copied), load_ack(processed), expected_event_id=EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(Store(left).state["outbox"]["cycle:s1"]["status"], "pending")
            self.assertEqual(Store(copied).state["outbox"]["cycle:s1"]["status"], "acked")
            self.assertFalse(os.path.exists(os.path.join(left, "judgments")))
            stale = json.loads(text)
            stale["sent_at"] = "2026-11-07T02:00:00Z"
            stale["envelope"]["sent_at"] = stale["sent_at"]
            stale["envelope_sha256"] = hashlib.sha256(_canonical_dump(stale["envelope"])).hexdigest()
            stale_path = os.path.join(tmp, "stale.json")
            Path(stale_path).write_text(json.dumps(stale), encoding="utf-8")
            expired = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "verify-context", "--file", stale_path, "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(json.loads(expired.stdout)["reason"], "expired")

    def test_consumer_crash_points_leave_pending_until_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp)
            missing = subprocess.run(
                [PYTHON, "-m", "src.phase1.production_transport", "commit-judgment", "--state-dir", tmp, "--event-id", EVENT, "--evaluation-id", "s1", "--now", NOW],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(json.loads(missing.stdout)["reason"], "rejected_judgment")
            self.assertEqual(Store(tmp).state["outbox"]["cycle:s1"]["status"], "pending")
            os.makedirs(os.path.join(tmp, "judgments"))
            envelope = json.loads(Path(tmp, "envelopes", EVENT + ".json").read_text(encoding="utf-8"))
            with open(os.path.join(tmp, "judgments", EVENT + ".json"), "w", encoding="utf-8") as handle:
                json.dump(_judgment(envelope), handle)
            self.assertFalse(os.path.exists(os.path.join(tmp, "ack-inbox")))
            self.assertEqual(Store(tmp).state["outbox"]["cycle:s1"]["status"], "pending")
            committed = commit_judgment(tmp, EVENT, "s1", NOW)
            self.assertTrue(os.path.isfile(committed))
            self.assertEqual(Store(tmp).state["outbox"]["cycle:s1"]["status"], "pending")
            from src.phase1.ack_contract import apply_processed_ack, load_ack
            first = apply_processed_ack(Store(tmp), load_ack(committed), expected_event_id=EVENT, sent_at=SENT, now=NOW)
            second = apply_processed_ack(Store(tmp), load_ack(committed), expected_event_id=EVENT, sent_at=SENT, now=NOW)
            self.assertEqual(first["reason"], "processed")
            self.assertEqual(second["reason"], "duplicate")
            self.assertEqual(second["process_count"], 1)


def _canonical_dump(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
