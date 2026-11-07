"""離線把三個查詢 fixture 分成成功、未知、失敗。不讀 Mac、不取鎖、不打幣安。

這不是 live_trader status 的等效實作。缺的真介面標在 not_equivalent。
"""
import json

from .legacy_root import BINDINGS, DISPATCH, KNOWN_LIVE_FLAT_SHA
from .status_contract import SECRET_KEYS

QUERY_NAMES = ("account", "open_orders", "open_algo_orders")
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
)


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _classify(name, item):
    if not isinstance(item, dict):
        return "unknown", None
    state = item.get("state")
    if state == "failure":
        return "failure", None
    if state != "success":
        return "unknown", None
    count = _count(item.get(COUNT_KEY[name]))
    if count is None:
        return "unknown", None
    return "success", count


def map_queries(document):
    """任一成功且 count>0 要保護。未知或失敗都禁新倉。不宣稱 quiet 或實盤等效。"""
    if not isinstance(document, dict):
        raise ValueError("malformed_fixture")
    if any(key in document for key in SECRET_KEYS):
        raise ValueError("secret_rejected")
    queries = {}
    exposure = False
    blocked = False
    for name in QUERY_NAMES:
        state, count = _classify(name, document.get(name))
        queries[name] = {"state": state, "count": count}
        if state == "success" and count > 0:
            exposure = True
        if state != "success":
            blocked = True
    flat_known = not blocked and not exposure
    return {
        "schema_version": 1,
        "event_type": "offline_query_map",
        "queries": queries,
        "protection_required": exposure,
        "new_positions_allowed": flat_known,
        "candidate_allowed": False,
        "quiet": False,
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


def map_queries_text(raw):
    document = json.loads(raw)
    return map_queries(document)
