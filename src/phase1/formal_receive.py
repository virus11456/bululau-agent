"""未啟用的正式接收核對。不建立 routine、不發 HTTP、不填判斷。

五欄 trade_candidate_test 是舊測試入口，不是正式 wake 授權。
可驗收的是這個 handler。平台上的 prompt 要人自己存。有界 PUT／GET／POST 仍待批准。
"""
import json

from .box_receiver import assert_wake_bound
from .production_transport import CONTEXT_MAX_BYTES, verify_context
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


def verify_formal_delivery(document, now, seen=None):
    """離線核對 {wake, context}。不呼叫父 AI，也不把 HTTP 200 當成 ACK。"""
    legacy = classify_legacy_test(document)
    if legacy is not None:
        return legacy
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > CONTEXT_MAX_BYTES:
        raise ValueError("context_too_large")
    if not isinstance(document, dict) or set(document) != {"schema_version", "event_type", "wake", "context"}:
        raise ValueError("rejected_context")
    if document.get("schema_version") != 1 or document.get("event_type") != "evaluation_delivery":
        raise ValueError("rejected_context")
    context = document.get("context")
    verified = verify_context(context, now)
    assert_wake_bound(document.get("wake"), context)
    event_id = verified["event_id"]
    digest = verified["envelope_sha256"]
    duplicate = False
    if isinstance(seen, dict) and event_id in seen:
        if seen[event_id] != digest:
            raise ValueError("immutable_conflict")
        duplicate = True
    verdict = _base("delivery_schema_accepted")
    verdict.update({
        "class": "evaluation_delivery",
        "accepted": True,
        "duplicate": duplicate,
        "event_id": event_id,
        "evaluation_id": verified["evaluation_id"],
        "envelope_sha256": digest,
        "live_send": False,
        "trade_authorized": False,
    })
    return verdict
