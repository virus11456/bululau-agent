"""白名單行情。只收注入進來的既有查詢與公開 K 線，不寫 state、不開鎖、不讀餘額。"""
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
        if not isinstance(item, list) or len(item) < 5:
            continue
        opened = _num(item[0])
        close = _num(item[4])
        if opened is None or close is None:
            continue
        bars.append({"open_time": int(opened), "close": close})
    return bars


def _fresh(bars, now_ms):
    if not bars:
        return False
    age = now_ms - bars[-1]["open_time"]
    return 0 <= age <= _MAX_BAR_AGE_MS


def build_review(account, open_orders, algo_orders, klines, candidates, now_ms):
    """counts 不是持倉。缺 symbol、方向、數量、停損或 K 線就標記不足。"""
    rows = account.get("positions") if isinstance(account, dict) else None
    positions = [item for item in (_position(row) for row in (rows or [])) if item]
    orders = [item for item in (_order(row) for row in list(open_orders or []) + list(algo_orders or [])) if item]
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
    missing = []
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
        "context_sufficient": not missing,
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
        klines = {}
        for symbol in symbols:
            if not isinstance(symbol, str) or not symbol.isalnum():
                raise ValueError("rejected_symbol")
            klines[symbol] = public_get("/fapi/v1/klines?symbol={0}&interval=15m&limit=20".format(symbol))
    except (OSError, ValueError, TypeError):
        failed = build_review(None, [], [], {}, [], now_ms)
        failed["context_sufficient"] = False
        failed["missing"] = ["query_failed"]
        return failed
    return build_review(account, orders, algo, klines, [], now_ms)


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


def parent_package_ready(judgment, trace):
    """只檢查父層已經寫好的檔。不代填決定或 trace。"""
    if not isinstance(judgment, dict) or judgment.get("program_filled") is True:
        return {"ready": False, "reason": "program_must_not_fill_judgment", "discretion_complete": False}
    if judgment.get("ai_execution_proven") is not False or judgment.get("orders_placed") is not False:
        return {"ready": False, "reason": "not_a_live_order", "discretion_complete": False}
    if judgment.get("decision") not in ("keep_original_ai", "no_trade"):
        return {"ready": False, "reason": "decision_not_allowed", "discretion_complete": False}
    if not isinstance(trace, dict) or not isinstance(trace.get("trace"), str) or not trace["trace"].strip():
        return {"ready": False, "reason": "parent_trace_missing", "discretion_complete": False}
    if trace.get("event_id") != judgment.get("event_id"):
        return {"ready": False, "reason": "binding_mismatch", "discretion_complete": False}
    return {
        "ready": True,
        "reason": "parent_package_present",
        "discretion_complete": False,
        "keep_original_ai_is_not_a_fill": judgment.get("decision") == "keep_original_ai",
    }
