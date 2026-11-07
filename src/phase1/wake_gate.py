"""狀態感知喚醒。免 AI 只有一種情況：新鮮、0 倉、0 掛單、0 候選、無錯誤。

評估週期身份和候選指紋分開。manage 不經過這個閘門。
未配置 sender 時，舊 AI 輪詢維持 blocked，不送 HTTP。
"""
from .autoscan import ScanBook, freshness
from .token_switch import UnconfiguredWebhook, _identity


def _enqueue_cycle(store, evaluation_id, completed_at, reason):
    book = ScanBook(store)
    key = "cycle:" + evaluation_id
    outbox = store.state["outbox"]
    if key in outbox:
        return False
    outbox[key] = {
        "event_class": "evaluation_cycle",
        "evaluation_id": evaluation_id,
        "fingerprint": None,
        "reason": reason,
        "status": "pending",
        "scan_completed_at": completed_at,
        "notify_seen": False,
    }
    store.state["cycle_wake_count"] = int(store.state.get("cycle_wake_count") or 0) + 1
    store.trace({"kind": "cycle_wake", "evaluation_id": evaluation_id, "reason": reason})
    store.save()
    return book is not None


def decide(store, snapshot, prefilter, *, now, scan_id, notifier=None, recovery_verified=False):
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
    known = (
        snapshot.get("status") == "ok"
        and fresh["valid"]
        and not snapshot.get("manage_error")
        and not scan_error
        and positions == 0
        and orders == 0
    )
    quiet = known and not candidates
    evaluation_id = "cycle:" + scan_id
    reason = "quiet_flat_book" if quiet else _reason(snapshot, fresh, scan_error, positions, orders, candidates)
    if not quiet:
        _enqueue_cycle(store, evaluation_id, completed or now, reason)
        sender = notifier.notify({"evaluation_id": evaluation_id, "reason": reason})
    else:
        sender = {"status": "not_attempted", "delivered": False, "http_sent": False, "reason": "quiet_flat_book"}
    acked = bool(sender.get("processing_ack")) and bool(sender.get("delivered")) and not sender.get("http_sent") is None
    old_ai = "eligible_to_stop" if quiet and recovery_verified and sender.get("processing_ack") and sender.get("delivered") else "keep"
    if sender.get("status") == "blocked" or not recovery_verified:
        old_ai = "keep"
    return {
        "cycle_needs_ai": not quiet,
        "reason": reason,
        "evaluation_id": evaluation_id,
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
    if snapshot.get("status") != "ok" or not fresh.get("valid"):
        return "snapshot_" + str(fresh.get("status") or snapshot.get("status") or "unknown")
    if snapshot.get("manage_error") or scan_error:
        return "manage_or_scan_error"
    if positions not in (0,):
        return "positions_open"
    if orders not in (0,):
        return "orders_open"
    if candidates:
        return "candidates_present"
    return "not_quiet"
