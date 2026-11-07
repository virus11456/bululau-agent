"""離線 mock。數字來自合成契約，不是行情或績效。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.phase1.autoscan import ScanBook, fingerprint
from src.phase1.session import Session, Store, run_case
from src.phase1.summary import summarize_position

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "handoff_contract_v1.json"
EVIDENCE = ROOT / "tests" / "evidence"


def _cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _mismatch(result, expect):
    bad = []
    for key, wanted in expect.items():
        got = result.get(key)
        if got != wanted:
            bad.append((key, got, wanted))
    return bad


class Phase1ContractTest(unittest.TestCase):
    def test_matrix_cases(self):
        cases = _cases()
        self.assertEqual([case["id"] for case in cases], [
            "T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09",
            "T10", "T11", "T12", "T13", "T14", "T15", "T16", "T17", "T18", "T19",
            "A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08",
        ])
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        failures = []
        for case in cases:
            with tempfile.TemporaryDirectory() as tmp:
                result, evidence = run_case(case, Path(tmp) / case["id"])
            evidence["expect"] = case["expect"]
            bad = _mismatch(result, case["expect"])
            evidence["pass"] = not bad
            evidence["mismatch"] = [
                {"key": key, "got": got, "wanted": wanted} for key, got, wanted in bad
            ]
            (EVIDENCE / f"{case['id']}.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if bad or not evidence["trace"]:
                failures.append((case["id"], bad, len(evidence["trace"])))
        self.assertEqual(failures, [])

    def test_entry_fee_remainder_is_auditable(self):
        summary = summarize_position({
            "instrument": "synthetic_linear_quote_settled",
            "side": "long",
            "entry_fills": [{"id": "e", "qty": "10", "price": "100", "fee_quote": "1"}],
            "exit_fills": [{"id": "x", "qty": "4", "price": "110", "fee_quote": "0.2"}],
            "initial_risk_quote": "100",
            "planned_exit_price": "110",
        })
        self.assertEqual(summary["entry_fee_allocated_quote"], "0.4")
        self.assertEqual(summary["entry_fee_unallocated_quote"], "0.6")
        self.assertEqual(summary["gross_pnl_quote"], "40")
        self.assertEqual(summary["net_pnl_quote"], "39.4")

    def test_duplicate_fill_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), fresh=True)
            session.load_case({
                "id": "dup",
                "area": "瘸師弟",
                "given": {"position_qty": "10", "observed_R": "1.5"},
                "events": [],
            })
            session.threshold_reached()
            session.apply_fill("4", "fill-same")
            session.apply_fill("4", "fill-same")
            self.assertEqual(session.s["exits"]["life-1:full_exit"]["filled_qty"], "4")
            self.assertEqual(session.s["position_qty"], "6")

    def test_reduce_only_unknown_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), fresh=True)
            session.load_case({
                "id": "X",
                "area": "瘸師弟",
                "given": {
                    "position_qty": "10",
                    "observed_R": "1.5",
                    "reduce_only_supported": None,
                },
                "events": [],
            })
            session.threshold_reached()
            self.assertEqual(session.s["status"], "blocked")
            self.assertEqual(session.s["reason"], "reduce_only_unknown")
            self.assertEqual(session.s["exits"], {})

    def test_failed_scan_does_not_replace_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            book = ScanBook(store)
            book.publish(
                scan_id="scan-1",
                started_at="2026-11-07T09:00:00+08:00",
                completed_at="2026-11-07T09:00:01+08:00",
                status="ok",
                candidates=[{
                    "symbol": "SYNTHUSDT",
                    "setup": "瘸師弟",
                    "bar_time": "2026-11-07T09:00:00+08:00",
                    "ladder": [{"price": "1", "qty": "1"}],
                }],
                notify=True,
            )
            before = store.state["snapshot"]["scan_id"]
            rejected = book.publish(
                scan_id="scan-fail",
                started_at="2026-11-07T09:15:00+08:00",
                completed_at="2026-11-07T09:15:01+08:00",
                status="failed",
                candidates=[],
                notify=False,
            )
            self.assertFalse(rejected["replaced"])
            self.assertEqual(store.state["snapshot"]["scan_id"], before)
            self.assertNotEqual(store.state["snapshot"]["candidates"], [])

    def test_empty_snapshot_keeps_tombstone(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), fresh=True)
            session.load_case({"id": "A01", "area": "空快照", "given": {"previous_candidates": ["old"]}, "events": []})
            session._life()["tombstones"] = ["2R"]
            session.save()
            session.stage_empty()
            session.commit_staged_snapshot()
            self.assertEqual(session.s["snapshot"]["candidates"], [])
            self.assertEqual(session.s["lives"]["life-1"]["tombstones"], ["2R"])
            self.assertEqual(session.s["ai_wake_count"], 0)

    def test_older_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            book = ScanBook(store)
            book.publish(
                scan_id="new",
                started_at="2026-11-07T10:00:00+08:00",
                completed_at="2026-11-07T10:00:01+08:00",
                status="ok",
                candidates=[],
                notify=False,
            )
            rejected = book.publish(
                scan_id="old",
                started_at="2026-11-07T09:00:00+08:00",
                completed_at="2026-11-07T09:00:01+08:00",
                status="ok",
                candidates=[],
                notify=False,
            )
            self.assertEqual(rejected["status"], "blocked")
            self.assertEqual(store.state["snapshot"]["scan_id"], "new")

    def test_fingerprint_ignores_scan_id_and_order(self):
        ladder = [{"price": "100", "qty": "1", "level": "L1"}, {"price": "90", "qty": "2", "level": "L2"}]
        base = fingerprint("SYNTHUSDT", "synthetic-setup", "2026-11-07T09:00:00+08:00", ladder)
        reversed_ladder = list(reversed(ladder))
        self.assertEqual(
            base,
            fingerprint("SYNTHUSDT", "synthetic-setup", "2026-11-07T09:00:00+08:00", reversed_ladder),
        )
        self.assertNotIn("scan", base)


if __name__ == "__main__":
    unittest.main()
