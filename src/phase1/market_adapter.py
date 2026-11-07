"""Mac 帳戶與公開 K 線的接線。預設不取數、不連線。

私有三查詢只在執行時載入 SHA 已核的平鋪 live_trader.req。
不呼叫 open_algo。K 線用 repo 的公開 client，平鋪檔沒有 klines 函式。
"""
import hashlib
import importlib.util
import os

from .review_context import build_review, plan_round
from .status_contract import require_account, require_order_list
from .token_switch import UnconfiguredWebhook

_PRIVATE_PATHS = ("/fapi/v2/account", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders")
MAC_FLAT_SHA256 = "65f1d4526cd5d3b6c2115ca8eeebd102281b27a2fbd40d0ee07054ff4891459b"
MAC_FLAT_PATH = "/Users/frank/quantboy-live/live_trader.py"
REQ_CONTRACT = "req(method, path, params=None, signed=False, retries=5)"
_ABSENT_MAC = {
    "path": MAC_FLAT_PATH,
    "sha256": MAC_FLAT_SHA256,
    "layout": "flat live_trader.py, no src/, no status-json-2",
    "ast_verified": True,
    "req": REQ_CONTRACT,
    "open_algo_used": False,
    "klines_function": False,
    "error_may_write_log": True,
    "in_repo": False,
    "wired": False,
}


def source_gap():
    return {
        "repo_private": "src.phase1.account_status._signed_get(path) -> object",
        "repo_public": "src.common.get(path, params=None, retries=2) -> object",
        "private_paths": list(_PRIVATE_PATHS),
        "public_kline": {
            "path": "/fapi/v1/klines",
            "closed_only": True,
            "daily_ma_parameter": "undecided",
            "daily_ma_gate": "not_evaluated",
            "by_setup": {
                "瘸師弟": {"15m": 24, "4h": 6, "1d": 6},
                "小師弟": {"15m": 24, "4h": 6, "1d": 6},
                "布鹿流": {"1h": 48, "4h": 50, "1d": 6},
            },
            "btc_15m_when_candidate": 24,
            "fifteen_m_20_is_not_strategy_context": True,
        },
        "not_used": {
            "src.common.klines": "quote volume replaces base volume and the raw row is dropped",
            "open_algo": "BinanceError becomes [] after a warn log; adapter calls req directly",
        },
        "mac_flat_source_absent": dict(_ABSENT_MAC),
        "klines_api_verified": False,
        "routine_connected": False,
        "live_scan": "*/30 flexible ±7",
        "watch_seconds": 60,
    }


def disabled_entry():
    return {
        "enabled": False,
        "replaces_scan_entry": False,
        "live_fetch": False,
        "outbound": False,
        "gateway_base_url": "",
    }


def load_entry_config(document):
    if not isinstance(document, dict):
        raise ValueError("malformed_config")
    enabled = document.get("enabled") is True
    live_fetch = document.get("live_fetch") is True
    replaces = document.get("replaces_scan_entry") is True
    outbound = document.get("outbound") is True
    gateway = document.get("gateway_base_url") if isinstance(document.get("gateway_base_url"), str) else ""
    mac = document.get("mac_flat") if isinstance(document.get("mac_flat"), dict) else {}
    sha = mac.get("sha256", MAC_FLAT_SHA256)
    if sha not in ("", MAC_FLAT_SHA256):
        raise ValueError("unverified_module")
    path = mac.get("path") if isinstance(mac.get("path"), str) else ""
    if outbound or (gateway and not gateway.startswith("https://")):
        raise ValueError("endpoint_not_provided")
    if replaces and not enabled:
        raise ValueError("scan_entry_not_replaced")
    return {
        "enabled": enabled,
        "replaces_scan_entry": replaces,
        "live_fetch": live_fetch and enabled,
        "outbound": False,
        "gateway_base_url": gateway,
        "mac_flat_path": path,
        "mac_flat_sha256": MAC_FLAT_SHA256,
        "source_gap": source_gap(),
    }


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def require_req_contract(req):
    """只看已載入函式的參數名。不呼叫，不讀金鑰。"""
    code = getattr(req, "__code__", None)
    if code is None:
        raise ValueError("req_signature")
    names = code.co_varnames[:code.co_argcount]
    if names[:2] != ("method", "path") or "params" not in names or "signed" not in names or "retries" not in names:
        raise ValueError("req_signature")
    return req


def load_verified_req(path, expected_sha=MAC_FLAT_SHA256):
    """SHA 不符就不執行檔案。執行後只取出 req，不呼叫它。"""
    if expected_sha != MAC_FLAT_SHA256:
        raise ValueError("unverified_module")
    if not isinstance(path, str) or not path or os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("module_missing")
    if file_sha256(path) != MAC_FLAT_SHA256:
        raise ValueError("module_sha_mismatch")
    spec = importlib.util.spec_from_file_location("mac_flat_live_trader", path)
    if spec is None or spec.loader is None:
        raise ValueError("module_missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    req = getattr(module, "req", None)
    if not callable(req):
        raise ValueError("req_missing")
    return require_req_contract(req)


def call_verified_req(req, path):
    """三個 GET 都直接走 req。None 與非容器不當成空清單。"""
    if path not in _PRIVATE_PATHS:
        raise ValueError("rejected_path")
    value = req("GET", path, signed=True)
    if path == "/fapi/v2/account":
        return require_account(value)
    return require_order_list(value)


def _symbol_ok(symbol):
    return isinstance(symbol, str) and symbol.isalnum() and symbol.endswith("USDT")


def _real_private(path):
    if path not in _PRIVATE_PATHS:
        raise ValueError("rejected_path")
    try:
        from .account_status import _signed_get
    except ImportError:
        raise ValueError("fetcher_missing")
    return _signed_get(path)


def _load_public_get():
    """repo 根以 python -m 執行時，公開 K 線在 src.common。"""
    try:
        from src.common import get as public_get
    except ImportError:
        from common import get as public_get
    if not callable(public_get):
        raise ValueError("fetcher_missing")
    return public_get


_INTERVAL_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000}
# 24 = 前 20 根量比較再加最多 3 根訊號，都要已收。6 = 黑紅黑三根再留餘地，不是均線長度。
# 48 = 1H 看更高低與大約三波的結構窗。50 = 4H 趨勢 30–50 的上緣，程式不判定趨勢已成形。
_SETUP_BARS = {
    "瘸師弟": (("15m", 24), ("4h", 6), ("1d", 6)),
    "小師弟": (("15m", 24), ("4h", 6), ("1d", 6)),
    "布鹿流": (("1h", 48), ("4h", 50), ("1d", 6)),
}
_BTC_BARS = (("15m", 24),)
_OHLC_KEYS = ("open", "high", "low", "close", "volume")


def kline_request(symbol, interval="15m", limit=20):
    """平鋪檔沒有 klines。參數沿 repo 公開 client，不代表已打過真 API。"""
    if not _symbol_ok(symbol) or interval not in _INTERVAL_MS:
        raise ValueError("rejected_symbol")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("rejected_symbol")
    return "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}


def _real_public(symbol, interval="15m", limit=20):
    path, params = kline_request(symbol, interval, limit)
    try:
        public_get = _load_public_get()
    except ImportError:
        raise ValueError("fetcher_missing")
    return public_get(path, params)


def _merge_bars(spec, into):
    for interval, count in spec:
        into[interval] = max(into.get(interval, 0), count)


def _setup_names(symbol, candidates):
    found = []
    for item in candidates or []:
        if isinstance(item, dict) and item.get("symbol") == symbol and item.get("setup") not in found:
            found.append(item.get("setup"))
    return found


def closed_ohlcv(raw, interval, now_ms):
    """依 open_time 排序並去重。未收盤、沒對齊的根不留。重複根不能拿來湊數。"""
    step = _INTERVAL_MS[interval]
    bars = []
    if not isinstance(raw, list):
        return bars
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            continue
        opened = item[0]
        try:
            opened = int(opened)
            prices = [float(item[index]) for index in range(1, 6)]
        except (TypeError, ValueError):
            continue
        if opened % step != 0 or any(price != price or abs(price) == float("inf") for price in prices):
            continue
        close_time = int(item[6]) if len(item) > 6 else opened + step - 1
        if close_time >= now_ms or opened + step > now_ms:
            continue
        bars.append({
            "open_time": opened,
            "open": prices[0],
            "high": prices[1],
            "low": prices[2],
            "close": prices[3],
            "volume": prices[4],
            "closed": True,
        })
    bars.sort(key=lambda bar: bar["open_time"])
    unique = []
    seen = set()
    for bar in bars:
        if bar["open_time"] in seen:
            continue
        seen.add(bar["open_time"])
        unique.append(bar)
    return unique


def continuous_window(bars, interval, count):
    """最後 count 根必須步進剛好一個週期。中間挖空不能用更早的根湊數。"""
    if not isinstance(bars, list) or len(bars) < count or count < 1:
        return False
    step = _INTERVAL_MS[interval]
    window = bars[-count:]
    previous = None
    for bar in window:
        opened = bar.get("open_time") if isinstance(bar, dict) else None
        if not _exact_count(opened) or bar.get("closed") is not True:
            return False
        if previous is not None and opened - previous != step:
            return False
        previous = opened
    return True


def _exact_count(value):
    return type(value) is int


def _series_fresh(bars, interval, now_ms):
    if not bars:
        return False
    step = _INTERVAL_MS[interval]
    age = now_ms - bars[-1]["open_time"]
    return step <= age < 2 * step


def _finite_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and abs(value) != float("inf")


def bar_ohlcv_ok(bar, interval):
    """一根已收盤 K 要有齊、有限、高低合法的 OHLCV。缺 close 或非數字都不算。"""
    if not isinstance(bar, dict) or bar.get("closed") is not True:
        return False
    step = _INTERVAL_MS.get(interval)
    opened = bar.get("open_time")
    if step is None or type(opened) is not int or opened % step != 0:
        return False
    for key in _OHLC_KEYS:
        if not _finite_price(bar.get(key)):
            return False
    high = bar["high"]
    low = bar["low"]
    if high < low or high < bar["open"] or high < bar["close"] or low > bar["open"] or low > bar["close"]:
        return False
    if bar["volume"] < 0:
        return False
    return True


def candidate_requirements(candidates):
    """跟取數同一張 setup 表。有已知候選才加 BTC 的 15m。未知 setup 不在這裡發明根數。"""
    requirements = {}
    known = False
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        spec = _SETUP_BARS.get(item.get("setup"))
        if not _symbol_ok(symbol) or spec is None:
            continue
        known = True
        _merge_bars(spec, requirements.setdefault(symbol, {}))
    if known:
        _merge_bars(_BTC_BARS, requirements.setdefault("BTCUSDT", {}))
    return requirements


def uncovered_candidates(candidates):
    """每個 (symbol, setup) 都要有已知需求。同 symbol 的另一個 setup 不能被蓋掉。"""
    uncovered = []
    seen = set()
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol") if isinstance(item.get("symbol"), str) else ""
        setup = item.get("setup") if isinstance(item.get("setup"), str) else ""
        key = (symbol, setup)
        if key in seen:
            continue
        seen.add(key)
        if not _symbol_ok(symbol) or setup not in _SETUP_BARS:
            uncovered.append({"symbol": symbol, "setup": setup})
    return uncovered


def series_meets(bars, interval, count, now_ms):
    """必需根數的連續已收盤窗，而且每一根 OHLCV 都完整、有限、合法、新鮮。"""
    if type(now_ms) is not int or not continuous_window(bars, interval, count) or not _series_fresh(bars, interval, now_ms):
        return False
    if not isinstance(bars, list) or not all(bar_ohlcv_ok(bar, interval) for bar in bars):
        return False
    return continuous_window(bars, interval, len(bars))


def sent_at_ms(sent_at):
    from .timeparse import parse_instant
    try:
        parsed = parse_instant(sent_at)
    except (TypeError, ValueError):
        return None
    if getattr(parsed, "tzinfo", None) is None:
        return None
    return int(parsed.timestamp() * 1000)


def review_covers_setup(candidates, klines, now_ms):
    """sufficient 必須覆蓋每一筆候選。未知 setup 不能靠同 symbol 的已知子集通過。"""
    if uncovered_candidates(candidates):
        return False
    requirements = candidate_requirements(candidates)
    if not requirements or not isinstance(klines, dict):
        return False
    for symbol, intervals in requirements.items():
        series = klines.get(symbol)
        if not isinstance(series, dict):
            return False
        for interval, count in intervals.items():
            if not series_meets(series.get(interval), interval, count, now_ms):
                return False
    for series in klines.values():
        if not isinstance(series, dict) or not series:
            return False
        for interval, bars in series.items():
            if interval not in _INTERVAL_MS or not isinstance(bars, list) or not bars:
                return False
            if not all(bar_ohlcv_ok(bar, interval) for bar in bars) or not continuous_window(bars, interval, len(bars)):
                return False
    return True


def resolve_clients(entry, private_get=None, public_get=None):
    """live_fetch 為 false 時不匯入真實客戶端。測試可注入 mock。"""
    if entry.get("enabled") is not True:
        return None, None
    if private_get is not None or public_get is not None:
        if private_get is None or public_get is None:
            return None, None
        return private_get, public_get
    if entry.get("live_fetch") is not True:
        return None, None
    mac_path = entry.get("mac_flat_path") or ""
    if mac_path:
        req = load_verified_req(mac_path)
        def private(path, bound=req):
            return call_verified_req(bound, path)
        return private, _real_public
    return _real_private, _real_public


def _needed_symbols(review, candidates):
    symbols = []
    for item in list(review.get("positions") or []) + list(review.get("orders") or []) + list(candidates or []):
        symbol = item.get("symbol") if isinstance(item, dict) else None
        if _symbol_ok(symbol) and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _bars_complete(bars):
    if not isinstance(bars, list) or not bars:
        return False
    for bar in bars:
        if not isinstance(bar, dict):
            return False
        for key in ("open_time", "open", "high", "low", "close", "volume"):
            if bar.get(key) is None:
                return False
    return True


def fetch_market(private_get, public_get, candidates, now_ms):
    """三個私有查詢，以及候選、持倉、掛單每個 symbol 的完整 OHLCV。任一失敗留原 AI。"""
    if private_get is None or public_get is None:
        empty = build_review(None, [], [], {}, candidates or [], now_ms)
        empty["context_sufficient"] = False
        empty["missing"] = ["fetcher_missing"]
        return empty
    try:
        account = private_get("/fapi/v2/account")
        orders = private_get("/fapi/v1/openOrders")
        algo = private_get("/fapi/v1/openAlgoOrders")
        if account is None or orders is None or algo is None:
            raise ValueError("query_failed")
        preview = build_review(account, orders, algo, {}, candidates or [], now_ms, queries_present=True)
        klines = {}
        for symbol in _needed_symbols(preview, candidates):
            raw = public_get(symbol)
            if not isinstance(raw, list):
                raise ValueError("query_failed")
            klines[symbol] = raw
        review = build_review(account, orders, algo, klines, candidates or [], now_ms, queries_present=True)
    except Exception:
        failed = build_review(None, None, None, {}, candidates or [], now_ms, queries_present=False)
        failed["missing"] = ["query_failed"]
        failed["context_sufficient"] = False
        failed["old_ai_poll"] = "keep"
        return failed
    for symbol in _needed_symbols(review, candidates):
        if not _bars_complete(review.get("klines", {}).get(symbol)):
            review["missing"].append("klines:" + symbol)
            review["context_sufficient"] = False
    return review


def _bar_count(review):
    total = 0
    for series in (review.get("klines") or {}).values() if isinstance(review.get("klines"), dict) else []:
        if not isinstance(series, dict):
            continue
        for bars in series.values():
            if isinstance(bars, list):
                total += len(bars)
    return total


def _envelope_for(snapshot, prefilter, review, *, now, scan_id, event_id, reason):
    from .autoscan import freshness as freshness_of
    from .production_transport import build_envelope
    fresh = freshness_of(snapshot.get("as_of"), now) if snapshot.get("as_of") else {
        "valid": False, "status": "missing", "age_seconds": None,
    }
    return build_envelope(
        snapshot, prefilter, fresh, review=review, event_id=event_id, evaluation_id=scan_id,
        sent_at=now, reason=reason or "not_quiet",
    )


def _review_exceeds_context_limit(snapshot, prefilter, review, *, now, scan_id):
    """notifier 前量的是完整 delivery 的 UTF-8，不是 envelope，也不是字元數。"""
    from .production_transport import CONTEXT_MAX_BYTES, projected_delivery_bytes
    probe = "c-" + ("ab" * 16)
    try:
        envelope = _envelope_for(
            snapshot, prefilter, review, now=now, scan_id=scan_id, event_id=probe, reason="candidates_present",
        )
        raw = projected_delivery_bytes(envelope, "ab" * 16)
    except ValueError:
        return True
    return len(raw) > CONTEXT_MAX_BYTES


def _bind_review_envelope(store, snapshot, prefilter, review, *, now, scan_id, event_id, reason):
    from .production_transport import CONTEXT_MAX_BYTES, bind_envelope, projected_delivery_bytes
    if not event_id:
        return False
    envelope = _envelope_for(
        snapshot, prefilter, review, now=now, scan_id=scan_id, event_id=event_id, reason=reason,
    )
    try:
        raw = projected_delivery_bytes(envelope, "ab" * 16)
    except ValueError:
        return False
    if len(raw) > CONTEXT_MAX_BYTES:
        return False
    bind_envelope(store, envelope)
    return True


def _annotate_strategy(review):
    review["daily_ma_parameter"] = "undecided"
    review["daily_ma_gate"] = "not_evaluated"
    review["unclosed_bars_excluded"] = True
    review["old_ai_poll"] = "keep"
    review["context_sufficient"] = not review.get("missing")
    return review


def fetch_strategy_market(private_get, public_get, candidates, now_ms):
    """只拉候選與持倉 setup 需要的已收盤 K。空倉無候選不掃市場。"""
    if private_get is None or public_get is None:
        empty = build_review(None, [], [], {}, candidates or [], now_ms)
        empty["context_sufficient"] = False
        empty["missing"] = ["fetcher_missing"]
        return _annotate_strategy(empty)
    try:
        account = private_get("/fapi/v2/account")
        orders = private_get("/fapi/v1/openOrders")
        algo = private_get("/fapi/v1/openAlgoOrders")
        if account is None or orders is None or algo is None:
            raise ValueError("query_failed")
        preview = build_review(account, orders, algo, {}, candidates or [], now_ms, queries_present=True)
        named = [item for item in candidates or [] if isinstance(item, dict)]
        requirements = candidate_requirements(named)
        unknown = []
        for item in uncovered_candidates(named):
            unknown.append("unknown_setup:{0}:{1}".format(item["symbol"], item["setup"]))
        for pos in preview["positions"]:
            known = [name for name in _setup_names(pos["symbol"], named) if name in _SETUP_BARS]
            if not known:
                unknown.append(pos["symbol"])
            else:
                for name in known:
                    _merge_bars(_SETUP_BARS[name], requirements.setdefault(pos["symbol"], {}))
        for order in preview["orders"]:
            known = [name for name in _setup_names(order["symbol"], named) if name in _SETUP_BARS]
            if not known and order["symbol"] not in requirements:
                unknown.append(order["symbol"])
        series = {}
        strategy_missing = []
        for symbol, intervals in requirements.items():
            series[symbol] = {}
            for interval, count in intervals.items():
                raw = public_get(symbol, interval, count + 1)
                if not isinstance(raw, list):
                    raise ValueError("query_failed")
                closed = closed_ohlcv(raw, interval, now_ms)
                series[symbol][interval] = closed
                if not series_meets(closed, interval, count, now_ms):
                    strategy_missing.append("klines:{0}:{1}".format(symbol, interval))
        for item in unknown:
            if str(item).startswith("unknown_setup:"):
                strategy_missing.append(item)
            else:
                strategy_missing.append("unknown_setup:" + (item or ""))
    except Exception:
        failed = build_review(None, None, None, {}, candidates or [], now_ms, queries_present=False)
        failed["missing"] = ["query_failed"]
        failed["context_sufficient"] = False
        return _annotate_strategy(failed)
    kept = [item for item in preview.get("missing") or [] if not str(item).startswith("klines:")]
    preview["missing"] = kept + strategy_missing
    preview["klines"] = series
    return _annotate_strategy(preview)


def run_review_entry(store, snapshot, prefilter, *, now, scan_id, now_ms, entry, notifier=None, private_get=None, public_get=None):
    """quiet 在 notifier 之前。不改現有 routine，不宣稱已省下真喚醒。"""
    entry = entry or disabled_entry()
    notifier = notifier if notifier is not None else UnconfiguredWebhook()
    base = {
        "quiet": False,
        "wake_sent": False,
        "notifier_called": False,
        "external_send": False,
        "routine_stopped": False,
        "watch_modified": False,
        "wakes_saved": False,
        "routine_replaced": False,
        "live_fetch": False,
        "entry_enabled": entry.get("enabled") is True,
        "replaces_scan_entry": entry.get("replaces_scan_entry") is True and entry.get("enabled") is True,
        "old_ai_poll": "keep",
        "discretion_complete": False,
        "fetch_review_called": False,
        "now_ms": now_ms if type(now_ms) is int else None,
        "source_gap": source_gap(),
    }
    if type(now_ms) is not int or now_ms <= 0:
        base["reason"] = "clock_invalid"
        base["context_sufficient"] = False
        return base
    try:
        private, public = resolve_clients(entry, private_get, public_get)
    except Exception:
        private, public = None, None
        base["reason"] = "context_insufficient"
        base["context_sufficient"] = False
        base["old_ai_poll"] = "keep"
        base["fetch_review_called"] = True
        return base
    candidates = prefilter.get("candidates") if isinstance(prefilter, dict) else []
    if not isinstance(candidates, list):
        candidates = []
    if private is None or public is None:
        from .review_context import operate_round
        planned = operate_round(
            store, snapshot, prefilter, now=now, scan_id=scan_id, notifier=notifier, now_ms=now_ms,
        )
    else:
        try:
            review = fetch_strategy_market(private, public, candidates, now_ms)
        except Exception:
            review = {"context_sufficient": False, "missing": ["query_failed"], "klines": {}}
        bars_retained = _bar_count(review)
        if _review_exceeds_context_limit(snapshot, prefilter, review, now=now, scan_id=scan_id):
            planned = {
                "quiet": False,
                "notifier_called": False,
                "cycle_needs_ai": True,
                "reason": "context_too_large",
                "event_id": None,
                "old_ai_poll": "keep",
                "context_sufficient": False,
                "missing": list(review.get("missing") or []),
                "fetch_review_called": True,
                "trimmed": False,
                "bars_retained": bars_retained,
                "parent_received_klines": False,
            }
        elif review.get("context_sufficient") is not True:
            planned = {
                "quiet": False,
                "notifier_called": False,
                "cycle_needs_ai": True,
                "reason": "context_insufficient",
                "event_id": None,
                "old_ai_poll": "keep",
                "context_sufficient": False,
                "missing": list(review.get("missing") or []),
                "fetch_review_called": True,
                "trimmed": False,
                "bars_retained": bars_retained,
                "parent_received_klines": False,
            }
        else:
            planned = plan_round(store, snapshot, prefilter, review, now=now, scan_id=scan_id, notifier=notifier)
            planned["fetch_review_called"] = True
            planned["trimmed"] = False
            planned["bars_retained"] = bars_retained
            delivered = _bind_review_envelope(
                store, snapshot, prefilter, review, now=now, scan_id=scan_id,
                event_id=planned.get("event_id"), reason=planned.get("reason"),
            )
            planned["parent_received_klines"] = delivered
    planned.update({key: base[key] for key in (
        "wake_sent", "external_send", "routine_stopped", "watch_modified", "wakes_saved",
        "routine_replaced", "live_fetch", "entry_enabled", "replaces_scan_entry", "discretion_complete", "now_ms", "source_gap",
    )})
    planned["live_fetch"] = False
    planned["wakes_saved"] = False
    planned["routine_replaced"] = False
    if entry.get("replaces_scan_entry") is True and entry.get("enabled") is True:
        planned["routine_replaced"] = False
        planned["replacement_command"] = "python3 -m src.phase1.runner review-round --entry-config FILE"
        planned["installed_routine_unchanged"] = True
    return planned
