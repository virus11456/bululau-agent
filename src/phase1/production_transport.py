"""正式喚醒傳輸。獨立設定目錄。不讀測試 sender、不讀金鑰、不加 Authorization 前綴。

八欄 wake 只是通知。判斷上下文只從狀態目錄的固定 envelope 讀。HTTP 成功不是 processed。
"""
import hashlib
import json
import os
import stat
import sys
import urllib.error
import urllib.request

from .ack_contract import cycle_key, write_processed_file
from .sender_setup import (
    NoRedirect,
    RedirectBlocked,
    validate_authorization,
    validate_url,
)
from .session import Store, refuse_foreign_state_dir
from .wake_gate import ALLOWED_WAKE_REASONS

CONFIG_NAME = "transport.json"
ENVELOPE_NAME = "evaluation_envelope.json"
JUDGMENT_NAME = "dry_run_judgment.json"
ACK_DIR_NAME = "ack-inbox"
WAKE_FIELDS = (
    "schema_version", "event_type", "event_id", "evaluation_id",
    "sent_at", "reason", "test_channel", "position_channel",
)
CANDIDATE_CAP = 20


def default_config_dir():
    return os.path.join(os.path.expanduser("~"), ".config", "bululau-production-transport")


def _reject_sender_test_dir(directory):
    real = os.path.realpath(directory)
    if os.path.basename(real) == "bululau-sender-test":
        raise ValueError("refused_test_sender_dir")
    for name in ("sender.json", "trade_candidate_test.json"):
        if os.path.lexists(os.path.join(directory, name)):
            raise ValueError("refused_test_sender_dir")


def write_transport_config(directory, url, authorization):
    _reject_sender_test_dir(directory)
    validate_url(url)
    validate_authorization(authorization)
    from .sender_setup import _write_private
    return _write_private(directory, CONFIG_NAME, {"webhook_url": url, "authorization": authorization})


def load_transport_config(directory):
    _reject_sender_test_dir(directory)
    path = os.path.join(directory, CONFIG_NAME)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or set(data) != {"webhook_url", "authorization"}:
        raise ValueError("rejected_config_fields")
    validate_url(data["webhook_url"])
    validate_authorization(data["authorization"])
    return data


class HttpsTransport:
    """與測試 sender 相同的不跟隨重新導向。預設驗證憑證。測試才傳入本機 context。"""

    def __init__(self, ssl_context=None):
        self.ssl_context = ssl_context

    def post(self, url, authorization, body):
        validate_url(url)
        validate_authorization(authorization)
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Authorization", authorization)
        request.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=self.ssl_context))
        try:
            with opener.open(request, timeout=15) as response:
                response.read(0)
                return response.status
        except RedirectBlocked:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise RedirectBlocked()
            return exc.code


def wake_document(envelope):
    if not isinstance(envelope, dict) or envelope.get("reason") not in ALLOWED_WAKE_REASONS:
        raise ValueError("rejected_reason")
    document = {
        "schema_version": 1,
        "event_type": "evaluation_wake",
        "event_id": envelope.get("event_id"),
        "evaluation_id": envelope.get("evaluation_id"),
        "sent_at": envelope.get("sent_at"),
        "reason": envelope.get("reason"),
        "test_channel": False,
        "position_channel": False,
    }
    cycle_key(document["event_id"])
    cycle_key(document["evaluation_id"])
    if set(document) != set(WAKE_FIELDS):
        raise ValueError("rejected_wake_fields")
    return document


def _candidate(item):
    if not isinstance(item, dict):
        return None
    ladder = item.get("ladder") if isinstance(item.get("ladder"), list) else []
    return {
        "symbol": str(item.get("symbol") or "")[:64],
        "setup": str(item.get("setup") or "")[:64],
        "bar_time": str(item.get("bar_time") or item.get("bar_open_ms") or "")[:64],
        "ladder": ladder[:8],
    }


def build_envelope(snapshot, prefilter, freshness, *, event_id, evaluation_id, sent_at, reason):
    if reason not in ALLOWED_WAKE_REASONS:
        raise ValueError("rejected_reason")
    cycle_key(event_id)
    cycle_key(evaluation_id)
    snap = {
        key: snapshot.get(key) for key in (
            "status", "as_of", "positions", "orders", "manage_error", "manage_complete", "status_trusted",
        )
    }
    snap["limit_older_than_2h_implemented"] = False
    candidates = []
    for item in (prefilter.get("candidates") or [])[:20]:
        mapped = _candidate(item)
        if mapped:
            candidates.append(mapped)
    return {
        "schema_version": 1,
        "event_type": "evaluation_envelope",
        "event_id": event_id,
        "evaluation_id": evaluation_id,
        "sent_at": sent_at,
        "reason": reason,
        "freshness": {key: freshness.get(key) for key in ("valid", "status", "age_seconds")},
        "snapshot": snap,
        "candidates": candidates,
        "orders_allowed": False,
        "test_channel": False,
        "position_channel": False,
    }


def _canonical(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def bind_envelope(store, envelope):
    """只寫狀態目錄裡的固定檔，並把摘要記在同一個 outbox 項。"""
    refuse_foreign_state_dir(store.root)
    raw = _canonical(envelope)
    digest = hashlib.sha256(raw).hexdigest()
    key = cycle_key(envelope["evaluation_id"])
    item = store.state.get("outbox", {}).get(key)
    if not isinstance(item, dict) or item.get("event_id") != envelope["event_id"]:
        raise ValueError("binding_mismatch")
    item["envelope_sha256"] = digest
    store.save()
    path = os.path.join(str(store.root), ENVELOPE_NAME)
    if os.path.lexists(path) and stat.S_ISLNK(os.lstat(path).st_mode):
        raise ValueError("rejected_symlink")
    tmp = path + ".{0}.tmp".format(os.getpid())
    with open(tmp, "wb") as handle:
        handle.write(raw)
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return digest


def read_envelope(state_dir, event_id, evaluation_id):
    refuse_foreign_state_dir(state_dir)
    path = os.path.join(str(state_dir), ENVELOPE_NAME)
    store = Store(state_dir)
    item = store.state.get("outbox", {}).get(cycle_key(evaluation_id))
    if not isinstance(item, dict) or item.get("event_id") != event_id or not item.get("envelope_sha256"):
        raise ValueError("binding_mismatch")
    if os.path.islink(path):
        raise ValueError("rejected_symlink")
    with open(path, "rb") as handle:
        raw = handle.read()
    if hashlib.sha256(raw.strip()).hexdigest() != item["envelope_sha256"]:
        raise ValueError("binding_mismatch")
    document = json.loads(raw.decode("utf-8"))
    if document.get("event_id") != event_id or document.get("evaluation_id") != evaluation_id:
        raise ValueError("binding_mismatch")
    return document


def send_wake(config_dir, state_dir, transport):
    """只 POST 八欄通知。envelope 不進 body。HTTP 成功仍是 pending。"""
    config = load_transport_config(config_dir)
    store = Store(state_dir)
    path = os.path.join(str(state_dir), ENVELOPE_NAME)
    with open(path, encoding="utf-8") as handle:
        envelope = json.load(handle)
    read_envelope(state_dir, envelope["event_id"], envelope["evaluation_id"])
    document = wake_document(envelope)
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        status = transport.post(config["webhook_url"], config["authorization"], body)
    except RedirectBlocked:
        return {"delivered": False, "processing_ack": False, "pending": True, "http_sent": False, "reason": "redirect_blocked", "old_ai_poll": "keep"}
    except Exception:
        return {"delivered": False, "processing_ack": False, "pending": True, "http_sent": False, "reason": "sender_failed", "old_ai_poll": "keep"}
    delivered = 200 <= status < 300
    return {
        "delivered": delivered,
        "http_status": status,
        "processing_ack": False,
        "pending": True,
        "http_sent": delivered,
        "event_id": document["event_id"],
        "evaluation_id": document["evaluation_id"],
        "reason": "delivered" if delivered else "sender_failed",
        "old_ai_poll": "keep",
        "test_channel": False,
        "position_channel": False,
    }


def commit_judgment(state_dir):
    """只有 dry-run 判斷完成且未下單才寫 processed。收到 wake 不會寫。"""
    refuse_foreign_state_dir(state_dir)
    path = os.path.join(str(state_dir), JUDGMENT_NAME)
    with open(path, encoding="utf-8") as handle:
        judgment = json.load(handle)
    required = {
        "schema_version", "event_type", "event_id", "evaluation_id",
        "judgment_completed", "orders_placed", "decision", "completed_at",
    }
    if not isinstance(judgment, dict) or set(judgment) != required or judgment.get("schema_version") != 1:
        raise ValueError("rejected_judgment")
    if judgment.get("event_type") != "dry_run_judgment" or judgment.get("judgment_completed") is not True:
        raise ValueError("judgment_not_complete")
    if judgment.get("orders_placed") is not False or judgment.get("decision") not in ("keep_original_ai", "no_trade"):
        raise ValueError("orders_not_allowed")
    envelope = read_envelope(state_dir, judgment["event_id"], judgment["evaluation_id"])
    inbox = os.path.join(str(state_dir), ACK_DIR_NAME)
    os.makedirs(inbox, mode=0o700, exist_ok=True)
    document = {
        "kind": "processed",
        "event_id": judgment["event_id"],
        "evaluation_id": judgment["evaluation_id"],
        "processed_at": judgment["completed_at"],
    }
    return write_processed_file(inbox, document, processing_complete=True)


def configure_interactive(directory, read_pair=None):
    try:
        if read_pair is None:
            if not sys.stdin.isatty():
                print("請在本機終端執行。不從聊天或管線讀取。")
                return False
            import getpass
            url = getpass.getpass("Webhook HTTPS URL（不顯示）: ")
            authorization = getpass.getpass("Authorization 整欄原樣貼上（不顯示，程式不加前綴）: ")
        else:
            url, authorization = read_pair()
        write_transport_config(directory, url, authorization)
    except Exception:
        print("設定失敗。沒有寫出網址或認證。尚未發送。")
        return False
    print("已寫入正式傳輸設定，尚未發送。這不是五欄測試通道。")
    return True


def _parse(argv):
    found = {}
    index = 0
    while index < len(argv):
        key = argv[index]
        if key not in ("--config-dir", "--state-dir", "--event-id", "--evaluation-id") or index + 1 >= len(argv):
            return None
        found[key] = argv[index + 1]
        index += 2
    return found


def main(argv, config_dir=None, read_pair=None, transport=None):
    if argv == ["configure"]:
        return 0 if configure_interactive(config_dir or default_config_dir(), read_pair) else 1
    found = _parse(argv[1:]) if argv else None
    if argv[:1] == ["send-wake"] and found and "--config-dir" in found and "--state-dir" in found:
        try:
            result = send_wake(found["--config-dir"], found["--state-dir"], transport or HttpsTransport())
        except ValueError as exc:
            print(json.dumps({"delivered": False, "processing_ack": False, "pending": True, "old_ai_poll": "keep", "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({key: result.get(key) for key in ("delivered", "processing_ack", "pending", "reason", "old_ai_poll", "event_id")}, ensure_ascii=False))
        return 0 if result.get("delivered") else 1
    if argv[:1] == ["read-envelope"] and found and "--state-dir" in found and "--event-id" in found and "--evaluation-id" in found:
        try:
            document = read_envelope(found["--state-dir"], found["--event-id"], found["--evaluation-id"])
        except ValueError as exc:
            print(json.dumps({"old_ai_poll": "keep", "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(document, ensure_ascii=False))
        return 0
    if argv[:1] == ["commit-judgment"] and found and set(found) == {"--state-dir"}:
        try:
            path = commit_judgment(found["--state-dir"])
        except ValueError as exc:
            print(json.dumps({"processed": False, "old_ai_poll": "keep", "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"processed_file": os.path.basename(path), "old_ai_poll": "keep", "orders_allowed": False}, ensure_ascii=False))
        return 0
    print("用法: python3 -m src.phase1.production_transport configure | send-wake --config-dir DIR --state-dir DIR | read-envelope --state-dir DIR --event-id ID --evaluation-id ID | commit-judgment --state-dir DIR")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
