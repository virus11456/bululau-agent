"""跨日重置只換日欄。pending 與 meta 要留著。不連網。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="live-trader-day-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402

NOW = datetime(2026, 11, 14, 0, 2, tzinfo=lt.TPE)
ACC = {"totalWalletBalance": "200", "totalUnrealizedProfit": "0"}


class DayResetTest(unittest.TestCase):
    def run_reset(self, previous):
        saved = {}

        def load():
            return json.loads(json.dumps(previous))

        def save(s):
            saved.clear()
            saved.update(s)

        with patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch.object(lt, "now_tpe", return_value=NOW), \
                patch.object(lt, "log"):
            out = lt.day_state(ACC)
        return out, saved

    def test_pending_ladder_and_meta_survive(self):
        previous = {
            "day": "2026-11-13",
            "day_start_equity": 100.0,
            "slice_margin": 5.0,
            "halted_new": True,
            "killed": True,
            "created": "2026-11-13T00:01:00+08:00",
            "pending": {"DODOXUSDT": {"ladder": True, "orders": [{"orderId": 7, "price": "1"}]}},
            "meta": {"DODOXUSDT": {"setup": "瘸師弟", "stop": "0.9", "target": "1.2"}},
            "watch_heartbeat": "2026-11-13T23:10:00+08:00",
            "capital_flow_last_ms": 1000,
            "capital_flow_seen": ["99"],
        }
        out, saved = self.run_reset(previous)
        self.assertEqual(out["pending"], previous["pending"])
        self.assertEqual(out["meta"], previous["meta"])
        self.assertEqual(out["watch_heartbeat"], previous["watch_heartbeat"])
        self.assertEqual(saved["pending"], previous["pending"])
        self.assertEqual(out["day"], "2026-11-14")
        self.assertEqual(out["day_start_equity"], 200.0)
        self.assertEqual(out["slice_margin"], round(200 / lt.CFG["slices"], 4))
        self.assertFalse(out["halted_new"])
        self.assertFalse(out["killed"])
        self.assertEqual(out["capital_flow_seen"], [])
        self.assertEqual(out["capital_flow_last_ms"], int(NOW.timestamp() * 1000))
        self.assertTrue(str(out["created"]).startswith("2026-11-14"))

    def test_no_extra_keys_match_the_old_reset(self):
        previous = {
            "day": "2026-11-13",
            "day_start_equity": 1.0,
            "slice_margin": 1.0,
            "halted_new": False,
            "killed": False,
            "created": "old",
        }
        out, _saved = self.run_reset(previous)
        eq = 200.0
        expected = dict(day="2026-11-14", day_start_equity=eq,
                        slice_margin=round(eq / lt.CFG["slices"], 4),
                        halted_new=False, killed=False, created=out["created"],
                        capital_flow_last_ms=int(NOW.timestamp() * 1000), capital_flow_seen=[])
        self.assertEqual(out, expected)
        self.assertNotIn("pending", out)
        self.assertNotIn("meta", out)


if __name__ == "__main__":
    unittest.main()
