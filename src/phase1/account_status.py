"""只讀三個帳戶查詢。不開鎖、不寫 state、不建 data/、不呼叫 day_state。

這不是舊 state.json 快照。金鑰只從既有環境或 BINANCE_ENV_FILE 讀，不複製 .env。
"""
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .status_contract import failure_document, require_account, require_order_list, success_document
from .timeparse import parse_instant

BASE = "https://fapi.binance.com"


def _signed_get(path):
    try:
        from config import load_api_keys
    except ImportError:
        from src.config import load_api_keys
    try:
        key, secret = load_api_keys()
    except SystemExit:
        raise ValueError("missing_keys")
    params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
    query = urllib.parse.urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = "{0}{1}?{2}&signature={3}".format(BASE, path, query, signature)
    request = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read() or "null")


def query_status(now, get_path=None):
    """任一查詢不是規定容器就失敗。不輸出餘額。"""
    moment = parse_instant(now)
    as_of = moment.isoformat(timespec="seconds")
    getter = get_path or _signed_get
    queries = {"account": False, "open_orders": False, "open_algo_orders": False}
    try:
        account = require_account(getter("/fapi/v2/account"))
        queries["account"] = True
        orders = require_order_list(getter("/fapi/v1/openOrders"))
        queries["open_orders"] = True
        algo = require_order_list(getter("/fapi/v1/openAlgoOrders"))
        queries["open_algo_orders"] = True
    except (ValueError, TypeError, KeyError, OSError, urllib.error.URLError):
        return failure_document(queries), 2
    positions = [row for row in account["positions"] if float(row["positionAmt"]) != 0]
    return success_document(len(positions), len(orders), len(algo), as_of), 0


def main(argv):
    now = None
    index = 0
    while index < len(argv):
        if argv[index] != "--now" or index + 1 >= len(argv):
            print("用法: python3 -m src.phase1.account_status [--now TIME]")
            return 2
        now = argv[index + 1]
        index += 2
    if now is None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        document, code = query_status(now)
    except ValueError:
        document, code = failure_document({"account": False, "open_orders": False, "open_algo_orders": False}), 2
    print(json.dumps(document, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
