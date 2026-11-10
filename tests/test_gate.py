"""gate.py：只有新鮮可信的空輪才 QUIET，其他一律 WAKE。用假的 live_trader／prefilter，不連網。"""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import gate  # noqa: E402


def status_text(when, positions=0, pos_rows="", orders=0, algo=0, order_rows="", halted="False", killed="False"):
    lines = [
        "時間 {0} 台北".format(when.strftime("%Y-%m-%d %H:%M")),
        "錢包 700.00  未實現 +0.00  淨值 700.00  可用 700.00 USDT",
        "日初淨值 700.00  當日損益 +0.00 (0.00% 虧損側)  每份保證金 35.00",
        "停開新倉 {0}  全平旗標 {1}".format(halted, killed),
        "持倉 {0}/5".format(positions),
    ]
    if pos_rows:
        lines.extend(pos_rows.splitlines())
    lines.append("一般掛單 {0}  條件單 {1}".format(orders, algo))
    if order_rows:
        lines.extend(order_rows.splitlines())
    lines.append("雙向持倉 False  多資產模式 False")
    return "\n".join(lines) + "\n"


BUSY_POS = "  ETHUSDT 數量 0.1 進場 3000 未實現 +1.00 槓桿 5 逐倉 True"
BUSY_ORDERS = "\n".join((
    "  [algo] ETHUSDT SELL STOP_MARKET 觸發 2900 掛於 11-10 11:00 id 1",
    "  [algo] ETHUSDT SELL TAKE_PROFIT_MARKET 觸發 3200 掛於 11-10 11:00 id 2",
))


def quiet_prefilter(when, **overrides):
    body = {
        "time": when.isoformat(timespec="seconds"),
        "call_ai": False,
        "n_candidates": 0,
        "candidates": [],
    }
    body.update(overrides)
    code = 10 if body.get("call_ai") else 0
    return {"code": code, "stdout": json.dumps(body, ensure_ascii=False), "stderr": ""}


def ok(text=""):
    return {"code": 0, "stdout": text, "stderr": ""}


class FakeRunner:
    def __init__(self, manage=None, status=None, prefilter=None, when=None):
        when = when or datetime.now(gate.TPE)
        self.answers = {
            "manage": manage or ok(),
            "status": status or ok(status_text(when)),
            "prefilter": prefilter or quiet_prefilter(when),
        }
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
        self.when = datetime.now(gate.TPE).replace(microsecond=0)

    def clock(self):
        return self.when

    def flat(self, **kwargs):
        return status_text(self.when, **kwargs)

    def busy(self):
        return status_text(self.when, positions=1, pos_rows=BUSY_POS, algo=2, order_rows=BUSY_ORDERS)

    def run_gate(self, runner, clock=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = gate.main(
                ["--state-dir", self.tmp.name, "--root", self.tmp.name],
                runner=runner,
                clock=clock or self.clock,
            )
        return code, out.getvalue()

    def fresh_runner(self, **kwargs):
        kwargs.setdefault("when", self.when)
        return FakeRunner(**kwargs)

    def test_flat_book_and_no_candidate_is_quiet(self):
        runner = self.fresh_runner()
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET)
        self.assertTrue(out.startswith("QUIET"))
        self.assertIn(("manage", True), runner.calls)
        with open(os.path.join(self.tmp.name, "gate_log.jsonl"), encoding="utf-8") as handle:
            log = handle.read()
        self.assertIn('"decision": "QUIET"', log)

    def test_candidate_wakes_and_includes_outputs(self):
        candidate = quiet_prefilter(
            self.when, call_ai=True, n_candidates=1, candidates=[{"symbol": "SOLUSDT"}],
        )
        code, out = self.run_gate(self.fresh_runner(prefilter=candidate))
        self.assertEqual(code, gate.WAKE)
        self.assertTrue(out.startswith("WAKE candidates"))
        self.assertIn("SOLUSDT", out)
        self.assertIn("不必重跑", out)

    def test_open_position_wakes(self):
        code, out = self.run_gate(self.fresh_runner(status=ok(self.busy())))
        self.assertEqual(code, gate.WAKE)
        self.assertIn("positions_open", out.splitlines()[0])
        self.assertIn("orders_open", out.splitlines()[0])

    def test_book_just_went_flat_wakes_once(self):
        self.run_gate(self.fresh_runner(status=ok(self.busy())))
        code, out = self.run_gate(self.fresh_runner())
        self.assertEqual(code, gate.WAKE)
        self.assertIn("book_just_went_flat", out.splitlines()[0])
        code, _out = self.run_gate(self.fresh_runner())
        self.assertEqual(code, gate.QUIET)

    def test_any_failure_wakes(self):
        cases = [
            self.fresh_runner(manage={"code": 1, "stdout": "", "stderr": "幣安錯誤: HTTP 401 code -2015"}),
            self.fresh_runner(status={"code": 1, "stdout": "", "stderr": "幣安錯誤"}),
            self.fresh_runner(status=ok("持倉 0/5\n")),  # 缺掛單與旗標
            self.fresh_runner(prefilter={"code": 2, "stdout": json.dumps({"time": "t", "call_ai": False, "error": "HTTPError", "candidates": []}), "stderr": ""}),
            self.fresh_runner(prefilter={"code": 0, "stdout": "not json", "stderr": ""}),
            self.fresh_runner(prefilter={"code": 0, "stdout": json.dumps({"call_ai": True, "n_candidates": 1}), "stderr": ""}),
            self.fresh_runner(manage={"code": None, "stdout": "", "stderr": "timeout", "timeout": True}),
        ]
        for runner in cases:
            with self.subTest(runner=runner.answers):
                code, out = self.run_gate(runner)
                self.assertEqual(code, gate.WAKE, out)

    def test_header_and_rows_must_agree(self):
        lying = self.flat().replace(
            "一般掛單 0  條件單 0",
            "一般掛單 0  條件單 0\n  [algo] BTCUSDT SELL STOP_MARKET 觸發 1 掛於 - id 9",
        )
        self.assertIsNone(gate.parse_status(lying))
        code, _out = self.run_gate(self.fresh_runner(status=ok(lying)))
        self.assertEqual(code, gate.WAKE)

    def test_manage_action_words_wake(self):
        code, out = self.run_gate(self.fresh_runner(manage=ok("ETHUSDT 限價單 FILLED 已成交 0.1/0.1\n")))
        self.assertEqual(code, gate.WAKE)
        self.assertIn("manage_reported_action", out.splitlines()[0])

    def test_kill_flag_always_wakes_and_halt_wakes_once_per_day(self):
        killed = self.flat(killed="True")
        for _ in range(2):
            code, _out = self.run_gate(self.fresh_runner(status=ok(killed)))
            self.assertEqual(code, gate.WAKE)
        halted = self.flat(halted="True")
        code, out = self.run_gate(self.fresh_runner(status=ok(halted)))
        self.assertEqual(code, gate.WAKE)
        self.assertIn("halted_new_today", out)
        code, _out = self.run_gate(self.fresh_runner(status=ok(halted)))
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
        parsed = gate.parse_status(self.flat())
        prefilter = quiet_prefilter(self.when)
        _q, reasons = gate.decide(
            result, ok(self.flat()), parsed, prefilter, gate.parse_prefilter(prefilter),
            {}, self.when.strftime("%Y-%m-%d"), self.when,
        )
        self.assertIn("manage_lock_busy", reasons)

    def test_real_subprocess_end_to_end(self):
        root = self.tmp.name
        flat = self.flat()
        with open(os.path.join(root, "live_trader.py"), "w", encoding="utf-8") as handle:
            handle.write("import sys\nif sys.argv[1] == 'status':\n    print(" + repr(flat) + ")\n")
        payload = {
            "time": self.when.isoformat(timespec="seconds"),
            "call_ai": False,
            "n_candidates": 0,
            "candidates": [],
        }
        with open(os.path.join(root, "prefilter.py"), "w", encoding="utf-8") as handle:
            handle.write("import json\nprint(json.dumps(" + repr(payload) + "))\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = gate.main(
                ["--root", root, "--state-dir", os.path.join(root, "gate")],
                clock=self.clock,
            )
        self.assertEqual(code, gate.QUIET, out.getvalue())

    def test_orphan_conditional_cancel_wakes(self):
        lines = (
            "孤兒條件單 BTCUSDT 99\n當日虧損 0.00%  持倉 0\n",
            "孤兒條件單(舊端點) ETHUSDT 7\n當日虧損 0.00%  持倉 0\n",
        )
        for text in lines:
            with self.subTest(text=text):
                code, out = self.run_gate(self.fresh_runner(manage=ok(text)))
                self.assertEqual(code, gate.WAKE, out)
                self.assertIn("manage_reported_action", out.splitlines()[0])

    def test_plain_flat_manage_line_stays_quiet(self):
        code, out = self.run_gate(self.fresh_runner(manage=ok("當日虧損 0.00%  持倉 0\n")))
        self.assertEqual(code, gate.QUIET, out)

    def test_algo_unreadable_marker_wakes_even_if_exit_zero(self):
        text = self.flat() + gate.STATUS_ALGO_UNREADABLE + "\n"
        self.assertIsNone(gate.parse_status(text))
        code, out = self.run_gate(self.fresh_runner(status=ok(text)))
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("status_untrusted", out.splitlines()[0])
        self.assertNotIn("QUIET", out.splitlines()[0])

    def test_exception_after_manage_keeps_output(self):
        class BoomAfter:
            def __call__(self, argv, timeout, cwd=None, **_kw):
                if "manage" in argv:
                    return ok("MANAGE_SENTINEL 當日虧損 0.00%  持倉 0\n")
                if "status" in argv:
                    return ok(status_text(self_when))
                raise RuntimeError("prefilter boom")

        self_when = self.when
        code, out = self.run_gate(BoomAfter())
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("gate_error:RuntimeError", out)
        self.assertIn("MANAGE_SENTINEL", out)
        self.assertIn("持倉 0/5", out)
        self.assertIn("不必重跑", out)
        self.assertIn("=== manage --live（結束碼 0）===", out)
        self.assertIn("=== status（結束碼 0）===", out)

    def test_corrupt_or_unknown_previous_state_wakes_once(self):
        path = os.path.join(self.tmp.name, "gate_state.json")
        cases = (
            "{not json",
            "[]",
            json.dumps({"last": "unknown"}),
            json.dumps({"halt_reported_day": "2026-11-10"}),
            json.dumps({"last": None}),
        )
        for raw in cases:
            with self.subTest(raw=raw):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(raw)
                code, out = self.run_gate(self.fresh_runner())
                self.assertEqual(code, gate.WAKE, out)
                self.assertIn("previous_state_untrusted", out.splitlines()[0])
                code, out = self.run_gate(self.fresh_runner())
                self.assertEqual(code, gate.QUIET, out)

    def test_failed_read_does_not_keep_a_trustworthy_flat(self):
        self.run_gate(self.fresh_runner())
        code, _out = self.run_gate(self.fresh_runner(status={"code": 1, "stdout": "", "stderr": "幣安錯誤"}))
        self.assertEqual(code, gate.WAKE)
        with open(os.path.join(self.tmp.name, "gate_state.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["last"], "unknown")
        code, out = self.run_gate(self.fresh_runner())
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("previous_state_untrusted", out.splitlines()[0])

    def test_stale_or_future_status_wakes(self):
        stale = status_text(self.when - (gate.READING_MAX_AGE + timedelta(minutes=2)))
        future = status_text(self.when + (gate.READING_MAX_FUTURE_SKEW + timedelta(minutes=2)))
        for text, label in ((stale, "stale"), (future, "future")):
            with self.subTest(label=label):
                code, out = self.run_gate(self.fresh_runner(status=ok(text)))
                self.assertEqual(code, gate.WAKE, out)
                self.assertIn("status_stale", out.splitlines()[0])
        missing_time = "\n".join(line for line in self.flat().splitlines() if not line.startswith("時間 ")) + "\n"
        self.assertIsNone(gate.parse_status(missing_time))
        code, out = self.run_gate(self.fresh_runner(status=ok(missing_time)))
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("status_untrusted", out.splitlines()[0])

    def test_status_at_max_age_is_still_fresh(self):
        # status 只印到分鐘。用整分、且門檻本身是整分，解析回來才剛好等於門檻。
        when = self.when.replace(second=0, microsecond=0)
        borderline = when - gate.READING_MAX_AGE
        text = status_text(borderline)
        parsed = gate.parse_status(text)
        self.assertEqual(when - parsed["as_of"], gate.READING_MAX_AGE)
        code, out = self.run_gate(
            FakeRunner(status=ok(text), when=when),
            clock=lambda: when,
        )
        self.assertEqual(code, gate.QUIET, out)

    def test_stale_prefilter_wakes(self):
        old = self.when - (gate.READING_MAX_AGE + timedelta(minutes=5))
        code, out = self.run_gate(self.fresh_runner(prefilter=quiet_prefilter(old)))
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("prefilter_stale", out.splitlines()[0])
        missing_time = {"code": 0, "stdout": json.dumps({"call_ai": False, "n_candidates": 0, "candidates": []}), "stderr": ""}
        code, out = self.run_gate(self.fresh_runner(prefilter=missing_time))
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("prefilter_stale", out.splitlines()[0])

    def test_zero_candidates_with_nonempty_list_wakes(self):
        bad = quiet_prefilter(self.when, n_candidates=0, candidates=[{"symbol": "BTCUSDT"}])
        self.assertIsNone(gate.parse_prefilter(bad))
        code, out = self.run_gate(self.fresh_runner(prefilter=bad))
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("prefilter_failed", out.splitlines()[0])
        mismatched = quiet_prefilter(self.when, call_ai=True, n_candidates=1, candidates=[])
        self.assertIsNone(gate.parse_prefilter(mismatched))
        code, out = self.run_gate(self.fresh_runner(prefilter=mismatched))
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("prefilter_failed", out.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
