"""布鹿流 RR 1.2–1.5 紙上彙整。不連網，不改實盤門檻。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import bulu_rr_paper as paper  # noqa: E402


def gate_row(when, blocked):
    return {"time": when, "decision": "QUIET", "hardfilter": {"failed": False, "blocked": blocked}}


class BuluRrPaperTest(unittest.TestCase):
    def test_band_keeps_only_bulu_between_1_2_and_1_5(self):
        rows = [
            gate_row("2026-11-11T02:31:28+08:00", [
                {"symbol": "NIGHTUSDT", "setup": "布鹿流", "rule": "bulu_rr_after_widening",
                 "why": "停損 3.3% 套 5% 下限後 RR 1.29 <1.5（目標＝前高）"},
                {"symbol": "LOWUSDT", "setup": "布鹿流", "rule": "bulu_rr_after_widening",
                 "why": "RR 1.19 <1.5"},
                {"symbol": "EDGEUSDT", "setup": "布鹿流", "rule": "bulu_rr_after_widening",
                 "why": "RR 1.50 <1.5"},
                {"symbol": "ONEUSDT", "setup": "瘸師弟", "rule": "kuai_long_black", "why": "只有 1 根長黑"},
            ]),
        ]
        got = paper.collect(rows, [])
        self.assertEqual([row["symbol"] for row in got], ["NIGHTUSDT"])
        self.assertEqual(got[0]["rr"], 1.29)
        self.assertEqual(got[0]["entry"], "未取得")
        self.assertEqual(got[0]["stop"], "未取得")
        self.assertEqual(got[0]["prior_high"], "未取得")
        self.assertEqual(paper.LIVE_RR, 1.5)

    def test_same_bar_stop_and_target_counts_as_stop(self):
        item = {
            "symbol": "NIGHTUSDT", "setup": "布鹿流", "rule": "bulu_rr_after_widening",
            "why": "RR 1.31 <1.5", "entry": 100, "stop": 95, "prior_high": 110,
        }
        got = paper.collect([gate_row("2026-11-12T01:00:00+08:00", [item])], [])
        paper.simulate(got[0], {"NIGHTUSDT": [{"h": 110, "l": 95}]})
        self.assertEqual(got[0]["outcome"], "stop")
        self.assertEqual(got[0]["r_result"], -1.0)
        paper.simulate(got[0], {"NIGHTUSDT": [{"h": 109, "l": 96}, {"h": 110, "l": 96}]})
        self.assertEqual(got[0]["outcome"], "target")
        self.assertEqual(got[0]["r_result"], 1.31)

    def test_missing_bars_leave_win_rate_unknown(self):
        summary = paper.summarize(paper.collect([
            gate_row("2026-11-12T01:00:00+08:00", [
                {"symbol": "NIGHTUSDT", "setup": "布鹿流", "why": "RR 1.40 <1.5"},
            ]),
        ], []))
        self.assertEqual(summary["n"], 1)
        self.assertEqual(summary["win_rate"], "未取得")
        self.assertEqual(summary["expectancy_r"], "未取得")
        self.assertEqual(summary["outcomes_missing"], 1)

    def test_decision_log_is_attached_and_cli_reads_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate_path = os.path.join(tmp, "gate_log.jsonl")
            decision_path = os.path.join(tmp, "decision_log.jsonl")
            bars_path = os.path.join(tmp, "bars.json")
            with open(gate_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(gate_row("2026-11-12T01:00:00+08:00", [{
                    "symbol": "NIGHTUSDT", "setup": "布鹿流", "why": "RR 1.40 <1.5",
                    "entry": 10, "stop": 9, "prior_high": 12,
                }]), ensure_ascii=False) + "\n")
            with open(decision_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "symbol": "NIGHTUSDT", "round_time": "2026-11-12T01:00:00+08:00",
                    "decision": "reject", "reason": "紙上",
                }, ensure_ascii=False) + "\n")
            with open(bars_path, "w", encoding="utf-8") as handle:
                json.dump({"NIGHTUSDT": [{"h": 12, "l": 9.5}]}, handle)
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = paper.main(["--gate-log", gate_path, "--decision-log", decision_path, "--bars", bars_path])
            body = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(body["rows"][0]["decision"], "reject")
        self.assertEqual(body["rows"][0]["outcome"], "target")

    def test_cli_prints_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate_path = os.path.join(tmp, "gate_log.jsonl")
            with open(gate_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(gate_row("2026-11-12T01:00:00+08:00", [{
                    "symbol": "NIGHTUSDT", "setup": "布鹿流", "why": "RR 1.40 <1.5",
                    "entry": 10, "stop": 9, "prior_high": 12,
                }]), ensure_ascii=False) + "\n")
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = paper.main(["--gate-log", gate_path])
            body = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(body["rows"][0]["decision"], "未取得")
        self.assertEqual(body["live_threshold"], 1.5)
        self.assertEqual(body["rows"][0]["rr"], 1.4)
