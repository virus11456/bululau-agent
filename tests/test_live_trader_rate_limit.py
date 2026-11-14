"""O2／O3／O5／O6／O7。不連網、不讀金鑰。"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="live-trader-rate-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402

ACC = {
    "totalWalletBalance": "1000",
    "totalUnrealizedProfit": "0",
    "availableBalance": "1000",
    "positions": [],
}
DAY = {"day": "2026-11-14", "day_start_equity": 1000.0, "slice_margin": 50.0,
       "halted_new": False, "killed": False}


class Headers(object):
    def __init__(self, data):
        self._data = dict(data or {})

    def get(self, key, default=None):
        if key in self._data:
            return self._data[key]
        low = key.lower()
        for name, value in self._data.items():
            if str(name).lower() == low:
                return value
        return default


class Resp(object):
    def __init__(self, body, headers):
        self.headers = headers
        self._body = body if isinstance(body, bytes) else body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _http_error(status, body, headers):
    return urllib.error.HTTPError(
        "https://fapi.binance.com/x", status, "err", Headers(headers),
        io.BytesIO(body if isinstance(body, bytes) else body.encode()))


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rl-")
        lt.WEIGHT_1M_MAX = 0
        self._home = patch.object(lt, "HOME", self.tmp)
        self._home.start()
        self.addCleanup(self._home.stop)

    def test_418_stops_and_shared_file_blocks_the_next_call(self):
        opens = {"n": 0}
        logs = []
        sleeps = []

        def urlopen(_req, timeout=15):
            opens["n"] += 1
            raise _http_error(418, b'{"code":-1003,"msg":"banned"}', {"Retry-After": "30"})

        with patch.object(urllib.request, "urlopen", urlopen), \
                patch.object(lt, "_keys", return_value=("k", "s")), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))), \
                patch.object(lt.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            with self.assertRaises(lt.BinanceError) as caught:
                lt.req("GET", "/fapi/v1/ping", signed=True)
            self.assertEqual(caught.exception.status, 418)
            self.assertEqual(opens["n"], 1)
            self.assertEqual(sleeps, [])
            self.assertTrue(any(kind == "backoff_418" for kind, _kw in logs))
            with open(os.path.join(self.tmp, ".ratelimit_until")) as handle:
                text = handle.read().strip()
            self.assertTrue(text.startswith("418 "))
            with self.assertRaises(lt.BinanceError):
                lt.req("GET", "/fapi/v1/ping", signed=True)
            self.assertEqual(opens["n"], 1)

    def test_missing_or_corrupt_backoff_file_is_ignored(self):
        path = os.path.join(self.tmp, ".ratelimit_until")
        opens = {"n": 0}

        def urlopen(_req, timeout=15):
            opens["n"] += 1
            return Resp(b"{}", Headers({}))

        with patch.object(urllib.request, "urlopen", urlopen), \
                patch.object(lt, "_keys", return_value=("k", "s")), \
                patch.object(lt, "log"):
            lt.req("GET", "/fapi/v1/ping", signed=True)
            with open(path, "w") as handle:
                handle.write("not a backoff file\n")
            lt.req("GET", "/fapi/v1/ping", signed=True)
            os.remove(path)
            lt.req("GET", "/fapi/v1/ping", signed=True)
        self.assertEqual(opens["n"], 3)

    def test_429_still_retries(self):
        clock = {"t": 1000.0}
        sleeps = []
        calls = {"n": 0}

        def urlopen(_req, timeout=15):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(429, b'{"code":-1003,"msg":"too many"}', {"Retry-After": "0"})
            return Resp(b"{}", Headers({}))

        def now():
            return clock["t"]

        def snooze(sec):
            sleeps.append(sec)
            clock["t"] += float(sec)

        with patch.object(urllib.request, "urlopen", urlopen), \
                patch.object(lt, "_keys", return_value=("k", "s")), \
                patch.object(lt, "log"), \
                patch.object(lt.time, "time", now), \
                patch.object(lt.time, "sleep", snooze):
            self.assertEqual(lt.req("GET", "/fapi/v1/ping", signed=True), {})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(sleeps, [2])
        with open(os.path.join(self.tmp, ".ratelimit_until")) as handle:
            self.assertTrue(handle.read().startswith("429 "))

    def test_weight_max_and_high_log(self):
        logs = []
        seen = {"n": 0}

        def urlopen(_req, timeout=15):
            seen["n"] += 1
            weight = "1800" if seen["n"] == 1 else "1801"
            return Resp(b"{}", Headers({"X-MBX-USED-WEIGHT-1M": weight}))

        with patch.object(urllib.request, "urlopen", urlopen), \
                patch.object(lt, "_keys", return_value=("k", "s")), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))):
            lt.req("GET", "/fapi/v1/ping", signed=True)
            self.assertEqual(lt.WEIGHT_1M_MAX, 1800)
            self.assertFalse(any(kind == "weight_high" for kind, _kw in logs))
            lt.req("GET", "/fapi/v1/ping", signed=True)
        self.assertEqual(lt.WEIGHT_1M_MAX, 1801)
        highs = [kw for kind, kw in logs if kind == "weight_high"]
        self.assertEqual(highs, [{"weight": 1801}])

    def test_watch_heartbeat_appends_weight_only_when_seen(self):
        state = {"pending": {}}

        def load():
            return json.loads(json.dumps(state))

        def save(s):
            state.clear()
            state.update(s)

        def run():
            buf = io.StringIO()
            with patch.object(lt, "load_state", side_effect=load), \
                    patch.object(lt, "save_state", side_effect=save), \
                    patch.object(lt, "now_tpe", return_value=datetime(2026, 11, 14, 9, 5, tzinfo=lt.TPE)), \
                    patch.object(lt, "req", side_effect=AssertionError("no api")):
                with redirect_stdout(buf):
                    lt.cmd_watch(type("A", (), {"live": True})())
            return buf.getvalue()

        lt.WEIGHT_1M_MAX = 12
        text = run()
        self.assertIn("[watch] 無待成交限價單 09:05 weight=12", text)
        state["watch_heartbeat"] = "2026-11-14T07:00:00+08:00"
        lt.WEIGHT_1M_MAX = 0
        text = run()
        self.assertIn("[watch] 無待成交限價單 09:05", text)
        self.assertNotIn("weight=", text)


class StatusCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mode-")
        self._home = patch.object(lt, "HOME", self.tmp)
        self._home.start()
        self.addCleanup(self._home.stop)

    def _status_req(self, calls):
        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append(path)
            if path == "/fapi/v1/openOrders":
                return []
            if path == "/fapi/v1/openAlgoOrders":
                return []
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/multiAssetsMargin":
                return {"multiAssetsMargin": False}
            raise AssertionError(path)
        return fake_req

    def test_status_caches_dual_for_an_hour_and_open_still_checks_live(self):
        calls = []
        with patch.object(lt, "req", self._status_req(calls)), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "log"):
            with redirect_stdout(io.StringIO()):
                lt.cmd_status(object())
                lt.cmd_status(object())
        self.assertEqual(calls.count("/fapi/v1/positionSide/dual"), 1)
        self.assertEqual(calls.count("/fapi/v1/multiAssetsMargin"), 1)
        lt.write_account_mode({"dualSidePosition": False}, {"multiAssetsMargin": False})
        calls.clear()

        def open_req(method, path, params=None, signed=False, retries=5):
            calls.append(path)
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": True}
            raise AssertionError(path)

        args = type("A", (), {
            "live": True, "symbol": "XUSDT", "side": "BUY", "entry_type": "market",
            "price": None, "stop": "1", "target": "2", "setup": "瘸師弟",
            "leverage": None, "portion": 1.0,
        })()
        with patch.object(lt, "req", open_req), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "log"):
            with self.assertRaises(SystemExit):
                lt.cmd_open(args)
        self.assertIn("/fapi/v1/positionSide/dual", calls)

    def test_stale_or_corrupt_cache_is_refetched(self):
        path = lt._account_mode_path()
        with open(path, "w") as handle:
            handle.write("{not json")
        calls = []
        with patch.object(lt, "req", self._status_req(calls)), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "log"):
            with redirect_stdout(io.StringIO()):
                lt.cmd_status(object())
        self.assertEqual(calls.count("/fapi/v1/positionSide/dual"), 1)
        with open(path, "w") as handle:
            json.dump({"ts": time.time() - 4000, "dual": {"dualSidePosition": False},
                       "multi": {"multiAssetsMargin": False}}, handle)
        calls.clear()
        with patch.object(lt, "req", self._status_req(calls)), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "log"):
            with redirect_stdout(io.StringIO()):
                lt.cmd_status(object())
        self.assertEqual(calls.count("/fapi/v1/positionSide/dual"), 1)


class ManageStatusAndOrphanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="manage-rl-")
        self.state_path = os.path.join(self.tmp, "state.json")
        lt.WEIGHT_1M_MAX = 0
        self._patches = [
            patch.object(lt, "HOME", self.tmp),
            patch.object(lt, "STATE", self.state_path),
            patch.object(lt, "LOG", os.path.join(self.tmp, "log")),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)

    def test_with_status_reuses_the_account_read(self):
        counts = {"account": 0, "dual": 0}

        def account():
            counts["account"] += 1
            return dict(ACC)

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/positionSide/dual":
                counts["dual"] += 1
                return {"dualSidePosition": False}
            if path == "/fapi/v1/multiAssetsMargin":
                return {"multiAssetsMargin": False}
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
                return []
            raise AssertionError(path)

        def once(flag):
            counts["account"] = 0
            counts["dual"] = 0
            buf = io.StringIO()
            with patch.object(lt, "account", account), \
                    patch.object(lt, "req", fake_req), \
                    patch.object(lt, "day_state", return_value=dict(DAY)), \
                    patch.object(lt, "load_state", return_value={}), \
                    patch.object(lt, "save_state"), \
                    patch.object(lt, "log"):
                with redirect_stdout(buf):
                    lt.cmd_manage(type("A", (), {"live": False, "with_status": flag})())
            return buf.getvalue()

        plain = once(False)
        self.assertEqual(counts["account"], 1)
        self.assertEqual(counts["dual"], 0)
        self.assertNotIn("錢包", plain)
        shown = once(True)
        self.assertEqual(counts["account"], 1)
        self.assertEqual(counts["dual"], 1)
        self.assertIn("錢包", shown)
        self.assertIn("雙向持倉", shown)

    def test_orphan_scan_covers_first_later_and_after_close(self):
        btc = {"symbol": "BTCUSDT", "algoId": 1, "orderType": "STOP_MARKET", "side": "SELL"}
        eth = {"symbol": "ETHUSDT", "orderId": 7, "type": "STOP_MARKET", "side": "SELL"}
        deleted = []

        def write_state(data):
            json.dump(data, open(self.state_path, "w"))

        def drive(acc):
            calls = []

            def fake_req(method, path, params=None, signed=False, retries=5):
                sym = (params or {}).get("symbol")
                calls.append((method, path, sym))
                if path == "/fapi/v1/openAlgoOrders":
                    if sym:
                        return [btc] if sym == "BTCUSDT" else []
                    return [btc]
                if path == "/fapi/v1/openOrders":
                    if sym:
                        return []
                    return [eth]
                if path in ("/fapi/v1/income", "/fapi/v1/userTrades"):
                    return []
                if method == "DELETE":
                    deleted.append((path, params))
                    return {}
                raise AssertionError((method, path, params))

            with patch.object(lt, "req", fake_req), \
                    patch.object(lt, "account", return_value=acc), \
                    patch.object(lt, "day_state", return_value=dict(DAY)), \
                    patch.object(lt, "log"):
                with redirect_stdout(io.StringIO()):
                    lt.cmd_manage(type("A", (), {"live": True, "with_status": False})())
            return calls

        empty = dict(ACC)
        calls = drive(empty)
        self.assertTrue(any(m == "GET" and p == "/fapi/v1/openAlgoOrders" and s is None for m, p, s in calls))
        self.assertTrue(any(p == "/fapi/v1/algoOrder" for _p, p in ((None, d[0]) for d in deleted)))
        self.assertTrue(deleted)
        state = json.load(open(self.state_path))
        self.assertEqual(state["orphan_scan_n"], 1)

        deleted.clear()
        calls = drive(empty)
        self.assertFalse(any(m == "GET" and p == "/fapi/v1/openAlgoOrders" and s is None for m, p, s in calls))
        self.assertEqual(deleted, [])
        self.assertEqual(json.load(open(self.state_path))["orphan_scan_n"], 2)

        write_state({"orphan_scan_n": 4, "pending": {}, "meta": {}})
        deleted.clear()
        calls = drive(empty)
        self.assertTrue(any(s is None and p == "/fapi/v1/openOrders" for _m, p, s in calls))
        self.assertTrue(any(d[0] == "/fapi/v1/order" for d in deleted))

        write_state({"orphan_scan_n": 1, "pending": {}, "meta": {"XUSDT": {"setup": "瘸師弟", "stop": "1", "target": "2"}}})
        deleted.clear()
        calls = drive(empty)
        self.assertTrue(any(s is None and p == "/fapi/v1/openAlgoOrders" for _m, p, s in calls))
        self.assertTrue(deleted)

    def test_partial_scan_still_sees_known_symbols(self):
        acc = dict(ACC)
        acc["positions"] = [{"symbol": "BTCUSDT", "positionAmt": "1", "positionSide": "BOTH"}]
        json.dump({"orphan_scan_n": 1, "pending": {}, "meta": {}}, open(self.state_path, "w"))
        calls = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            sym = (params or {}).get("symbol")
            calls.append((method, path, sym))
            if path == "/fapi/v1/openAlgoOrders" and sym == "BTCUSDT":
                return [
                    {"symbol": "BTCUSDT", "orderType": "STOP_MARKET", "algoId": 1, "quantity": "1",
                     "closePosition": "false", "triggerPrice": "1"},
                    {"symbol": "BTCUSDT", "orderType": "TAKE_PROFIT_MARKET", "algoId": 2, "quantity": "1",
                     "closePosition": "false", "triggerPrice": "2"},
                ]
            if path == "/fapi/v1/openOrders" and sym == "BTCUSDT":
                return []
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders") and sym is None:
                raise AssertionError("這一輪不該全量掃描")
            if method == "POST":
                raise AssertionError("已知幣的保護單都在，不該補掛")
            raise AssertionError((method, path, sym))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "log"):
            with redirect_stdout(io.StringIO()):
                lt.cmd_manage(type("A", (), {"live": True, "with_status": False})())
        self.assertTrue(any(p == "/fapi/v1/openOrders" and s == "BTCUSDT" for _m, p, s in calls))
        self.assertFalse(any(s is None for _m, _p, s in calls))

    def test_day_reset_keeps_orphan_counter(self):
        saved = []
        state = {"day": "2000-01-01", "orphan_scan_n": 3, "pending": {"XUSDT": {"orderId": 1}},
                 "day_start_equity": 10, "slice_margin": 1, "halted_new": True, "killed": True}
        with patch.object(lt, "load_state", return_value=dict(state)), \
                patch.object(lt, "save_state", side_effect=lambda s: saved.append(s)), \
                patch.object(lt, "log"), \
                patch.object(lt, "now_tpe", return_value=datetime(2026, 11, 14, 0, 1, tzinfo=lt.TPE)):
            out = lt.day_state(ACC)
        self.assertEqual(out["orphan_scan_n"], 3)
        self.assertEqual(out["pending"]["XUSDT"]["orderId"], 1)
        self.assertEqual(out["day"], "2026-11-14")
        self.assertFalse(out["killed"])


class ReadFailureAndOrphanTest(unittest.TestCase):
    def test_with_status_algo_failure_is_not_zero(self):
        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/openAlgoOrders":
                raise lt.BinanceError(500, -1000, "algo down")
            if path == "/fapi/v1/openOrders":
                return []
            if path in ("/fapi/v1/income", "/fapi/v1/userTrades"):
                return []
            raise AssertionError(path)

        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "account", return_value=dict(ACC)), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value={}), \
                patch.object(lt, "save_state"), \
                patch.object(lt, "log"):
            with redirect_stdout(buf):
                with self.assertRaises(lt.BinanceError):
                    lt.cmd_manage(type("A", (), {"live": False, "with_status": True})())
        text = buf.getvalue()
        self.assertIn(lt.ALGO_ORDERS_UNREADABLE, text)
        self.assertNotIn("條件單 0", text)

    def test_algo_read_failure_does_not_flatten(self):
        acc = dict(ACC)
        acc["positions"] = [{"symbol": "XUSDT", "positionAmt": "2", "positionSide": "BOTH"}]
        logs = []
        calls = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, (params or {}).get("symbol")))
            if path == "/fapi/v1/openAlgoOrders":
                raise lt.BinanceError(500, -1000, "algo down")
            if path == "/fapi/v1/openOrders":
                return []
            if method == "POST":
                raise AssertionError("讀取失敗不能市價平倉")
            raise AssertionError((method, path))

        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "account", return_value=acc), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value={"pending": {}, "meta": {}}), \
                patch.object(lt, "save_state"), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))):
            with redirect_stdout(buf):
                lt.cmd_manage(type("A", (), {"live": True, "with_status": False})())
        self.assertFalse(any(m == "POST" for m, _p, _s in calls))
        self.assertTrue(any(kw.get("symbol") == "XUSDT" and "跳過" in str(kw.get("msg")) for _k, kw in logs))
        self.assertIn("條件單讀取失敗", buf.getvalue())
        self.assertNotIn("市價平倉", buf.getvalue())

    def test_close_and_reopen_clear_stale_symbol_orders(self):
        state = {"orphan_scan_n": 1, "pending": {}, "meta": {"XUSDT": {"setup": "瘸師弟", "stop": "1", "target": "2"}}}
        deleted = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            sym = (params or {}).get("symbol")
            if path == "/fapi/v1/openAlgoOrders" and sym is None:
                return []
            if path == "/fapi/v1/openAlgoOrders" and sym == "XUSDT":
                return [{"symbol": "XUSDT", "algoId": 9, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL"}]
            if path == "/fapi/v1/openOrders":
                return []
            if path in ("/fapi/v1/income", "/fapi/v1/userTrades"):
                return []
            if method == "DELETE" and path == "/fapi/v1/algoOrder":
                deleted.append(params.get("algoId"))
                return {}
            raise AssertionError((method, path, sym))

        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "account", return_value=dict(ACC)), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=json.loads(json.dumps(state))), \
                patch.object(lt, "save_state"), \
                patch.object(lt, "log"):
            with redirect_stdout(io.StringIO()):
                lt.cmd_manage(type("A", (), {"live": True, "with_status": False})())
        self.assertIn(9, deleted)

        deleted.clear()

        def open_req(method, path, params=None, signed=False, retries=5):
            sym = (params or {}).get("symbol")
            if path == "/fapi/v1/openAlgoOrders":
                return [{"symbol": "XUSDT", "algoId": 9, "orderType": "STOP_MARKET"}]
            if path == "/fapi/v1/openOrders":
                return []
            if method == "DELETE" and path == "/fapi/v1/algoOrder":
                deleted.append(params.get("algoId"))
                return {}
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": True}
            raise AssertionError((method, path, sym))

        args = type("A", (), {
            "live": True, "symbol": "XUSDT", "side": "BUY", "entry_type": "market",
            "price": None, "stop": "1", "target": "2", "setup": "瘸師弟",
            "leverage": None, "portion": 1.0,
        })()
        with patch.object(lt, "req", open_req), \
                patch.object(lt, "account", return_value=dict(ACC)), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value={"pending": {}, "meta": {}}), \
                patch.object(lt, "log"):
            with self.assertRaises(SystemExit):
                lt.cmd_open(args)
        self.assertEqual(deleted, [9])


if __name__ == "__main__":
    unittest.main()
