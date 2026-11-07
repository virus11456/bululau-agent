"""白名單行情。只收注入進來的既有查詢與公開 K 線，不寫 state、不開鎖、不讀餘額。"""
import json
import math

from .wake_gate import decide

_DROP = {
    "totalWalletBalance", "availableBalance", "walletBalance", "marginBalance",
    "unrealizedProfit", "totalUnrealizedProfit", "equity", "balance",
    "api_key", "api_secret", "authorization", "day_start_equity",
}
_MAX_BAR_AGE_MS = 20 * 60 * 1000


def _num(value):
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _position(row):
    if not isinstance(row, dict) or any(key in row for key in _DROP):
        row = {key: value for key, value in (row or {}).items() if key not in _DROP} if isinstance(row, dict) else None
    if not isinstance(row, dict):
        return None
    amount = _num(row.get("positionAmt"))
    symbol = row.get("symbol")
    if amount is None or amount == 0 or not isinstance(symbol, str) or not symbol.endswith("USDT"):
        return None
    return {"symbol": symbol, "side": "LONG" if amount > 0 else "SHORT", "qty": abs(amount), "entry": _num(row.get("entryPrice"))}


def _order(row):
    if not isinstance(row, dict):
        return None
    symbol = row.get("symbol")
    side = row.get("side")
    if not isinstance(symbol, str) or side not in ("BUY", "SELL"):
        return None
    stop = _num(row.get("stopPrice"))
    if stop is None:
        stop = _num(row.get("triggerPrice"))
    qty = _num(row.get("origQty"))
    if qty is None:
        qty = _num(row.get("quantity"))
    return {
        "symbol": symbol,
        "side": side,
        "type": str(row.get("type") or row.get("orderType") or "")[:32],
        "qty": qty,
        "stop": stop,
        "close_position": row.get("closePosition") is True,
    }


def _bars(raw):
    bars = []
    if not isinstance(raw, list):
        return bars
    for item in raw[-20:]:
        if not isinstance(item, list) or len(item) < 6:
            continue
        opened, opened_price, high, low, close, volume = (_num(item[index]) for index in range(6))
        if None in (opened, opened_price, high, low, close, volume):
            continue
        bars.append({
            "open_time": int(opened),
            "open": opened_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
    return bars


def _fresh(bars, now_ms):
    if not bars:
        return False
    age = now_ms - bars[-1]["open_time"]
    return 0 <= age <= _MAX_BAR_AGE_MS


def build_review(account, open_orders, algo_orders, klines, candidates, now_ms, queries_present=True):
    """counts 不是持倉。缺 symbol、方向、數量、停損或完整 K 線就標記不足。"""
    missing = []
    if queries_present is not True:
        missing.append("query_failed")
    if not isinstance(account, dict) or not isinstance(account.get("positions"), list):
        missing.append("account_query")
        rows = []
    else:
        rows = account["positions"]
    if not isinstance(open_orders, list) or not isinstance(algo_orders, list):
        missing.append("order_query")
        order_rows = []
    else:
        order_rows = list(open_orders) + list(algo_orders)
    positions = []
    for row in rows:
        amount = _num(row.get("positionAmt")) if isinstance(row, dict) else None
        if not isinstance(row, dict) or "positionAmt" not in row or amount is None:
            missing.append("bad_position")
            continue
        if amount == 0:
            continue
        item = _position(row)
        if item:
            positions.append(item)
        else:
            missing.append("bad_position")
    orders = [item for item in (_order(row) for row in order_rows) if item]
    cleaned = {}
    for symbol, raw in (klines or {}).items():
        if isinstance(symbol, str):
            cleaned[symbol] = _bars(raw)
    plans = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        plans.append({
            "symbol": item.get("symbol") if isinstance(item.get("symbol"), str) else "",
            "setup": str(item.get("setup") or "")[:64],
            "plan": item.get("plan") if isinstance(item.get("plan"), str) else "",
            "stop": _num(item.get("stop")),
            "entry": _num(item.get("entry")),
        })
    for pos in positions:
        if pos["qty"] <= 0:
            missing.append("qty:" + pos["symbol"])
        if not any(order["symbol"] == pos["symbol"] and order["stop"] for order in orders):
            missing.append("stop:" + pos["symbol"])
        if not _fresh(cleaned.get(pos["symbol"]) or [], now_ms):
            missing.append("klines:" + pos["symbol"])
    for plan in plans:
        if not plan["symbol"] or not (plan["plan"] or plan["stop"]):
            missing.append("candidate_plan")
        elif not _fresh(cleaned.get(plan["symbol"]) or [], now_ms):
            missing.append("klines:" + plan["symbol"])
    return {
        "schema_version": 1,
        "event_type": "review_context",
        "positions": positions,
        "orders": orders,
        "klines": cleaned,
        "candidates": plans,
        "context_sufficient": not missing and queries_present is True,
        "missing": missing,
        "counts_are_sufficient": False,
        "discretion_complete": False,
        "trading_discretion_replaced": False,
        "balance_included": False,
    }


def fetch_review(private_get, public_get, symbols, now_ms):
    """走注入的既有私有查詢與公開 K 線。沒有注入就不連線。"""
    if private_get is None or public_get is None:
        empty = build_review(None, [], [], {}, [], now_ms)
        empty.update(_gap("fetcher_missing"))
        return empty
    try:
        account = private_get("/fapi/v2/account")
        orders = private_get("/fapi/v1/openOrders")
        algo = private_get("/fapi/v1/openAlgoOrders")
        if account is None or orders is None or algo is None:
            failed = build_review(None, None, None, {}, [], now_ms, queries_present=False)
            return failed
        preview = build_review(account, orders, algo, {}, [], now_ms, queries_present=True)
        needed = []
        for pos in preview["positions"]:
            if pos["symbol"] not in needed:
                needed.append(pos["symbol"])
        for symbol in symbols or []:
            if not isinstance(symbol, str) or not symbol.isalnum():
                raise ValueError("rejected_symbol")
            if symbol not in needed:
                needed.append(symbol)
        klines = {}
        for symbol in needed:
            klines[symbol] = public_get("/fapi/v1/klines?symbol={0}&interval=15m&limit=20".format(symbol))
    except (OSError, ValueError, TypeError):
        failed = build_review(None, None, None, {}, [], now_ms, queries_present=False)
        failed["missing"] = ["query_failed"]
        return failed
    return build_review(account, orders, algo, klines, [], now_ms, queries_present=True)


def _gap(reason):
    return {"context_sufficient": False, "missing": [reason]}


def plan_round(store, snapshot, prefilter, review, *, now, scan_id, notifier):
    """quiet 在 wake 之前。不足就留原 AI。本程式不寫判斷、不開第二排程。"""
    decision = decide(store, snapshot, prefilter, now=now, scan_id=scan_id, notifier=notifier, mac_gate=True)
    quiet = decision["reason"] == "quiet_flat_book"
    sufficient = isinstance(review, dict) and review.get("context_sufficient") is True
    if isinstance(review, dict):
        detail_positions = review.get("positions") or []
        detail_orders = review.get("orders") or []
        if type(snapshot.get("positions")) is not int or snapshot.get("positions") != len(detail_positions):
            sufficient = False
        if type(snapshot.get("orders")) is not int or snapshot.get("orders") != len(detail_orders):
            sufficient = False
    else:
        sufficient = False
    return {
        "quiet": quiet,
        "wake_sent": False,
        "notifier_called": not quiet,
        "cycle_needs_ai": decision["cycle_needs_ai"],
        "reason": decision["reason"],
        "event_id": decision.get("event_id"),
        "old_ai_poll": "keep",
        "context_sufficient": sufficient,
        "missing": [] if not isinstance(review, dict) else list(review.get("missing") or []),
        "discretion_complete": False,
        "trading_discretion_replaced": False,
        "parent_writes_judgment": "judgments-in/{event_id}.json",
        "parent_writes_trace": "judgments-in/{event_id}.trace.json",
        "program_fills_judgment": False,
        "second_schedule": False,
        "routine_stopped": False,
    }


def _bound_id(value):
    return isinstance(value, str) and bool(value) and value.lower() != "none"


_CONTEXT_FIELDS = (
    "schema_version", "event_type", "event_id", "evaluation_id", "envelope_sha256",
    "sent_at", "producer_store_id", "envelope", "automatic_transport",
)
_ENVELOPE_FIELDS = (
    "schema_version", "event_type", "event_id", "evaluation_id", "sent_at", "reason",
    "freshness", "snapshot", "candidates", "orders_allowed", "test_channel", "position_channel",
)
_FRESHNESS_FIELDS = ("valid", "status", "age_seconds")
_FRESHNESS_STATUS = ("fresh", "stale", "missing", "blocked_clock_skew")
_SNAPSHOT_FIELDS = (
    "status", "as_of", "positions", "orders", "manage_error", "manage_complete",
    "status_trusted", "limit_older_than_2h_implemented",
)
_CANDIDATE_FIELDS = ("symbol", "setup", "bar_time", "ladder")
_CANDIDATE_OPTIONAL = (
    "tf", "bar", "entry", "stop", "target", "rr", "plan", "sizing", "side", "vol_x", "rs24", "rs4h",
)


def _exact_int(value):
    return type(value) is int


def _review_scalar(value):
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float, str)):
        return True
    if isinstance(value, list) and len(value) <= 8 and all(
        not isinstance(item, bool) and isinstance(item, (int, float, str)) for item in value
    ):
        return True
    return False


def _nested_envelope(envelope):
    """snapshot、freshness、candidates 的型別與必填。合法摘要不能代替這個契約。"""
    freshness = envelope.get("freshness")
    if not isinstance(freshness, dict) or set(freshness) != set(_FRESHNESS_FIELDS):
        return "rejected_context"
    if type(freshness.get("valid")) is not bool or freshness.get("status") not in _FRESHNESS_STATUS:
        return "rejected_context"
    if not _exact_int(freshness.get("age_seconds")):
        return "rejected_context"
    snapshot = envelope.get("snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != set(_SNAPSHOT_FIELDS):
        return "rejected_context"
    if not isinstance(snapshot.get("status"), str) or not snapshot.get("status"):
        return "rejected_context"
    as_of = snapshot.get("as_of")
    if as_of is not None and not isinstance(as_of, str):
        return "rejected_context"
    for key in ("positions", "orders"):
        if not _exact_int(snapshot.get(key)) or snapshot.get(key) < 0:
            return "rejected_context"
    for key in ("manage_error", "manage_complete", "status_trusted"):
        if type(snapshot.get(key)) is not bool:
            return "rejected_context"
    if snapshot.get("limit_older_than_2h_implemented") is not False:
        return "rejected_context"
    candidates = envelope.get("candidates")
    if not isinstance(candidates, list):
        return "rejected_context"
    for item in candidates:
        if not isinstance(item, dict):
            return "rejected_context"
        if any(key not in item for key in _CANDIDATE_FIELDS):
            return "rejected_context"
        if any(key not in _CANDIDATE_FIELDS and key not in _CANDIDATE_OPTIONAL for key in item):
            return "rejected_context"
        if not all(isinstance(item.get(key), str) for key in ("symbol", "setup", "bar_time")):
            return "rejected_context"
        ladder = item.get("ladder")
        if not isinstance(ladder, list) or len(ladder) > 8 or any(not _review_scalar(step) or isinstance(step, str) for step in ladder):
            return "rejected_context"
        for key in _CANDIDATE_OPTIONAL:
            if key in item and not _review_scalar(item.get(key)):
                return "rejected_context"
    return None


def _recomputed_context(context):
    """重算 envelope 內容摘要。宣告的同名摘要不能單獨通過。"""
    import hashlib
    from .canonical import canonical_json
    from .wake_gate import ALLOWED_WAKE_REASONS
    if not isinstance(context, dict) or set(context) != set(_CONTEXT_FIELDS):
        return None, "rejected_context"
    if context.get("schema_version") != 1 or context.get("event_type") != "immutable_context":
        return None, "rejected_context"
    if context.get("automatic_transport") is not False:
        return None, "rejected_context"
    if not _bound_id(context.get("event_id")) or not _bound_id(context.get("evaluation_id")):
        return None, "binding_mismatch"
    if not isinstance(context.get("sent_at"), str) or not context.get("sent_at"):
        return None, "rejected_context"
    store_id = context.get("producer_store_id")
    if not isinstance(store_id, str) or len(store_id) != 32 or any(ch not in "0123456789abcdef" for ch in store_id):
        return None, "rejected_context"
    envelope = context.get("envelope")
    if not isinstance(envelope, dict) or set(envelope) != set(_ENVELOPE_FIELDS):
        return None, "rejected_context"
    if envelope.get("schema_version") != 1 or envelope.get("event_type") != "evaluation_envelope":
        return None, "rejected_context"
    if envelope.get("reason") not in ALLOWED_WAKE_REASONS:
        return None, "rejected_context"
    if envelope.get("orders_allowed") is not False or envelope.get("test_channel") is not False or envelope.get("position_channel") is not False:
        return None, "rejected_context"
    nested = _nested_envelope(envelope)
    if nested:
        return None, nested
    if envelope.get("event_id") != context.get("event_id") or envelope.get("evaluation_id") != context.get("evaluation_id"):
        return None, "binding_mismatch"
    try:
        digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    except ValueError:
        return None, "digest_mismatch"
    if digest != context.get("envelope_sha256"):
        return None, "digest_mismatch"
    return digest, None


def parent_package_ready(judgment, trace, context=None):
    """只檢查父層已經寫好的檔。缺 event_id 不能用 None==None 通過。不代填 trace。"""
    if not isinstance(judgment, dict) or judgment.get("program_filled") is True:
        return {"ready": False, "reason": "program_must_not_fill_judgment", "discretion_complete": False}
    if not _bound_id(judgment.get("event_id")) or not _bound_id(judgment.get("evaluation_id")):
        return {"ready": False, "reason": "binding_mismatch", "discretion_complete": False}
    if judgment.get("ai_execution_proven") is not False or judgment.get("orders_placed") is not False:
        return {"ready": False, "reason": "not_a_live_order", "discretion_complete": False}
    if judgment.get("decision") not in ("keep_original_ai", "no_trade"):
        return {"ready": False, "reason": "decision_not_allowed", "discretion_complete": False}
    if not isinstance(trace, dict) or not isinstance(trace.get("trace"), str) or not trace["trace"].strip():
        return {"ready": False, "reason": "parent_trace_missing", "discretion_complete": False}
    if not _bound_id(trace.get("event_id")) or trace.get("event_id") != judgment.get("event_id"):
        return {"ready": False, "reason": "binding_mismatch", "discretion_complete": False}
    if not _bound_id(trace.get("evaluation_id")) or trace.get("evaluation_id") != judgment.get("evaluation_id"):
        return {"ready": False, "reason": "binding_mismatch", "discretion_complete": False}
    if "envelope_sha256" in trace and trace.get("envelope_sha256") != judgment.get("envelope_sha256"):
        return {"ready": False, "reason": "digest_mismatch", "discretion_complete": False}
    if context is not None:
        digest, problem = _recomputed_context(context)
        if problem:
            return {"ready": False, "reason": problem, "discretion_complete": False}
        if context.get("event_id") != judgment.get("event_id") or context.get("evaluation_id") != judgment.get("evaluation_id"):
            return {"ready": False, "reason": "binding_mismatch", "discretion_complete": False}
        if judgment.get("envelope_sha256") != digest or trace.get("envelope_sha256") != digest:
            return {"ready": False, "reason": "digest_mismatch", "discretion_complete": False}
    return {
        "ready": True,
        "reason": "parent_package_present",
        "discretion_complete": False,
        "keep_original_ai_is_not_a_fill": judgment.get("decision") == "keep_original_ai",
        "program_fills_trace": False,
    }


def read_parent_package(context, judgment_path, trace_path):
    """從實際檔案讀。不存在就不造 trace。"""
    try:
        with open(judgment_path, encoding="utf-8") as handle:
            judgment = json.load(handle)
        with open(trace_path, encoding="utf-8") as handle:
            trace = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"ready": False, "reason": "parent_trace_missing", "discretion_complete": False}
    return parent_package_ready(judgment, trace, context)


def operate_round(store, snapshot, prefilter, *, now, scan_id, notifier, private_get=None, public_get=None, symbols=None, now_ms=0):
    """可操作入口。外部發送保持關閉。quiet 在 notifier 之前。"""
    review = fetch_review(private_get, public_get, symbols or [], now_ms)
    planned = plan_round(store, snapshot, prefilter, review, now=now, scan_id=scan_id, notifier=notifier)
    planned["fetch_review_called"] = True
    planned["external_send"] = False
    planned["routine_stopped"] = False
    planned["watch_modified"] = False
    planned["wakes_saved"] = False
    return planned
