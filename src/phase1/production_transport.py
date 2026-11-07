"""正式喚醒傳輸。獨立設定目錄。不讀測試 sender、不讀金鑰、不加 Authorization 前綴。

八欄 wake 只是通知。判斷上下文只從狀態目錄的固定 envelope 讀。HTTP 成功不是 processed。
"""
import hashlib
import json
import os
import secrets
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


def _fsync_dir(directory):
    dir_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    fd = os.open(directory, dir_flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_regular(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("rejected_symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _store_id(store):
    """狀態目錄身份。不是金鑰，也不能用路徑字串代替。"""
    current = store.state.get("producer_store_id")
    if isinstance(current, str) and len(current) == 32 and all(ch in "0123456789abcdef" for ch in current):
        return current
    store.state["producer_store_id"] = secrets.token_hex(16)
    store.save()
    return store.state["producer_store_id"]


def bind_envelope(store, envelope, crash=None):
    """同一把跨程序鎖裡發布檔案並寫 outbox 摘要。hard link 不覆蓋既有檔。

    crash=before_replace 只表示行程在發布前中斷，鎖會隨行程結束釋放。不是斷電測試。
    """
    from .ack_store import directory_lock
    refuse_foreign_state_dir(store.root)
    raw = _canonical(envelope)
    digest = hashlib.sha256(raw).hexdigest()
    key = cycle_key(envelope["evaluation_id"])
    preview = store.state.get("outbox", {}).get(key)
    if not isinstance(preview, dict) or preview.get("event_id") != envelope["event_id"]:
        raise ValueError("binding_mismatch")
    directory, path = envelope_path(store.root, envelope["event_id"])
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory):
        raise ValueError("rejected_symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = os.path.join(directory, ".{0}.{1}.{2}.tmp".format(envelope["event_id"], os.getpid(), secrets.token_hex(4)))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        with directory_lock(directory, "publish.lock"):
            if crash == "before_replace":
                raise OSError("crash_before_replace")
            if os.path.lexists(path):
                file_digest = hashlib.sha256(_read_regular(path).strip()).hexdigest()
            else:
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    file_digest = hashlib.sha256(_read_regular(path).strip()).hexdigest()
                else:
                    _fsync_dir(directory)
                    file_digest = hashlib.sha256(_read_regular(path).strip()).hexdigest()
            if os.path.lexists(tmp) and not os.path.islink(tmp):
                os.unlink(tmp)
            with store.exclusive():
                store.reload()
                current = store.state.get("outbox", {}).get(key)
                if not isinstance(current, dict) or current.get("event_id") != envelope["event_id"]:
                    raise ValueError("binding_mismatch")
                _store_id(store)
                recorded = current.get("envelope_sha256")
                if recorded not in (None, "", file_digest):
                    raise ValueError("immutable_conflict")
                if recorded != file_digest:
                    current["envelope_sha256"] = file_digest
                    store.save()
                if file_digest != digest:
                    raise ValueError("immutable_conflict")
        return digest
    except Exception:
        if crash != "before_replace" and os.path.lexists(tmp) and not os.path.islink(tmp):
            os.unlink(tmp)
        raise


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


CONTEXT_FIELDS = (
    "schema_version", "event_type", "event_id", "evaluation_id", "envelope_sha256",
    "sent_at", "producer_store_id", "envelope", "automatic_transport",
)


def _secret_in(value):
    from .status_contract import SECRET_KEYS
    if isinstance(value, dict):
        if any(key in SECRET_KEYS for key in value):
            return True
        return any(_secret_in(item) for item in value.values())
    if isinstance(value, list):
        return any(_secret_in(item) for item in value)
    return False


def export_context(state_dir, event_id, evaluation_id, dest):
    """寫出一份可帶走的上下文。不開網路、不讀金鑰。自動跨機傳送沒有實作。"""
    refuse_foreign_state_dir(state_dir)
    refuse_foreign_state_dir(os.path.dirname(os.path.abspath(dest)) or ".")
    envelope = read_envelope(state_dir, event_id, evaluation_id)
    if _secret_in(envelope):
        raise ValueError("rejected_context")
    store = Store(state_dir)
    with store.exclusive():
        store.reload()
        store_id = _store_id(store)
        item = store.state.get("outbox", {}).get(cycle_key(evaluation_id))
    if not isinstance(item, dict) or item.get("envelope_sha256") != hashlib.sha256(_canonical(envelope)).hexdigest():
        raise ValueError("digest_mismatch")
    payload = {
        "schema_version": 1,
        "event_type": "immutable_context",
        "event_id": event_id,
        "evaluation_id": evaluation_id,
        "envelope_sha256": item["envelope_sha256"],
        "sent_at": envelope.get("sent_at"),
        "producer_store_id": store_id,
        "envelope": envelope,
        "automatic_transport": False,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    directory = os.path.dirname(os.path.abspath(dest))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = os.path.join(directory, ".context.{0}.{1}.tmp".format(os.getpid(), secrets.token_hex(4)))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except Exception:
        if os.path.lexists(tmp) and not os.path.islink(tmp):
            os.unlink(tmp)
        raise
    return payload


def verify_context(document, now):
    """只核對帶走的檔。不寫 store，不把路徑字串當成同一個 store。"""
    if not isinstance(document, dict) or set(document) != set(CONTEXT_FIELDS):
        raise ValueError("rejected_context")
    if document.get("schema_version") != 1 or document.get("event_type") != "immutable_context":
        raise ValueError("rejected_context")
    if document.get("automatic_transport") is not False:
        raise ValueError("automatic_transport_not_implemented")
    envelope = document.get("envelope")
    if not isinstance(envelope, dict) or _secret_in(envelope):
        raise ValueError("rejected_context")
    if envelope.get("event_id") != document.get("event_id") or envelope.get("evaluation_id") != document.get("evaluation_id"):
        raise ValueError("binding_mismatch")
    digest = hashlib.sha256(_canonical(envelope)).hexdigest()
    if digest != document.get("envelope_sha256"):
        raise ValueError("digest_mismatch")
    if _outside_window(_moment(document.get("sent_at")), now):
        raise ValueError("expired")
    store_id = document.get("producer_store_id")
    if not isinstance(store_id, str) or len(store_id) != 32:
        raise ValueError("rejected_context")
    return {
        "verified": True,
        "event_id": document["event_id"],
        "evaluation_id": document["evaluation_id"],
        "envelope_sha256": digest,
        "producer_store_id": store_id,
        "automatic_transport": False,
        "same_store": False,
        "physical_store_proven": False,
        "ai_execution_proven": False,
    }


def prepare_handoff(state_dir, event_id, evaluation_id, dest):
    """寫出要交給 box 的上下文清單。不 POST，不代寫判斷。"""
    export_context(state_dir, event_id, evaluation_id, dest)
    manifest = {
        "schema_version": 1,
        "event_type": "mac_box_handoff",
        "event_id": event_id,
        "evaluation_id": evaluation_id,
        "context_file": os.path.basename(dest),
        "transport_implemented": False,
        "post_authorized": False,
        "proposed_post_budget": {"new_ids": 1, "max_posts": 2, "approved": False},
        "prior_budget_exhausted": {"ids": 2, "posts": 3},
        "box_runs_scanner": False,
        "box_calls_binance": False,
        "program_fills_judgment": False,
        "judgment_path_on_return": "judgments/{0}.json".format(event_id),
        "apply_host": "mac_producer",
        "physical_store_proven": False,
        "replacement_allowed": False,
        "scan_interval_seconds": 3600,
        "scan_interval_reverts_to_15m": False,
    }
    side = os.path.splitext(dest)[0] + ".handoff.json"
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(side, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.write(b"\n")
    return manifest, side


def verify_same_store(state_dir, document):
    """只比對可被整份複製的邏輯 ID。不是原來那塊磁碟的證明。"""
    refuse_foreign_state_dir(state_dir)
    incoming = document.get("producer_store_id") if isinstance(document, dict) else None
    local = Store(state_dir).state.get("producer_store_id")
    match = isinstance(local, str) and local == incoming
    return {
        "logical_id_match": match,
        "physical_store_proven": False,
        "ack_requires_original_producer_apply": True,
        "same_store": False,
        "path_ignored": True,
        "automatic_transport": False,
        "ai_execution_proven": False,
        "reason": "logical_id_match" if match else "different_store",
    }


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
    """讀 judgments/<event_id>.json。本程式不代寫判斷。basis 只核對與 envelope 一致。"""
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
        if key not in ("--config-dir", "--state-dir", "--event-id", "--evaluation-id", "--now", "--out", "--file") or index + 1 >= len(argv):
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
        print(json.dumps({"processed_file": os.path.basename(path), "old_ai_poll": "keep", "orders_allowed": False, "ai_execution_proven": False}, ensure_ascii=False))
        return 0
    if argv[:1] == ["export-context"] and found and "--state-dir" in found and "--event-id" in found and "--evaluation-id" in found and "--out" in found:
        try:
            export_context(found["--state-dir"], found["--event-id"], found["--evaluation-id"], found["--out"])
        except ValueError as exc:
            print(json.dumps({"exported": False, "automatic_transport": False, "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"exported": True, "automatic_transport": False, "ai_execution_proven": False}, ensure_ascii=False))
        return 0
    if argv[:1] == ["prepare-handoff"] and found and "--state-dir" in found and "--event-id" in found and "--evaluation-id" in found and "--out" in found:
        try:
            manifest, _side = prepare_handoff(found["--state-dir"], found["--event-id"], found["--evaluation-id"], found["--out"])
        except ValueError as exc:
            print(json.dumps({"exported": False, "post_authorized": False, "transport_implemented": False, "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(manifest, ensure_ascii=False))
        return 0
    if argv[:1] == ["verify-context"] and found and "--file" in found and "--now" in found:
        try:
            with open(found["--file"], encoding="utf-8") as handle:
                report = verify_context(json.load(handle), found["--now"])
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "rejected_context"
            print(json.dumps({"verified": False, "automatic_transport": False, "same_store": False, "reason": reason}, ensure_ascii=False))
            return 1
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if argv[:1] == ["verify-same-store"] and found and "--state-dir" in found and "--file" in found:
        try:
            with open(found["--file"], encoding="utf-8") as handle:
                report = verify_same_store(found["--state-dir"], json.load(handle))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "rejected_context"
            print(json.dumps({"logical_id_match": False, "physical_store_proven": False, "same_store": False, "automatic_transport": False, "reason": reason}, ensure_ascii=False))
            return 1
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["logical_id_match"] else 1
    print("用法: python3 -m src.phase1.production_transport configure | send-wake --config-dir DIR --state-dir DIR | read-envelope --state-dir DIR --event-id ID --evaluation-id ID | export-context --state-dir DIR --event-id ID --evaluation-id ID --out FILE | verify-context --file FILE --now TIME | verify-same-store --state-dir DIR --file FILE | commit-judgment --state-dir DIR --event-id ID --evaluation-id ID --now TIME")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
