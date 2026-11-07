"""對 gateway 的 PUT/GET。預設關閉。沒有明確授權與注入 transport 就不會外發。"""
import json
import os

MAX_BYTES = 64 * 1024
_TRIAL_KEYS = ("approved", "dry_run_only", "new_ids", "max_wake_posts", "max_judgment_puts", "max_gets", "deadline_seconds")


def gateway_exchange(transport, method, installation_id, event_id, key_id, secret, document=None):
    if transport is None:
        raise ValueError("gateway_transport_required")
    if method not in ("PUT", "GET"):
        raise ValueError("method_not_allowed")
    path = "/v1/installations/{0}/judgments/{1}".format(installation_id, event_id)
    headers = {
        "accept": "application/json",
        "authorization": "Bearer {0}:{1}".format(key_id, secret),
    }
    body = None
    if method == "PUT":
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("too_large")
        headers["content-type"] = "application/json"
        body = raw
    status, response_headers, response_body = transport(method, path, headers, body)
    cache = ""
    if isinstance(response_headers, dict):
        cache = response_headers.get("Cache-Control") or response_headers.get("cache-control") or ""
    parsed = None
    if response_body:
        parsed = json.loads(response_body.decode("utf-8"))
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
        if trial.get("new_ids") != 1 or trial.get("max_wake_posts") != 2 or trial.get("max_judgment_puts") != 1:
            raise ValueError("trial_not_authorized")
        if not isinstance(trial.get("max_gets"), int) or isinstance(trial.get("max_gets"), bool) or not 1 <= trial["max_gets"] <= 6:
            raise ValueError("trial_not_authorized")
        if trial.get("deadline_seconds") != 600:
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


def authorized_exchange(config, transport, method, event_id, document=None, sent_count=0, secret=None):
    """live_send 仍要注入 transport。本函式不建立連線。"""
    if config.get("enabled") is not True:
        return {"sent": False, "reason": "gateway_disabled", "old_ai_poll": "keep"}
    if config.get("live_send") is not True:
        return {"sent": False, "reason": "live_send_disabled", "old_ai_poll": "keep"}
    trial = config["trial"]
    if method == "PUT" and sent_count >= trial["max_judgment_puts"]:
        return {"sent": False, "reason": "put_budget_exhausted", "old_ai_poll": "keep"}
    if method == "GET" and sent_count >= trial["max_gets"]:
        return {"sent": False, "reason": "get_budget_exhausted", "old_ai_poll": "keep"}
    if transport is None:
        raise ValueError("gateway_transport_required")
    if not secret:
        secret = os.environ.get(config["secret_env"])
    if not secret:
        return {"sent": False, "reason": "secret_not_configured", "old_ai_poll": "keep"}
    try:
        result = gateway_exchange(
            transport, method, config["installation_id"], event_id, config["key_id"], secret, document,
        )
    except (TimeoutError, OSError):
        return {"sent": False, "reason": "timeout", "old_ai_poll": "keep"}
    result["sent"] = True
    result["old_ai_poll"] = "keep"
    result["discretion_complete"] = False
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

    result = authorized_exchange(config, transport, "PUT" if argv[1] == "put" else "GET", event_id, document)
    print(json.dumps({key: result.get(key) for key in ("sent", "status", "reason", "old_ai_poll", "discretion_complete")}, ensure_ascii=False))
    return 0 if result.get("reason") in ("gateway_disabled", "live_send_disabled", "trial_not_authorized", "endpoint_not_provided") or result.get("status") in (200, 201) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
