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


def keep_all(input_text):
    """假的 hardfilter：全部留下。"""
    cands = json.loads(input_text)["candidates"]
    return ok(json.dumps({"ok": True, "kept": cands, "blocked": [], "notes": []}, ensure_ascii=False))


class FakeRunner:
    def __init__(self, manage=None, status=None, prefilter=None, when=None, hardfilter=None):
        when = when or datetime.now(gate.TPE)
        self.answers = {
            "manage": manage or ok(),
            "status": status or ok(status_text(when)),
            "prefilter": prefilter or quiet_prefilter(when),
        }
        self.hardfilter = hardfilter or keep_all
        self.hardfilter_inputs = []
        self.calls = []

    def __call__(self, argv, timeout, cwd=None, lock_retries=5, input_text=None, **_kw):
        if any(str(a).endswith("hardfilter.py") for a in argv):
            self.calls.append(("hardfilter",))
            self.hardfilter_inputs.append(input_text)
            answer = self.hardfilter(input_text) if callable(self.hardfilter) else self.hardfilter
            return dict(answer)
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

    def run_gate(self, runner, clock=None, sleep=None):
        out = io.StringIO()
        if sleep is None:
            sleep = lambda _s: None
        with redirect_stdout(out):
            code = gate.main(
                ["--state-dir", self.tmp.name, "--root", self.tmp.name],
                runner=runner,
                clock=clock or self.clock,
                sleep=sleep,
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


def blocked_rec(cand, rule="kuai_consecutive_blacks", why="只有 1 根長黑"):
    return {"symbol": cand["symbol"], "setup": cand.get("setup"), "blocked_by": rule, "why": why, "checks": []}


class GateHardfilterTest(unittest.TestCase):
    """gate.py 呼叫 hardfilter：全擋才可能 QUIET；任何失敗、看不懂都保留候選並 WAKE。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.when = datetime.now(gate.TPE).replace(microsecond=0)
        self.c1 = {"symbol": "CAPUSDT", "setup": "瘸師弟", "bar": "11-10 22:45"}
        self.c2 = {"symbol": "MERLUSDT", "setup": "布鹿流", "entry": 1.0, "stop": 0.97, "prior_high": 1.2}
        # 讓預設的 hardfilter 路徑存在（內容不會被執行，FakeRunner 攔下）
        self.hf_path = os.path.join(self.tmp.name, "hardfilter.py")
        with open(self.hf_path, "w", encoding="utf-8") as handle:
            handle.write("# fake\n")

    def clock(self):
        return self.when

    def pref(self, *cands):
        return quiet_prefilter(self.when, call_ai=bool(cands), n_candidates=len(cands), candidates=list(cands))

    def run_gate(self, runner, extra=None, sleep=None):
        out = io.StringIO()
        if sleep is None:
            sleep = lambda _s: None
        argv = ["--state-dir", self.tmp.name, "--root", self.tmp.name, "--hardfilter", self.hf_path] + (extra or [])
        with redirect_stdout(out):
            code = gate.main(argv, runner=runner, clock=self.clock, sleep=sleep)
        return code, out.getvalue()

    def log_rows(self):
        with open(os.path.join(self.tmp.name, "gate_log.jsonl"), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_all_blocked_is_quiet_and_logged(self):
        def block_all(text):
            cands = json.loads(text)["candidates"]
            return ok(json.dumps({"ok": True, "kept": [], "blocked": [blocked_rec(c) for c in cands], "notes": []}, ensure_ascii=False))
        runner = FakeRunner(prefilter=self.pref(self.c1), when=self.when, hardfilter=block_all)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET, out)
        self.assertTrue(out.startswith("QUIET"))
        self.assertIn("硬條件擋1", out)
        row = self.log_rows()[-1]
        self.assertEqual(row["n_candidates"], 0)
        self.assertEqual(row["hardfilter"]["before"], 1)
        self.assertEqual(row["hardfilter"]["blocked"][0]["symbol"], "CAPUSDT")
        self.assertEqual(json.loads(runner.hardfilter_inputs[0])["candidates"], [self.c1])

    def test_some_kept_wakes_with_section(self):
        def block_first(text):
            cands = json.loads(text)["candidates"]
            return ok(json.dumps({"ok": True, "kept": cands[1:], "blocked": [blocked_rec(cands[0])], "notes": []}, ensure_ascii=False))
        runner = FakeRunner(prefilter=self.pref(self.c1, self.c2), when=self.when, hardfilter=block_first)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.WAKE)
        self.assertTrue(out.startswith("WAKE candidates"), out)
        self.assertIn("=== hardfilter", out)
        self.assertEqual(self.log_rows()[-1]["n_candidates"], 1)

    def test_hardfilter_failures_keep_candidates_and_wake(self):
        bad = [
            {"code": 2, "stdout": json.dumps({"ok": False, "error": "rate_limited: 429 on klines CAPUSDT"}), "stderr": ""},
            {"code": None, "stdout": "", "stderr": "timeout", "timeout": True},
            ok("not json"),
            ok(json.dumps({"ok": True, "kept": [], "blocked": []})),  # 張數對不上
            ok(json.dumps({"ok": True, "kept": [{"symbol": "XUSDT"}], "blocked": []})),  # 留下的不是原候選
            ok(json.dumps({"ok": True, "kept": [], "blocked": [{"symbol": "CAPUSDT", "setup": "瘸師弟"}]})),  # 缺 blocked_by
            ok(json.dumps({"ok": True, "kept": [], "blocked": [{"symbol": "ZZZUSDT", "setup": "瘸師弟", "blocked_by": "x"}]})),
            ok(json.dumps({"ok": False, "kept": [], "blocked": []})),
            ok(json.dumps({"ok": True, "kept": "all", "blocked": []})),
        ]
        for answer in bad:
            with self.subTest(answer=answer):
                runner = FakeRunner(prefilter=self.pref(self.c1), when=self.when, hardfilter=answer)
                code, out = self.run_gate(runner)
                self.assertEqual(code, gate.WAKE)
                self.assertIn("hardfilter_failed", out.splitlines()[0])
                self.assertIn("candidates", out.splitlines()[0])
                self.assertEqual(self.log_rows()[-1]["n_candidates"], 1)

    def test_missing_hardfilter_file_wakes(self):
        os.remove(self.hf_path)
        runner = FakeRunner(prefilter=self.pref(self.c1), when=self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.WAKE)
        self.assertIn("hardfilter_failed", out)

    def test_hardfilter_off_restores_old_behaviour(self):
        runner = FakeRunner(prefilter=self.pref(self.c1), when=self.when)
        code, out = self.run_gate(runner, ["--hardfilter", "off"])
        self.assertEqual(code, gate.WAKE)
        self.assertTrue(out.startswith("WAKE candidates"))
        self.assertNotIn(("hardfilter",), runner.calls)

    def test_no_candidates_does_not_call_hardfilter(self):
        runner = FakeRunner(when=self.when)
        code, _out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET)
        self.assertNotIn(("hardfilter",), runner.calls)

    def test_all_blocked_but_position_open_still_wakes(self):
        def block_all(text):
            cands = json.loads(text)["candidates"]
            return ok(json.dumps({"ok": True, "kept": [], "blocked": [blocked_rec(c) for c in cands]}, ensure_ascii=False))
        busy = status_text(self.when, positions=1, pos_rows=BUSY_POS, algo=2, order_rows=BUSY_ORDERS)
        runner = FakeRunner(prefilter=self.pref(self.c1), status=ok(busy), when=self.when, hardfilter=block_all)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.WAKE)
        self.assertIn("positions_open", out)
        self.assertNotIn("candidates", out.splitlines()[0])

    def test_prefilter_429_still_wakes_without_hardfilter(self):
        # 11/10 prefilter_log 的同一種錯誤字串。重試兩次仍失敗才 WAKE，而且不跑 hardfilter。
        pref = {"code": 2, "stdout": json.dumps({"time": "t", "call_ai": False, "error": "rate_limited: 429 on /fapi/v1/klines", "candidates": []}), "stderr": ""}
        runner = FakeRunner(prefilter=pref, when=self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.WAKE)
        self.assertIn("prefilter_failed", out.splitlines()[0])
        self.assertIn("prefilter_retries=2", out.splitlines()[0])
        self.assertNotIn(("hardfilter",), runner.calls)
        self.assertEqual(runner.calls.count(("prefilter",)), 3)
        row = self.log_rows()[-1]
        self.assertEqual(row["prefilter_retry"]["retries"], 2)
        self.assertEqual(row["prefilter_retry"]["wait_s"], sum(gate.PREFILTER_BACKOFF_S))
        self.assertEqual(row["prefilter_retry"]["stopped"], "retries_exhausted")

    def test_hardfilter_retry_stats_are_logged(self):
        answer = {"code": 2, "stdout": json.dumps({
            "ok": False,
            "error": "rate_limited: 429 on klines CAPUSDT",
            "rate_limit_retries": 2,
            "rate_limit_wait_s": 8.0,
            "rate_limit_waits_s": [2.0, 6.0],
            "rate_limit_stopped": "retries_exhausted",
        }), "stderr": ""}
        runner = FakeRunner(prefilter=self.pref(self.c1), when=self.when, hardfilter=answer)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.WAKE)
        self.assertIn("hardfilter_failed", out.splitlines()[0])
        self.assertIn("hardfilter_retries=2", out.splitlines()[0])
        row = self.log_rows()[-1]
        self.assertEqual(row["hardfilter"]["retries"], 2)
        self.assertEqual(row["hardfilter"]["wait_s"], 8.0)
        self.assertEqual(row["hardfilter"]["waits_s"], [2.0, 6.0])
        self.assertEqual(row["hardfilter"]["stopped"], "retries_exhausted")

    def test_real_hardfilter_subprocess_with_stdin(self):
        """真的起子程序跑一支假 hardfilter，確認 stdin 有送到、輸出有被讀回。"""
        with open(self.hf_path, "w", encoding="utf-8") as handle:
            handle.write(
                "import json,sys\nb=json.load(sys.stdin)\n"
                "print(json.dumps({'ok':True,'kept':[],'blocked':[dict(symbol=c['symbol'],setup=c.get('setup'),blocked_by='t',why='t') for c in b['candidates']]}))\n"
            )
        result = gate.run_cmd([sys.executable, self.hf_path], 30, input_text=json.dumps({"candidates": [self.c1]}))
        parsed = gate.parse_hardfilter(result, [self.c1])
        self.assertIsNotNone(parsed, result)
        self.assertEqual(parsed["kept"], [])


def limited_prefilter(status, retry_after=None):
    """部署中 prefilter 的失敗形狀：結束碼 2，stdout 一行 JSON。Retry-After 只在有人印出來時才放 stderr。"""
    body = {
        "time": "t",
        "call_ai": False,
        "error": "rate_limited: {0} on /fapi/v1/klines".format(status),
        "candidates": [],
    }
    stderr = "" if retry_after is None else "Retry-After: {0}".format(retry_after)
    return {"code": 2, "stdout": json.dumps(body), "stderr": stderr}


class ScriptRunner:
    """prefilter 依序回傳。多叫一次就失敗，避免重試次數悄悄變多。"""

    def __init__(self, prefilters, when):
        self.prefilters = list(prefilters)
        self.when = when
        self.calls = []

    def __call__(self, argv, timeout, cwd=None, lock_retries=5, input_text=None, **_kw):
        if any(str(a).endswith("hardfilter.py") for a in argv):
            self.calls.append(("hardfilter",))
            return ok(json.dumps({"ok": True, "kept": [], "blocked": [], "notes": []}))
        if "manage" in argv:
            self.calls.append(("manage",))
            return ok()
        if "status" in argv:
            self.calls.append(("status",))
            return ok(status_text(self.when))
        self.calls.append(("prefilter",))
        if not self.prefilters:
            raise AssertionError("prefilter 被多呼叫了")
        return dict(self.prefilters.pop(0))


class PrefilterRateLimitRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.when = datetime.now(gate.TPE).replace(microsecond=0)
        self.sleeps = []

    def run_gate(self, runner):
        out = io.StringIO()
        with redirect_stdout(out):
            code = gate.main(
                ["--state-dir", self.tmp.name, "--root", self.tmp.name, "--hardfilter", "off"],
                runner=runner,
                clock=lambda: self.when,
                sleep=self.sleeps.append,
            )
        return code, out.getvalue()

    def log_row(self):
        with open(os.path.join(self.tmp.name, "gate_log.jsonl"), encoding="utf-8") as handle:
            return json.loads(handle.read().strip().splitlines()[-1])

    def test_agreed_caps(self):
        self.assertEqual(gate.PREFILTER_MAX_RETRIES, 2)
        self.assertEqual(gate.PREFILTER_BACKOFF_S, (5.0, 15.0))
        self.assertEqual(gate.PREFILTER_WAIT_CAP_S, 45.0)
        self.assertEqual(gate.PREFILTER_TOTAL_WAIT_CAP_S, 60.0)
        self.assertLessEqual(sum(gate.PREFILTER_BACKOFF_S), gate.PREFILTER_TOTAL_WAIT_CAP_S)
        self.assertTrue(all(step <= gate.PREFILTER_WAIT_CAP_S for step in gate.PREFILTER_BACKOFF_S))

    def test_429_then_success_is_quiet_and_logged(self):
        runner = ScriptRunner([limited_prefilter(429), quiet_prefilter(self.when)], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET, out)
        self.assertEqual(self.sleeps, [5.0])
        self.assertEqual(runner.calls.count(("prefilter",)), 2)
        row = self.log_row()
        self.assertEqual(row["decision"], "QUIET")
        self.assertEqual(row["prefilter_retry"]["attempts"], 2)
        self.assertEqual(row["prefilter_retry"]["retries"], 1)
        self.assertEqual(row["prefilter_retry"]["wait_s"], 5.0)
        self.assertEqual(row["prefilter_retry"]["waits_s"], [5.0])
        self.assertIsNone(row["prefilter_retry"]["stopped"])

    def test_418_then_success_uses_same_backoff(self):
        runner = ScriptRunner([limited_prefilter(418), quiet_prefilter(self.when)], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET, out)
        self.assertEqual(self.sleeps, [5.0])
        self.assertEqual(self.log_row()["prefilter_retry"]["retries"], 1)

    def test_retries_exhausted_still_wakes(self):
        runner = ScriptRunner([limited_prefilter(429), limited_prefilter(429), limited_prefilter(429)], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("prefilter_failed", out.splitlines()[0])
        self.assertIn("prefilter_retries=2", out.splitlines()[0])
        self.assertEqual(self.sleeps, [5.0, 15.0])
        self.assertEqual(runner.calls.count(("prefilter",)), 3)
        self.assertEqual(runner.prefilters, [])
        row = self.log_row()
        self.assertEqual(row["prefilter_retry"]["attempts"], 3)
        self.assertEqual(row["prefilter_retry"]["retries"], 2)
        self.assertEqual(row["prefilter_retry"]["wait_s"], 20.0)
        self.assertEqual(row["prefilter_retry"]["stopped"], "retries_exhausted")

    def test_retry_after_is_honored(self):
        runner = ScriptRunner([limited_prefilter(429, retry_after=4), quiet_prefilter(self.when)], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET, out)
        self.assertEqual(self.sleeps, [4.0])
        self.assertEqual(self.log_row()["prefilter_retry"]["wait_s"], 4.0)

    def test_retry_after_zero_uses_backoff(self):
        runner = ScriptRunner([limited_prefilter(429, retry_after=0), quiet_prefilter(self.when)], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET, out)
        self.assertEqual(self.sleeps, [5.0])

    def test_retry_after_over_single_cap_does_not_wait(self):
        runner = ScriptRunner([limited_prefilter(429, retry_after=46), quiet_prefilter(self.when)], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("prefilter_failed", out.splitlines()[0])
        self.assertNotIn("prefilter_retries=", out.splitlines()[0])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(runner.calls.count(("prefilter",)), 1)
        self.assertEqual(len(runner.prefilters), 1)  # 成功那次沒被用到
        row = self.log_row()
        self.assertEqual(row["prefilter_retry"]["retries"], 0)
        self.assertEqual(row["prefilter_retry"]["wait_s"], 0)
        self.assertEqual(row["prefilter_retry"]["stopped"], "wait_cap")

    def test_total_wait_cap_stops_before_the_next_sleep(self):
        # 40 與 25 各自都沒超過單次 45 秒，但 40+25 超過累計 60 秒，第二次不等、也不再呼叫。
        runner = ScriptRunner([
            limited_prefilter(429, retry_after=40),
            limited_prefilter(418, retry_after=25),
            quiet_prefilter(self.when),
        ], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.WAKE, out)
        self.assertIn("prefilter_failed", out.splitlines()[0])
        self.assertEqual(self.sleeps, [40.0])
        self.assertEqual(runner.calls.count(("prefilter",)), 2)
        row = self.log_row()
        self.assertEqual(row["prefilter_retry"]["retries"], 1)
        self.assertEqual(row["prefilter_retry"]["wait_s"], 40.0)
        self.assertEqual(row["prefilter_retry"]["stopped"], "wait_cap")

    def test_total_wait_equal_to_cap_is_allowed(self):
        runner = ScriptRunner([
            limited_prefilter(429, retry_after=30),
            limited_prefilter(429, retry_after=30),
            quiet_prefilter(self.when),
        ], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET, out)
        self.assertEqual(self.sleeps, [30.0, 30.0])
        self.assertEqual(self.log_row()["prefilter_retry"]["wait_s"], 60.0)

    def test_non_429_failure_does_not_retry(self):
        cases = [
            {"code": 2, "stdout": json.dumps({"time": "t", "call_ai": False, "error": "HTTPError", "candidates": []}), "stderr": ""},
            {"code": 2, "stdout": json.dumps({"error": "timeout after 429ms"}), "stderr": ""},
            {"code": 2, "stdout": "", "stderr": ""},
            {"code": 1, "stdout": "", "stderr": "幣安錯誤: HTTP 401 code -2015"},
            {"code": None, "stdout": "", "stderr": "timeout", "timeout": True},
        ]
        for answer in cases:
            with self.subTest(answer=answer):
                self.sleeps.clear()
                runner = ScriptRunner([dict(answer), quiet_prefilter(self.when)], self.when)
                code, out = self.run_gate(runner)
                self.assertEqual(code, gate.WAKE, out)
                self.assertIn("prefilter_failed", out.splitlines()[0])
                self.assertEqual(self.sleeps, [])
                self.assertEqual(runner.calls.count(("prefilter",)), 1)
                self.assertIsNone(self.log_row()["prefilter_retry"])

    def test_http_error_text_on_stderr_is_identified(self):
        stderr = "urllib.error.HTTPError: HTTP Error 418: I'm a teapot\n"
        runner = ScriptRunner([
            {"code": 1, "stdout": "", "stderr": stderr},
            quiet_prefilter(self.when),
        ], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET, out)
        self.assertEqual(self.sleeps, [5.0])

    def test_normal_quiet_round_calls_prefilter_once(self):
        runner = ScriptRunner([quiet_prefilter(self.when)], self.when)
        code, out = self.run_gate(runner)
        self.assertEqual(code, gate.QUIET, out)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(runner.calls.count(("prefilter",)), 1)
        self.assertIsNone(self.log_row()["prefilter_retry"])
