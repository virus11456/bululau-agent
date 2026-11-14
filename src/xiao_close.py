#!/usr/bin/env python3
"""小師弟 15 分 K 收盤觸發。只跑小師弟篩網，有訊號就走 live_trader 現有的 IOC 開倉。

不叫 AI。進場、停損、止盈用設定檔裡那支 prefilter 的 xiao_shidi。
數量、1% 風險、件數、日損、事件窗、0.3% IOC 上限都留在 live_trader.cmd_open。
不下單時不寫 signal_seen、不拿 ~/quantboy-live/.lock。--dry-run 不寫任何檔。
"""
import argparse
import contextlib
import fcntl
import importlib.util
import inspect
import io
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))
INTERVAL_S = 15 * 60
INTERVAL_MS = INTERVAL_S * 1000
# prefilter.run 在 len(k) < 100 時不跑小師弟。兩邊用同一個門檻。
MIN_15M_BARS = 100
DEFAULT_PREFILTER = "/Users/frank/quantboy-paper/discretionary/prefilter.py"
DEFAULT_TRADER = os.path.expanduser("~/quantboy-live/live_trader.py")
DEFAULT_GRACE_S = 5
DEFAULT_LOCK_WAIT_S = 25
# 這些會停整輪。該幣自己的阻擋（symbol_blocked、risk_over_1pct）不在這裡。
STOP_KINDS = ("exchange_error", "network_error", "risk_blocked", "stop_place_failed")
_ACCOUNT_BLOCK = (
    "當日虧損", "今日停開新倉", "30%", "全平", "禁止開倉", "5 倉",
    "重大事件", "雙向持倉", "檔案鎖", "正在執行", "已觸發",
)
_SYMBOL_BLOCK = (
    "已有倉位", "單幣最多", "停損/止盈方向錯誤", "低於最小量",
    "最小名目", "找不到幣種", " 狀態 ",
)


class LockBusy(Exception):
    pass


class TraderNotIoc(Exception):
    pass


def boundary_s(now):
    """這一刻之前、最近的 15 分收盤（Unix 秒）。15 分界線跟時區對齊。"""
    epoch = int(now.timestamp())
    return epoch - (epoch % INTERVAL_S)


def iso_s(epoch_s):
    return datetime.fromtimestamp(epoch_s, TPE).isoformat(timespec="seconds")


def wait_until_grace(now_fn, grace_s, sleep):
    """launchd 只能在整分啟動。收盤後再等 grace 秒，讓這根 K 線進得了 klines。"""
    start = now_fn()
    close_s = boundary_s(start)
    elapsed = start.timestamp() - close_s
    if grace_s > 0 and elapsed < grace_s:
        sleep(grace_s - elapsed)
    trigger = now_fn()
    delay_s = round(trigger.timestamp() - close_s, 3)
    return close_s, trigger, delay_s


def load_module(path, name):
    """依路徑載入。旁邊有 common.py 就用那一份，避免掃到別的目錄。"""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise SystemExit("找不到 {0}".format(path))
    folder = os.path.dirname(path)
    common_path = os.path.join(folder, "common.py")
    saved = sys.modules.get("common")
    try:
        if os.path.isfile(common_path):
            cspec = importlib.util.spec_from_file_location(name + "_common", common_path)
            cmod = importlib.util.module_from_spec(cspec)
            sys.modules["common"] = cmod
            cspec.loader.exec_module(cmod)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is None:
            sys.modules.pop("common", None)
        else:
            sys.modules["common"] = saved


def _is_scan_error(exc):
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return True
    return exc.__class__.__name__ == "RateLimited"


def call_xiao(mod, bars, symbol):
    fn = mod.xiao_shidi
    try:
        names = inspect_params(fn)
    except (TypeError, ValueError):
        names = None
    if names is not None and "symbol" not in names:
        return fn(bars)
    return fn(bars, symbol)


def inspect_params(fn):
    return inspect.signature(fn).parameters


def universe(mod):
    """跟 prefilter.run 同一份流動性池，但不跑其他型態。"""
    info = mod.get("/fapi/v1/exchangeInfo")
    perps = [s for s in info["symbols"]
             if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
    tick = {t["symbol"]: t for t in mod.get("/fapi/v1/ticker/24hr")}
    avoid = set(getattr(mod, "AVOID", ()) or ())
    min_qv = float(getattr(mod, "MIN_QV", 5e6))
    stock_like = getattr(mod, "stock_like", None)
    out = []
    for meta in perps:
        sym = meta["symbol"]
        if sym in avoid:
            continue
        if stock_like and stock_like(meta):
            continue
        qv = float(tick.get(sym, {}).get("quoteVolume", 0))
        if qv < min_qv:
            continue
        out.append(sym)
    return out


def _pmap(mod, fn, items):
    pmap = getattr(mod, "pmap", None)
    if pmap:
        return list(pmap(fn, items))
    return [fn(item) for item in items]


def _safe_klines(mod, symbol):
    try:
        return mod.klines(symbol, "15m", 120)
    except Exception as exc:
        if _is_scan_error(exc):
            raise
        return None


def screen_market(mod):
    """只拉 15m。只呼叫 xiao_shidi 和 btc_guard。"""
    syms = universe(mod)
    if not syms:
        return {"universe": 0, "hits": [], "n_miss": 0, "guard": None, "btc_open": None, "bars": {}}
    need = list(syms)
    if "BTCUSDT" not in need:
        need.insert(0, "BTCUSDT")
    loaded = dict(zip(need, _pmap(mod, lambda sym: _safe_klines(mod, sym), need)))
    btc = loaded.get("BTCUSDT")
    guard = None
    btc_open = None
    if btc:
        btc_open = btc[-1].get("t")
        guard_fn = getattr(mod, "btc_guard", None)
        if guard_fn:
            guard = guard_fn(btc)
    hits = []
    misses = 0
    bars = {}
    for sym in syms:
        rows = loaded.get(sym)
        if not rows or len(rows) < MIN_15M_BARS:
            misses += 1
            continue
        bars[sym] = rows[-1].get("t")
        hit, _why = call_xiao(mod, rows, sym)
        if not hit or hit.get("setup") != "小師弟":
            misses += 1
            continue
        row = dict(hit)
        row["symbol"] = sym
        row["bar_open"] = rows[-1].get("t")
        hits.append(row)
    return {"universe": len(syms), "hits": hits, "n_miss": misses, "guard": guard,
            "btc_open": btc_open, "bars": bars}


def expected_open_ms(close_s):
    return int(close_s) * 1000 - INTERVAL_MS


def bar_is_just_closed(open_ms, close_s):
    return open_ms == expected_open_ms(close_s)


def seen_path_of(mod, explicit):
    if explicit:
        return explicit
    fn = getattr(mod, "default_seen_path", None)
    if fn:
        return fn()
    name = getattr(mod, "SEEN_NAME", "signal_seen.jsonl")
    base = getattr(mod, "DATA_DIR", None) or getattr(mod, "HERE", None)
    if not base:
        base = os.path.dirname(os.path.abspath(mod.__file__))
    return os.path.join(base, name)


def trader_is_xiao_ioc(trader):
    """小師弟必須走 IOC，滑價維持 0.003。其他型態不能被改成 IOC。"""
    resolve = getattr(trader, "resolve_entry_type", None)
    if resolve is None:
        return False
    if resolve("小師弟", "market") != "ioc" or resolve("小師弟", "limit") != "ioc":
        return False
    if resolve("瘸師弟", "limit") != "limit":
        return False
    if resolve("大師兄", "limit") != "limit":
        return False
    cfg = getattr(trader, "CFG", None) or {}
    slip = cfg.get("xiao_slippage_pct")
    try:
        if format(float(slip), ".3f") != "0.003":
            return False
    except (TypeError, ValueError):
        return False
    if not hasattr(trader, "cmd_open") or not getattr(trader, "LOCK", None):
        return False
    return True


def open_args(cand, live):
    if cand.get("setup") != "小師弟":
        raise TraderNotIoc("只下小師弟")
    return argparse.Namespace(
        live=bool(live), symbol=cand["symbol"], side="BUY", entry_type="limit", price=None,
        stop=str(cand["stop"]), target=str(cand["target"]), setup="小師弟",
        target_3r=None, leverage=None, portion=1.0, ladder=None,
    )


def _clip(text, limit=500):
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit]


def classify_exit(text):
    """cmd_open 的 SystemExit。停損掛失敗單獨記。帳戶層級停整輪，該幣的問題只跳過該幣。"""
    text = str(text or "")
    if "停損失敗" in text:
        return "stop_place_failed"
    if any(piece in text for piece in _ACCOUNT_BLOCK):
        return "risk_blocked"
    if any(piece in text for piece in _SYMBOL_BLOCK):
        return "symbol_blocked"
    return "risk_blocked"


def place_xiao(trader, cand):
    """呼叫 cmd_open 一次。失敗就回傳原因，不再送、也不改成市價。"""
    if not trader_is_xiao_ioc(trader):
        raise TraderNotIoc("live_trader 不是小師弟 IOC 0.3%")
    args = open_args(cand, True)
    if args.entry_type == "market":
        raise TraderNotIoc("不送市價")
    buf = io.StringIO()
    binance_error = getattr(trader, "BinanceError", None)
    try:
        with contextlib.redirect_stdout(buf):
            trader.cmd_open(args)
    except SystemExit as exc:
        return {"kind": classify_exit(exc), "detail": _clip(exc)}
    except Exception as exc:
        if binance_error is not None and isinstance(exc, binance_error):
            return {"kind": "exchange_error", "detail": _clip(exc)}
        if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
            return {"kind": "network_error", "detail": _clip(exc)}
        raise
    text = buf.getvalue()
    if "預估風險超過" in text:
        kind = "risk_over_1pct"
    elif "沒成交" in text:
        kind = "ioc_unfilled"
    elif "已進場" in text:
        kind = "ordered"
    elif "市價平倉" in text:
        kind = "target_passed"
    else:
        kind = "submitted"
    return {"kind": kind, "detail": _clip(text)}


@contextlib.contextmanager
def trader_lock(path, wait_s, sleep, monotonic=None):
    """跟 live_trader.Lock 搶同一檔。open 用 append，不截斷對方的鎖檔。"""
    mono = monotonic or time.monotonic
    handle = open(path, "a")
    acquired = False
    try:
        deadline = mono() + max(0.0, float(wait_s))
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if mono() >= deadline:
                    raise LockBusy()
                sleep(min(0.05, max(0.0, deadline - mono())))
        yield handle
    finally:
        if acquired:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _compact_hit(hit):
    return {
        "symbol": hit.get("symbol"),
        "low_t": hit.get("low_t"),
        "entry": hit.get("entry"),
        "stop": hit.get("stop"),
        "target": hit.get("target"),
        "bar_open": hit.get("bar_open"),
    }


def _screen_view(screen, close_s):
    guard = screen.get("guard") or {}
    return {
        "universe": screen.get("universe"),
        "n_hits": len(screen.get("hits") or []),
        "n_miss": screen.get("n_miss"),
        "hits": [_compact_hit(hit) for hit in screen.get("hits") or []],
        "btc_guard": guard.get("why") if guard.get("blocked") else None,
        "btc_open": screen.get("btc_open"),
        "bar_ready": bar_is_just_closed(screen.get("btc_open"), close_s) if screen.get("universe") else False,
    }


def _claim(mod, cand, path, record):
    fn = getattr(mod, "filter_new_signals", None)
    if fn is None:
        return None
    kept = fn([cand], path=path, record=record)
    return kept


def _act_on_hits(job, mod, hits, seen, record):
    actions = []
    for cand in hits:
        key_fn = getattr(mod, "signal_id", None)
        key = key_fn(cand) if key_fn else None
        if not key:
            actions.append({"symbol": cand.get("symbol"), "signal_id": None, "kind": "no_signal_id",
                            "detail": "沒有穩定的訊號編號，不能跟 routine 去重，不下單"})
            continue
        if not bar_is_just_closed(cand.get("bar_open"), job.close_s):
            actions.append({"symbol": cand.get("symbol"), "signal_id": key, "kind": "bar_not_closed",
                            "detail": "這根 15m 還沒對上剛收盤的那一根"})
            continue
        kept = _claim(mod, cand, seen, record)
        if kept is None:
            actions.append({"symbol": cand.get("symbol"), "signal_id": key, "kind": "no_signal_id",
                            "detail": "prefilter 沒有 filter_new_signals，不能去重，不下單"})
            continue
        if not kept:
            actions.append({"symbol": cand.get("symbol"), "signal_id": key, "kind": "duplicate",
                            "detail": "signal_seen 已經有這一筆"})
            continue
        if not record:
            actions.append({"symbol": cand.get("symbol"), "signal_id": key, "kind": "would_order",
                            "detail": "dry-run，不下單、不寫去重"})
            continue
        try:
            outcome = place_xiao(job.trader, cand)
        except TraderNotIoc as exc:
            actions.append({"symbol": cand.get("symbol"), "signal_id": key, "kind": "trader_not_ioc",
                            "detail": _clip(exc)})
            break
        except Exception as exc:
            actions.append({"symbol": cand.get("symbol"), "signal_id": key, "kind": "error",
                            "detail": _clip("{0}: {1}".format(type(exc).__name__, exc))})
            break
        outcome["symbol"] = cand.get("symbol")
        outcome["signal_id"] = key
        actions.append(outcome)
        if outcome["kind"] in STOP_KINDS:
            break
    return actions


def _finish(job, screen, actions, result, detail=None):
    trigger = job.trigger
    record = {
        "kind": "xiao_close",
        "trigger_tpe": trigger.isoformat(timespec="seconds"),
        "bar_close_tpe": iso_s(job.close_s),
        "delay_s": job.delay_s,
        "dry_run": bool(job.dry_run),
        "prefilter": job.prefilter_path,
        "result": result,
        "screen": _screen_view(screen, job.close_s) if screen else None,
        "actions": actions,
    }
    if detail:
        record["detail"] = _clip(detail)
    line = json.dumps(record, ensure_ascii=False)
    print(line)
    if not job.dry_run and job.log_path:
        folder = os.path.dirname(job.log_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(job.log_path, "a") as handle:
            handle.write(line + "\n")
    return record


def _empty_screen():
    return {"universe": 0, "hits": [], "n_miss": 0, "guard": None, "btc_open": None, "bars": {}}


def run_job(job, now_fn=None, sleep=None):
    now_fn = now_fn or (lambda: datetime.now(TPE))
    sleep = sleep or time.sleep
    close_s, trigger, delay_s = wait_until_grace(now_fn, job.grace_s, sleep)
    job.close_s = close_s
    job.trigger = trigger
    job.delay_s = delay_s
    mod = job.prefilter
    try:
        screen = job.screen(mod)
    except Exception as exc:
        if not _is_scan_error(exc):
            raise
        return _finish(job, _empty_screen(), [], "scan_error",
                       "{0}: {1}".format(type(exc).__name__, exc))
    hits = list(screen.get("hits") or [])
    guard = screen.get("guard") or {}
    if not screen.get("universe"):
        return _finish(job, screen, [], "no_signal")
    if not bar_is_just_closed(screen.get("btc_open"), close_s):
        return _finish(job, screen, [], "bar_not_ready")
    if guard.get("blocked"):
        return _finish(job, screen, [], "btc_guard")
    fresh = [hit for hit in hits if bar_is_just_closed(hit.get("bar_open"), close_s)]
    if not fresh:
        why = "bar_not_ready" if hits else "no_signal"
        return _finish(job, screen, [], why)
    seen = seen_path_of(mod, job.seen_path)
    if job.dry_run:
        actions = _act_on_hits(job, mod, fresh, seen, False)
        result = "dry_run"
        if actions and all(item["kind"] == "duplicate" for item in actions):
            result = "duplicate"
        return _finish(job, screen, actions, result)
    if not trader_is_xiao_ioc(job.trader):
        return _finish(job, screen, [], "trader_not_ioc")
    if not hasattr(mod, "filter_new_signals") or not hasattr(mod, "signal_id"):
        return _finish(job, screen, [], "prefilter_not_ready")
    try:
        with trader_lock(job.trader.LOCK, job.lock_wait_s, sleep):
            actions = _act_on_hits(job, mod, fresh, seen, True)
    except LockBusy:
        return _finish(job, screen, [], "lock_busy")
    if any(item["kind"] == "ordered" for item in actions):
        result = "ordered"
    elif actions:
        result = actions[-1]["kind"]
    else:
        result = "no_signal"
    return _finish(job, screen, actions, result)


class Job(object):
    def __init__(self, prefilter, prefilter_path, trader=None, seen_path=None, log_path=None,
                 dry_run=True, grace_s=DEFAULT_GRACE_S, lock_wait_s=DEFAULT_LOCK_WAIT_S, screen=None):
        self.prefilter = prefilter
        self.prefilter_path = prefilter_path
        self.trader = trader
        self.seen_path = seen_path
        self.log_path = log_path
        self.dry_run = dry_run
        self.grace_s = grace_s
        self.lock_wait_s = lock_wait_s
        self.screen = screen or screen_market
        self.close_s = None
        self.trigger = None
        self.delay_s = None


def parse_args(argv):
    ap = argparse.ArgumentParser(description="小師弟 15 分 K 收盤觸發。預設 dry-run，不寫狀態。")
    ap.add_argument("--prefilter", default=DEFAULT_PREFILTER)
    ap.add_argument("--trader", default=DEFAULT_TRADER)
    ap.add_argument("--seen", default=None, help="signal_seen.jsonl。預設跟 prefilter 的 default_seen_path 同一個檔")
    ap.add_argument("--log", default=None, help="只在非 dry-run 時追加。預設放在 trader 旁邊")
    ap.add_argument("--grace", type=float, default=DEFAULT_GRACE_S, help="收盤後再等幾秒才拉 K 線")
    ap.add_argument("--lock-wait", type=float, default=DEFAULT_LOCK_WAIT_S, dest="lock_wait")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args(argv)
    if args.live and args.dry_run:
        raise SystemExit("--live 和 --dry-run 只能擇一")
    args.dry_run = not args.live
    if args.log is None and args.live:
        args.log = os.path.join(os.path.dirname(os.path.abspath(args.trader)), "xiao_close_log.jsonl")
    return args


def main(argv=None):
    args = parse_args(argv)
    mod = load_module(args.prefilter, "xiao_close_prefilter")
    trader = load_module(args.trader, "xiao_close_trader") if args.live else None
    job = Job(mod, os.path.abspath(args.prefilter), trader=trader, seen_path=args.seen, log_path=args.log,
              dry_run=args.dry_run, grace_s=args.grace, lock_wait_s=args.lock_wait)
    run_job(job)
    return 0


if __name__ == "__main__":
    sys.exit(main())
