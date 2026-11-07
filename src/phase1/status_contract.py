"""status-json 的嚴格成功契約。文字裡的零倉零單不能單獨當成功。

不讀金鑰、不輸出餘額。limit_older_than_2h 不是這份契約已實作的老單訊號。
"""
import json

SCHEMA = 1
COMMAND = "status-json"
QUERIES = ("account", "open_orders", "open_algo_orders")
SECRET_KEYS = {
    "wallet", "equity", "balance", "available", "availableBalance",
    "authorization", "api_key", "api_secret", "totalWalletBalance",
    "totalUnrealizedProfit", "day_start_equity",
}
FAILURE_TEXT = ("查詢失敗", "HTTP 429", "BinanceError", "rate limit")


def require_account(value):
    """帳戶本體必須是 dict，positions 必須是 list。null、False、0、空字串都不是空倉。"""
    if not isinstance(value, dict):
        raise ValueError("bad_account")
    rows = value.get("positions")
    if not isinstance(rows, list):
        raise ValueError("bad_account")
    for row in rows:
        if not isinstance(row, dict) or "positionAmt" not in row:
            raise ValueError("bad_account")
        amount = row["positionAmt"]
        if isinstance(amount, bool) or amount is None or amount == "" or isinstance(amount, (list, dict)):
            raise ValueError("bad_account")
        try:
            float(amount)
        except (TypeError, ValueError):
            raise ValueError("bad_account")
    return value


def require_order_list(value):
    """只有 list 算查詢成功。空 list 表示真的沒有單。null、False、0、空字串、dict 都拒絕。"""
    if not isinstance(value, list):
        raise ValueError("bad_order_list")
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("bad_order_list")
    return value


def success_document(positions, open_orders, algo_orders, as_of):
    return {
        "schema_version": SCHEMA,
        "command": COMMAND,
        "ok": True,
        "as_of": as_of,
        "positions": positions,
        "open_orders": open_orders,
        "algo_orders": algo_orders,
        "queries": {name: True for name in QUERIES},
        "limit_older_than_2h_implemented": False,
    }


def failure_document(queries):
    return {
        "schema_version": SCHEMA,
        "command": COMMAND,
        "ok": False,
        "error": "query_failed",
        "queries": {name: queries.get(name) is True for name in QUERIES},
    }


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def output_claims_failure(text):
    blob = text or ""
    return any(mark in blob for mark in FAILURE_TEXT)


def parse_status_json(text, code, stderr=""):
    """結束碼 0 且 ok=true 且三個查詢都是 true 才可信。失敗文字或然否都不能猜成空倉。"""
    untrusted = {"trusted": False, "positions": None, "orders": None, "as_of": None}
    if code != 0 or output_claims_failure(text) or output_claims_failure(stderr):
        return untrusted
    try:
        body = json.loads(text) if isinstance(text, str) and text.strip() else None
    except json.JSONDecodeError:
        return untrusted
    if not isinstance(body, dict):
        return untrusted
    if any(key in body for key in SECRET_KEYS):
        return untrusted
    if body.get("schema_version") != SCHEMA or body.get("command") != COMMAND or body.get("ok") is not True:
        return untrusted
    queries = body.get("queries")
    if not isinstance(queries, dict) or any(queries.get(name) is not True for name in QUERIES):
        return untrusted
    positions = _count(body.get("positions"))
    regular = _count(body.get("open_orders"))
    algo = _count(body.get("algo_orders"))
    as_of = body.get("as_of")
    if positions is None or regular is None or algo is None or not isinstance(as_of, str) or not as_of:
        return untrusted
    return {
        "trusted": True,
        "positions": positions,
        "open_orders": regular,
        "algo_orders": algo,
        "orders": regular + algo,
        "queries": {name: True for name in QUERIES},
        "as_of": as_of,
    }
