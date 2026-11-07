"""一輪整合：manage 先跑且不受候選限制；喚醒只用可信狀態。

不送正式持倉通道。HTTP 2xx 不會把 outbox 標成已處理。
"""
from .autoscan import fingerprint, freshness
from .manage_job import build_manage_argv
from .token_switch import _identity
from .wake_gate import decide


def _fingerprints(prefilter):
    found = []
    for item in prefilter.get("candidates") or []:
        ident = _identity(item)
        if ident is None:
            continue
        found.append(fingerprint(ident["symbol"], ident["setup"], ident["bar_time"], ident["ladder"]))
    return found


def _cycle_key(evaluation_id):
    return "cycle:" + evaluation_id


def ack_cycle(store, evaluation_id, *, processing_ack):
    """只有明確的處理 ACK 才能離開 pending。"""
    if processing_ack is not True:
        return False
    item = store.state.get("outbox", {}).get(_cycle_key(evaluation_id))
    if item is None:
        return False
    item["status"] = "acked"
    item["processing_ack"] = True
    store.save()
    return True


def run_cycle(store, snapshot, prefilter, *, now, scan_id, manage_exit, notifier=None):
    manage_argv = build_manage_argv()
    manage_complete = (
        manage_exit == 0
        and snapshot.get("manage_complete") is True
        and not snapshot.get("manage_error")
    )
    gate_snapshot = dict(snapshot)
    if not manage_complete or snapshot.get("limit_older_than_2h"):
        gate_snapshot["manage_error"] = True
    seen = store.state.setdefault("seen_candidate_fingerprints", [])
    fingerprints = [] if prefilter.get("error") else _fingerprints(prefilter)
    new_fingerprints = [item for item in fingerprints if item not in seen]
    flat = (
        gate_snapshot.get("status") == "ok"
        and gate_snapshot.get("positions") == 0
        and gate_snapshot.get("orders") == 0
        and not gate_snapshot.get("manage_error")
    )
    prefilter_for_gate = prefilter
    if flat and not new_fingerprints:
        prefilter_for_gate = {"candidates": []}
    decision = decide(
        store, gate_snapshot, prefilter_for_gate, now=now, scan_id=scan_id, notifier=notifier,
    )
    sender = dict(decision["sender"])
    http_status = sender.get("http_status")
    if isinstance(http_status, int) and 200 <= http_status < 300:
        sender["processing_ack"] = False
        sender["http_ok_is_not_ack"] = True
    key = _cycle_key(decision["evaluation_id"])
    item = store.state.get("outbox", {}).get(key)
    if item is not None and sender.get("processing_ack") is not True:
        item["status"] = "pending"
        item["processing_ack"] = False
        store.save()
    if not decision["cycle_needs_ai"]:
        for fp in fingerprints:
            if fp not in seen:
                seen.append(fp)
        store.save()
    elif new_fingerprints and decision["reason"] == "candidates_present":
        for fp in new_fingerprints:
            if fp not in seen:
                seen.append(fp)
        store.save()
    decision["sender"] = sender
    decision["new_candidate_fingerprints"] = new_fingerprints
    decision["protection_complete"] = False if not manage_complete else bool(decision["snapshot_fresh"])
    if decision["cycle_needs_ai"] and decision["reason"] in ("manage_or_scan_error", "snapshot_missing", "snapshot_stale", "snapshot_unknown"):
        decision["protection_complete"] = False
    decision["manage_argv"] = manage_argv
    decision["old_ai_poll"] = "keep"
    decision["formal_channel"] = False
    return decision
