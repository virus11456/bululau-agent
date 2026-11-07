"""判斷回傳的離線 adapter。不建立服務、不連 Vercel、不讀交易金鑰。

私人 Blob 的 allowOverwrite=false 是否能在並行建立時原子拒絕，官方行為尚未在這裡證明。
因此沒有 Blob 客戶端。測試儲存先寫完暫存檔，再用 link 掛上最終名，不用先 GET 再 PUT。
"""
import hashlib
import hmac
import json
import os
import secrets
import time

from .ack_contract import apply_processed_ack, cycle_key, load_ack
from .production_transport import commit_judgment, import_staged
from .session import Store

MAX_BYTES = 64 * 1024
VERCEL_BLOB_ATOMIC_CREATE_PROVEN = False
BACKOFF_SECONDS = (5, 10, 20, 40, 60)
POLL_DEADLINE_SECONDS = 600
PUT_FIELDS = (
    "schema_version", "event_type", "installation_id", "event_id", "evaluation_id",
    "envelope_sha256", "judgment_sha256", "judgment",
)
RETRY_REASONS = ("backoff", "not_ready", "timeout", "disconnect")


def _digest(document):
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), raw


def judgment_put(installation_id, event_id, evaluation_id, envelope_sha256, judgment):
    if not isinstance(judgment, dict) or judgment.get("ai_execution_proven") is not False:
        raise ValueError("ai_not_proven_by_attestation")
    judgment_sha, _raw = _digest(judgment)
    if judgment.get("envelope_sha256") != envelope_sha256 or judgment.get("event_id") != event_id or judgment.get("evaluation_id") != evaluation_id:
        raise ValueError("binding_mismatch")
    return {
        "schema_version": 1,
        "event_type": "judgment_put",
        "installation_id": installation_id,
        "event_id": event_id,
        "evaluation_id": evaluation_id,
        "envelope_sha256": envelope_sha256,
        "judgment_sha256": judgment_sha,
        "judgment": judgment,
    }


def exclusive_create(path, raw):
    """先寫完暫存檔，再用 link 掛上最終名。link 不會覆蓋已存在的檔。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = "{0}.{1}.{2}.tmp".format(path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)
    finally:
        if os.path.lexists(tmp) and not os.path.islink(tmp):
            os.unlink(tmp)


def _replace_file(path, raw):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = "{0}.{1}.{2}.tmp".format(path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.lexists(tmp) and not os.path.islink(tmp):
            os.unlink(tmp)
        raise


class InstallationStore:
    def __init__(self, root):
        self.root = root
        self.vercel_blob_atomic_create_proven = VERCEL_BLOB_ATOMIC_CREATE_PROVEN
        os.makedirs(root, mode=0o700, exist_ok=True)

    def _path(self, installation_id, event_id):
        cycle_key(installation_id)
        cycle_key(event_id)
        directory = os.path.join(self.root, "returns", installation_id)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        return os.path.join(directory, event_id + ".json")

    def create(self, installation_id, event_id, document):
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("too_large")
        path = self._path(installation_id, event_id)
        try:
            exclusive_create(path, raw)
            return {"created": True, "duplicate": False, "status": 201}
        except FileExistsError:
            with open(path, "rb") as handle:
                existing = json.loads(handle.read().decode("utf-8"))
            if existing.get("judgment_sha256") == document.get("judgment_sha256") and existing.get("envelope_sha256") == document.get("envelope_sha256"):
                return {"created": False, "duplicate": True, "status": 200}
            raise ValueError("conflict")


def _scope_ok(credential, installation_id, role):
    if not isinstance(credential, dict):
        return False
    if credential.get("installation_id") != installation_id or credential.get("role") != role:
        return False
    secret = credential.get("secret")
    expected = credential.get("secret_sha256")
    if not isinstance(secret, str) or not isinstance(expected, str):
        return False
    actual = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, expected)


def publish(store, credential, installation_id, document):
    if not _scope_ok(credential, installation_id, "publisher"):
        return {"status": 403, "reason": "scope_rejected", "written": False}
    if not isinstance(document, dict) or set(document) != set(PUT_FIELDS) or document.get("installation_id") != installation_id:
        return {"status": 400, "reason": "rejected_schema", "written": False}
    if "pathname" in document or "callback_url" in document:
        return {"status": 400, "reason": "client_path_rejected", "written": False}
    try:
        result = store.create(installation_id, document["event_id"], document)
    except ValueError as exc:
        code = 409 if str(exc) == "conflict" else 400
        return {"status": code, "reason": str(exc), "written": False}
    result["written"] = result["created"]
    return result


def pending_event_ids(state_dir):
    store = Store(state_dir)
    found = []
    for item in (store.state.get("outbox") or {}).values():
        if isinstance(item, dict) and item.get("status") == "pending" and item.get("event_id"):
            found.append(item["event_id"])
    return found


def authorize_request(method, path, credential, installation_id, secret_matches):
    """只接受固定路徑。不接收客戶端 pathname，也不記錄憑證。"""
    parts = path.strip("/").split("/")
    if parts[:2] != ["v1", "installations"] or len(parts) != 5 or parts[3] != "judgments":
        return 404, None
    routed, event_id = parts[2], parts[4]
    if routed != installation_id or not isinstance(credential, dict) or credential.get("installation_id") != routed:
        return 403, None
    role = "publisher" if method == "PUT" else "reader" if method == "GET" else ""
    if not role or credential.get("role") != role or not secret_matches:
        return 403, None
    return 200, event_id


def _secret_matches(expected, presented):
    if not isinstance(expected, str) or not isinstance(presented, str):
        return False
    left = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    right = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return hmac.compare_digest(left, right)


class FakeReturnHttp:
    """測試用。不聽真實連接埠，也不跟 redirect。"""

    def __init__(self, store, secrets_by_role, installation_id):
        self.store = store
        self.secrets_by_role = secrets_by_role
        self.installation_id = installation_id
        self.calls = []
        self.fail = None
        self.failures = []
        self.omit_cache = False

    def request(self, method, path, headers, body, credential):
        self.calls.append(method)
        kind = None
        if self.fail:
            kind = self.fail
            self.fail = None
        elif self.failures:
            kind = self.failures.pop(0)
        if kind == "redirect":
            return 302, {"Location": "https://evil.example/x"}, b""
        if kind == "tls":
            raise OSError("tls_rejected")
        if kind == "timeout":
            raise TimeoutError("timeout")
        if kind == "disconnect":
            raise ConnectionError("disconnect")
        if kind == "backoff":
            return 429, {"Retry-After": "5"}, b""
        if kind == "unavailable":
            return 503, {}, b""
        if kind == "unauthorized":
            return 401, {}, b""
        role = "publisher" if method == "PUT" else "reader" if method == "GET" else ""
        expected = self.secrets_by_role.get(role)
        status, event_id = authorize_request(
            method, path, credential, self.installation_id,
            _secret_matches(expected, credential.get("secret") if isinstance(credential, dict) else None),
        )
        if status != 200:
            return status, {}, json.dumps({"reason": "scope_rejected"}).encode("utf-8")
        cache = {} if self.omit_cache else {"Cache-Control": "private, no-store"}
        if method == "PUT":
            if body is None or len(body) > MAX_BYTES:
                return 400, cache, json.dumps({"reason": "too_large"}).encode("utf-8")
            document = json.loads(body.decode("utf-8"))
            scoped = dict(credential)
            scoped["secret_sha256"] = hashlib.sha256(credential["secret"].encode("utf-8")).hexdigest()
            result = publish(self.store, scoped, self.installation_id, document)
            return result["status"], cache, json.dumps(result).encode("utf-8")
        target = self.store._path(self.installation_id, event_id)
        if not os.path.isfile(target):
            return 404, cache, b""
        with open(target, "rb") as handle:
            raw = handle.read()
        return 200, cache, raw


def _stage_path(state_dir, event_id):
    return os.path.join(str(state_dir), "return-recovery", event_id + ".json")


def _save_stage(state_dir, event_id, stage):
    path = _stage_path(state_dir, event_id)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    raw = json.dumps(stage, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    _replace_file(path, raw)


def _load_stage(state_dir, event_id):
    path = _stage_path(state_dir, event_id)
    if not os.path.isfile(path):
        return {"phase": "pending", "event_id": event_id}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _private_cache(headers):
    value = headers.get("Cache-Control") if isinstance(headers, dict) else ""
    parts = [item.strip().lower() for item in str(value or "").split(",")]
    return "private" in parts and "no-store" in parts


def _kept(reason, **extra):
    body = {
        "polled": True,
        "reason": reason,
        "processed": False,
        "old_ai_poll": "keep",
        "ai_execution_proven": False,
        "vercel_blob_atomic_create_proven": False,
    }
    body.update(extra)
    return body


def _validate_put(document, state_dir, installation_id, event_id, evaluation_id):
    if not isinstance(document, dict) or set(document) != set(PUT_FIELDS):
        raise ValueError("rejected_schema")
    if document.get("schema_version") != 1 or document.get("event_type") != "judgment_put":
        raise ValueError("rejected_schema")
    if document.get("installation_id") != installation_id or document.get("event_id") != event_id or document.get("evaluation_id") != evaluation_id:
        raise ValueError("binding_mismatch")
    judgment = document.get("judgment")
    digest, raw = _digest(judgment)
    if digest != document.get("judgment_sha256") or len(raw) > MAX_BYTES:
        raise ValueError("digest_mismatch")
    if not isinstance(judgment, dict) or judgment.get("envelope_sha256") != document.get("envelope_sha256"):
        raise ValueError("digest_mismatch")
    item = Store(state_dir).state.get("outbox", {}).get(cycle_key(evaluation_id))
    if not isinstance(item, dict) or item.get("event_id") != event_id or item.get("envelope_sha256") != document.get("envelope_sha256"):
        raise ValueError("digest_mismatch")
    return judgment


def _scratch(state_dir, event_id, judgment):
    path = os.path.join(str(state_dir), "return-recovery", event_id + ".judgment.json")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    raw = json.dumps(judgment, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    _replace_file(path, raw)
    return path


def download_one(state_dir, http, credential, installation_id, event_id, evaluation_id, now, sent_at):
    """只讀這個 pending id。200 先驗證，再走原 producer 的 import、commit、apply。"""
    try:
        cycle_key(installation_id)
        cycle_key(event_id)
        cycle_key(evaluation_id)
    except ValueError:
        return _kept("rejected_id", polled=False)
    stage = _load_stage(state_dir, event_id)
    if stage.get("phase") == "applied":
        count = Store(state_dir).state.get("test_events", {}).get(event_id, {}).get("process_count")
        return {
            "polled": False, "reason": "duplicate", "processed": True, "process_count": count,
            "old_ai_poll": "keep", "ai_execution_proven": False, "vercel_blob_atomic_create_proven": False,
        }
    recoverable = stage.get("phase") in ("downloaded", "imported", "committed") and isinstance(stage.get("body"), dict)
    if event_id not in pending_event_ids(state_dir) and not recoverable:
        return _kept("not_pending", polled=False)
    path = "/v1/installations/{0}/judgments/{1}".format(installation_id, event_id)
    if recoverable:
        document = stage["body"]
    else:
        try:
            status, headers, body = http.request("GET", path, {}, None, credential)
        except TimeoutError:
            return _kept("timeout")
        except ConnectionError:
            return _kept("disconnect")
        except OSError as exc:
            return _kept("tls_rejected" if "tls" in str(exc) else "disconnect")
        if status in (401, 403):
            return _kept("auth_stopped", alert_once=True)
        if status in (302, 303, 307, 308):
            return _kept("redirect_blocked")
        if status == 429 or 500 <= status <= 599:
            return _kept("backoff", retry_after=headers.get("Retry-After") if isinstance(headers, dict) else None)
        if status in (304, 404):
            return _kept("not_ready")
        if status != 200 or not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BYTES or not _private_cache(headers):
            return _kept("rejected")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return _kept("rejected")
        stage = {"phase": "downloaded", "event_id": event_id, "body": document, "judgment_sha256": document.get("judgment_sha256") if isinstance(document, dict) else None}
        _save_stage(state_dir, event_id, stage)
    try:
        judgment = _validate_put(document, state_dir, installation_id, event_id, evaluation_id)
        if stage.get("phase") == "downloaded":
            staged = _scratch(state_dir, event_id, judgment)
            import_staged(state_dir, staged, now)
            stage["phase"] = "imported"
            _save_stage(state_dir, event_id, stage)
        if stage.get("phase") == "imported":
            processed = commit_judgment(state_dir, event_id, evaluation_id, now)
            stage["phase"] = "committed"
            stage["processed_file"] = processed
            _save_stage(state_dir, event_id, stage)
        processed = stage.get("processed_file")
        result = apply_processed_ack(Store(state_dir), load_ack(processed), expected_event_id=event_id, sent_at=sent_at, now=now)
    except (ValueError, OSError) as exc:
        return _kept(str(exc) or "store_failed")
    if result.get("reason") not in ("processed", "duplicate"):
        return _kept(result.get("reason") or "ack_rejected")
    item = Store(state_dir).state.get("outbox", {}).get(cycle_key(evaluation_id))
    if not isinstance(item, dict) or item.get("status") != "acked":
        return _kept("readback_failed")
    stage["phase"] = "applied"
    _save_stage(state_dir, event_id, stage)
    return {
        "polled": True,
        "reason": result.get("reason"),
        "processed": result.get("reason") == "processed",
        "process_count": result.get("process_count"),
        "old_ai_poll": "keep",
        "ai_execution_proven": False,
        "vercel_blob_atomic_create_proven": False,
    }


def backoff_seconds(attempt, retry_after=None, jitter=0):
    base = BACKOFF_SECONDS[min(max(int(attempt), 0), len(BACKOFF_SECONDS) - 1)]
    extra = retry_after if isinstance(retry_after, (int, float)) and retry_after >= 0 else 0
    if isinstance(retry_after, str) and retry_after.isdigit():
        extra = int(retry_after)
    return max(base, extra) + jitter


def poll_pending(state_dir, http, credential, installation_id, event_id, evaluation_id, now, sent_at, sleep=None, clock=None, jitter=None, deadline_seconds=POLL_DEADLINE_SECONDS):
    """只在已有 pending 時 GET。退避不送 wake，也不增加 POST。"""
    sleep = time.sleep if sleep is None else sleep
    clock = time.monotonic if clock is None else clock
    jitter = (lambda _attempt: secrets.randbelow(1000) / 1000.0) if jitter is None else jitter
    started = clock()
    attempt = 0
    while True:
        result = download_one(state_dir, http, credential, installation_id, event_id, evaluation_id, now, sent_at)
        if result.get("reason") not in RETRY_REASONS:
            return result
        wait = backoff_seconds(attempt, result.get("retry_after"), jitter(attempt))
        remaining = deadline_seconds - (clock() - started)
        if wait > remaining:
            result = dict(result)
            result["reason"] = "deadline"
            result["processed"] = False
            result["old_ai_poll"] = "keep"
            return result
        sleep(wait)
        attempt += 1
