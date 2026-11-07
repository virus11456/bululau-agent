"""同機處理 ACK 檔。accepted 收件不是 processed。

不讀傳輸設定，不新增 HTTP API，也不改五欄測試本體。
"""
import json
import os
import stat
from datetime import datetime, timedelta, timezone

ACCEPTED_FIELDS = ("kind", "event_id", "schema_valid", "status")
PROCESSED_FIELDS = ("kind", "event_id", "evaluation_id", "processed_at")
MAX_ACK_AGE = timedelta(minutes=15)
KNOWN_TEST_EVENT = "t-4b2c8c37a3dd22be73af862aac4dcae5"


def _parse_time(value):
    if not isinstance(value, str):
        raise ValueError("rejected_ack_time")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("rejected_ack_time")
    return parsed.astimezone(timezone.utc)


def classify_file(document):
    if not isinstance(document, dict):
        raise ValueError("rejected_ack")
    kind = document.get("kind")
    if kind == "accepted":
        if set(document) != set(ACCEPTED_FIELDS) or document.get("status") != "accepted" or document.get("schema_valid") is not True:
            raise ValueError("rejected_ack_fields")
        return "accepted"
    if kind == "processed":
        if set(document) != set(PROCESSED_FIELDS):
            raise ValueError("rejected_ack_fields")
        _parse_time(document.get("processed_at"))
        return "processed"
    raise ValueError("rejected_ack_kind")


def _event(store, event_id):
    events = store.state.setdefault("test_events", {})
    return events.setdefault(event_id, {"status": "pending", "process_count": 0, "receipt": None})


def note_accepted(store, document):
    """記錄測試收件。不把 outbox 或舊 AI 狀態改成完成。"""
    if classify_file(document) != "accepted":
        raise ValueError("rejected_ack_kind")
    row = _event(store, document["event_id"])
    if row["receipt"] is None:
        row["receipt"] = "accepted"
    row["status"] = "pending" if row["process_count"] == 0 else row["status"]
    store.save()
    return {"applied": False, "reason": "accepted_is_not_processed", "pending": row["process_count"] == 0, "old_ai_poll": "keep"}


def apply_processed_ack(store, document, *, expected_event_id, sent_at, now):
    try:
        kind = classify_file(document)
    except ValueError as exc:
        return {"applied": False, "reason": str(exc), "pending": True, "old_ai_poll": "keep", "process_count": None}
    if kind != "processed":
        return note_accepted(store, document)
    event_id = document["event_id"]
    if event_id != expected_event_id:
        return {"applied": False, "reason": "wrong_event", "pending": True, "old_ai_poll": "keep"}
    processed_at = _parse_time(document["processed_at"])
    sent = _parse_time(sent_at)
    current = _parse_time(now) if isinstance(now, str) else now.astimezone(timezone.utc)
    if processed_at < sent:
        return {"applied": False, "reason": "too_early", "pending": True, "old_ai_poll": "keep"}
    if processed_at > current + timedelta(seconds=60) or current - processed_at > MAX_ACK_AGE:
        return {"applied": False, "reason": "expired_or_future", "pending": True, "old_ai_poll": "keep"}
    key = "cycle:" + document["evaluation_id"]
    item = store.state.get("outbox", {}).get(key)
    if item is None:
        return {"applied": False, "reason": "unknown_event", "pending": True, "old_ai_poll": "keep"}
    row = _event(store, event_id)
    if row["process_count"] >= 1 or item.get("status") == "acked":
        return {"applied": False, "reason": "duplicate", "pending": False, "process_count": row["process_count"], "old_ai_poll": "keep"}
    row["process_count"] = 1
    row["status"] = "processed"
    item["status"] = "acked"
    item["processing_ack"] = True
    store.save()
    return {"applied": True, "reason": "processed", "pending": False, "process_count": 1, "old_ai_poll": "keep"}


def write_processed_file(directory, document):
    if classify_file(document) != "processed":
        raise ValueError("rejected_ack_kind")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    final = os.path.join(directory, "processed-" + document["event_id"] + ".json")
    if os.path.lexists(final) and stat.S_ISLNK(os.lstat(final).st_mode):
        raise ValueError("rejected_symlink")
    tmp = final + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, final)
    os.chmod(final, 0o600)
    return final


def load_ack(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
