"""未啟用的正式接收核對。不建立 routine、不發 HTTP、不填判斷。

執行器超時沒有接上。這裡不證明 timeout。


五欄 trade_candidate_test 是舊測試入口，不是正式 wake 授權。
可驗收的是這個 handler。平台上的 prompt 要人自己存。有界 PUT／GET／POST 仍待批准。
"""
import fcntl
import json
import os
import secrets

from .ack_contract import cycle_key
from .box_receiver import assert_wake_bound
from .production_transport import CONTEXT_MAX_BYTES, _outside_window, _moment
from .review_context import _recomputed_context, parent_package_ready
from .sender_setup import validate_event

LEGACY_TEST_ROUTINE = {
    "owner_label": "總經理boy",
    "owner_id": "ef5f0556-2d5c-49a4-8b61-a4c5eef57780",
    "automation_id": "automation-1794023232755",
    "name": "交易候選接收測試（不下單）",
    "state": "paused_webhook",
    "contract_path_on_mac": "/workspace/trading-webhook-e2e-test-20261107",
    "contract_readable_here": False,
    "expires_taipei": "2026-11-08T00:00:00+08:00",
    "budget_exhausted": {"ids": 2, "posts": 3},
    "formal_wake_authorized": False,
}

LAYERS = {
    "handler_verifiable": True,
    "prompt_saved_by_platform": False,
    "bounded_trial_approved": False,
}


def _base(reason):
    return {
        "schema_version": 1,
        "event_type": "formal_receive_check",
        "reason": reason,
        "formal_wake_authorized": False,
        "orders_placed": False,
        "http_sent": False,
        "http_200_is_ack": False,
        "program_fills_judgment": False,
        "routine_created": False,
        "routine_modified": False,
        "parent_must_read_context": True,
        "layers": dict(LAYERS),
        "legacy_test_routine": dict(LEGACY_TEST_ROUTINE),
    }


def classify_legacy_test(document):
    """舊五欄測試。本機沒有那份 CONTRACT 檔，不猜內容，只套 repo 已有的欄位契約。"""
    if not isinstance(document, dict) or document.get("event_type") != "trade_candidate_test":
        return None
    try:
        validate_event(document)
        schema_valid = True
    except ValueError:
        schema_valid = False
    verdict = _base("legacy_test_not_formal_wake")
    verdict.update({
        "class": "legacy_trade_candidate_test",
        "schema_valid": schema_valid,
        "accepted": False,
        "budget_reusable": False,
    })
    return verdict


def _checked_delivery(document, now):
    """沿用 publisher 的完整 context 驗證。摘要一致不能代替 snapshot／行情契約。"""
    legacy = classify_legacy_test(document)
    if legacy is not None:
        return None, legacy
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > CONTEXT_MAX_BYTES:
        raise ValueError("context_too_large")
    if not isinstance(document, dict) or set(document) != {"schema_version", "event_type", "wake", "context"}:
        raise ValueError("rejected_context")
    if document.get("schema_version") != 1 or document.get("event_type") != "evaluation_delivery":
        raise ValueError("rejected_context")
    context = document.get("context")
    digest, problem = _recomputed_context(context)
    if problem:
        raise ValueError(problem)
    if _outside_window(_moment(context.get("sent_at")), now):
        raise ValueError("expired")
    assert_wake_bound(document.get("wake"), context)
    return {
        "event_id": context["event_id"],
        "evaluation_id": context["evaluation_id"],
        "envelope_sha256": digest,
    }, None


def verify_formal_delivery(document, now, seen=None):
    """離線核對 {wake, context}。不呼叫父 AI，也不把 HTTP 200 當成 ACK。"""
    checked, legacy = _checked_delivery(document, now)
    if legacy is not None:
        return legacy
    duplicate = False
    if isinstance(seen, dict) and checked["event_id"] in seen:
        if seen[checked["event_id"]] != checked["envelope_sha256"]:
            raise ValueError("immutable_conflict")
        duplicate = True
    verdict = _base("delivery_schema_accepted")
    verdict.update({
        "class": "evaluation_delivery",
        "accepted": True,
        "duplicate": duplicate,
        "event_id": checked["event_id"],
        "evaluation_id": checked["evaluation_id"],
        "envelope_sha256": checked["envelope_sha256"],
        "live_send": False,
        "trade_authorized": False,
        "durable": False,
        "ack": False,
        "timeout_executor": False,
    })
    return verdict


def _inbox_lock(root, event_id):
    cycle_key(event_id)
    path = os.path.join(str(root), "formal-" + event_id + ".claim")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _unlock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_formal(store, event_id):
    with store.exclusive():
        store.reload()
        row = (store.state.get("formal_inbox") or {}).get(event_id)
    return row if isinstance(row, dict) else None


def _row_verdict(row, *, duplicate, parent_called, claim_held=False):
    status = row.get("status") if isinstance(row, dict) else "pending"
    verdict = _base("publish_ready" if status == "publish_ready" else "parent_pending")
    verdict.update({
        "class": "evaluation_delivery",
        "accepted": True,
        "duplicate": duplicate,
        "claim_held": claim_held,
        "status": status,
        "event_id": row.get("event_id") if isinstance(row, dict) else None,
        "evaluation_id": row.get("evaluation_id") if isinstance(row, dict) else None,
        "envelope_sha256": row.get("digest") if isinstance(row, dict) else None,
        "parent_called": parent_called,
        "ack": status == "publish_ready",
        "live_send": False,
        "trade_authorized": False,
        "durable": True,
        "timeout_executor": False,
    })
    return verdict


def receive_delivery(store, document, now, parent=None):
    """寫進 Store。沒有父 callback 不假 ACK。程式不生成 judgment。"""
    checked, legacy = _checked_delivery(document, now)
    if legacy is not None:
        return legacy
    fd = _inbox_lock(store.root, checked["event_id"])
    if fd is None:
        row = read_formal(store, checked["event_id"]) or {
            "status": "pending", "event_id": checked["event_id"],
            "evaluation_id": checked["evaluation_id"], "digest": checked["envelope_sha256"],
        }
        verdict = _row_verdict(row, duplicate=False, parent_called=False, claim_held=True)
        verdict["ack"] = False
        return verdict
    try:
        with store.exclusive():
            store.reload()
            inbox = store.state.setdefault("formal_inbox", {})
            prior = inbox.get(checked["event_id"])
            if isinstance(prior, dict) and prior.get("digest") != checked["envelope_sha256"]:
                raise ValueError("immutable_conflict")
            if not isinstance(prior, dict):
                inbox[checked["event_id"]] = {
                    "status": "pending",
                    "event_id": checked["event_id"],
                    "evaluation_id": checked["evaluation_id"],
                    "digest": checked["envelope_sha256"],
                    "parent_invoked": False,
                }
                store.save()
                prior = inbox[checked["event_id"]]
                duplicate = False
            else:
                duplicate = True
            status = prior.get("status")
            invoked = prior.get("parent_invoked") is True
        if status == "publish_ready" or invoked or not callable(parent):
            return _row_verdict(read_formal(store, checked["event_id"]), duplicate=duplicate, parent_called=False)
        with store.exclusive():
            store.reload()
            row = store.state["formal_inbox"][checked["event_id"]]
            if row.get("parent_invoked") is True or row.get("status") == "publish_ready":
                return _row_verdict(row, duplicate=True, parent_called=False)
            row["parent_invoked"] = True
            store.save()
        returned = parent(document)
        ready = False
        if isinstance(returned, dict):
            package = parent_package_ready(returned.get("judgment"), returned.get("trace"), document.get("context"))
            ready = package.get("ready") is True
        if ready:
            with store.exclusive():
                store.reload()
                row = store.state["formal_inbox"][checked["event_id"]]
                if row.get("digest") == checked["envelope_sha256"] and row.get("status") == "pending":
                    row["status"] = "publish_ready"
                    store.save()
        return _row_verdict(read_formal(store, checked["event_id"]), duplicate=duplicate, parent_called=True)
    finally:
        _unlock(fd)
