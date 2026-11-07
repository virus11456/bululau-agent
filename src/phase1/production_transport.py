"""正式喚醒傳輸。獨立設定目錄。不讀測試 sender、不讀金鑰、不加 Authorization 前綴。

八欄 wake 只是通知。判斷上下文只從狀態目錄的固定 envelope 讀。HTTP 成功不是 processed。
"""
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone

import urllib.error
import urllib.parse
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
ENVELOPE_DIR = "envelopes"
JUDGMENT_DIR = "judgments"
ACK_DIR_NAME = "ack-inbox"
ALLOWED_DECISIONS = ("keep_original_ai", "no_trade")
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


def write_transport_config(directory, url, authorization, ca_file=None):
    _reject_sender_test_dir(directory)
    _production_url(url)
    validate_authorization(authorization)
    from .sender_setup import _write_private
    payload = {"webhook_url": url, "authorization": authorization}
    if ca_file:
        if not os.path.isfile(ca_file):
            raise ValueError("rejected_ca")
        payload["ca_file"] = ca_file
    return _write_private(directory, CONFIG_NAME, payload)


def _production_url(url):
    validate_url(url)
    parts = urllib.parse.urlsplit(url)
    if parts.query or parts.fragment:
        raise ValueError("rejected_url")


def load_transport_config(directory):
    _reject_sender_test_dir(directory)
    path = os.path.join(directory, CONFIG_NAME)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("rejected_config_fields")
    allowed = {"webhook_url", "authorization", "ca_file"}
    if set(data) - allowed or "webhook_url" not in data or "authorization" not in data:
        raise ValueError("rejected_config_fields")
    _production_url(data["webhook_url"])
    validate_authorization(data["authorization"])
    if "ca_file" in data and (not isinstance(data["ca_file"], str) or not os.path.isfile(data["ca_file"])):
        raise ValueError("rejected_ca")
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


def envelope_path(state_dir, event_id):
    cycle_key(event_id)
    directory = os.path.join(str(state_dir), ENVELOPE_DIR)
    return directory, os.path.join(directory, event_id + ".json")


def bind_envelope(store, envelope, crash=None):
    """先寫獨立暫存檔並 fsync，再換上最終檔名。這只模擬行程在換檔前中斷，不是斷電。"""
    refuse_foreign_state_dir(store.root)
    raw = _canonical(envelope)
    digest = hashlib.sha256(raw).hexdigest()
    key = cycle_key(envelope["evaluation_id"])
    item = store.state.get("outbox", {}).get(key)
    if not isinstance(item, dict) or item.get("event_id") != envelope["event_id"]:
        raise ValueError("binding_mismatch")
    directory, path = envelope_path(store.root, envelope["event_id"])
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory) or (os.path.lexists(path) and os.path.islink(path)):
        raise ValueError("rejected_symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = os.path.join(directory, ".{0}.{1}.tmp".format(envelope["event_id"], os.getpid()))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        if crash == "before_replace":
            raise OSError("crash_before_replace")
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                existing = handle.read().strip()
            os.unlink(tmp)
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ValueError("immutable_conflict")
        else:
            os.replace(tmp, path)
            dir_flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                dir_flags |= os.O_DIRECTORY
            dir_fd = os.open(directory, dir_flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        if crash != "before_replace" and os.path.lexists(tmp) and not os.path.islink(tmp):
            os.unlink(tmp)
        raise
    item["envelope_sha256"] = digest
    store.save()
    return digest


def read_envelope(state_dir, event_id, evaluation_id):
    refuse_foreign_state_dir(state_dir)
    _directory, path = envelope_path(state_dir, event_id)
    store = Store(state_dir)
    item = store.state.get("outbox", {}).get(cycle_key(evaluation_id))
    if not isinstance(item, dict) or item.get("event_id") != event_id or not item.get("envelope_sha256"):
        raise ValueError("binding_mismatch")
    if not os.path.isfile(path) or os.path.islink(path):
        raise ValueError("binding_mismatch")
    with open(path, "rb") as handle:
        raw = handle.read()
    if hashlib.sha256(raw.strip()).hexdigest() != item["envelope_sha256"]:
        raise ValueError("binding_mismatch")
    document = json.loads(raw.decode("utf-8"))
    if document.get("event_id") != event_id or document.get("evaluation_id") != evaluation_id:
        raise ValueError("binding_mismatch")
    return document


def _moment(value):
    from .ack_contract import _parse_time
    return _parse_time(value)


def _outside_window(moment, now):
    from datetime import timedelta
    from .ack_contract import MAX_ACK_AGE
    current = _moment(now)
    if moment > current + timedelta(seconds=60) or current - moment > MAX_ACK_AGE:
        return True
    return False


def _transport(config, override):
    if override is not None:
        return override
    context = None
    if config.get("ca_file"):
        import ssl
        context = ssl.create_default_context(cafile=config["ca_file"])
    return HttpsTransport(ssl_context=context)


def send_wake(config_dir, state_dir, transport, event_id, evaluation_id, now):
    """只 POST 指定事件的八欄通知。過期不送。HTTP 成功仍是 pending。"""
    config = load_transport_config(config_dir)
    envelope = read_envelope(state_dir, event_id, evaluation_id)
    if _outside_window(_moment(envelope["sent_at"]), now):
        return {"delivered": False, "processing_ack": False, "pending": True, "http_sent": False, "reason": "expired", "old_ai_poll": "keep"}
    document = wake_document(envelope)
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        status = _transport(config, transport).post(config["webhook_url"], config["authorization"], body)
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


def send_wake_stdin(config_dir, state_dir, raw, now, transport=None):
    """stdin 必須等於該事件 envelope 導出的八欄。錯誤訊息不含 URL 或認證。"""
    try:
        incoming = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("rejected_wake")
    if not isinstance(incoming, dict) or set(incoming) != set(WAKE_FIELDS):
        raise ValueError("rejected_wake")
    envelope = read_envelope(state_dir, incoming.get("event_id"), incoming.get("evaluation_id"))
    if wake_document(envelope) != incoming:
        raise ValueError("rejected_wake")
    return send_wake(config_dir, state_dir, transport, incoming["event_id"], incoming["evaluation_id"], now)


def judgment_path(state_dir, event_id):
    cycle_key(event_id)
    return os.path.join(str(state_dir), JUDGMENT_DIR, event_id + ".json")


def commit_judgment(state_dir, event_id, evaluation_id, now):
    """每筆判斷各寫一個檔。布林值不能證明 AI 真的跑過。過期直接拒絕。"""
    refuse_foreign_state_dir(state_dir)
    path = judgment_path(state_dir, event_id)
    try:
        with open(path, encoding="utf-8") as handle:
            judgment = json.load(handle)
    except (OSError, json.JSONDecodeError):
        raise ValueError("rejected_judgment")
    required = {
        "schema_version", "event_type", "event_id", "evaluation_id", "envelope_sha256",
        "decision", "basis", "completed_at", "orders_placed", "ai_execution_proven",
    }
    if not isinstance(judgment, dict) or set(judgment) != required or judgment.get("schema_version") != 1:
        raise ValueError("rejected_judgment")
    if judgment.get("event_type") != "dry_run_judgment" or judgment.get("event_id") != event_id or judgment.get("evaluation_id") != evaluation_id:
        raise ValueError("binding_mismatch")
    if judgment.get("ai_execution_proven") is not False:
        raise ValueError("ai_not_proven_by_attestation")
    if judgment.get("orders_placed") is not False or judgment.get("decision") not in ALLOWED_DECISIONS:
        raise ValueError("orders_not_allowed")
    envelope = read_envelope(state_dir, judgment["event_id"], judgment["evaluation_id"])
    item = Store(state_dir).state["outbox"][cycle_key(judgment["evaluation_id"])]
    if judgment.get("envelope_sha256") != item.get("envelope_sha256"):
        raise ValueError("digest_mismatch")
    basis = judgment.get("basis")
    if not isinstance(basis, dict):
        raise ValueError("rejected_basis")
    expected = {
        "reason": envelope.get("reason"),
        "as_of": envelope.get("snapshot", {}).get("as_of"),
        "positions": envelope.get("snapshot", {}).get("positions"),
        "orders": envelope.get("snapshot", {}).get("orders"),
        "freshness_status": envelope.get("freshness", {}).get("status"),
        "candidate_count": len(envelope.get("candidates") or []),
    }
    if set(basis) != set(expected) or any(basis.get(key) != value for key, value in expected.items()):
        raise ValueError("rejected_basis")
    completed = _moment(judgment.get("completed_at"))
    if completed < _moment(envelope["sent_at"]) or _outside_window(completed, now):
        raise ValueError("expired")
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
        if key not in ("--config-dir", "--state-dir", "--event-id", "--evaluation-id", "--now") or index + 1 >= len(argv):
            return None
        found[key] = argv[index + 1]
        index += 2
    return found


def main(argv, config_dir=None, read_pair=None, transport=None):
    if argv == ["configure"]:
        return 0 if configure_interactive(config_dir or default_config_dir(), read_pair) else 1
    found = _parse(argv[1:]) if argv else None
    if argv[:1] in (["send-wake"], ["send-wake-stdin"]) and found and "--config-dir" in found and "--state-dir" in found:
        try:
            now = found.get("--now") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if argv[0] == "send-wake-stdin":
                result = send_wake_stdin(found["--config-dir"], found["--state-dir"], sys.stdin.read(), now, transport)
            else:
                if "--event-id" not in found or "--evaluation-id" not in found:
                    raise ValueError("rejected_wake")
                result = send_wake(found["--config-dir"], found["--state-dir"], transport, found["--event-id"], found["--evaluation-id"], now)
        except ValueError as exc:
            print(json.dumps({"delivered": False, "processing_ack": False, "pending": True, "old_ai_poll": "keep", "reason": str(exc)}, ensure_ascii=False))
            return 1
        shown = {key: result.get(key) for key in ("delivered", "processing_ack", "pending", "reason", "old_ai_poll", "event_id")}
        shown["schema_version"] = 1
        print(json.dumps(shown, ensure_ascii=False))
        return 0 if result.get("delivered") else 1
    if argv[:1] == ["read-envelope"] and found and "--state-dir" in found and "--event-id" in found and "--evaluation-id" in found:
        try:
            document = read_envelope(found["--state-dir"], found["--event-id"], found["--evaluation-id"])
        except ValueError as exc:
            print(json.dumps({"old_ai_poll": "keep", "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(document, ensure_ascii=False))
        return 0
    if argv[:1] == ["commit-judgment"] and found and "--state-dir" in found and "--event-id" in found and "--evaluation-id" in found and "--now" in found:
        try:
            path = commit_judgment(found["--state-dir"], found["--event-id"], found["--evaluation-id"], found["--now"])
        except ValueError as exc:
            print(json.dumps({"processed": False, "old_ai_poll": "keep", "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"processed_file": os.path.basename(path), "old_ai_poll": "keep", "orders_allowed": False}, ensure_ascii=False))
        return 0
    print("用法: python3 -m src.phase1.production_transport configure | send-wake --config-dir DIR --state-dir DIR | read-envelope --state-dir DIR --event-id ID --evaluation-id ID | commit-judgment --state-dir DIR")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
