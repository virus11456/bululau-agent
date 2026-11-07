"""本機 sender 設定與單筆五欄測試。不讀交易金鑰，不跟隨重新導向。

設定只寫檔。發送要另外下 send-test。HTTP 成功不代表處理完成。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

CONFIG_DIR = ".config/bululau-agent"
CONFIG_NAME = "sender.json"
EVENT_NAME = "trade_candidate_test.json"
EVENT_FIELDS = ("schema_version", "event_type", "event_id", "test_only", "sent_at")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SENT_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
REDIRECTS = {301, 302, 303, 307, 308}


def default_config_dir():
    return os.path.join(os.path.expanduser("~"), ".config", "bululau-agent")


def _reject_crlf(value):
    if "\r" in value or "\n" in value:
        raise ValueError("rejected_crlf")


def validate_url(url):
    if not isinstance(url, str) or not url:
        raise ValueError("rejected_url")
    _reject_crlf(url)
    if not url.startswith("https://") or url.startswith("https:///") or " " in url:
        raise ValueError("rejected_non_https")


def validate_authorization(value):
    if not isinstance(value, str) or value == "":
        raise ValueError("rejected_authorization")
    _reject_crlf(value)


def validate_event(event):
    if not isinstance(event, dict) or set(event) != set(EVENT_FIELDS):
        raise ValueError("rejected_event_fields")
    version = event["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("rejected_schema_version")
    if event["event_type"] != "trade_candidate_test":
        raise ValueError("rejected_event_type")
    if not isinstance(event["event_id"], str) or EVENT_ID_RE.fullmatch(event["event_id"]) is None:
        raise ValueError("rejected_event_id")
    if event["test_only"] is not True:
        raise ValueError("rejected_test_only")
    sent = event["sent_at"]
    if not isinstance(sent, str) or SENT_AT_RE.fullmatch(sent) is None:
        raise ValueError("rejected_sent_at")
    return event


def _write_private(directory, name, obj):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
    except Exception:
        os.chmod(path, 0o600)
        raise
    os.chmod(path, 0o600)
    return path


def write_config(directory, url, authorization):
    validate_url(url)
    validate_authorization(authorization)
    return _write_private(directory, CONFIG_NAME, {"webhook_url": url, "authorization": authorization})


def load_config(directory):
    path = os.path.join(directory, CONFIG_NAME)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or set(data) != {"webhook_url", "authorization"}:
        raise ValueError("rejected_config_fields")
    validate_url(data["webhook_url"])
    validate_authorization(data["authorization"])
    return data


def _new_event(now):
    if now.tzinfo is None:
        raise ValueError("rejected_sent_at")
    import secrets
    event = {
        "schema_version": 1,
        "event_type": "trade_candidate_test",
        "event_id": "t-" + secrets.token_hex(16),
        "test_only": True,
        "sent_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return validate_event(event)


def load_or_create_event(directory, now):
    path = os.path.join(directory, EVENT_NAME)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return validate_event(json.load(handle))
    event = _new_event(now)
    _write_private(directory, EVENT_NAME, event)
    return event


class RedirectBlocked(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectBlocked()


class UrllibTransport:
    """只有 send-test 會用到。不跟隨重新導向。測試不要呼叫它。"""

    def post(self, url, authorization, body):
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Authorization", authorization)
        request.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=15) as response:
                response.read(0)
                return response.status
        except RedirectBlocked:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in REDIRECTS:
                raise RedirectBlocked()
            return exc.code


def send_saved_test(directory, transport, now):
    """送出已保存的測試事件。成功的 HTTP 不把 pending 改成已處理。"""
    config = load_config(directory)
    event = load_or_create_event(directory, now)
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        status = transport.post(config["webhook_url"], config["authorization"], body)
    except Exception:
        return {"ok": False, "processing_ack": False, "pending": True, "error": "sender_failed", "test_channel": True, "position_channel": False}
    if status in REDIRECTS:
        return {"ok": False, "processing_ack": False, "pending": True, "error": "redirect_blocked", "test_channel": True, "position_channel": False}
    return {
        "ok": 200 <= status < 300,
        "http_status": status,
        "processing_ack": False,
        "pending": True,
        "event_id": event["event_id"],
        "test_channel": True,
        "position_channel": False,
    }


def configure_interactive(directory, read_pair=None):
    if read_pair is None:
        if not sys.stdin.isatty():
            raise SystemExit("請在本機終端執行。不從聊天或管線讀取。")
        import getpass
        url = getpass.getpass("Webhook HTTPS URL（不顯示）: ")
        authorization = getpass.getpass("Authorization 整欄原樣貼上（不顯示，程式不加前綴）: ")
    else:
        url, authorization = read_pair()
    write_config(directory, url, authorization)
    print("已寫入本機設定，尚未發送。")


def main(argv, directory=None, read_pair=None, transport=None, now=None):
    directory = directory or default_config_dir()
    now = now or datetime.now(timezone.utc)
    if argv == ["configure"]:
        configure_interactive(directory, read_pair)
        return 0
    if argv == ["send-test"]:
        result = send_saved_test(directory, transport or UrllibTransport(), now)
        if result.get("error"):
            print("測試發送失敗。沒有寫出網址或認證。事件仍是 pending。")
            return 1
        print("測試請求已送出。HTTP 成功不等於處理完成，事件仍是 pending。這不是持倉通道。")
        return 0
    print("用法: python3 -m src.phase1.sender_setup configure|send-test")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
