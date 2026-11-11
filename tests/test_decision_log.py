"""decision_log.py：AI 判斷追加一行 jsonl。不連網、不讀金鑰。"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import decision_log  # noqa: E402


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()
WHEN = datetime(2026, 11, 11, 9, 31, 12, tzinfo=decision_log.TPE)
EXPECTED_LINE = (
    '{"time": "2026-11-11T09:31:12+08:00", "symbol": "ONEUSDT", '
    '"pattern": "瘸師弟", "decision": "reject", "reason": "只有1根長黑", '
    '"round_time": "2026-11-11T09:30:00+08:00"}\n'
)


class DecisionLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "decision_log.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_log(self, extra=None, now=WHEN):
        argv = [
            "--symbol", "ONEUSDT",
            "--pattern", "瘸師弟",
            "--decision", "reject",
            "--reason", "只有1根長黑",
            "--round-time", "2026-11-11T09:30:00+08:00",
            "--log", self.path,
        ]
        if extra:
            # extra 放在 --log 前面，避免把路徑蓋掉時還指到預設目錄。
            argv = extra + ["--log", self.path]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(argv, now=now)
        return code, out.getvalue(), err.getvalue()

    def read_rows(self):
        with open(self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_writes_one_record(self):
        code, out, err = self.run_log()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("已記錄 ONEUSDT reject", out)
        self.assertNotIn("QUIET", out)
        self.assertNotIn("WAKE", out)
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text, EXPECTED_LINE)
        row = json.loads(text)
        self.assertEqual(
            list(row),
            ["time", "symbol", "pattern", "decision", "reason", "round_time"],
        )
        self.assertEqual(row["symbol"], "ONEUSDT")
        self.assertEqual(row["pattern"], "瘸師弟")
        self.assertEqual(row["decision"], "reject")
        self.assertEqual(row["reason"], "只有1根長黑")
        self.assertEqual(row["round_time"], "2026-11-11T09:30:00+08:00")
        self.assertTrue(row["time"].endswith("+08:00"))

    def test_appends_multiple_records(self):
        first, _, _ = self.run_log()
        self.assertEqual(first, 0)
        with open(self.path, encoding="utf-8") as handle:
            before = handle.read()
        second_argv = [
            "--symbol", "ETHUSDT",
            "--pattern", "布鹿流",
            "--decision", "enter",
            "--reason", "回檔底底高",
            "--round-time", "2026-11-11T09:30:00+08:00",
            "--log", self.path,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(second_argv, now=WHEN)
        self.assertEqual(code, 0, err)
        with open(self.path, encoding="utf-8") as handle:
            after = handle.read()
        self.assertTrue(after.startswith(before))
        lines = after.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0] + "\n", before)
        second = json.loads(lines[1])
        self.assertEqual(second["symbol"], "ETHUSDT")
        self.assertEqual(second["pattern"], "布鹿流")
        self.assertEqual(second["decision"], "enter")
        self.assertEqual(second["reason"], "回檔底底高")

    def test_rejects_unknown_decision(self):
        for bad in ("maybe", "REJECT", "skip", "no", ""):
            path = os.path.join(self.tmp.name, "bad-{0}.jsonl".format(bad or "empty"))
            argv = [
                "--symbol", "ONEUSDT",
                "--pattern", "瘸師弟",
                "--decision", bad,
                "--reason", "只有1根長黑",
                "--log", path,
            ]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = decision_log.main(argv, now=WHEN)
            self.assertNotEqual(code, 0, bad)
            self.assertEqual(code, 2, bad)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("decision 只接受", err.getvalue())
            self.assertFalse(os.path.exists(path))

    def test_truncates_reason_to_500_characters(self):
        reason = "頭" + ("中" * 498) + ("尾" * 20)
        self.assertGreater(len(reason), 500)
        argv = [
            "--symbol", "ONEUSDT",
            "--pattern", "瘸師弟",
            "--decision", "reject",
            "--reason", reason,
            "--log", self.path,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(argv, now=WHEN)
        self.assertEqual(code, 0, err.getvalue())
        row = self.read_rows()[0]
        self.assertEqual(len(row["reason"]), decision_log.REASON_MAX)
        self.assertEqual(row["reason"], reason[:decision_log.REASON_MAX])
        self.assertTrue(reason.startswith(row["reason"]))
        self.assertNotIn("…", row["reason"])
        self.assertNotEqual(row["reason"], reason)
        # 500 個中文字不是 500 個位元組。截斷若按位元組切，長度不會剛好是 500 字。
        self.assertGreater(len(row["reason"].encode("utf-8")), 500)

    def test_reason_of_exactly_500_is_kept(self):
        reason = "甲" * 500
        argv = [
            "--symbol", "ONEUSDT",
            "--pattern", "小師弟",
            "--decision", "skip_other",
            "--reason", reason,
            "--log", self.path,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(argv, now=WHEN)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.read_rows()[0]["reason"], reason)

    def test_keeps_chinese_text(self):
        code, _, err = self.run_log()
        self.assertEqual(code, 0, err)
        raw = _read_bytes(self.path)
        self.assertIn("瘸師弟".encode("utf-8"), raw)
        self.assertIn("只有1根長黑".encode("utf-8"), raw)
        self.assertNotIn(b"\\u", raw)
        row = json.loads(raw.decode("utf-8"))
        self.assertEqual(row["pattern"], "瘸師弟")
        self.assertEqual(row["reason"], "只有1根長黑")

    def test_reason_quotes_and_newline_stay_one_line(self):
        reason = "只有1根長黑 \"引號\" \\ 反斜線\n第二句\u2028行分隔"
        argv = [
            "--symbol", "ONEUSDT",
            "--pattern", "大師兄",
            "--decision", "reject",
            "--reason", reason,
            "--log", self.path,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(argv, now=WHEN)
        self.assertEqual(code, 0, err.getvalue())
        raw = _read_bytes(self.path)
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertNotIn("\u2028".encode("utf-8"), raw)
        row = json.loads(raw.decode("utf-8"))
        self.assertEqual(row["reason"], reason)

    def test_creates_file_when_missing(self):
        self.assertFalse(os.path.exists(self.path))
        code, _, err = self.run_log()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(self.path))
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(len(text.splitlines()), 1)

    def test_missing_parent_directory(self):
        missing = os.path.join(self.tmp.name, "no-such-dir", "decision_log.jsonl")
        argv = [
            "--symbol", "ONEUSDT",
            "--pattern", "瘸師弟",
            "--decision", "reject",
            "--reason", "只有1根長黑",
            "--log", missing,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(argv, now=WHEN)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("路徑不存在", err.getvalue())
        self.assertFalse(os.path.exists(missing))

    def test_bad_round_time_does_not_write(self):
        for bad in ("2026-11-11 09:30", "2026-11-11T09:30:00", "2026-02-31T09:30:00+08:00", "昨天"):
            argv = [
                "--symbol", "ONEUSDT",
                "--pattern", "瘸師弟",
                "--decision", "reject",
                "--reason", "只有1根長黑",
                "--round-time", bad,
                "--log", self.path,
            ]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = decision_log.main(argv, now=WHEN)
            self.assertEqual(code, 2, bad)
            self.assertIn("round-time", err.getvalue())
            self.assertFalse(os.path.exists(self.path), bad)

    def test_round_time_is_stored_in_taipei(self):
        cases = (
            ("2026-11-11T09:30:00+08:00", "2026-11-11T09:30:00+08:00"),
            ("2026-11-11T01:30:00Z", "2026-11-11T09:30:00+08:00"),
            ("2026-11-11T01:30:00+00:00", "2026-11-11T09:30:00+08:00"),
            ("2026-11-11T10:00:00+09:00", "2026-11-11T09:00:00+08:00"),
            ("2026-11-11T20:30:00Z", "2026-11-12T04:30:00+08:00"),
        )
        for index, (raw, stored) in enumerate(cases):
            path = os.path.join(self.tmp.name, "round-{0}.jsonl".format(index))
            argv = [
                "--symbol", "BTCUSDT",
                "--pattern", "大師兄",
                "--decision", "skip_other",
                "--reason", "事件窗口",
                "--round-time", raw,
                "--log", path,
            ]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = decision_log.main(argv, now=WHEN)
            self.assertEqual(code, 0, err.getvalue())
            row = json.loads(_read_text(path))
            self.assertEqual(row["round_time"], stored)

    def test_omitted_round_time_is_null(self):
        argv = [
            "--symbol", "ONEUSDT",
            "--pattern", "瘸師弟",
            "--decision", "reject",
            "--reason", "只有1根長黑",
            "--log", self.path,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(argv, now=WHEN)
        self.assertEqual(code, 0, err.getvalue())
        row = self.read_rows()[0]
        self.assertIsNone(row["round_time"])

    def test_default_clock_is_taipei(self):
        before = datetime.now(decision_log.TPE) - timedelta(seconds=1)
        argv = [
            "--symbol", "ONEUSDT",
            "--pattern", "瘸師弟",
            "--decision", "reject",
            "--reason", "只有1根長黑",
            "--log", self.path,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(argv)
        after = datetime.now(decision_log.TPE) + timedelta(seconds=1)
        self.assertEqual(code, 0, err.getvalue())
        got = datetime.fromisoformat(self.read_rows()[0]["time"])
        self.assertEqual(got.utcoffset(), timedelta(hours=8))
        self.assertGreaterEqual(got, before)
        self.assertLessEqual(got, after)

    def test_enter_and_skip_other(self):
        for decision in ("enter", "skip_other"):
            path = os.path.join(self.tmp.name, decision + ".jsonl")
            argv = [
                "--symbol", "SOLUSDT",
                "--pattern", "小師弟",
                "--decision", decision,
                "--reason", "理由原文",
                "--log", path,
            ]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = decision_log.main(argv, now=WHEN)
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(json.loads(_read_text(path))["decision"], decision)

    def test_empty_reason_and_bad_symbol(self):
        cases = (
            ["--symbol", "oneusdt", "--pattern", "瘸師弟", "--decision", "reject", "--reason", "只有1根長黑"],
            ["--symbol", "ONEUSDT", "--pattern", "瘸師弟", "--decision", "reject", "--reason", "   "],
            ["--symbol", "ONEUSDT", "--pattern", "", "--decision", "reject", "--reason", "只有1根長黑"],
            ["--symbol", "ONEUSDT", "--pattern", "瘸師弟", "--decision", "reject"],
        )
        for argv in cases:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = decision_log.main(argv + ["--log", self.path], now=WHEN)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out.getvalue(), "")
            self.assertNotEqual(err.getvalue(), "")
            self.assertFalse(os.path.exists(self.path))

    def test_invalid_input_does_not_change_existing_file(self):
        code, _, err = self.run_log()
        self.assertEqual(code, 0, err)
        with open(self.path, "rb") as handle:
            before = handle.read()
        out, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err_buf):
            again = decision_log.main([
                "--symbol", "ONEUSDT",
                "--pattern", "瘸師弟",
                "--decision", "accept",
                "--reason", "不該寫入",
                "--log", self.path,
            ], now=WHEN)
        self.assertEqual(again, 2)
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_help_and_cli_entry(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        text = out.getvalue()
        self.assertIn("reject", text)
        self.assertIn("enter", text)
        self.assertIn("skip_other", text)
        self.assertIn("decision_log.jsonl", text)
        proc = subprocess.run(
            [sys.executable, decision_log.__file__, "--help"],
            capture_output=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("否決", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_equals_form_and_default_log_constant(self):
        self.assertEqual(decision_log.DEFAULT_LOG, "~/quantboy-gate/decision_log.jsonl")
        argv = [
            "--symbol=ONEUSDT",
            "--pattern=瘸師弟",
            "--decision=reject",
            "--reason=只有1根長黑",
            "--log", self.path,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decision_log.main(argv, now=WHEN)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.read_rows()[0]["symbol"], "ONEUSDT")

    def test_concurrent_appends_stay_one_line_each(self):
        count = 24
        procs = []
        for index in range(count):
            procs.append(subprocess.Popen(
                [
                    sys.executable, decision_log.__file__,
                    "--symbol", "C{0}USDT".format(index),
                    "--pattern", "瘸師弟",
                    "--decision", "reject",
                    "--reason", "並發第{0}筆，只有1根長黑".format(index),
                    "--round-time", "2026-11-11T09:30:00+08:00",
                    "--log", self.path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ))
        failures = []
        for index, proc in enumerate(procs):
            out, err = proc.communicate(timeout=60)
            if proc.returncode != 0:
                failures.append((index, proc.returncode, err.decode("utf-8", "replace")))
        self.assertEqual(failures, [])
        raw = _read_bytes(self.path)
        self.assertTrue(raw.endswith(b"\n"))
        lines = raw.split(b"\n")
        self.assertEqual(lines[-1], b"")
        body = lines[:-1]
        self.assertEqual(len(body), count)
        symbols = []
        for line in body:
            self.assertNotEqual(line, b"")
            self.assertFalse(line[:1].isspace())
            row = json.loads(line.decode("utf-8"))
            symbols.append(row["symbol"])
            self.assertEqual(row["decision"], "reject")
            self.assertEqual(row["pattern"], "瘸師弟")
            self.assertIn("只有1根長黑", row["reason"])
            self.assertEqual(row["round_time"], "2026-11-11T09:30:00+08:00")
            self.assertTrue(row["time"].endswith("+08:00"))
        self.assertEqual(sorted(symbols), sorted("C{0}USDT".format(index) for index in range(count)))

    def test_stdlib_only_and_gate_does_not_import_it(self):
        source = _read_text(decision_log.__file__)
        tree = ast.parse(source)
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module.split(".")[0])
        allowed = {"errno", "fcntl", "json", "os", "re", "sys", "datetime"}
        self.assertEqual(sorted(set(modules) - allowed), [])
        for banned in ("urllib", "hmac", "hashlib", "binance", "os.environ", "BINANCE", "http"):
            self.assertNotIn(banned, source)
        src_dir = os.path.join(ROOT, "src")
        for name in ("gate.py", "hardfilter.py", "prefilter.py", "live_trader.py"):
            text = _read_text(os.path.join(src_dir, name))
            self.assertNotIn("decision_log", text)
