"""對 gateway 的 PUT/GET。預設關閉。沒有明確授權與注入 transport 就不會外發。"""
import fcntl
import hashlib
import json
import math
import os
import re
import threading

MAX_BYTES = 64 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TRIAL_KEYS = (
    "approved", "dry_run_only", "event_id", "new_ids", "max_wake_posts",
    "max_judgment_puts", "max_gets", "deadline_seconds",
)
_THREAD_LOCKS = {}
_FIXED_REASONS = {
    "gateway_disabled", "live_send_disabled", "trial_not_authorized", "endpoint_not_provided",
    "gateway_transport_required", "secret_not_configured", "put_budget_exhausted",
    "get_budget_exhausted", "wake_budget_exhausted", "deadline_exceeded", "event_not_approved",
    "rejected_id", "role_rejected", "timeout", "transport_failed", "too_large",
    "rejected_schema", "non_finite", "digest_mismatch", "binding_mismatch",
    "not_applied", "store_failed",
}


def canonical_json(value):
    """與 gateway/lib/contract.js 的 canonical 相同。1.0 編成 1，NaN 拒絕。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non_finite")
        if value.is_integer() and abs(value) < 2 ** 53:
            return str(int(value))
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + canonical_json(value[key]) for key in sorted(value)
        ) + "}"
    raise ValueError("rejected_schema")


def _exact_int(value):
    return type(value) is int


def _kept(reason, **extra):
    if reason not in _FIXED_REASONS:
        reason = "transport_failed"
    body = {"sent": False, "reason": reason, "old_ai_poll": "keep", "discretion_complete": False, "written": False}
    body.update(extra)
    return body


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
    return json.loads(text, parse_constant=reject)


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
        if _ID_RE.fullmatch(trial.get("event_id") or "") is None:
            raise ValueError("trial_not_authorized")
        if not base_url.startswith("https://") or "<" in base_url:
            raise ValueError("endpoint_not_provided")
    return {
        "enabled": enabled,
        "live_send": live_send,
        "base_url": base_url,
        "installation_id": document.get("installation_id") if isinstance(document.get("installation_id"), str) else "",
        "key_id": document.get("key_id") if isinstance(document.get("key_id"), str) else "",
        "role": document.get("role") if document.get("role") in ("publisher", "reader") else "",
        "trial": trial,
        "secret_env": "GATEWAY_MACHINE_SECRET",
    }


def _stage_limit(trial, stage):
    if stage == "judgment_put":
        return "max_judgment_puts", "put_budget_exhausted"
    if stage == "get":
        return "max_gets", "get_budget_exhausted"
    if stage == "wake_post":
        return "max_wake_posts", "wake_budget_exhausted"
    return None, "trial_not_authorized"


def reserve_attempt(ledger_path, trial, stage, event_id, now_epoch):
    """失敗也先佔用次數。鎖同時擋住同程序緒與其他程序。"""
    if _ID_RE.fullmatch(event_id or "") is None:
        return _kept("rejected_id")
    if event_id != trial.get("event_id"):
        return _kept("event_not_approved")
    limit_key, exhausted = _stage_limit(trial, stage)
    if limit_key is None or not _exact_int(trial.get(limit_key)):
        return _kept("trial_not_authorized")
    os.makedirs(os.path.dirname(ledger_path) or ".", mode=0o700, exist_ok=True)
    lock = _THREAD_LOCKS.setdefault(ledger_path, threading.Lock())
    with lock:
        fd = os.open(ledger_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, MAX_BYTES)
            state = json.loads(raw.decode("utf-8")) if raw.strip() else {
                "event_id": event_id,
                "started_at": now_epoch,
                "judgment_put": 0,
                "get": 0,
                "wake_post": 0,
            }
            if state.get("event_id") != event_id:
                return _kept("event_not_approved")
            started = state.get("started_at")
            if type(started) is not int:
                started = now_epoch
                state["started_at"] = started
            if type(now_epoch) is not int or now_epoch > started + trial["deadline_seconds"]:
                return _kept("deadline_exceeded")
            used = state.get(stage)
            if type(used) is not int:
                used = 0
            if used >= trial[limit_key]:
                return _kept(exhausted)
            state[stage] = used + 1
            encoded = json.dumps(state, sort_keys=True).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, encoded)
            os.fsync(fd)
            return {"reserved": True, "count": state[stage]}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def authorized_exchange(config, transport, method, event_id, document=None, secret=None, ledger_path=None, now_epoch=0, stage=None):
    """live_send 仍要注入 transport。本函式不建立連線。次數寫在 ledger，失敗也算。"""
    try:
        if config.get("enabled") is not True:
            return _kept("gateway_disabled")
        if config.get("live_send") is not True:
            return _kept("live_send_disabled")
        role = config.get("role")
        if stage is None:
            stage = "judgment_put" if method == "PUT" else "get"
        if not ledger_path:
            return _kept("trial_not_authorized")
        reserved = reserve_attempt(ledger_path, config["trial"], stage, event_id, now_epoch)
        if not reserved.get("reserved"):
            return reserved
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
    if result.get("status") != 200 or not isinstance(result.get("body"), dict):
        result["reason"] = result.get("reason") or "not_applied"
        return result
    body = result["body"]
    judgment = body.get("judgment") if body.get("event_type") == "judgment_put" else body
    if not isinstance(judgment, dict) or judgment.get("event_id") != event_id or judgment.get("evaluation_id") != evaluation_id:
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


def main(argv):
    if len(argv) < 2 or argv[1] not in ("put", "get", "show-config"):
        print("用法: python3 -m src.phase1.gateway_client show-config|put|get --config FILE [--send]")
        return 2
    if "--send" not in argv:
        print(json.dumps({
            "sent": False,
            "reason": "gateway_disabled",
            "live_send": False,
            "endpoint_guessed": False,
            "discretion_complete": False,
            "old_ai_poll": "keep",
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
        if not judgment_path:
            print(json.dumps({"sent": False, "reason": "parent_judgment_missing", "old_ai_poll": "keep"}, ensure_ascii=False))
            return 2
        with open(judgment_path, encoding="utf-8") as handle:
            document = json.load(handle)

    def transport(method, path, headers, body):
        return https_exchange(config["base_url"], method, path, headers, body)

    ledger = config_path + ".ledger"
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
            config, transport, event_id, state_dir, evaluation_id, sent_at, now, ledger_path=ledger, now_epoch=now_epoch,
        )
        print(json.dumps({key: result.get(key) for key in ("sent", "status", "reason", "applied", "written", "old_ai_poll", "discretion_complete", "process_count")}, ensure_ascii=False))
        return 0 if result.get("applied") is True else 1
    result = authorized_exchange(config, transport, "PUT", event_id, document, ledger_path=ledger, now_epoch=now_epoch)
    print(json.dumps({key: result.get(key) for key in ("sent", "status", "reason", "old_ai_poll", "discretion_complete", "written")}, ensure_ascii=False))
    return 0 if result.get("status") in (200, 201) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
