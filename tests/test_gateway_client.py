"""gateway 客戶端只組契約，不開真連線。"""
import json
import unittest

from src.phase1.gateway_client import gateway_exchange
from src.phase1.return_adapter import judgment_put


def _document():
    envelope = "cd" * 32
    judgment = {
        "schema_version": 1,
        "event_type": "dry_run_judgment",
        "event_id": "c-" + "ab" * 16,
        "evaluation_id": "s1",
        "envelope_sha256": envelope,
        "decision": "keep_original_ai",
        "basis": {"reason": "orders_open"},
        "completed_at": "2026-11-07T04:22:00Z",
        "orders_placed": False,
        "ai_execution_proven": False,
    }
    return judgment_put("inst-local-1", judgment["event_id"], "s1", envelope, judgment)


class GatewayClientTest(unittest.TestCase):
    def test_requires_injected_transport_and_hides_the_secret(self):
        with self.assertRaises(ValueError):
            gateway_exchange(None, "PUT", "inst-local-1", "c-" + "ab" * 16, "pub-key", "publisher-secret", _document())
        seen = []

        def transport(method, path, headers, body):
            seen.append((method, path, headers["authorization"], body))
            if method == "GET":
                return 302, {"Location": "https://evil.example/x", "Cache-Control": "private, no-store"}, b""
            return 201, {"Cache-Control": "private, no-store"}, body

        result = gateway_exchange(transport, "PUT", "inst-local-1", "c-" + "ab" * 16, "pub-key", "publisher-secret", _document())
        self.assertEqual(seen[0][0], "PUT")
        self.assertEqual(seen[0][1], "/v1/installations/inst-local-1/judgments/c-" + "ab" * 16)
        self.assertEqual(result["status"], 201)
        self.assertNotIn("publisher-secret", json.dumps(result))
        self.assertEqual(result["cache_control"], "private, no-store")
        redirected = gateway_exchange(transport, "GET", "inst-local-1", "c-" + "ab" * 16, "read-key", "reader-secret")
        self.assertEqual(redirected["status"], 302)
        self.assertEqual(len(seen), 2)
        self.assertFalse(redirected["followed_redirect"])


if __name__ == "__main__":
    unittest.main()
