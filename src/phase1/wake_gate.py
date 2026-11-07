"""狀態感知喚醒。免 AI 只有一種情況：新鮮、0 倉、0 掛單、0 候選、無錯誤。

評估週期身份和候選指紋分開。manage 不經過這個閘門。
未配置 sender 時，舊 AI 輪詢維持 blocked，不送 HTTP。
"""
import secrets

from .ack_contract import cycle_key

ALLOWED_WAKE_REASONS = (
    "snapshot_missing",
    "snapshot_stale",
    "snapshot_blocked_clock_skew",
    "snapshot_unknown",
    "manage_or_scan_error",
    "positions_open",
    "orders_open",
    "candidates_present",
    "not_quiet",
)
from .autoscan import ScanBook, freshness
from .token_switch import UnconfiguredWebhook, _identity


def _enqueue_cycle(store, evaluation_id, completed_at, reason):
    """已存在的鍵不覆寫，避免把 pending 清掉。"""
    try:
        key = cycle_key(evaluation_id)
    except ValueError:
        return None
    ScanBook(store)
    outbox = store.state.setdefault("outbox", {})
    current = outbox.get(key)
    if isinstance(current, dict):
        return current
    item = {
        "event_class": "evaluation_cycle",
        "evaluation_id": evaluation_id,
        "event_id": "c-" + secrets.token_hex(16),
        "fingerprint": None,
        "reason": reason,
        "status": "pending",
        "processing_ack": False,
        "scan_completed_at": completed_at,
        "notify_seen": False,
    }
    outbox[key] = item
    store.state["cycle_wake_count"] = int(store.state.get("cycle_wake_count") or 0) + 1
    store.trace({"kind": "cycle_wake", "evaluation_id": evaluation_id, "event_id": item["event_id"], "reason": reason})
    return item


def _pending_event(store):
    for item in (store.state.get("outbox") or {}).values():
        if isinstance(item, dict) and item.get("status") == "pending":
            return True
    return False


def decide(store, snapshot, prefilter, *, now, scan_id, notifier=None, recovery_verified=False, mac_gate=False):
    notifier = notifier or UnconfiguredWebhook()
    completed = snapshot.get("as_of")
    fresh = freshness(completed, now) if completed else {"valid": False, "status": "missing"}
    candidates = []
    scan_error = bool(prefilter.get("error"))
    for item in prefilter.get("candidates") or []:
        ident = _identity(item)
        if ident is None:
            if isinstance(item, dict) and (item.get("fail") or item.get("blocked_by_btc_guard")):
                continue
            scan_error = True
            continue
        candidates.append(ident)
    positions = snapshot.get("positions")
    orders = snapshot.get("orders")
    queries = snapshot.get("queries") if isinstance(snapshot.get("queries"), dict) else {}
    query_names = ("account", "open_orders", "open_algo_orders")
    classes_zero = snapshot.get("open_orders") == 0 and snapshot.get("algo_orders") == 0 and orders == 0
    queries_ready = snapshot.get("queries_ok") is True and classes_zero and all(queries.get(name) is True for name in query_names)
    known = (
        snapshot.get("status") == "ok"
        and snapshot.get("status_trusted") is not False
        and fresh["valid"]
        and not snapshot.get("manage_error")
        and not scan_error
        and positions == 0
        and (queries_ready if mac_gate else orders == 0)
    )
    pending = _pending_event(store) if mac_gate else False
    quiet = known and not candidates and not pending
    try:
        cycle_key(scan_id)
        evaluation_id = scan_id
    except ValueError:
        evaluation_id = None
    if quiet:
        reason = "quiet_flat_book"
    elif mac_gate and pending and known and not candidates:
        reason = "pending_return"
    elif (
        mac_gate
        and not known
        and snapshot.get("status") == "ok"
        and fresh["valid"]
        and not snapshot.get("manage_error")
        and not scan_error
        and positions in (0, None)
        and orders in (0, None)
        and not candidates
    ):
        reason = "snapshot_unknown"
    else:
        reason = _reason(snapshot, fresh, scan_error, positions, orders, candidates)
    registered = None
    if not quiet and evaluation_id is not None:
        registered = _enqueue_cycle(store, evaluation_id, completed or now, reason)
        try:
            sender = notifier.notify({"evaluation_id": evaluation_id, "event_id": registered.get("event_id") if isinstance(registered, dict) else None, "reason": reason})
        except Exception:
            sender = {"status": "blocked", "delivered": False, "http_sent": False, "processing_ack": False, "reason": "sender_failed"}
    elif not quiet:
        sender = {"status": "blocked", "delivered": False, "http_sent": False, "processing_ack": False, "reason": "rejected_id"}
    else:
        sender = {"status": "not_attempted", "delivered": False, "http_sent": False, "reason": "quiet_flat_book"}
        if mac_gate:
            seen = store.state.setdefault("suppressed_quiet", [])
            if not any(isinstance(row, dict) and row.get("evaluation_id") == evaluation_id for row in seen):
                seen.append({
                    "evaluation_id": evaluation_id,
                    "reason": "quiet_flat_book",
                    "as_of": completed,
                    "queries": {name: True for name in query_names},
                    "positions": 0,
                    "open_orders": 0,
                    "algo_orders": 0,
                    "candidates": 0,
                })
                store.save()
    acked = bool(sender.get("processing_ack")) and bool(sender.get("delivered")) and not sender.get("http_sent") is None
    old_ai = "keep"
    return {
        "cycle_needs_ai": not quiet,
        "reason": reason,
        "evaluation_id": evaluation_id,
        "event_id": registered.get("event_id") if isinstance(registered, dict) else None,
        "registered": bool(isinstance(registered, dict) and registered.get("event_id")),
        "candidate_fingerprints": [item["symbol"] + "|" + item["setup"] + "|" + item["bar_time"] for item in candidates],
        "persistent_event": not quiet,
        "sender": sender,
        "old_ai_poll": old_ai,
        "sender_blocked": sender.get("status") == "blocked" or not recovery_verified,
        "manage_independent": True,
        "snapshot_fresh": bool(fresh.get("valid")),
        "acked": acked,
    }


def _reason(snapshot, fresh, scan_error, positions, orders, candidates):
    if snapshot.get("status") != "ok":
        return "snapshot_unknown"
    if not fresh.get("valid"):
        status = fresh.get("status")
        if status == "stale":
            return "snapshot_stale"
        if status == "blocked_clock_skew":
            return "snapshot_blocked_clock_skew"
        if status == "missing":
            return "snapshot_missing"
        return "snapshot_unknown"
    if snapshot.get("manage_error") or scan_error:
        return "manage_or_scan_error"
    if positions not in (0,):
        return "positions_open"
    if orders not in (0,):
        return "orders_open"
    if candidates:
        return "candidates_present"
    return "not_quiet"
