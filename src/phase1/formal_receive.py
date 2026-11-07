"""未啟用的正式接收核對。不建立 routine、不發 HTTP、不填判斷。

執行器超時沒有接上。這裡不證明 timeout。


五欄 trade_candidate_test 是舊測試入口，不是正式 wake 授權。
可驗收的是這個 handler。平台上的 prompt 要人自己存。有界 PUT／GET／POST 仍待批准。
"""
import fcntl
import json
import os

from .ack_contract import cycle_key
from .box_receiver import assert_wake_bound
from .production_transport import CONTEXT_MAX_BYTES, _outside_window, _moment
from .review_context import _recomputed_context
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


def _row_verdict(row, *, duplicate, parent_called, claim_held=False, accepted=True):
    status = row.get("status") if isinstance(row, dict) else "pending"
    verdict = _base("publish_ready" if status == "publish_ready" else "parent_pending")
    verdict.update({
        "class": "evaluation_delivery",
        "accepted": accepted and not claim_held,
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


def _formal_dir(root, event_id):
    cycle_key(event_id)
    return os.path.join(str(root), "formal-files", event_id)


def _formal_paths(root, event_id):
    directory = _formal_dir(root, event_id)
    return {
        "context": os.path.join(directory, "context.json"),
        "judgment": os.path.join(directory, "judgment.json"),
        "trace": os.path.join(directory, "trace.json"),
    }


def _write_json(path, document):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_parent_files(store, event_id):
    paths = _formal_paths(store.root, event_id)
    if not all(os.path.isfile(path) for path in paths.values()):
        return None
    return {name: _read_json(path) for name, path in paths.items()}


def _offline_publisher_config():
    return {"installation_id": "local-offline", "enabled": False, "live_send": False}


def _publish_files(store, event_id):
    """讀已寫入的檔，走既有 publisher_document。不發 HTTP。"""
    from .gateway_client import publisher_document
    paths = _formal_paths(store.root, event_id)
    return publisher_document(
        _offline_publisher_config(), event_id, paths["judgment"], paths["trace"], paths["context"],
    )


def receive_delivery(store, document, now, parent=None):
    """寫進 Store。沒有父 callback 不假 ACK。程式不生成 judgment。"""
    checked, legacy = _checked_delivery(document, now)
    if legacy is not None:
        return legacy
    fd = _inbox_lock(store.root, checked["event_id"])
    if fd is None:
        return _busy_delivery(store, checked)
    try:
        with store.exclusive():
            store.reload()
            inbox = store.state.setdefault("formal_inbox", {})
            prior = inbox.get(checked["event_id"])
            if isinstance(prior, dict) and (
                prior.get("digest") != checked["envelope_sha256"] or prior.get("evaluation_id") != checked["evaluation_id"]
            ):
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
                _write_json(_formal_paths(store.root, checked["event_id"])["context"], document["context"])
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
        returned = parent(document) if callable(parent) else None
        if isinstance(returned, dict) and isinstance(returned.get("judgment"), dict) and isinstance(returned.get("trace"), dict):
            _install_parent_files(store, checked["event_id"], returned["judgment"], returned["trace"])
        return _row_verdict(read_formal(store, checked["event_id"]), duplicate=duplicate, parent_called=True)
    finally:
        _unlock(fd)


def _busy_delivery(store, checked):
    """claim 還在時也要核對已記住的 evaluation／digest。衝突不能接受。"""
    row = read_formal(store, checked["event_id"])
    if isinstance(row, dict) and (
        row.get("digest") != checked["envelope_sha256"] or row.get("evaluation_id") != checked["evaluation_id"]
    ):
        raise ValueError("immutable_conflict")
    held = row or {
        "status": "pending",
        "event_id": checked["event_id"],
        "evaluation_id": None,
        "digest": None,
    }
    verdict = _row_verdict(held, duplicate=False, parent_called=False, claim_held=True, accepted=False)
    verdict["ack"] = False
    return verdict


def _install_parent_files(store, event_id, judgment, trace):
    """呼叫端已持有 claim。不生成 judgment，只驗證並保存父層交來的檔。"""
    paths = _formal_paths(store.root, event_id)
    with store.exclusive():
        store.reload()
        row = (store.state.get("formal_inbox") or {}).get(event_id)
        if not isinstance(row, dict) or row.get("status") != "pending":
            return _row_verdict(row or {"status": "pending", "event_id": event_id}, duplicate=True, parent_called=False)
    if not os.path.isfile(paths["context"]):
        raise ValueError("rejected_context")
    _write_json(paths["judgment"], judgment)
    _write_json(paths["trace"], trace)
    try:
        _publish_files(store, event_id)
    except ValueError:
        for name in ("judgment", "trace"):
            if os.path.isfile(paths[name]):
                os.remove(paths[name])
        verdict = _row_verdict(read_formal(store, event_id), duplicate=False, parent_called=False)
        verdict["ack"] = False
        verdict["reason"] = "parent_package_not_ready"
        return verdict
    with store.exclusive():
        store.reload()
        row = store.state["formal_inbox"][event_id]
        if row.get("status") == "pending":
            row["status"] = "publish_ready"
            row["judgment_path"] = paths["judgment"]
            row["trace_path"] = paths["trace"]
            row["context_path"] = paths["context"]
            store.save()
    return _row_verdict(read_formal(store, event_id), duplicate=False, parent_called=False)


def submit_parent_files(store, event_id, judgment, trace, now):
    """pending 補交父層已經寫好的 judgment／trace。不重新呼叫父 AI，也不假完成。"""
    del now
    cycle_key(event_id)
    if not isinstance(judgment, dict) or not isinstance(trace, dict):
        raise ValueError("parent_package_not_ready")
    fd = _inbox_lock(store.root, event_id)
    if fd is None:
        raise ValueError("claim_held")
    try:
        return _install_parent_files(store, event_id, judgment, trace)
    finally:
        _unlock(fd)
