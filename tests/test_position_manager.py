"""position_manager 的 2026-10 出場。不連網，不改規則數字。"""
import os
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
# test_mac_prefilter 會先把 backups 的 common 放進 sys.modules。這裡暫時改回 src 的 common，
# 匯入完把原本的 common 放回去，避免改到那份測試。
_saved_common = sys.modules.get("common")
_saved_file = getattr(_saved_common, "__file__", "") or ""
_swapped = "backups" in _saved_file
if _swapped:
    del sys.modules["common"]
sys.path.insert(0, SRC)
import position_manager as pm  # noqa: E402
sys.path.remove(SRC)
if _swapped:
    sys.modules["common"] = _saved_common


def bar(t_ms, low, high, close=None):
    return {"t": t_ms, "l": low, "h": high, "c": close if close is not None else high}


class PositionManagerTest(unittest.TestCase):
    def setUp(self):
        self.opened = "2026-11-12T00:00:00+08:00"
        self.t0 = pm.iso_ms(self.opened)

    def position(self, **extra):
        row = {
            "symbol": "XUSDT",
            "pattern": "布鹿流",
            "entry": 100.0,
            "stop": 90.0,
            "initial_stop": 90.0,
            "target": 120.0,
            "status": "open",
            "opened": self.opened,
            "filled": self.opened,
        }
        row.update(extra)
        return row

    def test_rules_stay_october_2026_and_header_says_not_live(self):
        self.assertEqual(pm.RULES["布鹿流"]["be_at_r"], 1.0)
        self.assertIsNone(pm.RULES["布鹿流"]["time_bars"])
        self.assertEqual(pm.RULES["瘸師弟"]["be_at_r"], 0.5)
        self.assertEqual(pm.RULES["瘸師弟"]["time_bars"], 4)
        text = pm.__doc__ or ""
        self.assertIn("2026-10", text)
        self.assertIn("未接現行監控", text)

    def test_same_bar_stop_and_target_closes_at_stop(self):
        events = []
        row = self.position()
        pm.klines = lambda *_a, **_k: [bar(self.t0, 90.0, 120.0, 100.0)]
        result = pm.manage(row, events)
        self.assertEqual(result, "close")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertEqual(row["exit"], 90.0)

    def test_target_only_closes_at_target(self):
        events = []
        row = self.position()
        pm.klines = lambda *_a, **_k: [bar(self.t0, 95.0, 120.0, 119.0)]
        result = pm.manage(row, events)
        self.assertEqual(result, "close")
        self.assertEqual(row["exit_reason"], "target")
        self.assertEqual(row["exit"], 120.0)

    def test_xiao_full_exit_at_2r_and_stop_stays(self):
        events = []
        row = self.position(pattern="小師弟", target=120.0)
        pm.klines = lambda *_a, **_k: [bar(self.t0, 95.0, 110.0, 109.0)]
        self.assertIsNone(pm.manage(row, events))
        self.assertEqual(row["stop"], 90.0)
        self.assertEqual(events, [])
        events = []
        pm.klines = lambda *_a, **_k: [bar(self.t0 + 900000, 95.0, 120.0, 119.0)]
        row["pm_last_bar"] = self.t0
        result = pm.manage(row, events)
        self.assertEqual(result, "close")
        self.assertEqual(row["exit_reason"], "exit_2r")
        self.assertEqual(row["exit"], 120.0)
        self.assertEqual(row["stop"], 90.0)
        self.assertIsNone(pm.RULES["小師弟"]["be_at_r"])
        self.assertIsNone(pm.RULES["小師弟"]["time_bars"])

    def test_da_shixiong_scales_at_2r_and_is_flat_before_4r(self):
        events = []
        row = self.position(pattern="大師兄", target=120.0)
        pm.klines = lambda *_a, **_k: [bar(self.t0, 95.0, 125.0, 124.0)]
        self.assertIsNone(pm.manage(row, events))
        self.assertEqual(row["stop"], 90.0)
        self.assertTrue(row["scaled_2r"])
        self.assertEqual(events[0]["action"], "scale_50_at_2r")
        events = []
        row = self.position(pattern="大師兄", target=120.0)
        pm.klines = lambda *_a, **_k: [bar(self.t0, 95.0, 135.0, 134.0)]
        self.assertEqual(pm.manage(row, events), "close")
        self.assertEqual(row["exit_reason"], "exit_3r_rest")
        self.assertEqual(row["exit"], 130.0)
        self.assertEqual(row["stop"], 90.0)
        events = []
        row = self.position(pattern="大師兄", target=120.0)
        pm.klines = lambda *_a, **_k: [bar(self.t0, 95.0, 145.0, 144.0)]
        self.assertEqual(pm.manage(row, events), "close")
        self.assertEqual(row["exit_reason"], "exit_3r_rest")
        self.assertEqual(row["exit"], 130.0)
        self.assertEqual(row["r"], 2.5)

    def test_da_shixiong_stop_after_half_is_half_r(self):
        events = []
        row = self.position(pattern="大師兄", target=120.0)
        pm.klines = lambda *_a, **_k: [
            bar(self.t0, 95.0, 125.0, 124.0),
            bar(self.t0 + 900000, 90.0, 110.0, 91.0),
        ]
        self.assertEqual(pm.manage(row, events), "close")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertEqual(row["r"], 0.5)
        self.assertIsNone(pm.RULES["大師兄"]["be_at_r"])
        self.assertIsNone(pm.RULES["大師兄"]["time_bars"])
        self.assertEqual(pm.RULES["瘸師弟"]["be_at_r"], 0.5)
        self.assertEqual(pm.RULES["布鹿流"]["be_at_r"], 1.0)
