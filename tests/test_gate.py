"""gate.py：只有新鮮可信的空輪才 QUIET，其他一律 WAKE。用假的 live_trader／prefilter，不連網。"""
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import gate  # noqa: E402

FLAT_STATUS = textwrap.dedent("""\
    時間 2026-11-10 12:00 台北
    錢包 700.00  未實現 +0.00  淨值 700.00  可用 700.00 USDT
    日初淨值 700.00  當日損益 +0.00 (0.00% 虧損側)  每份保證金 35.00
    停開新倉 False  全平旗標 False
    持倉 0/5
    一般掛單 0  條件單 0
    雙向持倉 False  多資產模式 False
""")

BUSY_STATUS = textwrap.dedent("""\
    時間 2026-11-10 12:00 台北
    停開新倉 False  全平旗標 False
    持倉 1/5
      ETHUSDT 數量 0.1 進場 3000 未實現 +1.00 槓桿 5 逐倉 True
    一般掛單 0  條件單 2
      [algo] ETHUSDT SELL STOP_MARKET 觸發 2900 掛於 11-10 11:00 id 1
      [algo] ETHUSDT SELL TAKE_PROFIT_MARKET 觸發 3200 掛於 11-10 11:00 id 2
    雙向持倉 False  多資產模式 False
""")

NO_CANDIDATE = {"code": 0, "stdout": json.dumps({"time": "t", "call_ai": False, "n_candidates": 0, "candidates": []}), "stderr": ""}
CANDIDATE = {"code": 10, "stdout": json.dumps({"time": "t", "call_ai": True, "n_candidates": 1, "candidates": [{"symbol": "SOLUSDT"}]}), "stderr": ""}
PREFILTER_ERROR = {"code": 2, "stdout": json.dumps({"time": "t", "call_ai": False, "error": "HTTPError", "candidates": []}), "stderr": ""}


def ok(text=""):
    return {"code": 0, "stdout": text, "stderr": ""}


class FakeRunner:
    def __init__(self, manage=None, status=None, prefilter=None):
        self.answers = {"manage": manage or ok(), "status": status or ok(FLAT_STATUS), "prefilter": prefilter or NO_CANDIDATE}
        self.calls = []

    def __call__(self, argv, timeout, cwd=None, lock_retries=5, **_kw):
        if "manage" in argv:
            key = "manage"
            self.calls.append(("manage", "--live" in argv))
        elif "status" in argv:
            key = "status"
            self.calls.append(("status",))
        else:
            key = "prefilter"
            self.calls.append(("prefilter",))
        return dict(self.answers[key])


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_gate(self, runner):
        out = io.StringIO()
        with redirect_stdout(out):
            code = gate.main(["--state-dir", self.tmp.name, "--root", self.tmp.name], runner=runner)
        return code, out.getvalue()

    def test_flat_book_and_no_candidate_is_quiet(self):
        runner = FakeRunner()
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET)
        self.assertTrue(out.startswith("QUIET"))
        self.assertIn(("manage", True), runner.calls)
        with open(os.path.join(self.tmp.name, "gate_log.jsonl"), encoding="utf-8") as handle:
            log = handle.read()
        self.assertIn('"decision": "QUIET"', log)

    def test_candidate_wakes_and_includes_outputs(self):
        code, out = self.run_gate(FakeRunner(prefilter=CANDIDATE))
        self.assertEqual(code, gate.WAKE)
        self.assertTrue(out.startswith("WAKE candidates"))
        self.assertIn("SOLUSDT", out)
        self.assertIn("不必重跑", out)

    def test_open_position_wakes(self):
        code, out = self.run_gate(FakeRunner(status=ok(BUSY_STATUS)))
        self.assertEqual(code, gate.WAKE)
        self.assertIn("positions_open", out.splitlines()[0])
        self.assertIn("orders_open", out.splitlines()[0])

    def test_book_just_went_flat_wakes_once(self):
        self.run_gate(FakeRunner(status=ok(BUSY_STATUS)))
        code, out = self.run_gate(FakeRunner())
        self.assertEqual(code, gate.WAKE)
        self.assertIn("book_just_went_flat", out.splitlines()[0])
        code, _out = self.run_gate(FakeRunner())
        self.assertEqual(code, gate.QUIET)

    def test_any_failure_wakes(self):
        cases = [
            FakeRunner(manage={"code": 1, "stdout": "", "stderr": "幣安錯誤: HTTP 401 code -2015"}),
            FakeRunner(status={"code": 1, "stdout": "", "stderr": "幣安錯誤"}),
            FakeRunner(status=ok("持倉 0/5\n")),  # 缺掛單與旗標
            FakeRunner(prefilter=PREFILTER_ERROR),
            FakeRunner(prefilter={"code": 0, "stdout": "not json", "stderr": ""}),
            FakeRunner(prefilter={"code": 0, "stdout": json.dumps({"call_ai": True, "n_candidates": 1}), "stderr": ""}),
            FakeRunner(manage={"code": None, "stdout": "", "stderr": "timeout", "timeout": True}),
        ]
        for runner in cases:
            with self.subTest(runner=runner.answers):
                code, out = self.run_gate(runner)
                self.assertEqual(code, gate.WAKE, out)

    def test_header_and_rows_must_agree(self):
        lying = FLAT_STATUS.replace("一般掛單 0  條件單 0", "一般掛單 0  條件單 0\n  [algo] BTCUSDT SELL STOP_MARKET 觸發 1 掛於 - id 9")
        self.assertIsNone(gate.parse_status(lying))
        code, _out = self.run_gate(FakeRunner(status=ok(lying)))
        self.assertEqual(code, gate.WAKE)

    def test_manage_action_words_wake(self):
        code, out = self.run_gate(FakeRunner(manage=ok("ETHUSDT 限價單 FILLED 已成交 0.1/0.1\n")))
        self.assertEqual(code, gate.WAKE)
        self.assertIn("manage_reported_action", out.splitlines()[0])

    def test_kill_flag_always_wakes_and_halt_wakes_once_per_day(self):
        killed = FLAT_STATUS.replace("全平旗標 False", "全平旗標 True")
        for _ in range(2):
            code, _out = self.run_gate(FakeRunner(status=ok(killed)))
            self.assertEqual(code, gate.WAKE)
        halted = FLAT_STATUS.replace("停開新倉 False", "停開新倉 True")
        code, out = self.run_gate(FakeRunner(status=ok(halted)))
        self.assertEqual(code, gate.WAKE)
        self.assertIn("halted_new_today", out)
        code, _out = self.run_gate(FakeRunner(status=ok(halted)))
        self.assertEqual(code, gate.QUIET)

    def test_runner_crash_wakes(self):
        def broken(*_a, **_k):
            raise RuntimeError("boom")
        code, out = self.run_gate(broken)
        self.assertEqual(code, gate.WAKE)
        self.assertIn("gate_error:RuntimeError", out)

    def test_lock_busy_is_retried_then_reported(self):
        sleeps = []
        script = os.path.join(self.tmp.name, "busy.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("import sys\nprint('另一個 live_trader 正在執行（檔案鎖），稍後再試')\nsys.exit(1)\n")
        result = gate.run_cmd([sys.executable, script], 30, lock_retries=2, lock_wait=0, sleep=sleeps.append)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(result["lock_busy"])
        _q, reasons = gate.decide(result, ok(FLAT_STATUS), gate.parse_status(FLAT_STATUS), NO_CANDIDATE,
                                  gate.parse_prefilter(NO_CANDIDATE), {}, "2026-11-10")
        self.assertIn("manage_lock_busy", reasons)

    def test_real_subprocess_end_to_end(self):
        root = self.tmp.name
        with open(os.path.join(root, "live_trader.py"), "w", encoding="utf-8") as handle:
            handle.write("import sys\nif sys.argv[1] == 'status':\n    print(" + repr(FLAT_STATUS) + ")\n")
        with open(os.path.join(root, "prefilter.py"), "w", encoding="utf-8") as handle:
            handle.write("import json\nprint(json.dumps({'call_ai': False, 'n_candidates': 0, 'candidates': []}))\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = gate.main(["--root", root, "--state-dir", os.path.join(root, "gate")])
        self.assertEqual(code, gate.QUIET, out.getvalue())


if __name__ == "__main__":
    unittest.main()
