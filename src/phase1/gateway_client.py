"""對 gateway 子目錄的 PUT/GET 契約。沒有 transport 就不開網路。"""
import json

MAX_BYTES = 64 * 1024


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
