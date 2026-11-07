"""status-json。零倉文字加查詢失敗不能當成功。不匯入 live_trader，避免讀金鑰。"""
import json
import unittest

from src.phase1.status_contract import failure_document, parse_status_json, success_document

AS_OF = "2026-11-07T12:19:30+08:00"


def _ok():
    return json.dumps(success_document(0, 0, 0, AS_OF))


class StatusContractTest(unittest.TestCase):
    def test_complete_zero_plus_error_text_is_not_success(self):
        text = "時間 2026-11-07 12:19 台北\n持倉 0/5\n一般掛單 0  條件單 0\n查詢失敗 HTTP 429\n"
        self.assertFalse(parse_status_json(text, 0)["trusted"])
        self.assertFalse(parse_status_json(_ok() + "\n查詢失敗 HTTP 429\n", 0)["trusted"])

    def test_exit_zero_with_ok_false_or_hidden_algo_query(self):
        hidden = success_document(0, 0, 0, AS_OF)
        hidden["queries"]["open_algo_orders"] = False
        self.assertFalse(parse_status_json(json.dumps(hidden), 0)["trusted"])
        failed = failure_document({"account": True, "open_orders": True, "open_algo_orders": False})
        self.assertNotIn("positions", failed)
        self.assertFalse(parse_status_json(json.dumps(failed), 0)["trusted"])
        self.assertFalse(parse_status_json(json.dumps(success_document(0, 0, 0, AS_OF)), 2)["trusted"])

    def test_all_queries_true_and_exit_zero_is_trusted(self):
        parsed = parse_status_json(_ok(), 0)
        self.assertTrue(parsed["trusted"])
        self.assertEqual(parsed["positions"], 0)
        self.assertEqual(parsed["orders"], 0)
        secret = success_document(0, 0, 0, AS_OF)
        secret["totalWalletBalance"] = "1"
        self.assertFalse(parse_status_json(json.dumps(secret), 0)["trusted"])


if __name__ == "__main__":
    unittest.main()
