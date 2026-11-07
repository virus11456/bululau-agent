"""對 gateway 的 PUT/GET。預設關閉。沒有明確授權與注入 transport 就不會外發。

Ledger 信任邊界只涵蓋正常設定與行程崩潰：
路徑與 method→stage 以 load 當時的授權為準，呼叫參數不能改。
已啟用的 ledger 缺失、空檔或損壞就停，不會用這次呼叫重開期限。
有磁碟管理權限的人刪掉檔案後再 provision，次數會歸零；期限仍是授權裡的 approved_at，不會往後延。
本模組不宣稱擋得住這種刪檔。
"""
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import threading

from .canonical import canonical_json

MAX_BYTES = 64 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
_TRIAL_KEYS = (
    "approved", "dry_run_only", "event_id", "approved_at", "new_ids", "max_wake_posts",
    "max_judgment_puts", "max_gets", "deadline_seconds",
)
_PUT_FIELDS = (
    "schema_version", "event_type", "installation_id", "event_id", "evaluation_id",
    "envelope_sha256", "judgment_sha256", "judgment",
)
_JUDGMENT_FIELDS = (
    "schema_version", "event_type", "event_id", "evaluation_id", "envelope_sha256",
    "decision", "basis", "completed_at", "orders_placed", "ai_execution_proven",
)
_THREAD_LOCKS = {}
_FIXED_REASONS = {
    "gateway_disabled", "live_send_disabled", "trial_not_authorized", "endpoint_not_provided",
    "gateway_transport_required", "secret_not_configured", "put_budget_exhausted",
    "get_budget_exhausted", "wake_budget_exhausted", "deadline_exceeded", "event_not_approved",
    "rejected_id", "role_rejected", "timeout", "transport_failed", "too_large",
    "rejected_schema", "non_finite", "unsafe_integer", "digest_mismatch", "binding_mismatch",
    "not_applied", "store_failed", "rejected_context", "untrustworthy_discretion", "ledger_missing", "ledger_corrupt", "ledger_path_rejected",
    "stage_rejected", "parent_package_not_ready", "clock_invalid",
}
LEDGER_LIMITS = (
    "caller_cannot_retarget_ledger_or_stage",
    "missing_or_corrupt_ledger_fails_closed",
    "approved_at_does_not_restart",
    "crash_during_replace_keeps_previous_bytes",
    "disk_admin_delete_then_provision_resets_counts",
)


def _exact_int(value):
    return type(value) is int


def _kept(reason, **extra):
    if reason not in _FIXED_REASONS:
        reason = "transport_failed"
    body = {"sent": False, "reason": reason, "old_ai_poll": "keep", "discretion_complete": False, "written": False}
    body.update(extra)
    return body


def _walk_finite(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) > 2 ** 53 - 1:
            try:
                as_float = float(value)
            except OverflowError:
                raise ValueError("unsafe_integer")
            if not math.isfinite(as_float) or int(as_float) != value:
                raise ValueError("unsafe_integer")
        return
    if isinstance(value, float):
        if value != value or abs(value) == float("inf"):
            raise ValueError("non_finite")
        return
    if isinstance(value, list):
        for item in value:
            _walk_finite(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("rejected_schema")
            _walk_finite(item)
        return
    raise ValueError("rejected_schema")


def _parse_json(raw):
    if raw is None:
        return None
    if isinstance(raw, bytes) and len(raw) > MAX_BYTES:
        raise ValueError("too_large")

    def reject(_name):
        raise ValueError("non_finite")

    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("too_large")
    parsed = json.loads(text, parse_constant=reject)
    _walk_finite(parsed)
    return parsed


def _same_fields(value, fields):
    return isinstance(value, dict) and set(value) == set(fields)


def require_put_document(document, installation_id, event_id):
    """外層 installation/event 必須與授權路徑相同，才算這筆 PUT/GET。"""
    _walk_finite(document)
    if not _same_fields(document, _PUT_FIELDS):
        raise ValueError("rejected_schema")
    if document.get("schema_version") != 1 or document.get("event_type") != "judgment_put":
        raise ValueError("rejected_schema")
    if document.get("installation_id") != installation_id or document.get("event_id") != event_id:
        raise ValueError("binding_mismatch")
    if _ID_RE.fullmatch(document.get("evaluation_id") or "") is None:
        raise ValueError("binding_mismatch")
    if _SHA_RE.fullmatch(document.get("envelope_sha256") or "") is None or _SHA_RE.fullmatch(document.get("judgment_sha256") or "") is None:
        raise ValueError("digest_mismatch")
    judgment = document.get("judgment")
    if not _same_fields(judgment, _JUDGMENT_FIELDS):
        raise ValueError("rejected_schema")
    if judgment.get("schema_version") != 1 or judgment.get("event_type") != "dry_run_judgment":
        raise ValueError("rejected_schema")
    if judgment.get("event_id") != event_id or judgment.get("evaluation_id") != document.get("evaluation_id"):
        raise ValueError("binding_mismatch")
    if judgment.get("envelope_sha256") != document.get("envelope_sha256"):
        raise ValueError("digest_mismatch")
    if judgment.get("ai_execution_proven") is not False or judgment.get("orders_placed") is not False:
        raise ValueError("rejected_schema")
    if judgment.get("decision") not in ("keep_original_ai", "no_trade"):
        raise ValueError("rejected_schema")
    if not isinstance(judgment.get("basis"), dict):
        raise ValueError("rejected_schema")
    digest = hashlib.sha256(canonical_json(judgment).encode("utf-8")).hexdigest()
    if document.get("judgment_sha256") != digest:
        raise ValueError("digest_mismatch")
    return document


def gateway_exchange(transport, method, installation_id, event_id, key_id, secret, document=None):
    if transport is None:
        raise ValueError("gateway_transport_required")
    if method not in ("PUT", "GET"):
        raise ValueError("method_not_allowed")
    if _ID_RE.fullmatch(installation_id or "") is None or _ID_RE.fullmatch(event_id or "") is None:
        raise ValueError("rejected_id")
    path = "/v1/installations/{0}/judgments/{1}".format(installation_id, event_id)
    headers = {
        "accept": "application/json",
        "authorization": "Bearer {0}:{1}".format(key_id, secret),
    }
    body = None
    if method == "PUT":
        if isinstance(document, dict) and isinstance(document.get("judgment"), dict):
            digest = hashlib.sha256(canonical_json(document["judgment"]).encode("utf-8")).hexdigest()
            if document.get("judgment_sha256") != digest:
                raise ValueError("digest_mismatch")
        raw = canonical_json(document).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("too_large")
        headers["content-type"] = "application/json"
        body = raw
    status, response_headers, response_body = transport(method, path, headers, body)
    if isinstance(response_body, bytes) and len(response_body) > MAX_BYTES:
        raise ValueError("too_large")
    cache = ""
    if isinstance(response_headers, dict):
        cache = response_headers.get("Cache-Control") or response_headers.get("cache-control") or ""
    parsed = _parse_json(response_body) if response_body else None
    return {"status": status, "cache_control": cache, "body": parsed, "followed_redirect": False}


def load_gateway_config(document):
    """enabled 與 live_send 預設必須是 false。域名留空，不猜。"""
    if not isinstance(document, dict):
        raise ValueError("gateway_disabled")
    trial = document.get("trial") if isinstance(document.get("trial"), dict) else {}
    if set(trial) != set(_TRIAL_KEYS):
        raise ValueError("trial_not_authorized")
    enabled = document.get("enabled") is True
    live_send = document.get("live_send") is True
    base_url = document.get("base_url") if isinstance(document.get("base_url"), str) else ""
    if enabled or live_send:
        if trial.get("approved") is not True or trial.get("dry_run_only") is not True:
            raise ValueError("trial_not_authorized")
        if not _exact_int(trial.get("new_ids")) or trial.get("new_ids") != 1:
            raise ValueError("trial_not_authorized")
        if not _exact_int(trial.get("max_wake_posts")) or trial.get("max_wake_posts") != 2:
            raise ValueError("trial_not_authorized")
        if not _exact_int(trial.get("max_judgment_puts")) or trial.get("max_judgment_puts") != 1:
            raise ValueError("trial_not_authorized")
        if not _exact_int(trial.get("max_gets")) or not 1 <= trial["max_gets"] <= 6:
            raise ValueError("trial_not_authorized")
        if not _exact_int(trial.get("deadline_seconds")) or trial.get("deadline_seconds") != 600:
            raise ValueError("trial_not_authorized")
        if not _exact_int(trial.get("approved_at")) or trial.get("approved_at") < 0:
            raise ValueError("trial_not_authorized")
        if _ID_RE.fullmatch(trial.get("event_id") or "") is None:
            raise ValueError("trial_not_authorized")
        ledger_path = document.get("ledger_path")
        if not isinstance(ledger_path, str) or not ledger_path or ".." in ledger_path.split(os.sep):
            raise ValueError("trial_not_authorized")
        if not base_url.startswith("https://") or "<" in base_url:
            raise ValueError("endpoint_not_provided")
        if _ID_RE.fullmatch(document.get("installation_id") or "") is None or _ID_RE.fullmatch(document.get("key_id") or "") is None:
            raise ValueError("trial_not_authorized")
    else:
        ledger_path = document.get("ledger_path") if isinstance(document.get("ledger_path"), str) else ""
    return {
        "enabled": enabled,
        "live_send": live_send,
        "base_url": base_url,
        "installation_id": document.get("installation_id") if isinstance(document.get("installation_id"), str) else "",
        "key_id": document.get("key_id") if isinstance(document.get("key_id"), str) else "",
        "role": document.get("role") if document.get("role") in ("publisher", "reader") else "",
        "ledger_path": ledger_path,
        "approved_at": trial.get("approved_at") if _exact_int(trial.get("approved_at")) else None,
        "trial": trial,
        "secret_env": "GATEWAY_MACHINE_SECRET",
        "ledger_limits": list(LEDGER_LIMITS),
    }


def _stage_limit(trial, stage):
    if stage == "judgment_put":
        return "max_judgment_puts", "put_budget_exhausted"
    if stage == "get":
        return "max_gets", "get_budget_exhausted"
    if stage == "wake_post":
        return "max_wake_posts", "wake_budget_exhausted"
    return None, "trial_not_authorized"


def _ledger_document(config):
    trial = config.get("trial") if isinstance(config.get("trial"), dict) else {}
    return {
        "event_id": trial.get("event_id"),
        "approved_at": config.get("approved_at"),
        "installation_id": config.get("installation_id"),
        "role": config.get("role"),
        "judgment_put": 0,
        "get": 0,
        "wake_post": 0,
    }


def _atomic_replace(path, payload):
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".ledger-", dir=directory)
    try:
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        dir_fd = os.open(directory, dir_flags)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.lexists(tmp):
            os.unlink(tmp)


def provision_ledger(config):
    """只建立一次。已存在就拒絕，避免把次數清掉。不連網。"""
    path = config.get("ledger_path")
    if not isinstance(path, str) or not path:
        raise ValueError("trial_not_authorized")
    if os.path.lexists(path):
        raise ValueError("ledger_exists")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    _atomic_replace(path, json.dumps(_ledger_document(config), sort_keys=True).encode("utf-8"))
    return path


def _read_ledger(path):
    if not os.path.lexists(path):
        return None, "ledger_missing"
    info = os.lstat(path)
    if not info.st_mode or os.path.islink(path) or not os.path.isfile(path) or info.st_size == 0 or info.st_size > MAX_BYTES:
        return None, "ledger_corrupt"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if not raw.strip():
        return None, "ledger_corrupt"
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, "ledger_corrupt"
    if not isinstance(state, dict):
        return None, "ledger_corrupt"
    return state, None


def reserve_attempt(config, stage, event_id, now_epoch):
    """失敗也先佔用次數。缺檔、空檔、壞檔都不重開期限。"""
    trial = config.get("trial") if isinstance(config.get("trial"), dict) else {}
    ledger_path = config.get("ledger_path")
    if _ID_RE.fullmatch(event_id or "") is None:
        return _kept("rejected_id")
    if event_id != trial.get("event_id"):
        return _kept("event_not_approved")
    limit_key, exhausted = _stage_limit(trial, stage)
    if limit_key is None or not _exact_int(trial.get(limit_key)):
        return _kept("trial_not_authorized")
    if not isinstance(ledger_path, str) or not ledger_path:
        return _kept("ledger_missing")
    approved_at = config.get("approved_at")
    if not _exact_int(approved_at) or not _exact_int(trial.get("deadline_seconds")):
        return _kept("trial_not_authorized")
    lock = _THREAD_LOCKS.setdefault(ledger_path, threading.Lock())
    with lock:
        lock_path = ledger_path + ".lock"
        lock_dir = os.path.dirname(lock_path) or "."
        if not os.path.isdir(lock_dir):
            return _kept("ledger_missing")
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            state, problem = _read_ledger(ledger_path)
            if problem:
                return _kept(problem)
            if state.get("event_id") != event_id or state.get("installation_id") != config.get("installation_id"):
                return _kept("event_not_approved")
            if state.get("role") != config.get("role") or state.get("approved_at") != approved_at:
                return _kept("ledger_corrupt")
            for name in ("judgment_put", "get", "wake_post"):
                if type(state.get(name)) is not int:
                    return _kept("ledger_corrupt")
            if type(now_epoch) is not int or now_epoch < approved_at or now_epoch > approved_at + trial["deadline_seconds"]:
                return _kept("deadline_exceeded")
            used = state.get(stage)
            if used >= trial[limit_key]:
                return _kept(exhausted)
            state[stage] = used + 1
            encoded = json.dumps(state, sort_keys=True).encode("utf-8")
            _atomic_replace(ledger_path, encoded)
            return {"reserved": True, "count": state[stage]}
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _method_stage(method):
    if method == "PUT":
        return "judgment_put"
    if method == "GET":
        return "get"
    return None


def authorized_exchange(config, transport, method, event_id, document=None, secret=None, ledger_path=None, now_epoch=0, stage=None):
    """live_send 仍要注入 transport。本函式不建立連線。次數寫在授權綁定的 ledger，失敗也算。"""
    reserved = {"count": None}
    try:
        if config.get("enabled") is not True:
            return _kept("gateway_disabled")
        if config.get("live_send") is not True:
            return _kept("live_send_disabled")
        derived = _method_stage(method)
        if derived is None:
            return _kept("role_rejected")
        if stage is not None and stage != derived:
            return _kept("stage_rejected")
        pinned = config.get("ledger_path")
        if ledger_path is not None and ledger_path != pinned:
            return _kept("ledger_path_rejected")
        reserved = reserve_attempt(config, derived, event_id, now_epoch)
        if not reserved.get("reserved"):
            return reserved
        role = config.get("role")
        if role == "publisher" and method != "PUT":
            return _kept("role_rejected")
        if role == "reader" and method != "GET":
            return _kept("role_rejected")
        if role not in ("publisher", "reader"):
            return _kept("role_rejected")
        if transport is None:
            return _kept("gateway_transport_required")
        if not secret:
            secret = os.environ.get(config["secret_env"])
        if not secret:
            return _kept("secret_not_configured")
        result = gateway_exchange(
            transport, method, config["installation_id"], event_id, config["key_id"], secret, document,
        )
        if result.get("status") in (200, 201):
            require_put_document(result.get("body"), config.get("installation_id"), event_id)
    except TimeoutError:
        return _kept("timeout")
    except OSError:
        return _kept("timeout")
    except ValueError as exc:
        reason = str(exc) if str(exc) in _FIXED_REASONS else "transport_failed"
        return _kept(reason)
    except Exception:
        return _kept("transport_failed")
    result["sent"] = True
    result["old_ai_poll"] = "keep"
    result["discretion_complete"] = False
    result["written"] = False
    result["attempt"] = reserved.get("count")
    return result


def apply_gateway_get(config, transport, event_id, state_dir, evaluation_id, sent_at, now, secret=None, ledger_path=None, now_epoch=0):
    """GET 合格才寫入原來的 producer。不合格不 import、不 commit、不 apply。"""
    from .ack_contract import apply_processed_ack, load_ack
    from .production_transport import commit_judgment, import_staged, read_envelope
    from .session import Store
    result = authorized_exchange(
        config, transport, "GET", event_id, secret=secret, ledger_path=ledger_path, now_epoch=now_epoch, stage="get",
    )
    result["applied"] = False
    result["written"] = False
    result["discretion_complete"] = False
    if result.get("sent") is not True or result.get("status") != 200 or not isinstance(result.get("body"), dict):
        result["reason"] = result.get("reason") or "not_applied"
        return result
    body = result["body"]
    try:
        require_put_document(body, config.get("installation_id"), event_id)
    except ValueError as exc:
        result["sent"] = False
        result["reason"] = str(exc) if str(exc) in _FIXED_REASONS else "binding_mismatch"
        return result
    judgment = body.get("judgment")
    if body.get("evaluation_id") != evaluation_id or judgment.get("evaluation_id") != evaluation_id:
        result["sent"] = False
        result["reason"] = "binding_mismatch"
        return result
    try:
        envelope = read_envelope(state_dir, event_id, evaluation_id)
    except (OSError, ValueError):
        result["reason"] = "binding_mismatch"
        return result
    from .production_transport import _canonical
    if judgment.get("envelope_sha256") != hashlib.sha256(_canonical(envelope)).hexdigest():
        result["reason"] = "digest_mismatch"
        return result
    try:
        digest = hashlib.sha256(canonical_json(judgment).encode("utf-8")).hexdigest()
    except ValueError:
        result["reason"] = "non_finite"
        return result
    if body.get("event_type") == "judgment_put" and body.get("judgment_sha256") != digest:
        result["reason"] = "digest_mismatch"
        return result
    path = os.path.join(str(state_dir), ".parent-judgment.json")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(judgment, handle)
        import_staged(state_dir, path, now)
        processed = commit_judgment(state_dir, event_id, evaluation_id, now)
        applied = apply_processed_ack(
            Store(state_dir), load_ack(processed), expected_event_id=event_id, sent_at=sent_at, now=now,
        )
    except (OSError, ValueError):
        result["reason"] = "not_applied"
        return result
    finally:
        if os.path.lexists(path):
            os.unlink(path)
    item = Store(state_dir).state.get("outbox", {}).get("cycle:" + evaluation_id) or {}
    if applied.get("reason") not in ("processed", "duplicate") or item.get("status") != "acked" or item.get("event_id") != event_id:
        result["reason"] = "not_applied"
        return result
    result["applied"] = True
    result["written"] = True
    result["reason"] = applied.get("reason")
    result["process_count"] = applied.get("process_count")
    return result


def https_exchange(base_url, method, path, headers, body):
    """只有 CLI 帶 --send 且設定已授權才會進到這裡。不跟隨重新導向。"""
    import urllib.error
    import urllib.request
    from .production_transport import NoRedirect, validate_authorization, validate_url
    url = base_url.rstrip("/") + path
    validate_url(url)
    validate_authorization(headers["authorization"])
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", headers["authorization"])
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler())
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            from .production_transport import RedirectBlocked
            raise RedirectBlocked()
        return exc.code, {}, exc.read(MAX_BYTES + 1)


def _option(argv, name):
    if name not in argv:
        return None
    index = argv.index(name)
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


def publisher_document(config, event_id, judgment_path, trace_path, context_path):
    """先讀父層檔並驗綁。不合就不會組 PUT，也不會呼叫 transport。"""
    from .return_adapter import judgment_put
    from .review_context import read_parent_package
    with open(context_path, encoding="utf-8") as handle:
        context = json.load(handle)
    ready = read_parent_package(context, judgment_path, trace_path)
    if ready.get("ready") is not True:
        raise ValueError(ready.get("reason") or "parent_package_not_ready")
    with open(judgment_path, encoding="utf-8") as handle:
        judgment = json.load(handle)
    if not isinstance(judgment, dict) or judgment.get("event_type") != "dry_run_judgment":
        raise ValueError("parent_package_not_ready")
    if judgment.get("event_id") != event_id:
        raise ValueError("binding_mismatch")
    document = judgment_put(
        config.get("installation_id"), event_id, judgment.get("evaluation_id"), judgment.get("envelope_sha256"), judgment,
    )
    require_put_document(document, config.get("installation_id"), event_id)
    return document


def main(argv):
    if len(argv) < 2 or argv[1] not in ("put", "get", "show-config", "init-ledger"):
        print("用法: python3 -m src.phase1.gateway_client show-config|init-ledger|put|get --config FILE [--send]")
        return 2
    if argv[1] == "init-ledger":
        config_path = _option(argv, "--config")
        if not config_path:
            print(json.dumps({"provisioned": False, "network": False, "reason": "trial_not_authorized"}, ensure_ascii=False))
            return 2
        with open(config_path, encoding="utf-8") as handle:
            config = load_gateway_config(json.load(handle))
        try:
            provision_ledger(config)
        except ValueError as exc:
            print(json.dumps({"provisioned": False, "network": False, "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"provisioned": True, "network": False, "ledger_limits": list(LEDGER_LIMITS)}, ensure_ascii=False))
        return 0
    if "--send" not in argv:
        print(json.dumps({
            "sent": False,
            "reason": "gateway_disabled",
            "live_send": False,
            "endpoint_guessed": False,
            "discretion_complete": False,
            "old_ai_poll": "keep",
            "ledger_limits": list(LEDGER_LIMITS),
        }, ensure_ascii=False))
        return 0
    config_path = _option(argv, "--config")
    event_id = _option(argv, "--event-id")
    if not config_path or not event_id:
        print(json.dumps({"sent": False, "reason": "gateway_disabled", "old_ai_poll": "keep"}, ensure_ascii=False))
        return 2
    with open(config_path, encoding="utf-8") as handle:
        config = load_gateway_config(json.load(handle))
    document = None
    if argv[1] == "put":
        judgment_path = _option(argv, "--file")
        trace_path = _option(argv, "--trace")
        context_path = _option(argv, "--context")
        if not judgment_path or not trace_path or not context_path:
            print(json.dumps(_kept("parent_package_not_ready"), ensure_ascii=False))
            return 1
        try:
            document = publisher_document(config, event_id, judgment_path, trace_path, context_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            reason = str(exc) if str(exc) in _FIXED_REASONS else "parent_package_not_ready"
            print(json.dumps(_kept(reason), ensure_ascii=False))
            return 1

    def transport(method, path, headers, body):
        return https_exchange(config["base_url"], method, path, headers, body)

    import time
    now_epoch = int(time.time())
    if argv[1] == "get":
        state_dir = _option(argv, "--state-dir")
        evaluation_id = _option(argv, "--evaluation-id")
        sent_at = _option(argv, "--sent-at")
        now = _option(argv, "--now")
        if not state_dir or not evaluation_id or not sent_at or not now:
            print(json.dumps(_kept("not_applied"), ensure_ascii=False))
            return 1
        result = apply_gateway_get(
            config, transport, event_id, state_dir, evaluation_id, sent_at, now, ledger_path=None, now_epoch=now_epoch,
        )
        print(json.dumps({key: result.get(key) for key in ("sent", "status", "reason", "applied", "written", "old_ai_poll", "discretion_complete", "process_count")}, ensure_ascii=False))
        return 0 if result.get("applied") is True else 1
    result = authorized_exchange(config, transport, "PUT", event_id, document, ledger_path=None, now_epoch=now_epoch)
    print(json.dumps({key: result.get(key) for key in ("sent", "status", "reason", "old_ai_poll", "discretion_complete", "written")}, ensure_ascii=False))
    return 0 if result.get("status") in (200, 201) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
