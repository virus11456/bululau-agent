"""對既有 cmd_manage / cmd_watch 的離線契約。不連幣安、不讀金鑰。"""
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import live_trader as lt
from src.phase1.manage_job import TWO_H_MS, build_manage_argv, discretion_wakes, run_manage


class A:
    def __init__(self, live):
        self.live = live


def _pos(symbol, amt):
    return {"symbol": symbol, "positionAmt": str(amt), "entryPrice": "1", "unrealizedProfit": "0", "leverage": "5", "isolated": True}


class World:
    def __init__(self):
        self.acc = {"totalWalletBalance": "10000", "totalUnrealizedProfit": "0", "positions": []}
        self.orders = []
        self.algos = []
        self.order_status = {}
        self.query_fail = {}
        self.protects = []
        self.closes = []
        self.cancels = []
        self.protect_error = None
        self.calls = []


def _bind(world):
    lt.account = lambda: world.acc
    lt.open_orders = lambda symbol=None: [o for o in world.orders if symbol is None or o["symbol"] == symbol]
    lt.open_algo = lambda symbol=None: [o for o in world.algos if symbol is None or o.get("symbol") == symbol]

    def req(method, path, params=None, signed=False, retries=5):
        world.calls.append((method, path, params))
        if method == "GET" and path == "/fapi/v1/order":
            oid = str((params or {}).get("orderId"))
            if oid in world.query_fail:
                raise world.query_fail[oid]
            return world.order_status[oid]
        return {"ok": True}

    def place_protect(symbol, close_side, otype, trigger, live, qty=None):
        if world.protect_error:
            raise world.protect_error
        world.protects.append({"symbol": symbol, "type": otype, "qty": qty, "live": live, "trigger": trigger})
        return {"ok": True}

    lt.req = req
    lt.place_protect = place_protect
    lt.market_close = lambda symbol, amt, live, reason: world.closes.append((symbol, amt, live, reason))
    lt.cancel_protect = lambda symbol, po, live: world.cancels.append((symbol, po, live))


class ManageContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = (lt.HOME, lt.LOG, lt.STATE, lt.HALT_FLAG, lt.LOCK)
        lt.HOME = str(root)
        lt.LOG = str(root / "live_log.jsonl")
        lt.STATE = str(root / "state.json")
        lt.HALT_FLAG = str(root / "HALT_REPORT_USER.flag")
        lt.LOCK = str(root / ".lock")
        today = lt.now_tpe().strftime("%Y-%m-%d")
        json.dump({
            "day": today, "day_start_equity": 10000, "slice_margin": 500,
            "halted_new": False, "killed": False, "pending": {}, "meta": {},
        }, open(lt.STATE, "w"), ensure_ascii=False)
        self.world = World()
        _bind(self.world)

    def tearDown(self):
        lt.HOME, lt.LOG, lt.STATE, lt.HALT_FLAG, lt.LOCK = self.paths
        self.tmp.cleanup()

    def _state(self):
        return json.load(open(lt.STATE, encoding="utf-8"))

    def test_seven_percent_halts_new_and_still_scans_positions(self):
        self.world.acc["totalWalletBalance"] = "9200"
        self.world.acc["positions"] = [_pos("ETHUSDT", 1)]
        self._state_write(meta={"ETHUSDT": {"stop": "1", "target": "2", "setup": "布鹿流"}})
        lt.cmd_manage(A(False))
        self.assertTrue(self._state()["halted_new"])
        self.assertFalse(Path(lt.HALT_FLAG).exists())
        self.assertEqual([p["type"] for p in self.world.protects], ["STOP_MARKET", "TAKE_PROFIT_MARKET"])

    def test_thirty_percent_live_flattens_and_sets_halt_once(self):
        self.world.acc["totalWalletBalance"] = "6900"
        self.world.acc["positions"] = [_pos("ETHUSDT", 2)]
        lt.cmd_manage(A(True))
        self.assertTrue(self._state()["killed"])
        self.assertTrue(Path(lt.HALT_FLAG).exists())
        self.assertEqual(self.world.closes, [("ETHUSDT", 2.0, True, "daily_kill_30pct")])
        self.world.acc["positions"] = []
        self.world.closes.clear()
        lt.cmd_manage(A(True))
        self.assertEqual(self.world.closes, [])

    def test_dry_run_thirty_percent_does_not_persist_halt(self):
        self.world.acc["totalWalletBalance"] = "6900"
        lt.cmd_manage(A(False))
        self.assertFalse(self._state()["killed"])
        self.assertFalse(Path(lt.HALT_FLAG).exists())

    def test_position_without_orders_is_scanned_by_manage_not_watch(self):
        self.world.acc["positions"] = [_pos("ETHUSDT", 3)]
        self._state_write(meta={"ETHUSDT": {"stop": "1", "target": "2", "setup": "瘸師弟"}})
        seen = {"account": 0}
        real = lt.account
        lt.account = lambda: seen.__setitem__("account", seen["account"] + 1) or real()
        lt.cmd_watch(A(True))
        self.assertEqual(seen["account"], 0)
        lt.cmd_manage(A(False))
        self.assertGreaterEqual(seen["account"], 1)
        self.assertEqual(len(self.world.protects), 2)

    def test_partial_fill_while_limit_open_does_not_shrink_protection(self):
        self.world.acc["positions"] = [_pos("ETHUSDT", 2)]
        self.world.orders = [{
            "symbol": "ETHUSDT", "type": "STOP_MARKET", "origQty": "5",
            "stopPrice": "1", "closePosition": False, "orderId": 9,
        }]
        self.world.order_status["7"] = {"status": "PARTIALLY_FILLED", "executedQty": "2", "origQty": "5"}
        self._state_write(pending={"ETHUSDT": {"orderId": 7, "stop": "1", "target": "2", "setup": "瘸師弟"}})
        lt.cmd_manage(A(False))
        self.assertTrue(all(p["type"] != "STOP_MARKET" for p in self.world.protects))
        self.assertEqual(self.world.cancels, [])
        self.assertIn("ETHUSDT", self._state()["pending"])

    def test_old_limit_is_not_cancelled_by_manage(self):
        self.world.order_status["7"] = {"status": "NEW", "executedQty": "0", "origQty": "5", "time": 1}
        self._state_write(pending={"ETHUSDT": {"orderId": 7, "stop": "1", "target": "2", "setup": "瘸師弟"}})
        lt.cmd_manage(A(True))
        self.assertEqual(self.world.cancels, [])
        self.assertNotIn(("DELETE", "/fapi/v1/order"), [(c[0], c[1]) for c in self.world.calls])

    def test_query_error_skips_that_symbol_and_continues(self):
        self.world.query_fail["1"] = lt.BinanceError(500, -1007, "unknown")
        self.world.order_status["2"] = {"status": "CANCELED", "executedQty": "0", "origQty": "1"}
        self._state_write(pending={
            "AAAUSDT": {"orderId": 1, "stop": "1", "target": "2", "setup": "瘸師弟"},
            "BBBUSDT": {"orderId": 2, "stop": "1", "target": "2", "setup": "瘸師弟"},
        })
        lt.cmd_manage(A(True))
        state = self._state()
        self.assertIn("AAAUSDT", state["pending"])
        self.assertNotIn("BBBUSDT", state["pending"])

    def test_disconnect_aborts_remaining_symbols(self):
        self.world.query_fail["2"] = urllib.error.URLError("timed out")
        self.world.order_status["1"] = {"status": "CANCELED", "executedQty": "0", "origQty": "1"}
        self._state_write(pending={
            "AAAUSDT": {"orderId": 1, "stop": "1", "target": "2", "setup": "瘸師弟"},
            "BBBUSDT": {"orderId": 2, "stop": "1", "target": "2", "setup": "瘸師弟"},
            "CCCUSDT": {"orderId": 3, "stop": "1", "target": "2", "setup": "瘸師弟"},
        })
        with self.assertRaises(urllib.error.URLError):
            lt.cmd_manage(A(True))
        state = self._state()
        self.assertNotIn("AAAUSDT", state["pending"])
        self.assertIn("CCCUSDT", state["pending"])

    def test_resize_failure_keeps_old_protection(self):
        self.world.acc["positions"] = [_pos("ETHUSDT", 2)]
        self.world.orders = [{
            "symbol": "ETHUSDT", "type": "STOP_MARKET", "origQty": "5",
            "stopPrice": "1", "closePosition": False, "orderId": 9,
        }]
        self.world.protect_error = lt.BinanceError(504, -1007, "unknown")
        self._state_write(meta={"ETHUSDT": {"stop": "1", "target": "2", "setup": "布鹿流"}})
        lt.cmd_manage(A(True))
        self.assertEqual(self.world.cancels, [])

    def test_missing_stop_without_meta_market_closes(self):
        self.world.acc["positions"] = [_pos("ETHUSDT", 1)]
        lt.cmd_manage(A(True))
        self.assertEqual(self.world.closes[0][3], "no_stop_no_meta")

    def test_second_process_does_not_enter(self):
        with lt.Lock():
            with self.assertRaises(SystemExit):
                with lt.Lock():
                    pass

    def test_two_hour_limit_wakes_ai_without_cancelling(self):
        now = 10_000_000_000
        wakes = discretion_wakes([
            {"symbol": "ETHUSDT", "type": "LIMIT", "orderId": 7, "time": now - TWO_H_MS - 1},
            {"symbol": "BTCUSDT", "type": "LIMIT", "orderId": 8, "time": now - TWO_H_MS},
        ], now)
        limits = [item for item in wakes if item["kind"] == "limit_older_than_2h"]
        self.assertEqual([item["order_id"] for item in limits], [7])
        self.assertFalse(limits[0]["cancel"])
        self.assertEqual(wakes[-1]["detector"], None)
        self.assertEqual(wakes[-1]["action"], "keep_original_ai_poll")

    def test_job_does_not_read_candidates_and_alerts_on_failure(self):
        argv = build_manage_argv()
        self.assertEqual(argv[-2:], ["manage", "--live"])
        alert = Path(self.tmp.name) / "alert.jsonl"
        code = run_manage(argv, lambda cmd: 1, alert)
        self.assertEqual(code, 1)
        self.assertIn("exit_code", alert.read_text(encoding="utf-8"))
        self.assertNotIn("candidates", " ".join(argv))

    def _state_write(self, pending=None, meta=None):
        state = self._state()
        if pending is not None:
            state["pending"] = pending
        if meta is not None:
            state["meta"] = meta
        json.dump(state, open(lt.STATE, "w"), ensure_ascii=False)
