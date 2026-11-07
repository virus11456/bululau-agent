"""Box 接收端。只核對送來的上下文，不代替模型填判斷，不打幣安。

父層 routine 的順序：accept 之後用 envelope_for_model 把 envelope 交給模型，
模型回答後才呼叫 submit_model_judgment。GET /returns/<event_id> 才有回傳。
"""
import hashlib
import json
import os
import secrets

from .production_transport import WAKE_FIELDS, _canonical, _secret_in, verify_context
from .timeparse import parse_instant


def _write_new(path, raw):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = os.path.join(directory, ".{0}.{1}.tmp".format(os.path.basename(path), secrets.token_hex(4)))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _require_channels(envelope):
    test_channel = envelope.get("test_channel")
    position_channel = envelope.get("position_channel")
    if not isinstance(test_channel, bool) or not isinstance(position_channel, bool):
        raise ValueError("rejected_channel")
    return test_channel, position_channel


def assert_wake_bound(wake, context):
    """八欄逐一對上 context 與 envelope。缺欄或不一致就拒絕，不把未知改成 false。"""
    if not isinstance(wake, dict) or set(wake) != set(WAKE_FIELDS):
        raise ValueError("rejected_wake_fields")
    if not isinstance(context, dict):
        raise ValueError("rejected_context")
    envelope = context.get("envelope")
    if not isinstance(envelope, dict):
        raise ValueError("rejected_context")
    if wake.get("schema_version") != 1 or context.get("schema_version") != 1 or envelope.get("schema_version") != 1:
        raise ValueError("binding_mismatch")
    if wake.get("event_type") != "evaluation_wake" or context.get("event_type") != "immutable_context" or envelope.get("event_type") != "evaluation_envelope":
        raise ValueError("binding_mismatch")
    for key in ("event_id", "evaluation_id", "sent_at"):
        if wake.get(key) != context.get(key) or wake.get(key) != envelope.get(key):
            raise ValueError("binding_mismatch")
    if wake.get("reason") != envelope.get("reason"):
        raise ValueError("binding_mismatch")
    test_channel, position_channel = _require_channels(envelope)
    if wake.get("test_channel") is not test_channel or wake.get("position_channel") is not position_channel:
        raise ValueError("binding_mismatch")
    if envelope.get("orders_allowed") is not False:
        raise ValueError("orders_not_allowed")


def accept_delivery(document, now, inbox):
    """接收 {wake, context}。wake 必須剛好八欄，而且和 envelope 逐欄相同。"""
    if not isinstance(document, dict) or set(document) != {"schema_version", "event_type", "wake", "context"}:
        raise ValueError("rejected_context")
    if document.get("schema_version") != 1 or document.get("event_type") != "evaluation_delivery":
        raise ValueError("rejected_context")
    wake = document.get("wake")
    context = document.get("context")
    verified = verify_context(context, now)
    assert_wake_bound(wake, context)
    directory = os.path.join(inbox, "inbox")
    path = os.path.join(directory, verified["event_id"] + ".json")
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if os.path.isfile(path):
        existing = json.loads(_read(path).decode("utf-8"))
        if existing.get("context", {}).get("envelope_sha256") != verified["envelope_sha256"]:
            raise ValueError("immutable_conflict")
        return {"accepted": True, "duplicate": True, "processed": False, "ai_execution_proven": False}
    _write_new(path, raw)
    return {"accepted": True, "duplicate": False, "processed": False, "ai_execution_proven": False}


def accept_context(document, now, inbox):
    verified = verify_context(document, now)
    directory = os.path.join(inbox, "inbox")
    path = os.path.join(directory, verified["event_id"] + ".json")
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if os.path.isfile(path):
        existing = json.loads(_read(path).decode("utf-8"))
        if existing.get("envelope_sha256") != verified["envelope_sha256"]:
            raise ValueError("immutable_conflict")
        return {"accepted": True, "duplicate": True, "processed": False, "ai_execution_proven": False}
    _write_new(path, raw)
    return {"accepted": True, "duplicate": False, "processed": False, "ai_execution_proven": False}


def envelope_for_model(inbox, event_id):
    path = os.path.join(inbox, "inbox", event_id + ".json")
    if not os.path.isfile(path):
        raise ValueError("context_missing")
    document = json.loads(_read(path).decode("utf-8"))
    if document.get("event_type") == "evaluation_delivery":
        envelope = (document.get("context") or {}).get("envelope")
    else:
        envelope = document.get("envelope")
    if not isinstance(envelope, dict) or _secret_in(envelope):
        raise ValueError("rejected_context")
    return envelope


def _basis(envelope):
    return {
        "reason": envelope.get("reason"),
        "as_of": envelope.get("snapshot", {}).get("as_of"),
        "positions": envelope.get("snapshot", {}).get("positions"),
        "orders": envelope.get("snapshot", {}).get("orders"),
        "freshness_status": envelope.get("freshness", {}).get("status"),
        "candidate_count": len(envelope.get("candidates") or []),
    }


def submit_model_judgment(inbox, judgment, now):
    """判斷內容必須由呼叫端傳入。這個函式不決定 keep 或 no_trade。"""
    if not isinstance(judgment, dict) or judgment.get("ai_execution_proven") is not False:
        raise ValueError("ai_not_proven_by_attestation")
    if judgment.get("orders_placed") is not False or judgment.get("decision") not in ("keep_original_ai", "no_trade"):
        raise ValueError("orders_not_allowed")
    event_id = judgment.get("event_id")
    envelope = envelope_for_model(inbox, event_id)
    if judgment.get("evaluation_id") != envelope.get("evaluation_id"):
        raise ValueError("binding_mismatch")
    digest = hashlib.sha256(_canonical(envelope)).hexdigest()
    if judgment.get("envelope_sha256") != digest:
        raise ValueError("digest_mismatch")
    basis = judgment.get("basis")
    expected = _basis(envelope)
    if not isinstance(basis, dict) or set(basis) != set(expected) or any(basis.get(key) != value for key, value in expected.items()):
        raise ValueError("rejected_basis")
    completed = parse_instant(judgment.get("completed_at"))
    sent = parse_instant(envelope.get("sent_at"))
    current = parse_instant(now)
    if completed.tzinfo is None or sent.tzinfo is None or current.tzinfo is None:
        raise ValueError("rejected_ack_time")
    if completed < sent:
        raise ValueError("expired")
    from .production_transport import _outside_window
    if _outside_window(completed, now):
        raise ValueError("expired")
    stored = json.loads(_read(os.path.join(inbox, "inbox", event_id + ".json")).decode("utf-8"))
    if stored.get("event_type") == "evaluation_delivery":
        store_id = (stored.get("context") or {}).get("producer_store_id")
    else:
        store_id = stored.get("producer_store_id")
    document = {
        "schema_version": 1,
        "event_type": "model_return",
        "event_id": event_id,
        "evaluation_id": judgment["evaluation_id"],
        "envelope_sha256": digest,
        "producer_store_id": store_id,
        "judgment": judgment,
        "ai_execution_proven": False,
        "program_filled": False,
    }
    path = os.path.join(inbox, "returns", event_id + ".json")
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if os.path.isfile(path):
        if _read(path).strip() != raw:
            raise ValueError("immutable_conflict")
        return {"returned": True, "duplicate": True, "ai_execution_proven": False}
    _write_new(path, raw)
    return {"returned": True, "duplicate": False, "ai_execution_proven": False}


def read_return(inbox, event_id):
    path = os.path.join(inbox, "returns", event_id + ".json")
    if not os.path.isfile(path):
        raise ValueError("judgment_not_ready")
    return json.loads(_read(path).decode("utf-8"))


def main(argv):
    if argv[:1] == ["box-show"] and len(argv) == 5 and argv[1] == "--inbox" and argv[3] == "--event-id":
        try:
            print(json.dumps(envelope_for_model(argv[2], argv[4]), ensure_ascii=False))
        except ValueError as exc:
            print(json.dumps({"reason": str(exc), "ai_execution_proven": False}, ensure_ascii=False))
            return 1
        return 0
    if argv[:1] == ["box-accept"] and len(argv) == 7 and argv[1] == "--inbox" and argv[3] == "--file" and argv[5] == "--now":
        try:
            with open(argv[4], encoding="utf-8") as handle:
                result = accept_delivery(json.load(handle), argv[6], argv[2])
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "rejected_context"
            print(json.dumps({"accepted": False, "reason": reason}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print("用法: python3 -m src.phase1.box_receiver box-accept --inbox DIR --file DELIVERY --now TIME | box-show --inbox DIR --event-id EVENT")
    return 2


class ContextHandler(object):
    """給測試與父層自架的接收函式。不是對外公開的服務。"""

    def __init__(self, inbox, now, authorization):
        self.inbox = inbox
        self.now = now
        self.authorization = authorization

    def post_context(self, body, authorization):
        if authorization != self.authorization:
            return 401, {"accepted": False, "reason": "auth_rejected", "ai_execution_proven": False}
        try:
            document = json.loads(body.decode("utf-8"))
            result = accept_context(document, self.now, self.inbox)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return 400, {"accepted": False, "reason": "rejected_context", "ai_execution_proven": False}
        result["processed"] = False
        return 200, result

    def get_return(self, event_id, authorization):
        if authorization != self.authorization:
            return 401, {"reason": "auth_rejected"}
        try:
            return 200, read_return(self.inbox, event_id)
        except ValueError as exc:
            return 404, {"reason": str(exc), "ai_execution_proven": False}


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
