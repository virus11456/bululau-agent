"""R2 部分成交停損維持計畫總量，O12 逾時撤未成交檔，O8 資料流備援。不連網。"""
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="ladder-risk-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)
import live_trader as lt  # noqa: E402
import user_stream  # noqa: E402

ACC = {
    "totalWalletBalance": "1000", "totalUnrealizedProfit": "0",
    "availableBalance": "1000", "positions": [],
}
DAY = {"day": "2026-11-14", "day_start_equity": 1000.0, "slice_margin": 50.0,
       "halted_new": False, "killed": False}


def _algo(kind, algo_id, qty, trig):
    return {"symbol": "XUSDT", "orderType": kind, "algoId": algo_id,
            "quantity": qty, "closePosition": "false", "triggerPrice": trig}


class PartialStopTest(unittest.TestCase):
    def test_partial_fill_keeps_full_stop_qty_and_reduce_only(self):
        calls = []
        orders = {
            1: {"orderId": 1, "status": "FILLED", "executedQty": "2", "origQty": "2", "time": 1},
            2: {"orderId": 2, "status": "NEW", "executedQty": "0", "origQty": "8", "time": 1},
        }
        protects = [
            _algo("STOP_MARKET", 9, "2", "90"),
            _algo("TAKE_PROFIT_MARKET", 8, "2", "120"),
        ]

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if method == "GET" and path == "/fapi/v1/order":
                return dict(orders[params["orderId"]])
            if path == "/fapi/v1/openAlgoOrders":
                return list(protects)
            if path == "/fapi/v1/openOrders":
                return []
            if method == "POST" and path == "/fapi/v1/algoOrder":
                return {"algoId": 30}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path, params))

        pd = {"ladder": True, "qty": "10", "stop": "90", "target": "120", "setup": "瘸師弟",
              "placed_at": datetime.now(lt.TPE).isoformat(timespec="seconds"),
              "orders": [{"orderId": 1}, {"orderId": 2}]}
        with patch.object(lt, "req", fake_req), patch.object(lt, "log"), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            done = lt.manage_ladder_pending("XUSDT", pd, {"XUSDT": 2.0}, True)
        self.assertFalse(done)
        posts = [c for c in calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
        stops = [c for c in posts if c[2]["type"] == "STOP_MARKET"]
        tps = [c for c in posts if c[2]["type"] == "TAKE_PROFIT_MARKET"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][2]["quantity"], "10")
        self.assertEqual(stops[0][2]["reduceOnly"], "true")
        self.assertEqual(stops[0][2]["side"], "SELL")
        self.assertNotIn("closePosition", stops[0][2])
        self.assertEqual(tps, [])
        buys = [c for c in posts if c[2].get("side") == "BUY"]
        self.assertEqual(buys, [])


class LadderExpireTest(unittest.TestCase):
    def _orders(self, specs):
        return {oid: dict(row, orderId=oid) for oid, row in specs.items()}

    def drive(self, orders, amt, placed_hours, protects):
        calls, logs = [], []

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if method == "GET" and path == "/fapi/v1/order":
                return dict(orders[params["orderId"]])
            if path == "/fapi/v1/openAlgoOrders":
                return list(protects)
            if path == "/fapi/v1/openOrders":
                return []
            if method == "DELETE" and path == "/fapi/v1/order":
                oid = params["orderId"]
                if oid in orders:
                    orders[oid]["status"] = "CANCELED"
                return {"orderId": oid, "status": "CANCELED", "executedQty": orders.get(oid, {}).get("executedQty", "0")}
            if method == "DELETE":
                return {}
            if method == "POST":
                raise AssertionError("不該送出新單或平倉")
            raise AssertionError((method, path))

        placed = (datetime.now(lt.TPE) - timedelta(hours=placed_hours)).isoformat(timespec="seconds")
        pd = {"ladder": True, "qty": "10", "stop": "90", "target": "120",
              "placed_at": placed, "orders": [{"orderId": i} for i in orders]}
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))):
            done = lt.manage_ladder_pending("XUSDT", pd, {"XUSDT": amt} if amt else {}, True)
        return done, calls, logs

    def test_zero_fills_cancels_entries_and_stop(self):
        orders = self._orders({
            1: {"status": "NEW", "executedQty": "0", "origQty": "10"},
            2: {"status": "NEW", "executedQty": "0", "origQty": "10"},
        })
        done, calls, logs = self.drive(orders, 0, 3, [_algo("STOP_MARKET", 9, "10", "90")])
        self.assertEqual(done, "drop")
        deleted = [c[2].get("orderId") for c in calls if c[0] == "DELETE" and c[1] == "/fapi/v1/order"]
        self.assertEqual(sorted(deleted), [1, 2])
        self.assertTrue(any(c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" for c in calls))
        self.assertTrue(any(k == "ladder_expire" and kw.get("cancel") == "stop" for k, kw in logs))
        self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_zero_fills_also_cancels_take_profit(self):
        orders = self._orders({
            1: {"status": "NEW", "executedQty": "0", "origQty": "10"},
        })
        protects = [
            _algo("STOP_MARKET", 9, "10", "90"),
            _algo("TAKE_PROFIT_MARKET", 8, "10", "120"),
        ]
        done, calls, logs = self.drive(orders, 0, 3, protects)
        self.assertEqual(done, "drop")
        algo_ids = [c[2].get("algoId") for c in calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"]
        self.assertEqual(sorted(algo_ids), [8, 9])
        self.assertTrue(any(kw.get("cancel") == "tp" for _k, kw in logs))
        self.assertTrue(any(kw.get("cancel") == "stop" for _k, kw in logs))

    def test_partial_fill_cancels_only_open_entries(self):
        orders = self._orders({
            1: {"status": "FILLED", "executedQty": "4", "origQty": "4"},
            2: {"status": "NEW", "executedQty": "0", "origQty": "6"},
        })
        protects = [_algo("STOP_MARKET", 9, "10", "90"), _algo("TAKE_PROFIT_MARKET", 8, "4", "120")]
        done, calls, logs = self.drive(orders, 4, 3, protects)
        self.assertFalse(done)
        deleted = [c[2].get("orderId") for c in calls if c[0] == "DELETE" and c[1] == "/fapi/v1/order"]
        self.assertEqual(deleted, [2])
        self.assertFalse(any(c[1] == "/fapi/v1/algoOrder" and c[0] == "DELETE" for c in calls))
        self.assertTrue(any(k == "ladder_expire" for k, _kw in logs))

    def test_full_fill_before_two_hours_is_kept(self):
        orders = self._orders({
            1: {"status": "FILLED", "executedQty": "10", "origQty": "10"},
        })
        done, calls, logs = self.drive(orders, 10, 0.5, [_algo("STOP_MARKET", 9, "10", "90")])
        self.assertEqual(done, "meta")
        self.assertFalse(any(k == "ladder_expire" for k, _kw in logs))
        self.assertFalse(any(c[0] == "DELETE" and c[1] == "/fapi/v1/order" for c in calls))

    def test_single_order_older_than_two_hours_is_not_cancelled(self):
        calls = []
        old_ms = int((time.time() - 5 * 3600) * 1000)

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if method == "GET" and path == "/fapi/v1/order":
                return {"orderId": 5, "status": "NEW", "executedQty": "0", "origQty": "1", "time": old_ms}
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return []
            if method == "DELETE":
                raise AssertionError("單張不該被逾時撤掉")
            raise AssertionError((method, path))

        state = {"pending": {"XUSDT": {"orderId": 5, "stop": "1", "target": "2", "setup": "瘸師弟"}}, "meta": {}}
        with patch.object(lt, "req", fake_req), patch.object(lt, "log"), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state"):
            lt.cmd_manage(type("A", (), {"live": True})())
        self.assertTrue(any(c[1] == "/fapi/v1/order" and c[0] == "GET" for c in calls))
        self.assertFalse(any(c[0] == "DELETE" for c in calls))

    def test_cancel_failure_leaves_stop(self):
        orders = self._orders({
            1: {"status": "NEW", "executedQty": "0", "origQty": "10"},
            2: {"status": "NEW", "executedQty": "0", "origQty": "10"},
        })
        calls, logs = [], []

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if method == "GET" and path == "/fapi/v1/order":
                return dict(orders[params["orderId"]])
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return [_algo("STOP_MARKET", 9, "10", "90")] if "Algo" in path else []
            if method == "DELETE" and path == "/fapi/v1/order":
                raise lt.BinanceError(400, -1000, "cancel rejected")
            if method == "DELETE":
                raise AssertionError("撤進場單失敗時不能撤停損")
            raise AssertionError((method, path))

        placed = (datetime.now(lt.TPE) - timedelta(hours=3)).isoformat(timespec="seconds")
        pd = {"ladder": True, "qty": "10", "stop": "90", "target": "120",
              "placed_at": placed, "orders": [{"orderId": 1}, {"orderId": 2}]}
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))):
            done = lt.manage_ladder_pending("XUSDT", pd, {}, True)
        self.assertNotEqual(done, "drop")
        self.assertFalse(any(c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" for c in calls))
        self.assertFalse(any(kw.get("cancel") == "stop" for _k, kw in logs))

    def test_fill_seen_on_requery_leaves_stop(self):
        orders = self._orders({
            1: {"status": "NEW", "executedQty": "0", "origQty": "10"},
        })
        calls, logs = [], []
        cancelled = set()

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if method == "GET" and path == "/fapi/v1/order":
                row = dict(orders[params["orderId"]])
                if params["orderId"] in cancelled:
                    row["status"] = "FILLED"
                    row["executedQty"] = "3"
                return row
            if path in ("/fapi/v1/openAlgoOrders", "/fapi/v1/openOrders"):
                return [_algo("STOP_MARKET", 9, "10", "90")] if "Algo" in path else []
            if method == "DELETE" and path == "/fapi/v1/order":
                cancelled.add(params["orderId"])
                return {"orderId": params["orderId"], "status": "CANCELED", "executedQty": "0"}
            if method == "DELETE":
                raise AssertionError("再查有成交時不能撤停損")
            raise AssertionError((method, path))

        placed = (datetime.now(lt.TPE) - timedelta(hours=3)).isoformat(timespec="seconds")
        pd = {"ladder": True, "qty": "10", "stop": "90", "target": "120",
              "placed_at": placed, "orders": [{"orderId": 1}]}
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))):
            done = lt.manage_ladder_pending("XUSDT", pd, {}, True)
        self.assertNotEqual(done, "drop")
        self.assertFalse(any(c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" for c in calls))
        self.assertFalse(any(kw.get("cancel") == "stop" for _k, kw in logs))
        self.assertTrue(cancelled)


class UserStreamTest(unittest.TestCase):
    def setUp(self):
        path = lt._ratelimit_path()
        if os.path.exists(path):
            os.remove(path)

    def test_fill_event_triggers_manage_and_reconnects(self):
        managed = []
        connects = {"n": 0}

        class Sock(object):
            def __init__(self, messages, fail=False):
                self._messages = messages
                self.fail = fail

            def messages(self):
                if self.fail:
                    raise ConnectionError("drop")
                for msg in self._messages:
                    yield msg

        def connect():
            connects["n"] += 1
            if connects["n"] == 1:
                return Sock([], fail=True)
            return Sock([{"e": "ORDER_TRADE_UPDATE", "o": {"s": "XUSDT", "x": "TRADE", "X": "FILLED"}}])

        stopped = {"n": 0}

        def stop():
            return stopped["n"] >= 1

        def on_manage(msg):
            managed.append(msg)
            stopped["n"] += 1

        user_stream.run_loop(connect, stop, on_manage, lambda _s: None, lambda: None, lambda: None)
        self.assertEqual(connects["n"], 2)
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["o"]["X"], "FILLED")

    def test_polling_fallback_when_heartbeat_stale(self):
        path = user_stream.heartbeat_path()
        calls = []

        def fake_req(*_a, **_k):
            calls.append(1)
            raise AssertionError("心跳還新時不該輪詢")

        with open(path, "w") as handle:
            handle.write(str(time.time()))
        with patch.object(lt, "req", fake_req):
            lt.cmd_watch(type("A", (), {"live": True})())
        self.assertEqual(calls, [])

        with open(path, "w") as handle:
            handle.write(str(time.time() - 1000))
        state = {"pending": {}}

        def load():
            return json.loads(json.dumps(state))

        def save(s):
            state.clear()
            state.update(s)

        def fake_req_poll(*_a, **_k):
            calls.append("polled")
            raise AssertionError("空 pending 的備援不該打 API")

        calls.clear()
        with patch.object(lt, "req", fake_req_poll), \
                patch.object(lt, "load_state", side_effect=load), \
                patch.object(lt, "save_state", side_effect=save), \
                patch.object(lt, "now_tpe", return_value=datetime(2026, 11, 14, 1, 2, tzinfo=lt.TPE)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                lt.cmd_watch(type("A", (), {"live": True})())
        self.assertIn("[watch] 無待成交限價單 01:02", buf.getvalue())
        self.assertEqual(calls, [])

    def test_idle_ticks_do_not_reconnect(self):
        connects = {"n": 0}
        ticks = {"n": 0}

        class Sock(object):
            def messages(self):
                while ticks["n"] < 3:
                    ticks["n"] += 1
                    yield None

        def connect():
            connects["n"] += 1
            return Sock()

        def stop():
            return ticks["n"] >= 3

        sleeps = []
        user_stream.run_loop(connect, stop, lambda _m: None, sleeps.append, lambda: None, lambda: None)
        self.assertEqual(connects["n"], 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(ticks["n"], 3)

    def test_recv_timeout_sends_masked_ping_and_stays_up(self):
        class Sock(object):
            def __init__(self):
                self.timeouts = 2
                self.sent = []
                self.buf = bytes([0x89, 4]) + b"ping"

            def settimeout(self, _t):
                return None

            def recv(self, n):
                if self.timeouts:
                    self.timeouts -= 1
                    raise socket.timeout()
                if not self.buf:
                    raise ConnectionError("closed")
                out, self.buf = self.buf[:n], self.buf[n:]
                return out

            def sendall(self, data):
                self.sent.append(data)

        sock = Sock()
        seen = []
        try:
            for msg in user_stream.iter_ws_messages(sock):
                seen.append(msg)
        except ConnectionError:
            pass
        self.assertEqual(seen[:2], [None, None])
        self.assertGreaterEqual(len(sock.sent), 3)
        for frame in sock.sent[:2]:
            self.assertEqual(frame[0], 0x89)
            self.assertTrue(frame[1] & 0x80)
        pong = sock.sent[2]
        self.assertEqual(pong[0], 0x8A)
        self.assertEqual(pong[1] & 0x80, 0x80)
        key = pong[2:6]
        plain = bytes(pong[6 + i] ^ key[i % 4] for i in range(4))
        self.assertEqual(plain, b"ping")

    def test_reconnect_backoff_and_stop_on_418(self):
        sleeps = []
        logs = []
        n = {"n": 0}

        def flappy():
            n["n"] += 1
            if n["n"] < 4:
                raise ConnectionError("listenKey SECRET")

            class Sock(object):
                def messages(self):
                    yield {"e": "noop"}
            return Sock()

        with patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))):
            user_stream.run_loop(flappy, lambda: n["n"] >= 4, lambda _m: None, sleeps.append, lambda: None, lambda: None)
        self.assertEqual(sleeps, [1, 2, 4])
        self.assertTrue(all(kw.get("err") == "ConnectionError" for kind, kw in logs if kind == "user_stream_reconnect"))
        self.assertNotIn("SECRET", json.dumps(logs))

        sleeps.clear()
        logs.clear()

        def limited():
            raise user_stream.StreamRateLimit(418, 30)

        with patch.object(lt, "log", side_effect=lambda kind, **kw: logs.append((kind, kw))):
            with self.assertRaises(SystemExit) as caught:
                user_stream.run_loop(limited, lambda: False, lambda _m: None, sleeps.append, lambda: None, lambda: None)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(sleeps, [30])
        self.assertEqual(logs, [("user_stream_stop", {"status": 418, "wait": 30})])

        sleeps.clear()
        path = lt._ratelimit_path()
        if os.path.exists(path):
            os.remove(path)
        hit = {"n": 0}

        def once_429():
            hit["n"] += 1
            if hit["n"] == 1:
                raise user_stream.StreamRateLimit(429, 9)

            class Sock(object):
                def messages(self):
                    yield {"e": "noop"}
            return Sock()

        with patch.object(lt, "log"):
            user_stream.run_loop(once_429, lambda: hit["n"] >= 2, lambda _m: None, sleeps.append, lambda: None, lambda: None)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 9, delta=0.05)

    def test_fill_burst_is_one_manage(self):
        clock = {"t": 1000.0}
        managed = []

        class Sock(object):
            def messages(self):
                yield {"e": "ORDER_TRADE_UPDATE", "o": {"x": "TRADE", "X": "FILLED"}}
                clock["t"] = 1001.0
                yield {"e": "ORDER_TRADE_UPDATE", "o": {"x": "TRADE", "X": "PARTIALLY_FILLED"}}
                clock["t"] = 1003.0
                yield None

        user_stream.run_loop(
            lambda: Sock(), lambda: len(managed) >= 1, managed.append,
            lambda _s: None, lambda: None, lambda: None, now=lambda: clock["t"])
        self.assertEqual(len(managed), 1)

    def test_lock_busy_does_not_exit_and_retries_without_reconnect(self):
        held = lt.Lock()
        held.__enter__()
        try:
            with patch.object(lt, "cmd_manage", side_effect=AssertionError("鎖還在不該跑 manage")):
                with self.assertRaises(user_stream.LockBusy):
                    user_stream.manage_under_lock(True)
        finally:
            held.__exit__(None, None, None)
        calls = []
        with patch.object(lt, "cmd_manage", side_effect=lambda a: calls.append(a.live)):
            user_stream.manage_under_lock(False)
        self.assertEqual(calls, [False])

        lt.save_state({"pending": {}, "meta": {}})
        held = lt.Lock()
        held.__enter__()
        try:
            st = lt.load_state()
            st["pending"] = {"XUSDT": {"ladder": True, "stop": "1", "orders": [{"orderId": 1}]}}
            lt.save_state(st)
            with self.assertRaises(user_stream.LockBusy):
                user_stream.manage_under_lock(True)
            self.assertIn("XUSDT", lt.load_state()["pending"])
        finally:
            held.__exit__(None, None, None)

        connects = {"n": 0}
        clock = {"t": 500.0}
        released = {"ok": False}
        managed = []

        class Sock(object):
            def messages(self):
                yield {"e": "ACCOUNT_UPDATE"}
                clock["t"] = 503.0
                yield None
                released["ok"] = True
                clock["t"] = 505.0
                yield None

        def connect():
            connects["n"] += 1
            return Sock()

        def on_manage(_msg):
            if not released["ok"]:
                raise user_stream.LockBusy()
            managed.append(1)

        user_stream.run_loop(connect, lambda: len(managed) >= 1, on_manage, lambda _s: None, lambda: None, lambda: None, now=lambda: clock["t"])
        self.assertEqual(connects["n"], 1)
        self.assertEqual(managed, [1])

    def test_watch_runs_when_user_stream_module_is_missing(self):
        import builtins
        real_import = builtins.__import__

        def guarded(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "user_stream":
                raise ImportError("absent")
            return real_import(name, globals, locals, fromlist, level)

        calls = []
        state = {"pending": {"XUSDT": {"orderId": 5, "stop": "1", "target": "2", "setup": "瘸師弟"}}}

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path))
            if method == "GET" and path == "/fapi/v1/order":
                return {"orderId": 5, "status": "NEW", "executedQty": "0", "origQty": "1"}
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "XUSDT", "positionAmt": "0", "markPrice": "1.5"}]
            raise AssertionError((method, path))

        with patch("builtins.__import__", guarded), \
                patch.object(lt, "req", fake_req), \
                patch.object(lt, "log"), \
                patch.object(lt, "load_state", return_value=state), \
                patch.object(lt, "save_state"):
            lt.cmd_watch(type("A", (), {"live": True})())
        self.assertIn(("GET", "/fapi/v1/order"), calls)
        self.assertIn(("GET", "/fapi/v2/positionRisk"), calls)

    def test_idle_timeout_does_not_refresh_heartbeat(self):
        touches = {"n": 0}
        connects = {"n": 0}

        class Sock(object):
            def messages(self):
                yield None
                yield ("frame",)
                yield {"e": "noop"}

        def connect():
            connects["n"] += 1
            return Sock()

        user_stream.run_loop(
            connect, lambda: connects["n"] >= 1 and touches["n"] >= 3,
            lambda _m: None, lambda _s: None, lambda: touches.__setitem__("n", touches["n"] + 1),
            lambda: None)
        # 連上一次，加上收到 pong／資料各一次。閒置逾時那次不加。
        self.assertEqual(touches["n"], 3)
        self.assertEqual(connects["n"], 1)

    def test_shared_418_sleeps_without_connecting(self):
        start = 1000000.0
        lt.write_ratelimit(418, start + 80)
        connects = {"n": 0}
        sleeps = []
        clock = {"t": start}

        def connect():
            connects["n"] += 1
            raise AssertionError("418 期間不能連線")

        def snooze(sec):
            sleeps.append(sec)
            clock["t"] += float(sec)

        with patch.object(user_stream.time, "time", lambda: clock["t"]), \
                patch.object(lt, "log"):
            with self.assertRaises(SystemExit) as caught:
                user_stream.run_loop(connect, lambda: False, lambda _m: None, snooze, lambda: None, lambda: None)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(connects["n"], 0)
        self.assertTrue(sleeps and sleeps[0] > 70)

    def test_418_from_connect_writes_backoff_and_stops(self):
        connects = {"n": 0}
        sleeps = []

        def connect():
            connects["n"] += 1
            raise user_stream.StreamRateLimit(418, 30)

        with patch.object(lt, "log"):
            with self.assertRaises(SystemExit) as caught:
                user_stream.run_loop(connect, lambda: False, lambda _m: None, sleeps.append, lambda: None, lambda: None)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(connects["n"], 1)
        self.assertEqual(sleeps, [30])
        status, _until = lt.read_ratelimit()
        self.assertEqual(status, 418)

    def test_manage_failure_retries_with_backoff_then_stops(self):
        clock = {"t": 1000.0}
        calls = {"n": 0}
        connects = {"n": 0}

        class Sock(object):
            def messages(self):
                yield {"e": "ACCOUNT_UPDATE"}
                clock["t"] = 1004.0
                yield None
                clock["t"] = 1008.0
                yield None
                clock["t"] = 1020.0
                yield None

        def connect():
            connects["n"] += 1
            return Sock()

        def on_manage(_msg):
            calls["n"] += 1
            raise RuntimeError("manage down")

        user_stream.run_loop(
            connect, lambda: calls["n"] >= user_stream.MANAGE_RETRY_MAX,
            on_manage, lambda _s: None, lambda: None, lambda: None, now=lambda: clock["t"])
        self.assertEqual(calls["n"], user_stream.MANAGE_RETRY_MAX)
        self.assertEqual(connects["n"], 1)

    def test_one_fill_does_not_repeat_but_expiry_does(self):
        both = [
            {"symbol": "XUSDT", "orderType": "STOP_MARKET", "algoId": 9, "quantity": "1",
             "closePosition": "false", "triggerPrice": "1"},
            {"symbol": "XUSDT", "orderType": "TAKE_PROFIT_MARKET", "algoId": 8, "quantity": "1",
             "closePosition": "false", "triggerPrice": "2"},
        ]
        orders = {
            1: {"orderId": 1, "status": "FILLED", "executedQty": "2"},
            2: {"orderId": 2, "status": "NEW", "executedQty": "0"},
        }
        store = {"pending": {"XUSDT": {
            "ladder": True,
            "orders": [{"orderId": 1}, {"orderId": 2}],
            "placed_at": datetime.now(lt.TPE).isoformat(timespec="seconds"),
        }}}
        managed = []

        def fake_req(method, path, params=None, signed=False, retries=5):
            if path == "/fapi/v1/openAlgoOrders":
                return list(both)
            if path == "/fapi/v1/openOrders":
                return []
            if method == "GET" and path == "/fapi/v1/order":
                return dict(orders[params["orderId"]])
            raise AssertionError((method, path))

        def load():
            return json.loads(json.dumps(store))

        def save(s):
            store.clear()
            store.update(s)

        def watch():
            with patch.object(lt, "req", fake_req), \
                    patch.object(lt, "log"), \
                    patch.object(lt, "_user_stream_fresh", return_value=False), \
                    patch.object(lt, "load_state", side_effect=load), \
                    patch.object(lt, "save_state", side_effect=save), \
                    patch.object(lt, "cmd_manage", side_effect=lambda a: managed.append(1)):
                lt.cmd_watch(type("A", (), {"live": True})())

        watch()
        watch()
        self.assertEqual(managed, [1])
        store["pending"]["XUSDT"]["placed_at"] = (
            datetime.now(lt.TPE) - timedelta(hours=3)).isoformat(timespec="seconds")
        watch()
        self.assertEqual(managed, [1, 1])


if __name__ == "__main__":
    unittest.main()
