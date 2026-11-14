#!/usr/bin/env python3
"""幣安 USDT-M 使用者資料流。只用標準庫，Python 3.9 可跑，不必 pip 安裝。

listenKey：POST 建立、每 30 分鐘 PUT 續期。斷線或 listenKey 過期就重連。
成交（ORDER_TRADE_UPDATE）或帳戶更新（ACCOUNT_UPDATE）時呼叫 live_trader.cmd_manage。
心跳寫在資料目錄的 .ws_watch_heartbeat。60 秒的 watch --live 看到心跳還新就不再輪詢。
"""
import argparse
import json
import os
import socket
import ssl
import time
import urllib.request

HEARTBEAT_NAME = ".ws_watch_heartbeat"
HEARTBEAT_MAX_AGE_S = 90
KEEPALIVE_S = 30 * 60
WS_HOST = "fstream.binance.com"


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
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read() or b"null"
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


def run_loop(connect, stop, on_manage, sleep, touch, keepalive, keepalive_every=KEEPALIVE_S, now=None):
    """connect() 回傳一個有 messages() 的物件，或直接丟出例外表示要重連。"""
    last_ka = now() if now else time.time()
    while not stop():
        try:
            sock = connect()
            touch()
            for raw in sock.messages():
                touch()
                current = now() if now else time.time()
                if current - last_ka >= keepalive_every:
                    keepalive()
                    last_ka = current
                if event_needs_manage(raw):
                    on_manage(raw)
                if stop():
                    return
        except Exception:
            import live_trader
            live_trader.log("user_stream_reconnect", err="disconnected")
            sleep(1)
            continue


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def iter_ws_messages(sock):
    """讀 websocket 幀。只處理文字、ping 與關閉。"""
    while True:
        header = _read_exact(sock, 2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(_read_exact(sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(_read_exact(sock, 8), "big")
        payload = _read_exact(sock, length) if length else b""
        if opcode == 0x8:
            raise ConnectionError("ws close")
        if opcode == 0x9:
            # pong，mask 後送回。伺服器 ping 通常未遮罩。
            frame = bytes([0x8A, len(payload)]) + payload
            sock.sendall(frame)
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
        raise ConnectionError(status.decode(errors="replace"))

    class _Sock(object):
        def messages(self):
            return iter_ws_messages(tls)
    return _Sock()


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
        live_trader.cmd_manage(argparse.Namespace(live=args.live))

    run_loop(connect, lambda: state["stop"], on_manage, time.sleep, touch_heartbeat, keepalive_listen_key)


if __name__ == "__main__":
    main()
