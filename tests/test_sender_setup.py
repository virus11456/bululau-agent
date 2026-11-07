"""sender 設定與五欄測試契約。mock transport，不外發。"""
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone

from src.phase1.sender_setup import (
    configure_interactive,
    load_or_create_event,
    main,
    send_saved_test,
    validate_event,
    write_config,
)


SECRET_URL = "https://example.test/hook"
SECRET_AUTH = "panel-field-raw-value"


class Transport:
    def __init__(self, status=200, calls=1):
        self.status = status
        self.calls = 0
        self.max_calls = calls
        self.seen = []

    def post(self, url, authorization, body):
        self.calls += 1
        if self.calls > self.max_calls:
            raise AssertionError("followed another request")
        self.seen.append(json.loads(body.decode("utf-8")))
        self.last_auth = authorization
        return self.status


class SenderSetupTest(unittest.TestCase):
    def test_configure_writes_private_file_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = Transport()
            code = main(
                ["configure"],
                directory=tmp,
                read_pair=lambda: (SECRET_URL, SECRET_AUTH),
                transport=transport,
            )
            self.assertEqual(code, 0)
            self.assertEqual(transport.calls, 0)
            self.assertEqual(stat.S_IMODE(os.stat(tmp).st_mode), 0o700)
            path = os.path.join(tmp, "sender.json")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            saved = json.loads(open(path, encoding="utf-8").read())
            self.assertEqual(saved["webhook_url"], SECRET_URL)
            self.assertEqual(saved["authorization"], SECRET_AUTH)

    def test_rejects_bad_url_and_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            for url, auth in (
                ("http://example.test/hook", SECRET_AUTH),
                ("https://example.test/hook\r\nX: y", SECRET_AUTH),
                (SECRET_URL, "value\nBearer x"),
            ):
                with self.assertRaises(ValueError):
                    write_config(tmp, url, auth)

    def test_event_contract_and_resend_keeps_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(tmp, SECRET_URL, SECRET_AUTH)
            now = datetime(2026, 11, 7, 4, 20, tzinfo=timezone.utc)
            first = load_or_create_event(tmp, now)
            validate_event(first)
            self.assertEqual(set(first), {"schema_version", "event_type", "event_id", "test_only", "sent_at"})
            self.assertIs(first["test_only"], True)
            self.assertEqual(first["schema_version"], 1)
            transport = Transport(calls=2)
            send_saved_test(tmp, transport, now)
            again = send_saved_test(tmp, transport, datetime(2026, 11, 7, 5, 0, tzinfo=timezone.utc))
            self.assertEqual(transport.seen[0]["event_id"], transport.seen[1]["event_id"])
            self.assertEqual(transport.seen[0], transport.seen[1])
            self.assertEqual(again["event_id"], first["event_id"])
            self.assertFalse(again["processing_ack"])
            self.assertTrue(again["pending"])
            self.assertTrue(again["test_channel"])
            self.assertFalse(again["position_channel"])
            self.assertEqual(transport.last_auth, SECRET_AUTH)

    def test_http_success_and_redirect_do_not_mark_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_config(tmp, SECRET_URL, SECRET_AUTH)
            now = datetime(2026, 11, 7, 4, 20, tzinfo=timezone.utc)
            ok = send_saved_test(tmp, Transport(200), now)
            self.assertTrue(ok["ok"])
            self.assertFalse(ok["processing_ack"])
            self.assertTrue(ok["pending"])
            blocked = send_saved_test(tmp, Transport(302), now)
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"], "redirect_blocked")
            self.assertTrue(blocked["pending"])
            self.assertNotIn(SECRET_URL, json.dumps(blocked))
            self.assertNotIn(SECRET_AUTH, json.dumps(blocked))

    def test_exception_is_redacted(self):
        class Boom:
            def post(self, url, authorization, body):
                raise RuntimeError(url + " " + authorization)

        with tempfile.TemporaryDirectory() as tmp:
            write_config(tmp, SECRET_URL, SECRET_AUTH)
            now = datetime(2026, 11, 7, 4, 20, tzinfo=timezone.utc)
            result = send_saved_test(tmp, Boom(), now)
            text = json.dumps(result)
            self.assertEqual(result["error"], "sender_failed")
            self.assertNotIn(SECRET_URL, text)
            self.assertNotIn(SECRET_AUTH, text)
            self.assertTrue(result["pending"])

    def test_rejects_extra_or_mistyped_fields(self):
        good = {
            "schema_version": 1,
            "event_type": "trade_candidate_test",
            "event_id": "abc_1",
            "test_only": True,
            "sent_at": "2026-11-07T04:20:00Z",
        }
        validate_event(good)
        bad = dict(good)
        bad["note"] = "x"
        with self.assertRaises(ValueError):
            validate_event(bad)
        bad = dict(good)
        bad["schema_version"] = True
        with self.assertRaises(ValueError):
            validate_event(bad)
        bad = dict(good)
        bad["event_id"] = "bad id"
        with self.assertRaises(ValueError):
            validate_event(bad)

    def test_send_is_a_separate_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = Transport()
            main(["configure"], directory=tmp, read_pair=lambda: (SECRET_URL, SECRET_AUTH), transport=transport)
            self.assertEqual(transport.calls, 0)
            code = main(["send-test"], directory=tmp, transport=transport, now=datetime(2026, 11, 7, 4, 20, tzinfo=timezone.utc))
            self.assertEqual(code, 0)
            self.assertEqual(transport.calls, 1)


if __name__ == "__main__":
    unittest.main()
