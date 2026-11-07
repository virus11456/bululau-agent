"""Mac 帳戶與公開 K 線的接線。預設不取數、不連線。

repo 裡可呼叫的是 account_status._signed_get 與 common.get。
~/quantboy-live/live_trader.py 不在這個 repo，不能把它說成已經接上。
"""
from .review_context import build_review, plan_round
from .token_switch import UnconfiguredWebhook

_PRIVATE_PATHS = ("/fapi/v2/account", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders")
_ABSENT_MAC = {
    "path": "/Users/frank/quantboy-live/live_trader.py",
    "sha256": "65f1d4526cd5d3b6c2115ca8eeebd102281b27a2fbd40d0ee07054ff4891459b",
    "layout": "flat live_trader.py, no src/, no status-json-2",
    "expected_signature_if_later_proven": "req(method, path, params=None, signed=False) -> object",
    "wired": False,
}


def source_gap():
    return {
        "repo_private": "src.phase1.account_status._signed_get(path) -> object",
        "repo_public": "src.common.get(path, params=None, retries=2) -> object",
        "private_paths": list(_PRIVATE_PATHS),
        "public_kline": {"path": "/fapi/v1/klines", "interval": "15m", "limit": 20, "fields": ["open_time", "open", "high", "low", "close", "volume"]},
        "not_used": {
            "src.common.klines": "quote volume replaces base volume and the raw row is dropped",
            "src.live_trader.open_algo": "errors become an empty list",
        },
        "mac_flat_source_absent": dict(_ABSENT_MAC),
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
        "source_gap": source_gap(),
    }


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


def _real_public(symbol):
    if not _symbol_ok(symbol):
        raise ValueError("rejected_symbol")
    try:
        public_get = _load_public_get()
    except ImportError:
        raise ValueError("fetcher_missing")
    return public_get("/fapi/v1/klines", {"symbol": symbol, "interval": "15m", "limit": 20})


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
            review = fetch_market(private, public, candidates, now_ms)
        except Exception:
            review = {"context_sufficient": False, "missing": ["query_failed"]}
        if review.get("context_sufficient") is not True:
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
            }
        else:
            planned = plan_round(store, snapshot, prefilter, review, now=now, scan_id=scan_id, notifier=notifier)
            planned["fetch_review_called"] = True
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
