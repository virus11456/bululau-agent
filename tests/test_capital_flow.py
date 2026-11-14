"""USDT-M 劃轉重算當日風險基準。不連網、不讀金鑰。"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="capital-flow-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=lt.TPE)
TODAY = "2026-11-14"


def info():
    return dict(tick=Decimal("0.01"), step=Decimal("0.001"), mstep=Decimal("0.001"),
                minqty=Decimal("0.001"), maxmqty=Decimal("1000000"), minnot=Decimal("5"),
                status="TRADING", ctype="PERPETUAL")


def start_ms():
    return int(NOW.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def transfer(tran_id, amount, when, asset="USDT"):
    return {"tranId": tran_id, "income": str(amount), "asset": asset, "time": when,
            "incomeType": "TRANSFER"}


def base_state(**kw):
    state = {
        "day": TODAY,
        "day_start_equity": 10000.0,
        "slice_margin": 500.0,
        "halted_new": False,
        "killed": False,
        "capital_flow_last_ms": start_ms(),
        "capital_flow_seen": [],
        "pending": {},
        "meta": {},
    }
    state.update(kw)
    return state


class Store:
    def __init__(self, state):
        self.state = json.loads(json.dumps(state))
        self.saves = []

    def load(self):
        return json.loads(json.dumps(self.state))

    def save(self, state):
        self.state = json.loads(json.dumps(state))
        self.saves.append(json.loads(json.dumps(state)))


class CapitalFlowTest(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.calls = []
        self.rows = []
        self.read_error = None
        limit = lt._ratelimit_path()
        if os.path.exists(limit):
            os.remove(limit)

    def tearDown(self):
        limit = lt._ratelimit_path()
        if os.path.exists(limit):
            os.remove(limit)

    def _apply(self, state, live=True, now=None):
        store = Store(state)

        def fake_req(method, path, params=None, signed=False, retries=5):
            self.calls.append((method, path, dict(params or {})))
            if self.read_error:
                raise self.read_error
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return list(self.rows)
            raise AssertionError((method, path, params))

        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "now_tpe", return_value=now or NOW), \
                patch.object(lt, "load_state", side_effect=store.load), \
                patch.object(lt, "save_state", side_effect=store.save):
            with redirect_stdout(buf):
                loaded = store.load()
                out = lt.apply_capital_flows(loaded, live)
        return out, store, buf.getvalue()

    def flows(self):
        return [kw for kind, kw in self.logs if kind == "capital_flow"]

    def test_missing_cursor_bootstraps_without_reading(self):
        deposit_at = int(datetime(2026, 11, 14, 20, 0, 10, tzinfo=lt.TPE).timestamp() * 1000)
        self.rows = [transfer(430869042813, "238.16898", deposit_at)]
        for missing in (None, ""):
            with self.subTest(missing=missing):
                self.logs.clear()
                self.calls.clear()
                state = base_state(day_start_equity=10238.16898, capital_flow_last_ms=missing,
                                   capital_flow_seen=["430869042813"])
                out, store, _text = self._apply(state, live=True)
                self.assertEqual(self.calls, [])
                self.assertEqual(out["day_start_equity"], 10238.16898)
                self.assertEqual(out["capital_flow_last_ms"], int(NOW.timestamp() * 1000))
                self.assertEqual(out["capital_flow_seen"], [])
                self.assertEqual(store.state["capital_flow_last_ms"], int(NOW.timestamp() * 1000))
                self.assertEqual(store.state["capital_flow_seen"], [])
                self.assertEqual(store.state["day_start_equity"], 10238.16898)
                self.assertEqual([kind for kind, _kw in self.logs], ["capital_flow_init"])
                self.assertEqual(self.flows(), [])

    def test_bootstrap_dry_run_logs_without_writing(self):
        state = base_state()
        del state["capital_flow_last_ms"]
        out, store, _text = self._apply(state, live=False)
        self.assertEqual(self.calls, [])
        self.assertEqual(store.saves, [])
        self.assertNotIn("capital_flow_last_ms", store.state)
        self.assertEqual(out["capital_flow_last_ms"], int(NOW.timestamp() * 1000))
        self.assertEqual(out["capital_flow_seen"], [])
        self.assertEqual(out["day_start_equity"], 10000.0)
        self.assertEqual([kind for kind, _kw in self.logs], ["capital_flow_init"])

    def test_first_run_after_bootstrap_skips_earlier_deposit(self):
        later = datetime(2026, 11, 14, 21, 0, tzinfo=lt.TPE)
        deposit_at = int(datetime(2026, 11, 14, 20, 0, 10, tzinfo=lt.TPE).timestamp() * 1000)
        equity = 10238.16898
        state = base_state(day_start_equity=equity, slice_margin=round(equity / lt.CFG["slices"], 4))
        del state["capital_flow_last_ms"]
        del state["capital_flow_seen"]
        self.rows = [transfer(430869042813, "238.16898", deposit_at)]
        _out, store, _text = self._apply(state, live=True, now=later)
        self.assertEqual(self.calls, [])
        self.assertEqual(store.state["day_start_equity"], equity)
        boot_ms = store.state["capital_flow_last_ms"]
        self.assertEqual(boot_ms, int(later.timestamp() * 1000))
        self.assertLess(deposit_at, boot_ms)
        self.logs.clear()
        self.calls.clear()
        again, store2, _text = self._apply(store.state, live=True, now=later)
        self.assertEqual(again["day_start_equity"], equity)
        self.assertEqual(store2.state["day_start_equity"], equity)
        self.assertEqual(self.flows(), [])
        self.assertEqual(self.calls[0][2]["startTime"], boot_ms)
        self.assertEqual(store2.state["capital_flow_seen"], [])

    def test_corrupt_cursor_skips_without_changes(self):
        self.rows = [transfer(1, "100", start_ms() + 1)]
        state = base_state(capital_flow_last_ms="not-a-time", capital_flow_seen=["9"])
        out, store, _text = self._apply(state)
        self.assertEqual(self.calls, [])
        self.assertEqual(store.saves, [])
        self.assertEqual(out["day_start_equity"], 10000.0)
        self.assertEqual(out["capital_flow_last_ms"], "not-a-time")
        self.assertEqual(out["capital_flow_seen"], ["9"])
        self.assertTrue(any(kind == "warn" for kind, _kw in self.logs))
        self.assertFalse(any(kind == "capital_flow_init" for kind, _kw in self.logs))

    def test_deposit_raises_baseline_and_slice(self):
        self.rows = [transfer(11, "1000", start_ms() + 1000)]
        out, store, _text = self._apply(base_state())
        self.assertEqual(out["day_start_equity"], 11000.0)
        self.assertEqual(out["slice_margin"], 550.0)
        self.assertEqual(out["capital_flow_seen"], ["11"])
        self.assertEqual(out["capital_flow_last_ms"], start_ms() + 1000)
        self.assertEqual(store.state["day_start_equity"], 11000.0)
        self.assertEqual(len(self.flows()), 1)
        self.assertEqual(self.flows()[0]["tranId"], 11)
        self.assertEqual(self.flows()[0]["amount"], 1000.0)
        self.assertEqual(self.flows()[0]["time"], start_ms() + 1000)
        self.assertEqual(self.flows()[0]["old_day_start_equity"], 10000.0)
        self.assertEqual(self.flows()[0]["day_start_equity"], 11000.0)
        self.assertEqual(self.flows()[0]["old_slice_margin"], 500.0)
        self.assertEqual(self.flows()[0]["slice_margin"], 550.0)
        self.assertEqual(self.calls[0][2]["incomeType"], "TRANSFER")
        self.assertEqual(self.calls[0][2]["startTime"], start_ms())

    def test_withdrawal_lowers_baseline(self):
        self.rows = [transfer(12, "-2500", start_ms() + 2000)]
        out, store, _text = self._apply(base_state())
        self.assertEqual(out["day_start_equity"], 7500.0)
        self.assertEqual(out["slice_margin"], 375.0)
        self.assertEqual(store.state["slice_margin"], 375.0)
        self.assertEqual(self.flows()[0]["amount"], -2500.0)
        self.assertEqual(self.flows()[0]["old_day_start_equity"], 10000.0)
        self.assertEqual(self.flows()[0]["day_start_equity"], 7500.0)

    def test_duplicate_tran_id_is_applied_once(self):
        self.rows = [transfer(13, "100", start_ms() + 3000)]
        out, store, _text = self._apply(base_state())
        self.assertEqual(out["day_start_equity"], 10100.0)
        self.logs.clear()
        self.calls.clear()
        again, store2, _text = self._apply(store.state)
        self.assertEqual(again["day_start_equity"], 10100.0)
        self.assertEqual(store2.state["day_start_equity"], 10100.0)
        self.assertEqual(self.flows(), [])
        self.assertEqual(store2.saves, [])

    def test_multiple_records_apply_in_time_order(self):
        self.rows = [
            transfer(22, "50", start_ms() + 5000),
            transfer(21, "100", start_ms() + 4000),
        ]
        out, _store, _text = self._apply(base_state())
        self.assertEqual([kw["tranId"] for kw in self.flows()], [21, 22])
        self.assertEqual(self.flows()[0]["old_day_start_equity"], 10000.0)
        self.assertEqual(self.flows()[0]["day_start_equity"], 10100.0)
        self.assertEqual(self.flows()[0]["slice_margin"], 505.0)
        self.assertEqual(self.flows()[1]["old_day_start_equity"], 10100.0)
        self.assertEqual(self.flows()[1]["day_start_equity"], 10150.0)
        self.assertEqual(self.flows()[1]["old_slice_margin"], 505.0)
        self.assertEqual(out["day_start_equity"], 10150.0)
        self.assertEqual(out["slice_margin"], 507.5)
        self.assertEqual(out["capital_flow_seen"], ["21", "22"])
        self.assertEqual(out["capital_flow_last_ms"], start_ms() + 5000)

    def test_non_usdt_is_logged_and_not_added(self):
        self.rows = [transfer(31, "0.5", start_ms() + 6000, asset="BTC")]
        out, store, _text = self._apply(base_state())
        self.assertEqual(out["day_start_equity"], 10000.0)
        self.assertEqual(out["slice_margin"], 500.0)
        self.assertEqual(self.flows(), [])
        ignored = [kw for kind, kw in self.logs if kind == "capital_flow_ignored"]
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0]["asset"], "BTC")
        self.assertEqual(ignored[0]["tranId"], 31)
        self.assertEqual(out["capital_flow_seen"], ["31"])
        self.assertEqual(store.state["capital_flow_last_ms"], start_ms() + 6000)
        self.logs.clear()
        again, _store2, _text = self._apply(store.state)
        self.assertEqual(again["day_start_equity"], 10000.0)
        self.assertEqual([kw for kind, kw in self.logs if kind == "capital_flow_ignored"], [])

    def test_rate_limit_skips_without_touching_state(self):
        for status in (418, 429):
            with self.subTest(status=status):
                self.logs.clear()
                self.calls.clear()
                self.rows = [transfer(41, "1000", start_ms() + 7000)]
                self.read_error = lt.BinanceError(status, -1003, "slow down")
                before = base_state()
                out, store, _text = self._apply(before)
                self.assertEqual(out["day_start_equity"], 10000.0)
                self.assertEqual(out["capital_flow_last_ms"], start_ms())
                self.assertEqual(store.saves, [])
                self.assertEqual(store.state["capital_flow_seen"], [])
                self.assertEqual(self.flows(), [])
                self.read_error = None

    def test_network_error_skips_without_touching_state(self):
        self.rows = [transfer(42, "1000", start_ms() + 8000)]
        self.read_error = urllib.error.URLError("timed out")
        out, store, _text = self._apply(base_state())
        self.assertEqual(out["day_start_equity"], 10000.0)
        self.assertEqual(out["capital_flow_last_ms"], start_ms())
        self.assertEqual(store.saves, [])
        self.assertEqual(self.flows(), [])

    def test_active_backoff_does_not_read(self):
        lt.write_ratelimit(429, NOW.timestamp() + 60)
        self.rows = [transfer(43, "1000", start_ms() + 9000)]
        with patch.object(lt, "time") as clock:
            clock.time.return_value = NOW.timestamp()
            out, store, _text = self._apply(base_state())
        self.assertEqual(self.calls, [])
        self.assertEqual(out["day_start_equity"], 10000.0)
        self.assertEqual(store.saves, [])

    def test_dry_run_logs_but_does_not_save(self):
        self.rows = [transfer(51, "1000", start_ms() + 1500)]
        original = base_state()
        out, store, text = self._apply(original, live=False)
        self.assertIn("資金異動（dry-run，不改 state）", text)
        self.assertNotIn("不改 state） tranId=51", text.replace("資金異動（dry-run，不改 state）", ""))
        self.assertEqual(store.saves, [])
        self.assertEqual(store.state["day_start_equity"], 10000.0)
        self.assertEqual(out["day_start_equity"], 11000.0)
        self.assertEqual(out["slice_margin"], 550.0)
        self.assertEqual(len(self.flows()), 1)
        self.assertEqual(self.flows()[0]["day_start_equity"], 11000.0)

    def test_day_reset_does_not_reapply_transfer_from_before_reset(self):
        reset_at = datetime(2026, 11, 14, 0, 5, tzinfo=lt.TPE)
        previous = base_state(day="2026-11-13", day_start_equity=5000.0, slice_margin=250.0,
                              capital_flow_last_ms=1000, capital_flow_seen=["7"])
        old = transfer(7, "1000", 1000)
        store = Store(previous)
        acc = {"totalWalletBalance": "8000", "totalUnrealizedProfit": "0", "positions": []}
        seen_starts = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            self.calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                start = int(params["startTime"])
                seen_starts.append(start)
                return [old] if old["time"] >= start else []
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
                return []
            raise AssertionError((method, path, params))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "now_tpe", return_value=reset_at), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "load_state", side_effect=store.load), \
                patch.object(lt, "save_state", side_effect=store.save):
            with redirect_stdout(io.StringIO()):
                lt.cmd_manage(type("A", (), {"live": True, "with_status": False})())
        self.assertEqual(store.state["day"], TODAY)
        self.assertEqual(store.state["day_start_equity"], 8000.0)
        self.assertEqual(store.state["capital_flow_seen"], [])
        self.assertEqual(store.state["capital_flow_last_ms"], int(reset_at.timestamp() * 1000))
        self.assertGreater(seen_starts[0], old["time"])
        self.assertEqual(self.flows(), [])
        self.assertFalse(store.state["halted_new"])
        self.assertFalse(store.state["killed"])

    def _manage_withdrawal(self, wallet, amount):
        state = base_state()
        store = Store(state)
        acc = {"totalWalletBalance": str(wallet), "totalUnrealizedProfit": "0",
               "availableBalance": str(wallet), "positions": []}
        self.rows = [transfer(61, amount, start_ms() + 1000)]

        def fake_req(method, path, params=None, signed=False, retries=5):
            self.calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return list(self.rows)
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders", "/fapi/v1/userTrades"):
                return []
            raise AssertionError((method, path, params))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "now_tpe", return_value=NOW), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "load_state", side_effect=store.load), \
                patch.object(lt, "save_state", side_effect=store.save):
            with redirect_stdout(io.StringIO()):
                lt.cmd_manage(type("A", (), {"live": True, "with_status": False})())
        return store

    def test_withdrawal_of_ten_percent_does_not_halt_or_kill(self):
        store = self._manage_withdrawal("9000", "-1000")
        self.assertEqual(store.state["day_start_equity"], 9000.0)
        self.assertEqual(store.state["slice_margin"], 450.0)
        self.assertFalse(store.state["halted_new"])
        self.assertFalse(store.state["killed"])
        kinds = [kind for kind, _kw in self.logs]
        self.assertIn("capital_flow", kinds)
        self.assertNotIn("daily_stop", kinds)
        self.assertNotIn("kill", kinds)
        self.assertFalse(any(c[0] == "POST" for c in self.calls))

    def test_large_withdrawal_does_not_kill(self):
        store = self._manage_withdrawal("6000", "-4000")
        self.assertEqual(store.state["day_start_equity"], 6000.0)
        self.assertFalse(store.state["halted_new"])
        self.assertFalse(store.state["killed"])
        self.assertNotIn("kill", [kind for kind, _kw in self.logs])
        self.assertFalse(any(c[0] == "POST" for c in self.calls))

    def test_open_rebases_before_sizing_and_dry_run_does_not_save(self):
        state = base_state()
        store = Store(state)
        self.rows = [transfer(71, "1000", start_ms() + 1000)]
        acc = {"totalWalletBalance": "11000", "totalUnrealizedProfit": "0",
               "availableBalance": "11000", "positions": []}

        def fake_req(method, path, params=None, signed=False, retries=5):
            self.calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return list(self.rows)
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "100"}
            if path in ("/fapi/v1/order/test", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
                return {} if path == "/fapi/v1/order/test" else []
            raise AssertionError((method, path, params))

        args = type("A", (), {
            "live": False, "symbol": "BTCUSDT", "side": "BUY", "entry_type": "limit",
            "price": "100", "stop": "90", "target": "120", "setup": "布鹿流",
            "target_3r": None, "leverage": None, "portion": 1.0, "ladder": None,
        })()
        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "now_tpe", return_value=NOW), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "day_state", side_effect=lambda _acc: store.load()), \
                patch.object(lt, "load_state", side_effect=store.load), \
                patch.object(lt, "save_state", side_effect=store.save), \
                patch.object(lt, "upcoming_event", return_value=None), \
                patch.object(lt, "sym_info", return_value=info()):
            with redirect_stdout(buf):
                lt.cmd_open(args)
        text = buf.getvalue()
        self.assertIn("資金異動（dry-run，不改 state）", text)
        plan = json.loads(text.split("計畫:", 1)[1].splitlines()[0])
        self.assertEqual(plan["slice_margin"], 550.0)
        paths = [c[1] for c in self.calls]
        self.assertLess(paths.index("/fapi/v1/income"), paths.index("/fapi/v1/order/test"))
        self.assertEqual(store.saves, [])
        self.assertEqual(store.state["day_start_equity"], 10000.0)

    def test_watch_without_orders_does_not_read_income(self):
        state = {"pending": {}}

        def fake_req(method, path, params=None, signed=False, retries=5):
            raise AssertionError((method, path, params))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state"), \
                patch.object(lt, "now_tpe", return_value=NOW), \
                patch.object(lt, "_user_stream_fresh", return_value=False):
            with redirect_stdout(io.StringIO()):
                lt.cmd_watch(type("A", (), {"live": True})())

    def test_watch_with_pending_order_applies_transfer(self):
        state = base_state(pending={"XUSDT": {"orderId": 9, "side": "BUY", "target": "120", "setup": "布鹿流"}})
        store = Store(state)
        self.rows = [transfer(81, "1000", start_ms() + 1000)]
        acc_unused = {}

        def fake_req(method, path, params=None, signed=False, retries=5):
            self.calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/income" and (params or {}).get("incomeType") == "TRANSFER":
                return list(self.rows)
            if path == "/fapi/v1/order":
                return {"orderId": 9, "status": "NEW", "executedQty": "0", "symbol": "XUSDT"}
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "XUSDT", "positionAmt": "0", "markPrice": "100"}]
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
                return []
            raise AssertionError((method, path, params))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: self.logs.append((kind, kw))), \
                patch.object(lt, "now_tpe", return_value=NOW), \
                patch.object(lt, "load_state", side_effect=store.load), \
                patch.object(lt, "save_state", side_effect=store.save), \
                patch.object(lt, "_user_stream_fresh", return_value=True), \
                patch.object(lt, "account", return_value=acc_unused):
            with redirect_stdout(io.StringIO()):
                lt.cmd_watch(type("A", (), {"live": True})())
        self.assertEqual(store.state["day_start_equity"], 11000.0)
        self.assertEqual(store.state["slice_margin"], 550.0)
        self.assertTrue(any(c[1] == "/fapi/v1/income" for c in self.calls))
        self.assertFalse(any(c[1] == "/fapi/v2/account" for c in self.calls))


if __name__ == "__main__":
    unittest.main()
