#!/usr/bin/env python3
"""幣安 USDT-M 使用者資料流。只用標準庫，Python 3.9 可跑，不必 pip 安裝。

listenKey：POST 建立、每 30 分鐘 PUT 續期。斷線或 listenKey 過期就重連。
成交（ORDER_TRADE_UPDATE）或帳戶更新（ACCOUNT_UPDATE）先合併約 2.5 秒，再呼叫一次 manage。
manage 跟 watch、open 用同一把檔案鎖。拿不到就晚一點再試，不會把這個程序關掉。
心跳寫在資料目錄的 .ws_watch_heartbeat。60 秒的 watch --live 看到心跳還新就不再輪詢。
log 只記例外類型和 HTTP 狀態，不記 listenKey。
"""
import argparse
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request

HEARTBEAT_NAME = ".ws_watch_heartbeat"
HEARTBEAT_MAX_AGE_S = 90
KEEPALIVE_S = 30 * 60
WS_HOST = "fstream.binance.com"
# 連線可以等 20 秒。連上之後的 recv 用這個，避免閒置 20 秒就被當成斷線。
IDLE_RECV_S = 180
MANAGE_COALESCE_S = 2.5
MANAGE_RETRY_MAX = 3
BACKOFF_MAX_S = 60
# run_loop 寫入，connect_fstream 的 recv 逾時讀它，這樣合併視窗到了會自己醒。
_manage_due = {"at": None, "now": None}


class LockBusy(Exception):
    """檔案鎖被 watch、manage 或 open 佔著。"""


class StreamRateLimit(Exception):
    def __init__(self, status, retry_after=0):
        self.status = int(status)
        try:
            self.retry_after = int(retry_after or 0)
        except (TypeError, ValueError):
            self.retry_after = 0
        super(StreamRateLimit, self).__init__("HTTP {0}".format(self.status))


def heartbeat_path():
    import live_trader
    return os.path.join(live_trader.HOME, HEARTBEAT_NAME)


def touch_heartbeat(now=None):
    path = heartbeat_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(str(time.time() if now is None else now))
    os.replace(tmp, path)


def heartbeat_fresh(max_age=HEARTBEAT_MAX_AGE_S, now=None):
    try:
        with open(heartbeat_path()) as handle:
            ts = float(handle.read().strip())
    except Exception:
        return False
    current = time.time() if now is None else now
    return (current - ts) <= max_age


def _api_request(method, path):
    import live_trader
    key, _secret = live_trader._keys()
    req = urllib.request.Request(
        live_trader.BASE + path, method=method, headers={"X-MBX-APIKEY": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read() or b"null"
    except urllib.error.HTTPError as e:
        retry = 0
        headers = getattr(e, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            try:
                retry = int(headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry = 0
        if e.code in (418, 429):
            import live_trader
            wait = retry or (60 if e.code == 418 else 1)
            live_trader.write_ratelimit(e.code, time.time() + wait)
            raise StreamRateLimit(e.code, wait)
        raise
    return json.loads(body.decode())


def create_listen_key():
    data = _api_request("POST", "/fapi/v1/listenKey")
    return data["listenKey"]


def keepalive_listen_key():
    _api_request("PUT", "/fapi/v1/listenKey")


def event_needs_manage(msg):
    if not isinstance(msg, dict):
        return False
    kind = msg.get("e")
    if kind == "ACCOUNT_UPDATE":
        return True
    if kind != "ORDER_TRADE_UPDATE":
        return False
    order = msg.get("o") or {}
    return order.get("x") == "TRADE" or order.get("X") in ("FILLED", "PARTIALLY_FILLED")


def _log_stream(kind, **kw):
    import live_trader
    live_trader.log(kind, **kw)


def _client_frame(opcode, payload):
    """客戶端幀。RFC 6455 要求必須加遮罩。"""
    payload = payload or b""
    key = os.urandom(4)
    masked = bytes(payload[i] ^ key[i % 4] for i in range(len(payload)))
    first = 0x80 | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        head = bytes([first, 0x80 | n])
    elif n < 65536:
        head = bytes([first, 0x80 | 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([first, 0x80 | 127]) + n.to_bytes(8, "big")
    return head + key + masked


def _unmask(payload, key):
    return bytes(payload[i] ^ key[i % 4] for i in range(len(payload)))


def run_loop(connect, stop, on_manage, sleep, touch, keepalive, keepalive_every=KEEPALIVE_S, now=None):
    """connect() 回傳一個有 messages() 的物件。messages() 在閒置時 yield None，不要因此重連。"""
    def current():
        return now() if now else time.time()

    last_ka = current()
    backoff = 1
    due = None
    queued = None
    manage_tries = 0
    _manage_due["now"] = now

    def fire():
        nonlocal due, queued, manage_tries
        raw = queued
        try:
            on_manage(raw if isinstance(raw, dict) else None)
        except LockBusy:
            due = current() + 1
            _manage_due["at"] = due
            return
        except Exception as e:
            manage_tries += 1
            _log_stream("user_stream_manage", err=type(e).__name__, attempt=manage_tries)
            if manage_tries < MANAGE_RETRY_MAX:
                delay = min(BACKOFF_MAX_S, 2 ** manage_tries)
                due = current() + delay
                _manage_due["at"] = due
                return
        due = None
        queued = None
        manage_tries = 0
        _manage_due["at"] = None

    def consider(raw):
        nonlocal due, queued
        cur = current()
        if isinstance(raw, dict) and event_needs_manage(raw):
            queued = raw
            if due is None:
                due = cur + MANAGE_COALESCE_S
                _manage_due["at"] = due
        if due is not None and cur >= due:
            fire()

    def honor_shared_limit():
        """共用退避檔還有效就先睡，這段時間不連線、不申請 listenKey。418 睡完就停。"""
        import live_trader
        gate = live_trader.read_ratelimit()
        if not gate:
            return False
        status, until = gate
        remain = until - time.time()
        if remain <= 0:
            return False
        _log_stream("user_stream_backoff", status=status, wait=round(remain, 3))
        sleep(remain)
        return status == 418

    while not stop():
        try:
            if honor_shared_limit():
                return
            sock = connect()
            touch()
            last_frame = current()
            for raw in sock.messages():
                backoff = 1
                cur = current()
                if raw is None:
                    if cur - last_frame >= 2 * IDLE_RECV_S:
                        raise ConnectionError("idle")
                else:
                    last_frame = cur
                    touch()
                if cur - last_ka >= keepalive_every:
                    keepalive()
                    last_ka = cur
                consider(raw)
                if stop():
                    return
            if due is not None:
                fire()
            if stop():
                return
            raise ConnectionError("stream ended")
        except StreamRateLimit as e:
            _manage_due["at"] = None
            import live_trader
            wait = e.retry_after or (60 if e.status == 418 else backoff)
            live_trader.write_ratelimit(e.status, time.time() + wait)
            if e.status == 418:
                _log_stream("user_stream_stop", status=418, wait=wait)
                sleep(wait)
                return
            _log_stream("user_stream_backoff", status=e.status, wait=wait)
            backoff = min(max(backoff * 2, 2), BACKOFF_MAX_S)
        except Exception as e:
            _manage_due["at"] = None
            _log_stream("user_stream_reconnect", err=type(e).__name__)
            sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _recv_timeout(sock):
    remain = None
    at = _manage_due.get("at")
    if at is not None:
        clock = _manage_due.get("now")
        cur = clock() if clock else time.time()
        remain = at - cur
    timeout = IDLE_RECV_S if remain is None else min(IDLE_RECV_S, max(0.05, remain))
    try:
        sock.settimeout(timeout)
    except Exception:
        pass


def iter_ws_messages(sock):
    """讀 websocket 幀。閒置逾時送出遮罩過的 ping，並 yield None，不當成斷線。"""
    while True:
        _recv_timeout(sock)
        try:
            header = _read_exact(sock, 2)
        except socket.timeout:
            try:
                sock.sendall(_client_frame(0x9, b""))
            except Exception:
                raise ConnectionError("ping failed")
            yield None
            continue
        try:
            sock.settimeout(IDLE_RECV_S)
        except Exception:
            pass
        opcode = header[0] & 0x0F
        masked = header[1] & 0x80
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(_read_exact(sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(_read_exact(sock, 8), "big")
        mask_key = _read_exact(sock, 4) if masked else None
        payload = _read_exact(sock, length) if length else b""
        if mask_key:
            payload = _unmask(payload, mask_key)
        if opcode == 0x8:
            raise ConnectionError("ws close")
        if opcode == 0x9:
            sock.sendall(_client_frame(0xA, payload))
            yield ("frame",)
            continue
        if opcode == 0xA:
            yield ("frame",)
            continue
        if opcode == 0x1:
            yield json.loads(payload.decode())


def connect_fstream(listen_key):
    raw = socket.create_connection((WS_HOST, 443), timeout=20)
    tls = ssl.create_default_context().wrap_socket(raw, server_hostname=WS_HOST)
    path = "/ws/{0}".format(listen_key)
    req = (
        "GET {0} HTTP/1.1\r\nHost: {1}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).format(path, WS_HOST)
    tls.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = tls.recv(4096)
        if not chunk:
            raise ConnectionError("handshake failed")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b"101" not in status:
        raise ConnectionError("handshake rejected")
    # 握手用的 20 秒不能留在連線上，否則沒事件時會一直重連、一直重拿 listenKey。
    try:
        tls.settimeout(IDLE_RECV_S)
    except Exception:
        pass

    class _Sock(object):
        def messages(self):
            return iter_ws_messages(tls)
    return _Sock()


def manage_under_lock(live):
    """跟 main() 同一把非阻塞鎖。拿不到就丟 LockBusy，不把 SystemExit 傳出去。"""
    import live_trader
    lock = live_trader.Lock()
    try:
        lock.__enter__()
    except SystemExit:
        raise LockBusy()
    try:
        live_trader.cmd_manage(argparse.Namespace(live=live))
    finally:
        lock.__exit__(None, None, None)


def main(argv=None):
    import live_trader
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    state = {"stop": False}

    def connect():
        key = create_listen_key()
        return connect_fstream(key)

    def on_manage(_msg):
        manage_under_lock(args.live)

    run_loop(connect, lambda: state["stop"], on_manage, time.sleep, touch_heartbeat, keepalive_listen_key)


if __name__ == "__main__":
    main()
