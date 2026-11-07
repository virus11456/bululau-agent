"""同機處理 ACK。accepted 收件不是 processed。

不讀 sender 設定，不 POST，也不把 event_id 拼進任意路徑。
"""
import json
import os
import re
import secrets
import stat
from datetime import datetime, timedelta, timezone

try:
    from .ack_store import AckStore
except ImportError:
    from ack_store import AckStore

ACCEPTED_FIELDS = ("kind", "event_id", "schema_valid", "status")
PROCESSED_FIELDS = ("kind", "event_id", "evaluation_id", "processed_at")
MAX_ACK_AGE = timedelta(minutes=15)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
KNOWN_TEST_EVENT = "t-4b2c8c37a3dd22be73af862aac4dcae5"


def _id(value):
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise ValueError("rejected_id")
    return value


def cycle_key(evaluation_id):
    return "cycle:" + _id(evaluation_id)


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
        _id(document.get("event_id"))
        return "accepted"
    if kind == "processed":
        if set(document) != set(PROCESSED_FIELDS):
            raise ValueError("rejected_ack_fields")
        _id(document.get("event_id"))
        _id(document.get("evaluation_id"))
        _parse_time(document.get("processed_at"))
        return "processed"
    raise ValueError("rejected_ack_kind")


def _pending(store, event_id=None):
    if event_id and store.state.get("test_events", {}).get(event_id, {}).get("process_count", 0) >= 1:
        return False
    for item in store.state.get("outbox", {}).values():
        if item.get("status") != "acked":
            return True
    return event_id is None or store.state.get("test_events", {}).get(event_id, {}).get("status") != "processed"


def _reject(reason, store, event_id=None):
    return {"applied": False, "reason": reason, "pending": _pending(store, event_id), "old_ai_poll": "keep", "process_count": store.state.get("test_events", {}).get(event_id, {}).get("process_count")}


def note_accepted(store, document):
    if classify_file(document) != "accepted":
        raise ValueError("rejected_ack_kind")
    with store.exclusive():
        store.reload()
        return _note_accepted(store, document)


def _note_accepted(store, document):
    event_id = document["event_id"]
    events = store.state.setdefault("test_events", {})
    row = events.setdefault(event_id, {"status": "pending", "process_count": 0, "receipt": None})
    before = json.dumps(store.state)
    if row["process_count"] == 0:
        row["receipt"] = "accepted"
        row["status"] = "pending"
    try:
        store.save()
    except Exception:
        store.state = json.loads(before)
        return _reject("store_failed", store, event_id)
    return {"applied": False, "reason": "accepted_is_not_processed", "pending": row["process_count"] == 0, "old_ai_poll": "keep", "process_count": row["process_count"]}


def apply_processed_ack(store, document, *, expected_event_id, sent_at, now):
    with store.exclusive():
        store.reload()
        return _apply_processed_ack(store, document, expected_event_id=expected_event_id, sent_at=sent_at, now=now)


def _apply_processed_ack(store, document, *, expected_event_id, sent_at, now):
    try:
        _id(expected_event_id)
        kind = classify_file(document)
    except ValueError as exc:
        return _reject(str(exc), store)
    if kind != "processed":
        return _note_accepted(store, document)
    event_id = document["event_id"]
    if event_id != expected_event_id:
        return _reject("wrong_event", store, expected_event_id)
    processed_at = _parse_time(document["processed_at"])
    sent = _parse_time(sent_at)
    current = _parse_time(now) if isinstance(now, str) else now.astimezone(timezone.utc)
    if processed_at < sent:
        return _reject("too_early", store, event_id)
    if processed_at > current + timedelta(seconds=60) or current - processed_at > MAX_ACK_AGE:
        return _reject("expired_or_future", store, event_id)
    evaluation_id = document["evaluation_id"]
    key = cycle_key(evaluation_id)
    item = store.state.get("outbox", {}).get(key)
    if item is None or item.get("event_id") != event_id or item.get("evaluation_id") != evaluation_id:
        return _reject("binding_mismatch", store, event_id)
    events = store.state.setdefault("test_events", {})
    row = events.setdefault(event_id, {"status": "pending", "process_count": 0, "receipt": None})
    if row["process_count"] >= 1 or item.get("status") == "acked":
        return {"applied": False, "reason": "duplicate", "pending": False, "process_count": row["process_count"], "old_ai_poll": "keep"}
    before = json.dumps(store.state)
    row["process_count"] = 1
    row["status"] = "processed"
    item["status"] = "acked"
    item["processing_ack"] = True
    try:
        store.save()
    except Exception:
        store.state = json.loads(before)
        return _reject("store_failed", store, event_id)
    return {"applied": True, "reason": "processed", "pending": False, "process_count": 1, "old_ai_poll": "keep"}


def write_processed_file(directory, document, *, processing_complete):
    if processing_complete is not True:
        raise ValueError("processing_not_complete")
    if classify_file(document) != "processed":
        raise ValueError("rejected_ack_kind")
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("rejected_symlink")
    root = os.path.realpath(directory)
    final = os.path.join(root, "processed-" + document["event_id"] + ".json")
    if os.path.realpath(os.path.dirname(final)) != root:
        raise ValueError("rejected_id")
    if os.path.lexists(final) and stat.S_ISLNK(os.lstat(final).st_mode):
        raise ValueError("rejected_symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    tmp = None
    for _ in range(8):
        tmp = os.path.join(root, ".processed-{0}.{1}.{2}.tmp".format(document["event_id"], os.getpid(), secrets.token_hex(8)))
        try:
            fd = os.open(tmp, flags, 0o600)
            break
        except FileExistsError:
            fd = None
    if fd is None:
        raise OSError("store_failed")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final)
    except Exception:
        if tmp and os.path.lexists(tmp) and not stat.S_ISLNK(os.lstat(tmp).st_mode):
            os.unlink(tmp)
        raise
    os.chmod(final, 0o600)
    return final


def load_ack(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def open_store(directory):
    return AckStore(directory)


def main(argv):
    if len(argv) != 11 or argv[0] != "apply" or argv[1:10:2] != ["--state-dir", "--file", "--expect", "--sent-at", "--now"]:
        print("用法: python3 ack_contract.py apply --state-dir DIR --file FILE --expect EVENT_ID --sent-at TIME --now TIME")
        return 2
    directory, path, expect, sent_at, now = argv[2], argv[4], argv[6], argv[8], argv[10]
    store = open_store(directory)
    result = apply_processed_ack(store, load_ack(path), expected_event_id=expect, sent_at=sent_at, now=now)
    print(json.dumps({key: result.get(key) for key in ("applied", "reason", "pending", "old_ai_poll", "process_count")}, ensure_ascii=False))
    return 0 if result.get("reason") in ("processed", "duplicate") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
