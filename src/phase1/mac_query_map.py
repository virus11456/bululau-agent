"""離線對照三個查詢。明細沿既有 account／openOrders／openAlgoOrders 契約。

不讀 Mac、不取鎖、不打幣安。純 count fixture 不是真查詢證明。
"""
import json

from .legacy_root import BINDINGS, DISPATCH, KNOWN_LIVE_FLAT_SHA
from .review_context import _num, _order, _position, exact_schema_version
from .status_contract import SECRET_KEYS, require_account, require_order_list

QUERY_NAMES = ("account", "open_orders", "open_algo_orders")
DETAIL_EVENT = "offline_query_details"
COUNT_KEY = {"account": "positions", "open_orders": "count", "open_algo_orders": "count"}
NOT_EQUIVALENT = (
    "live_trader_not_executed",
    "mac_lock_not_taken",
    "state_json_not_read",
    "current_mac_sha_not_reverified",
    "status_text_is_not_this_schema",
    "manage_halt_rules_not_evaluated",
    "two_hour_cancel_not_performed",
    "orchestrate_cli_has_no_callbacks",
    "cancel_capability_not_verified",
)


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _base(**extra):
    body = {
        "schema_version": 1,
        "event_type": "offline_query_map",
        "queries": {name: {"state": "unknown", "count": None} for name in QUERY_NAMES},
        "protection_required": False,
        "new_positions_allowed": False,
        "candidate_allowed": False,
        "quiet": False,
        "count_only": False,
        "detail_proven": False,
        "live_equivalent": False,
        "mac_executed": False,
        "http_sent": False,
        "flock_taken": False,
        "state_bytes_read": False,
        "known_flat_sha": KNOWN_LIVE_FLAT_SHA,
        "known_flat_sha_reverified": False,
        "lock_boundary": {
            "home_expr": BINDINGS["HOME"],
            "lock_expr": BINDINGS["LOCK"],
            "protocol": "fcntl.LOCK_EX|fcntl.LOCK_NB",
            "covers": list(DISPATCH),
            "taken": False,
        },
        "files_invented": False,
        "not_equivalent": list(NOT_EQUIVALENT),
    }
    body.update(extra)
    return body


def _has_detail(document):
    account = document.get("account")
    if isinstance(account, dict) and isinstance(account.get("positions"), list):
        return True
    return isinstance(document.get("open_orders"), list) or isinstance(document.get("open_algo_orders"), list)


def _schema_rejected(document):
    if "schema_version" in document and not exact_schema_version(document.get("schema_version")):
        return True
    if "event_type" in document and document.get("event_type") != DETAIL_EVENT:
        return True
    return False


def _account_detail(account):
    try:
        require_account(account)
    except ValueError:
        return "unknown", None, False
    exposure = False
    bad = False
    mapped = 0
    for row in account["positions"]:
        amount = _num(row.get("positionAmt")) if isinstance(row, dict) else None
        if amount is None:
            bad = True
            continue
        if amount == 0:
            continue
        exposure = True
        if _position(row):
            mapped += 1
        else:
            bad = True
    if bad:
        return "unknown", None, exposure
    return "success", mapped, exposure


def _order_detail(raw):
    try:
        require_order_list(raw)
    except ValueError:
        return "unknown", None, False
    exposure = len(raw) > 0
    bad = False
    for row in raw:
        item = _order(row)
        symbol = item.get("symbol") if isinstance(item, dict) else None
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            bad = True
    if bad:
        return "unknown", None, exposure
    return "success", len(raw), exposure


def _from_details(document):
    if not exact_schema_version(document.get("schema_version")) or document.get("event_type") != DETAIL_EVENT:
        return _base(reason="schema_rejected")
    account_state, account_count, account_exposure = _account_detail(document.get("account"))
    order_state, order_count, order_exposure = _order_detail(document.get("open_orders"))
    algo_state, algo_count, algo_exposure = _order_detail(document.get("open_algo_orders"))
    queries = {
        "account": {"state": account_state, "count": account_count},
        "open_orders": {"state": order_state, "count": order_count},
        "open_algo_orders": {"state": algo_state, "count": algo_count},
    }
    exposure = account_exposure or order_exposure or algo_exposure
    unknown = any(item["state"] != "success" for item in queries.values())
    proven = not unknown
    return _base(
        queries=queries,
        protection_required=exposure,
        new_positions_allowed=proven and not exposure,
        count_only=False,
        detail_proven=proven,
        reason="detail_mapped" if proven else "detail_unknown",
    )


def _from_counts(document):
    """外部 count 不能證明空倉。count>0 仍當宣告曝險去保護。"""
    queries = {}
    exposure = False
    for name in QUERY_NAMES:
        item = document.get(name)
        if not isinstance(item, dict) or item.get("state") == "failure":
            state = "failure" if isinstance(item, dict) and item.get("state") == "failure" else "unknown"
            queries[name] = {"state": state, "count": None}
            continue
        if item.get("state") != "success":
            queries[name] = {"state": "unknown", "count": None}
            continue
        count = _count(item.get(COUNT_KEY[name]))
        if count is None:
            queries[name] = {"state": "unknown", "count": None}
            continue
        queries[name] = {"state": "success", "count": count}
        if count > 0:
            exposure = True
    return _base(
        queries=queries,
        protection_required=exposure,
        new_positions_allowed=False,
        count_only=True,
        detail_proven=False,
        reason="count_only",
    )


def map_queries(document):
    """明細成功且沒有可靠曝險才允許 new_positions。錯 schema、不完整或純 count 都禁止。"""
    if not isinstance(document, dict):
        raise ValueError("malformed_fixture")
    if any(key in document for key in SECRET_KEYS):
        raise ValueError("secret_rejected")
    if _schema_rejected(document):
        return _base(reason="schema_rejected")
    if _has_detail(document):
        return _from_details(document)
    return _from_counts(document)


def map_queries_text(raw):
    return map_queries(json.loads(raw))
