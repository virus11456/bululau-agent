"""小師弟收盤觸發。不連網、不讀金鑰。"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix="xiao-close-")
os.environ["QUANTBOY_DATA_DIR"] = _DATA
os.environ.pop("BINANCE_ENV_FILE", None)
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

_spec = importlib.util.spec_from_file_location("xiao_close_under_test", os.path.join(SRC, "xiao_close.py"))
xiao_close = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xiao_close)

REAL = xiao_close.load_module(os.path.join(SRC, "prefilter.py"), "xiao_close_real_prefilter")

if "live_trader" in sys.modules:
    lt = sys.modules["live_trader"]
else:
    import live_trader as lt  # noqa: E402

TPE = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 14, 12, 15, 5, tzinfo=TPE)


class Clock(object):
    def __init__(self, when):
        self.when = when

    def __call__(self):
        return self.when

    def sleep(self, seconds):
        self.when = self.when + timedelta(seconds=seconds)


def info(tick="0.01"):
    return dict(tick=Decimal(tick), step=Decimal("0.001"), mstep=Decimal("0.001"),
                minqty=Decimal("0.001"), maxmqty=Decimal("1000000"), minnot=Decimal("5"),
                status="TRADING", ctype="PERPETUAL")


ACC = {
    "totalWalletBalance": "10000", "totalUnrealizedProfit": "0",
    "availableBalance": "10000", "positions": [],
}
DAY = {"day": "2026-11-14", "day_start_equity": 10000.0, "slice_margin": 500.0,
       "halted_new": False, "killed": False}


class Trader(object):
    def __init__(self, lock):
        self.LOCK = lock
        self.CFG = {"xiao_slippage_pct": 0.003}
        self.calls = []
        self.BinanceError = type("BinanceError", (Exception,), {})
        self._open = None

    def resolve_entry_type(self, setup, requested):
        if setup == "小師弟":
            return "ioc"
        return requested

    def cmd_open(self, args):
        self.calls.append(args)
        if self._open:
            return self._open(args)
        print("已進場並掛好保護單")


class CloseTriggerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="xiao-job-")
        self.seen = os.path.join(self.dir, "signal_seen.jsonl")
        self.log = os.path.join(self.dir, "xiao_close_log.jsonl")
        self.lock = os.path.join(self.dir, ".lock")
        self.state = os.path.join(self.dir, "state.json")
        with open(self.state, "w") as handle:
            handle.write("STAY")
        self.open_ms = xiao_close.expected_open_ms(xiao_close.boundary_s(NOW))
        self.clock = Clock(NOW)
        self.trader = Trader(self.lock)
        self.mod = self._mod()

    def _mod(self):
        mod = type("Mod", (), {})()
        mod.filter_new_signals = REAL.filter_new_signals
        mod.signal_id = REAL.signal_id
        mod.default_seen_path = lambda: self.seen
        mod.xiao_shidi = lambda bars, symbol="ETHUSDT": (None, "no")
        return mod

    def _hit(self, symbol="ETHUSDT", low_t=36, bar_open=None):
        return {
            "setup": "小師弟", "symbol": symbol, "entry": 100, "stop": 90, "target": 120,
            "low_t": low_t, "bar_open": self.open_ms if bar_open is None else bar_open,
        }

    def _screen(self, hits, guard=None, btc_open=None, universe=1):
        def screen(_mod):
            return {
                "universe": universe, "hits": hits, "n_miss": 0,
                "guard": {"blocked": False} if guard is None else guard,
                "btc_open": self.open_ms if btc_open is None else btc_open,
                "bars": {},
            }
        return screen

    def _job(self, dry_run=False, hits=None, screen=None, grace=0, lock_wait=1):
        if hits is None:
            hits = [self._hit()]
        job = xiao_close.Job(
            self.mod, "/Users/frank/quantboy-paper/discretionary/prefilter.py",
            trader=None if dry_run else self.trader, seen_path=self.seen, log_path=self.log,
            dry_run=dry_run, grace_s=grace, lock_wait_s=lock_wait,
            screen=screen or self._screen(hits),
        )
        return job

    def _run(self, job):
        return xiao_close.run_job(job, self.clock, self.clock.sleep)

    def _files(self):
        return sorted(os.listdir(self.dir))

    def test_grace_waits_a_few_seconds_after_the_close(self):
        start = datetime(2026, 11, 14, 12, 15, 1, tzinfo=TPE)
        clock = Clock(start)
        close_s, trigger, delay = xiao_close.wait_until_grace(clock, 5, clock.sleep)
        self.assertEqual(datetime.fromtimestamp(close_s, TPE).minute, 15)
        self.assertEqual(datetime.fromtimestamp(close_s, TPE).second, 0)
        self.assertEqual(trigger.second, 5)
        self.assertEqual(delay, 5.0)

    def test_dry_run_writes_no_state_and_does_not_consume_the_signal(self):
        job = self._job(dry_run=True)
        first = self._run(job)
        second = self._run(job)
        self.assertEqual(first["result"], "dry_run")
        self.assertEqual(first["actions"][0]["kind"], "would_order")
        self.assertEqual(second["actions"][0]["kind"], "would_order")
        self.assertEqual(first["bar_close_tpe"], "2026-11-14T12:15:00+08:00")
        self.assertEqual(first["delay_s"], 5.0)
        self.assertEqual(first["screen"]["hits"][0]["symbol"], "ETHUSDT")
        self.assertEqual(open(self.state).read(), "STAY")
        self.assertEqual(self._files(), ["state.json"])
        self.assertEqual(self.trader.calls, [])

    def test_duplicate_signal_is_ordered_once(self):
        job = self._job(dry_run=False)
        first = self._run(job)
        second = self._run(job)
        self.assertEqual(first["result"], "ordered")
        self.assertEqual(first["actions"][0]["signal_id"], "xiao|ETHUSDT|36")
        self.assertEqual(len(self.trader.calls), 1)
        order = self.trader.calls[0]
        self.assertEqual(order.setup, "小師弟")
        self.assertEqual(order.side, "BUY")
        self.assertEqual(order.entry_type, "limit")
        self.assertNotEqual(order.entry_type, "market")
        self.assertEqual(order.portion, 1.0)
        self.assertEqual(second["actions"][0]["kind"], "duplicate")
        self.assertEqual(len(self.trader.calls), 1)
        self.assertEqual(open(self.seen).read(), "xiao|ETHUSDT|36\n")
        self.assertEqual(open(self.state).read(), "STAY")
        logged = [json.loads(line) for line in open(self.log)]
        self.assertEqual(logged[0]["result"], "ordered")
        self.assertEqual(logged[1]["actions"][0]["kind"], "duplicate")

    def test_lock_busy_does_not_order_or_write_seen(self):
        script = "import fcntl,sys,time\nf=open(sys.argv[1],'a')\nfcntl.flock(f,fcntl.LOCK_EX)\nsys.stdout.write('held\\n')\nsys.stdout.flush()\ntime.sleep(30)\n"
        proc = subprocess.Popen([sys.executable, "-c", script, self.lock], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline(), "held\n")
            record = self._run(self._job(dry_run=False, lock_wait=0.2))
        finally:
            proc.kill()
            proc.wait(timeout=5)
        self.assertEqual(record["result"], "lock_busy")
        self.assertEqual(self.trader.calls, [])
        self.assertFalse(os.path.exists(self.seen))
        self.assertEqual(open(self.state).read(), "STAY")
        self.assertIn("lock_busy", open(self.log).read())

    def test_same_lock_file_blocks_live_trader(self):
        self.assertEqual(os.path.basename(lt.LOCK), ".lock")
        release = threading.Event()
        held = threading.Event()
        errors = []

        def holder():
            try:
                with xiao_close.trader_lock(lt.LOCK, 2, __import__("time").sleep):
                    held.set()
                    release.wait(5)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(2), errors)
        try:
            child = (
                "import os,sys\n"
                "os.environ['QUANTBOY_DATA_DIR']=sys.argv[1]\n"
                "sys.path.insert(0, sys.argv[2])\n"
                "import live_trader as trader\n"
                "try:\n"
                "    with trader.Lock():\n"
                "        sys.stdout.write('ACQUIRED\\n')\n"
                "except SystemExit:\n"
                "    sys.stdout.write('BUSY\\n')\n"
            )
            out = subprocess.check_output(
                [sys.executable, "-c", child, os.path.dirname(lt.LOCK), SRC], text=True,
            )
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(out.strip(), "BUSY")

    def test_btc_guard_and_stale_bar_and_missing_id_do_not_order(self):
        blocked = self._run(self._job(screen=self._screen(
            [self._hit()], guard={"blocked": True, "why": "BTC broke"})))
        self.assertEqual(blocked["result"], "btc_guard")
        stale = self._run(self._job(screen=self._screen([self._hit()], btc_open=self.open_ms - xiao_close.INTERVAL_MS)))
        self.assertEqual(stale["result"], "bar_not_ready")
        nameless = dict(self._hit(), low_t=None)
        missing = self._run(self._job(hits=[nameless]))
        self.assertEqual(missing["actions"][0]["kind"], "no_signal_id")
        self.assertEqual(self.trader.calls, [])
        self.assertFalse(os.path.exists(self.seen))
        self.assertEqual(open(self.state).read(), "STAY")

    def test_network_and_exchange_errors_do_not_retry_as_market(self):
        def boom(args):
            self.assertNotEqual(args.entry_type, "market")
            self.assertEqual(args.setup, "小師弟")
            raise urllib.error.URLError("timed out")

        self.trader._open = boom
        record = self._run(self._job(hits=[self._hit("ETHUSDT", 36), self._hit("SOLUSDT", 37)]))
        self.assertEqual(record["result"], "network_error")
        self.assertEqual(len(self.trader.calls), 1)
        self.assertEqual(self.trader.calls[0].symbol, "ETHUSDT")
        self.assertEqual(open(self.seen).read(), "xiao|ETHUSDT|36\n")
        self.assertNotIn("SOLUSDT", open(self.seen).read())

        self.trader.calls.clear()
        def reject(args):
            raise self.trader.BinanceError("HTTP 400 code -2010: rejected")

        self.trader._open = reject
        other = self.seen + ".other"
        job = self._job(hits=[self._hit("BTCUSDT", 99)])
        job.seen_path = other
        record = self._run(job)
        self.assertEqual(record["actions"][0]["kind"], "exchange_error")
        self.assertEqual(len(self.trader.calls), 1)
        self.assertNotEqual(self.trader.calls[0].entry_type, "market")

        self.trader.calls.clear()
        def halted(args):
            raise SystemExit("當日虧損 7.10% ≥ 7%，今日停開新倉")

        self.trader._open = halted
        job = self._job(hits=[self._hit("XRPUSDT", 7), self._hit("SOLUSDT", 8)])
        job.seen_path = self.seen + ".risk"
        record = self._run(job)
        self.assertEqual(record["result"], "risk_blocked")
        self.assertEqual(len(self.trader.calls), 1)

    def test_symbol_block_continues_and_account_block_stops(self):
        def already(args):
            if args.symbol == "ETHUSDT":
                raise SystemExit("ETHUSDT 已有倉位（單幣最多 1 份）")
            print("已進場並掛好保護單")

        self.trader._open = already
        record = self._run(self._job(hits=[self._hit("ETHUSDT", 36), self._hit("SOLUSDT", 37)]))
        self.assertEqual([item["kind"] for item in record["actions"]], ["symbol_blocked", "ordered"])
        self.assertEqual([call.symbol for call in self.trader.calls], ["ETHUSDT", "SOLUSDT"])
        self.assertNotEqual(self.trader.calls[1].entry_type, "market")

        self.trader.calls.clear()
        def too_small(args):
            if args.symbol == "ETHUSDT":
                raise SystemExit("數量 0.001 低於最小量/最小名目 5（停損太遠或份額太小）")
            print("小師弟預估風險超過 1%，放棄不下單")

        self.trader._open = too_small
        job = self._job(hits=[self._hit("ETHUSDT", 41), self._hit("SOLUSDT", 42)])
        job.seen_path = self.seen + ".qty"
        record = self._run(job)
        self.assertEqual([item["kind"] for item in record["actions"]], ["symbol_blocked", "risk_over_1pct"])
        self.assertEqual(len(self.trader.calls), 2)

        self.trader.calls.clear()
        def full(args):
            raise SystemExit("已達同時 5 倉上限")

        self.trader._open = full
        job = self._job(hits=[self._hit("BTCUSDT", 51), self._hit("SOLUSDT", 52)])
        job.seen_path = self.seen + ".full"
        record = self._run(job)
        self.assertEqual(record["actions"][0]["kind"], "risk_blocked")
        self.assertEqual(len(self.trader.calls), 1)

        self.trader.calls.clear()
        def locked(args):
            raise SystemExit("另一個 live_trader 正在執行（檔案鎖），稍後再試")

        self.trader._open = locked
        job = self._job(hits=[self._hit("XRPUSDT", 61), self._hit("SOLUSDT", 62)])
        job.seen_path = self.seen + ".lockmsg"
        record = self._run(job)
        self.assertEqual(record["result"], "risk_blocked")
        self.assertEqual(len(self.trader.calls), 1)

    def test_stop_place_failure_is_not_risk_blocked_and_stops_the_round(self):
        def failed(args):
            raise SystemExit("停損失敗已平倉: HTTP 400 code -2021: stop rejected")

        self.trader._open = failed
        record = self._run(self._job(hits=[self._hit("ETHUSDT", 71), self._hit("SOLUSDT", 72)]))
        self.assertEqual(record["actions"][0]["kind"], "stop_place_failed")
        self.assertNotEqual(record["actions"][0]["kind"], "risk_blocked")
        self.assertEqual(len(self.trader.calls), 1)
        self.assertNotEqual(self.trader.calls[0].entry_type, "market")

    def test_changed_slippage_is_refused_before_any_order(self):
        self.trader.CFG["xiao_slippage_pct"] = 0.01
        record = self._run(self._job())
        self.assertEqual(record["result"], "trader_not_ioc")
        self.assertEqual(self.trader.calls, [])
        self.assertFalse(os.path.exists(self.seen))

    def test_seen_lock_blocks_the_other_process(self):
        seen = os.path.join(self.dir, "race.jsonl")
        lock = seen + ".lock"
        script = "import fcntl,sys,time\nf=open(sys.argv[1],'a')\nfcntl.flock(f,fcntl.LOCK_EX)\nsys.stdout.write('held\\n')\nsys.stdout.flush()\ntime.sleep(5)\n"
        proc = subprocess.Popen([sys.executable, "-c", script, lock], stdout=subprocess.PIPE, text=True)
        found = []
        thread = None

        def claim():
            found.append(REAL.filter_new_signals(
                [{"symbol": "ETHUSDT", "setup": "小師弟", "low_t": 1}], path=seen, record=True,
            ))

        try:
            self.assertEqual(proc.stdout.readline(), "held\n")
            thread = threading.Thread(target=claim)
            thread.start()
            thread.join(0.4)
            self.assertTrue(thread.is_alive())
        finally:
            proc.kill()
            proc.wait(timeout=5)
            if thread is not None:
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(found[0]), 1)
        again = REAL.filter_new_signals(
            [{"symbol": "ETHUSDT", "setup": "小師弟", "low_t": 1}], path=seen, record=True,
        )
        self.assertEqual(again, [])
        self.assertEqual(open(seen).read(), "xiao|ETHUSDT|1\n")

    def test_screen_is_xiao_15m_only(self):
        calls = []
        eth = {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"}
        btc = dict(eth, symbol="BTCUSDT")
        stock = dict(eth, symbol="STOCKUSDT")
        poor = dict(eth, symbol="POORUSDT")
        avoid = dict(eth, symbol="INITUSDT")
        mod = type("M", (), {})()
        mod.AVOID = {"INITUSDT"}
        mod.MIN_QV = 5e6

        def get(path, params=None):
            calls.append(("get", path))
            if "exchangeInfo" in path:
                return {"symbols": [eth, btc, stock, poor, avoid]}
            if "ticker" in path:
                return [
                    {"symbol": "ETHUSDT", "quoteVolume": "20000000"},
                    {"symbol": "BTCUSDT", "quoteVolume": "20000000"},
                    {"symbol": "STOCKUSDT", "quoteVolume": "20000000"},
                    {"symbol": "POORUSDT", "quoteVolume": "1"},
                    {"symbol": "INITUSDT", "quoteVolume": "20000000"},
                ]
            raise AssertionError(path)

        def klines(sym, interval, limit=120, end_ms=None):
            calls.append(("klines", sym, interval))
            bar = {"t": self.open_ms, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "ct": self.open_ms}
            return [bar] * xiao_close.MIN_15M_BARS

        def xiao(bars, symbol="ETHUSDT"):
            calls.append(("xiao", symbol))
            if symbol == "ETHUSDT":
                return {"setup": "小師弟", "entry": 100, "stop": 90, "target": 120, "low_t": 36}, None
            return None, "no"

        def boom(name):
            def _fn(*_a, **_k):
                calls.append((name,))
                raise AssertionError(name)
            return _fn

        mod.get = get
        mod.klines = klines
        mod.pmap = lambda fn, items: [fn(item) for item in items]
        mod.stock_like = lambda meta: meta["symbol"] == "STOCKUSDT"
        mod.xiao_shidi = xiao
        mod.btc_guard = lambda bars: calls.append(("guard",)) or {"blocked": False}
        mod.kuai_shidi = boom("kuai")
        mod.buluuliu = boom("bulu")
        mod.da_shixiong = boom("da")
        mod.funding = boom("funding")
        screen = xiao_close.screen_market(mod)
        names = [item[0] for item in calls]
        self.assertIn("xiao", names)
        self.assertIn("guard", names)
        self.assertNotIn("kuai", names)
        self.assertNotIn("bulu", names)
        self.assertNotIn("da", names)
        self.assertNotIn("funding", names)
        kline_calls = [item for item in calls if item[0] == "klines"]
        self.assertTrue(kline_calls)
        self.assertTrue(all(item[2] == "15m" for item in kline_calls))
        self.assertEqual(sorted(item[1] for item in kline_calls), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual([hit["symbol"] for hit in screen["hits"]], ["ETHUSDT"])
        paths = [item[1] for item in calls if item[0] == "get"]
        self.assertTrue(all("klines" not in path for path in paths))

    def test_ninety_nine_bars_do_not_trigger_and_one_hundred_do(self):
        prefilter_src = open(os.path.join(SRC, "prefilter.py")).read()
        self.assertIn("len(k) < 100", prefilter_src)
        self.assertEqual(xiao_close.MIN_15M_BARS, 100)

        def screen_for(count):
            calls = []
            eth = {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"}
            mod = type("M", (), {})()
            mod.AVOID = set()
            mod.MIN_QV = 1
            mod.get = lambda path, params=None: (
                {"symbols": [eth]} if "exchangeInfo" in path else [{"symbol": "ETHUSDT", "quoteVolume": "20000000"}]
            )
            bar = {"t": self.open_ms, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "ct": self.open_ms}
            mod.klines = lambda sym, interval, limit=120, end_ms=None: [bar] * count
            mod.pmap = lambda fn, items: [fn(item) for item in items]
            mod.stock_like = lambda meta: False

            def xiao(bars, symbol="ETHUSDT"):
                calls.append((symbol, len(bars)))
                return {"setup": "小師弟", "entry": 100, "stop": 90, "target": 120, "low_t": 36}, None

            mod.xiao_shidi = xiao
            mod.btc_guard = lambda bars: {"blocked": False}
            return calls, xiao_close.screen_market(mod)

        short_calls, short = screen_for(99)
        self.assertEqual(short_calls, [])
        self.assertEqual(short["hits"], [])
        long_calls, long_screen = screen_for(100)
        self.assertEqual(long_calls, [("ETHUSDT", 100)])
        self.assertEqual([hit["symbol"] for hit in long_screen["hits"]], ["ETHUSDT"])

    def test_prefilter_path_uses_the_sibling_common(self):
        folder = tempfile.mkdtemp(prefix="xiao-pf-")
        with open(os.path.join(folder, "common.py"), "w") as handle:
            handle.write("HERE = {0!r}\nMARKER = 'paper-common'\n".format(folder))
        with open(os.path.join(folder, "prefilter.py"), "w") as handle:
            handle.write(
                "from common import *\n"
                "SEEN_NAME = 'signal_seen.jsonl'\n"
                "def default_seen_path():\n"
                "    base = globals().get('DATA_DIR') or globals().get('HERE')\n"
                "    return __import__('os').path.join(base, SEEN_NAME)\n"
                "def xiao_shidi(k, symbol='ETHUSDT'):\n"
                "    return {'setup': '小師弟', 'marker': MARKER}, None\n"
            )
        saved = sys.modules.get("common")
        mod = xiao_close.load_module(os.path.join(folder, "prefilter.py"), "xiao_paper_prefilter_case")
        self.assertEqual(mod.MARKER, "paper-common")
        self.assertEqual(mod.HERE, folder)
        self.assertEqual(xiao_close.seen_path_of(mod, None), os.path.join(folder, "signal_seen.jsonl"))
        self.assertIs(sys.modules.get("common"), saved)
        self.assertEqual(REAL.default_seen_path(), os.path.join(REAL.DATA_DIR, "signal_seen.jsonl"))
        self.assertFalse(os.path.exists(REAL.default_seen_path()))

    def test_cli_dry_run_writes_nothing(self):
        folder = tempfile.mkdtemp(prefix="xiao-cli-")
        prefilter = os.path.join(folder, "prefilter.py")
        with open(prefilter, "w") as handle:
            handle.write(
                "def get(path, params=None):\n"
                "    if 'exchangeInfo' in path:\n"
                "        return {'symbols': []}\n"
                "    if 'ticker' in path:\n"
                "        return []\n"
                "    raise RuntimeError(path)\n"
                "def klines(*a, **k):\n"
                "    raise RuntimeError('klines')\n"
                "def xiao_shidi(*a, **k):\n"
                "    raise RuntimeError('xiao')\n"
                "def kuai_shidi(*a, **k):\n"
                "    raise RuntimeError('kuai')\n"
                "def filter_new_signals(*a, **k):\n"
                "    raise RuntimeError('seen')\n"
            )
        seen = os.path.join(folder, "signal_seen.jsonl")
        log = os.path.join(folder, "xiao_close_log.jsonl")
        proc = subprocess.run(
            [sys.executable, os.path.join(SRC, "xiao_close.py"), "--dry-run", "--grace", "0",
             "--prefilter", prefilter, "--seen", seen, "--log", log,
             "--trader", os.path.join(folder, "missing_trader.py")],
            cwd=folder, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["result"], "no_signal")
        self.assertFalse(os.path.exists(seen))
        self.assertFalse(os.path.exists(log))
        self.assertFalse(os.path.exists(os.path.join(folder, ".lock")))
        source = open(os.path.join(SRC, "xiao_close.py")).read()
        self.assertNotIn("quantboy-auto-scan", source)
        self.assertIn("/Users/frank/quantboy-paper/discretionary/prefilter.py", source)


class XiaoCloseIocTest(unittest.TestCase):
    def place(self, mark="100", tick="0.01", stop="90", target="120", fail_stop=False):
        calls = []
        algo_n = {"n": 0}

        def fake_req(method, path, params=None, signed=False, retries=5):
            calls.append((method, path, dict(params or {})))
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": mark}
            if path in ("/fapi/v1/order/test", "/fapi/v1/marginType", "/fapi/v1/leverage"):
                return {}
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "ETHUSDT", "positionAmt": "10", "markPrice": mark}]
            if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders", "/fapi/v1/userTrades", "/fapi/v1/income"):
                return []
            if method == "POST" and path == "/fapi/v1/order":
                return {"orderId": 7, "avgPrice": mark, "executedQty": "1"}
            if method == "POST" and path == "/fapi/v1/algoOrder":
                algo_n["n"] += 1
                if fail_stop and algo_n["n"] == 1:
                    raise lt.BinanceError(400, -2021, "stop rejected")
                return {"algoId": 30}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path, params))

        args_info = info(tick)
        cand = {"setup": "小師弟", "symbol": "ETHUSDT", "stop": stop, "target": target, "low_t": 36}
        buf = io.StringIO()
        with patch.object(lt, "req", fake_req), \
                patch.object(lt, "log", lambda *a, **k: None), \
                patch.object(lt, "account", return_value=ACC), \
                patch.object(lt, "day_state", return_value=dict(DAY)), \
                patch.object(lt, "upcoming_event", return_value=None), \
                patch.object(lt, "sym_info", return_value=args_info), \
                patch.object(lt, "load_state", return_value={"pending": {}, "meta": {}}), \
                patch.object(lt, "save_state", lambda state: None), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            outcome = xiao_close.place_xiao(lt, cand)
        return outcome, calls

    def test_ioc_cap_is_the_existing_zero_point_three_percent(self):
        self.assertEqual(lt.CFG["xiao_slippage_pct"], 0.003)
        self.assertEqual(lt.resolve_entry_type("小師弟", "market"), "ioc")
        self.assertEqual(lt.resolve_entry_type("瘸師弟", "limit"), "limit")
        outcome, calls = self.place()
        self.assertEqual(outcome["kind"], "ordered")
        entry = [c for c in calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0][2]["type"], "LIMIT")
        self.assertEqual(entry[0][2]["timeInForce"], "IOC")
        self.assertEqual(entry[0][2]["price"], "100.3")
        self.assertNotIn("MARKET", [c[2].get("type") for c in calls if c[1] == "/fapi/v1/order"])
        loss = (Decimal(entry[0][2]["price"]) - Decimal("90")) * Decimal(entry[0][2]["quantity"])
        self.assertLessEqual(loss, Decimal("100"))

    def test_risk_over_one_percent_does_not_send_an_order(self):
        outcome, calls = self.place(mark="90.2", tick="1")
        self.assertEqual(outcome["kind"], "risk_over_1pct")
        self.assertFalse(any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in calls))
        self.assertFalse(any(c[2].get("type") == "MARKET" for c in calls))

    def test_stop_place_failure_keeps_market_flatten_and_is_not_risk_blocked(self):
        outcome, calls = self.place(fail_stop=True)
        self.assertEqual(outcome["kind"], "stop_place_failed")
        self.assertNotEqual(outcome["kind"], "risk_blocked")
        entries = [c for c in calls if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("type") == "LIMIT"]
        flats = [c for c in calls if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("type") == "MARKET"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][2]["timeInForce"], "IOC")
        self.assertEqual(len(flats), 1)
        self.assertEqual(flats[0][2]["reduceOnly"], "true")
        self.assertEqual(flats[0][2]["side"], "SELL")


if __name__ == "__main__":
    unittest.main()
