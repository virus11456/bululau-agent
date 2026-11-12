#!/usr/bin/env python3
"""省 token 閘門：每輪先跑這支，只有「真的需要判斷」才交給 Grok。

做法跟現行 routine 每輪的機械步驟一樣，只是改由程式跑：
  1. live_trader.py manage --live   （原本每輪就會跑）
  2. live_trader.py status          （只讀）
  3. prefilter.py --quiet           （只讀公開行情）
  4. hardfilter.py                  （只讀公開 K 線；有候選才跑。把確定不符合現行硬條件的候選拿掉）

第一行輸出：
  QUIET  → 新鮮、可信的 0 倉 0 單 0 候選，上一輪也是空的。本輪結束，不叫 AI。
  WAKE   → 其他所有情況（有倉、有單、有候選、狀態變化、風控旗標、任何錯誤或看不懂）。
           後面附上三支命令的輸出，Grok 直接用，不必重跑。

prefilter 只有在 stdout／stderr 辨識得出幣安 429 或 418 時才退避重試（最多再 2 次）。
結束碼 2 也代表其他行情失敗，不能單靠結束碼重試。重試後仍失敗一樣 WAKE。

結束碼：0 = QUIET，10 = WAKE。閘門本身出錯也一律 WAKE，絕不把未知當成空倉。
讀取失敗、過期、前後矛盾、上一輪狀態不可信，都是 WAKE。

帳戶狀態不可信（401、讀不到持倉／掛單、讀數過期）時，公開 prefilter 仍照跑。
這時只把候選的幣種、型態、訊號 K 記進狀態，不記階梯、停損、目標。
持倉／掛單恢復可信、而且這輪 prefilter 與 hardfilter 都跑完之後的第一輪，
才把「當時記過、且這輪重跑後仍留在 kept」的候選交給 AI（理由 catchup_review）。
交給 AI 的是這輪現算的候選，不是斷線期間存下的舊掛單價。狀態仍不可信就不補審。
訊號若已不在這輪 prefilter 的結果裡，就不補審，也不另放寬窗口。

每輪可記對外 IP。只認 live_trader 的 `HTTP 401 code -2015`（幣安錯誤那一句），
錢包 401.00、未實現 -2015.00 不算。認到才加 ip_not_whitelisted，本來就會 WAKE。
IP 優先取該句裡的 request ip。沒有 request ip 才查 ipify。沒有 401 -2015 時，
ipify 最多每小時一次，用來記 ip_changed，不改 QUIET／WAKE。查詢在新鮮度時鐘
與判定之後。失敗記「未取得」。

只用標準庫，Python 3.8+。不讀金鑰、不改 live_trader 的檔案、不自己下單。
"""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))
QUIET, WAKE = 0, 10
LOCK_BUSY = "另一個 live_trader 正在執行"
MAX_TAIL = 4000

# 與 main() 裡實際傳給 subprocess 的逾時相同。新鮮度用它們推上限，避免另造一組策略數字。
MANAGE_TIMEOUT_S = 180
STATUS_TIMEOUT_S = 90
PREFILTER_TIMEOUT_S = 240
# hardfilter 只在有候選時跑，只會減少候選。它在新鮮度檢查時間點之後才跑，所以不影響 READING_MAX_AGE。
HARDFILTER_TIMEOUT_S = 60
# prefilter 因 429／418 失敗才重試。結束碼 2 同時是限流與其他行情失敗，不能當辨識依據。
# 最多再試 2 次（一共最多 3 次呼叫）。沒有可用的 Retry-After 就等 5 秒、再 15 秒。
# 單次等待超過 45 秒，或累計睡眠會超過 60 秒：不等、也不再打下一發，直接走原本的失敗路徑。
# 另外，重試帶來的額外牆鐘（從第一次辨識到 429／418 起算，不含第一次呼叫本身）最多 120 秒。
# 第一次呼叫逾時維持 240 秒。進入重試後，每次逾時 = min(240, 剩餘的 120 秒預算)。
# 剩餘 <= 0，或剩餘蓋不住這次等待（下一發會沒有時間），就不再重試，記 budget_exhausted。
PREFILTER_MAX_RETRIES = 2
PREFILTER_BACKOFF_S = (5.0, 15.0)
PREFILTER_WAIT_CAP_S = 45.0
PREFILTER_TOTAL_WAIT_CAP_S = 60.0
PREFILTER_RETRY_WALL_S = 120.0
# 部署中的 public prefilter（與 src/prefilter.py）失敗時印：
#   {"error": "rate_limited: 429 on /fapi/v1/klines", ...}  結束碼 2
# 11/10 21:01 的 prefilter_log 原文就是 "rate_limited: 429 on /fapi/v1/klines"。
# 第三種是 urllib 沒被接住時的 "HTTP Error 429"。沒有這些字樣就不重試。
_PREFILTER_RATE_LIMIT_RE = re.compile(
    r"rate_limited:\s*(429|418)\b"
    r"|\b(429|418)\s+on\s+/fapi/"
    r"|HTTP(?:\s*Error)?\s+(429|418)\b",
    re.IGNORECASE,
)
_RETRY_AFTER_RE = re.compile(r"retry-after\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# status 的「時間」只印到分鐘，解析成該分鐘的 00 秒，年齡最多被多算不滿 1 分鐘。
STATUS_MINUTE_TRUNCATION_S = 60
# 待擁有者確認的餘裕。不是停損、倉位、下單或 30 分鐘排程的參數。
READING_FRESHNESS_MARGIN_S = 90
# 時鐘超前超過這個就不可信。同一台機器上正常應接近 0。待擁有者確認。
READING_MAX_FUTURE_SKEW = timedelta(seconds=120)
# status 時間印在 status 一開始；檢查在 status 與 prefilter 都結束之後。
# 正常年齡 ≈ status 逾時 + prefilter 逾時 + 分鐘捨去 + 上面的餘裕。
READING_MAX_AGE = timedelta(seconds=(
    STATUS_TIMEOUT_S + PREFILTER_TIMEOUT_S + STATUS_MINUTE_TRUNCATION_S + READING_FRESHNESS_MARGIN_S
))
# 對外 IP 只供 gate_log 與 401 -2015 的原因。不是策略、風控或新鮮度參數。
# 查詢在 decide() 與 checked 時鐘之後，失敗記「未取得」，不改 QUIET／WAKE。
PUBLIC_IP_URL = "https://api.ipify.org"
PUBLIC_IP_TIMEOUT_S = 2.0
PUBLIC_IP_LOOKUP_INTERVAL = timedelta(hours=1)
PUBLIC_IP_UNKNOWN = "未取得"
PUBLIC_IP_SOURCE_BINANCE = "binance_error"
PUBLIC_IP_SOURCE_LOOKUP = "ipify"
IP_NOT_WHITELISTED = "ip_not_whitelisted"
# live_trader.BinanceError 的字串是 "HTTP {status} code {code}: {msg}"，
# main() 再印 "幣安錯誤: {e}"。只認這一句裡的 "HTTP 401 code -2015"。
# 後面若緊接數字或小數點（401.00、-2015.00）不算。
_IP_WHITELIST_RE = re.compile(r"HTTP 401 code -2015(?![\d.])")
_REQUEST_IP_RE = re.compile(r"request\s*ip\s*[:=]\s*([0-9A-Fa-f:.]+)", re.IGNORECASE)

# manage 輸出出現這些字，代表這輪有動作或風控，要叫 AI 回報。
# 「孤兒條件單」是空倉時清掉殘留條件單印的那一行，原文不含「撤」或「取消」。
MANAGE_ALERT_WORDS = (
    "!!!", "停開新倉", "全平", "成交", "平倉", "撤", "取消", "補掛", "錯誤", "Error", "error", "失敗", "Traceback",
    "孤兒條件單",
)
# 與 live_trader.ALGO_ORDERS_UNREADABLE 同一句。status 裡出現就不可信，即使結束碼是 0、即使寫著 0 張。
STATUS_ALGO_UNREADABLE = "條件單讀取失敗"


def now_tpe():
    return datetime.now(TPE)


def _tail(text, limit=MAX_TAIL):
    text = text or ""
    return text if len(text) <= limit else "…(前略)…\n" + text[-limit:]


def run_cmd(argv, timeout, cwd=None, env=None, lock_retries=5, lock_wait=3.0, sleep=time.sleep, input_text=None):
    """跑一支命令。碰到 live_trader 檔案鎖（60 秒 watch 正在跑）就等一下重試。"""
    attempt = 0
    while True:
        try:
            proc = subprocess.run(
                argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False,
                input=input_text,
            )
            result = {"code": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or "", "timeout": False}
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return {"code": None, "stdout": out, "stderr": "timeout", "timeout": True}
        except OSError as exc:
            return {"code": None, "stdout": "", "stderr": "{0}: {1}".format(type(exc).__name__, exc), "timeout": False}
        busy = LOCK_BUSY in result["stdout"] or LOCK_BUSY in result["stderr"]
        if busy and attempt < lock_retries:
            attempt += 1
            sleep(lock_wait)
            continue
        result["lock_busy"] = busy
        return result


_POS_RE = re.compile(r"^持倉\s+(\d+)\s*/\s*\d+\s*$")
_ORD_RE = re.compile(r"^一般掛單\s+(\d+)\s+條件單\s+(\d+)\s*$")
_FLAG_RE = re.compile(r"^停開新倉\s+(True|False)\s+全平旗標\s+(True|False)\s*$")
_TIME_RE = re.compile(r"^時間\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+台北$")


def _as_tpe(when):
    if when is None:
        return None
    if when.tzinfo is None:
        return when.replace(tzinfo=TPE)
    return when


def _parse_clock(text):
    """prefilter 的 time。解析不了就回 None。沒有時區的時間當作台北。"""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return _as_tpe(parsed)


def reading_is_fresh(when, now):
    """讀數時間距檢查當下沒有超過 READING_MAX_AGE，也沒有超前超過容許誤差。"""
    when, now = _as_tpe(when), _as_tpe(now)
    if when is None or now is None:
        return False
    if now - when > READING_MAX_AGE:
        return False
    if when - now > READING_MAX_FUTURE_SKEW:
        return False
    return True


def parse_status(text):
    """解析 live_trader.py status 的文字輸出。任何一項讀不到就回 None（＝不可信）。

    除了表頭數字，也核對表頭下面列出的明細行數，數字和明細對不上一律不可信。
    必須有「時間 YYYY-MM-DD HH:MM 台北」。條件單讀取失敗的標記一律不可信。
    """
    raw = text or ""
    if STATUS_ALGO_UNREADABLE in raw:
        return None
    lines = [line.rstrip() for line in raw.splitlines()]
    positions = orders = algo = halted = killed = as_of = None
    pos_rows = order_rows = 0
    section = None
    for line in lines:
        stripped = line.strip()
        match = _TIME_RE.match(stripped)
        if match and as_of is None:
            as_of = datetime.strptime(
                "{0} {1}".format(match.group(1), match.group(2)), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=TPE)
            section = None
            continue
        match = _POS_RE.match(stripped)
        if match:
            positions, section = int(match.group(1)), "pos"
            continue
        match = _ORD_RE.match(stripped)
        if match:
            orders, algo, section = int(match.group(1)), int(match.group(2)), "ord"
            continue
        match = _FLAG_RE.match(stripped)
        if match:
            halted, killed = match.group(1) == "True", match.group(2) == "True"
            section = None
            continue
        if line.startswith("  ") and stripped:
            if section == "pos":
                pos_rows += 1
            elif section == "ord":
                order_rows += 1
            continue
        section = None
    if None in (positions, orders, algo, halted, killed, as_of):
        return None
    if pos_rows != positions or order_rows != orders + algo:
        return None
    return {
        "positions": positions, "orders": orders, "algo_orders": algo,
        "halted_new": halted, "killed": killed, "as_of": as_of,
    }


def parse_prefilter(result):
    """prefilter 結束碼 0 = 沒有候選，10 = 有候選，其他 = 行情失敗。0 也要 JSON 說 call_ai false 才算數。"""
    if result.get("code") not in (0, 10):
        return None
    body = None
    for line in reversed((result.get("stdout") or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                body = json.loads(line)
            except ValueError:
                body = None
            break
    if not isinstance(body, dict) or "call_ai" not in body:
        return None
    if body.get("error"):
        return None
    n = body.get("n_candidates")
    if type(n) is not int or n < 0 or type(body.get("call_ai")) is not bool:
        return None
    candidates = body.get("candidates", None)
    # 缺清單、型別不對、或張數和清單長度不一致（含 n_candidates 為 0 但清單不是空的）都不可信。
    if not isinstance(candidates, list) or n != len(candidates):
        return None
    if result["code"] == 0 and (body["call_ai"] or n != 0):
        return None
    if result["code"] == 10 and not body["call_ai"]:
        return None
    return {
        "call_ai": body["call_ai"], "n_candidates": n, "candidates": candidates,
        "as_of": _parse_clock(body.get("time")),
    }


def parse_hardfilter(result, candidates):
    """hardfilter 結束碼 0 且 JSON 自洽才採用。kept＋blocked 必須剛好是原本的候選，否則不可信（回 None）。"""
    if result.get("code") != 0:
        return None
    body = None
    for line in reversed((result.get("stdout") or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                body = json.loads(line)
            except ValueError:
                body = None
            break
    if not isinstance(body, dict) or body.get("ok") is not True:
        return None
    kept, blocked = body.get("kept"), body.get("blocked")
    if not isinstance(kept, list) or not isinstance(blocked, list):
        return None
    if len(kept) + len(blocked) != len(candidates):
        return None
    remaining = list(candidates)
    for item in kept:
        if item not in remaining:
            return None
        remaining.remove(item)
    left = sorted(str(c.get("symbol")) + "|" + str(c.get("setup")) for c in remaining if isinstance(c, dict))
    got = []
    for item in blocked:
        if not isinstance(item, dict) or not item.get("blocked_by"):
            return None
        got.append(str(item.get("symbol")) + "|" + str(item.get("setup")))
    if len(left) != len(remaining) or sorted(got) != left:
        return None
    return {"kept": kept, "blocked": blocked, "notes": body.get("notes") or []}


def apply_hardfilter(prefilter, runner, python, path, cwd):
    """有候選才跑。失敗、逾時、看不懂：保留全部候選，標記 hardfilter_failed（＝WAKE）。回傳這次的執行結果。"""
    candidates = prefilter.get("candidates") or []
    if not candidates:
        return None
    payload = json.dumps({"candidates": candidates}, ensure_ascii=False)
    if not os.path.exists(path):
        run = {"code": None, "stdout": "", "stderr": "hardfilter not found: " + path}
    else:
        run = runner([python, path], HARDFILTER_TIMEOUT_S, cwd=cwd, lock_retries=0, input_text=payload)
    parsed = parse_hardfilter(run, candidates)
    if parsed is None:
        prefilter["hardfilter_failed"] = True
        return run
    prefilter["n_candidates_before_hardfilter"] = len(candidates)
    prefilter["hardfilter_blocked"] = parsed["blocked"]
    prefilter["candidates"] = parsed["kept"]
    prefilter["n_candidates"] = len(parsed["kept"])
    prefilter["call_ai"] = bool(parsed["kept"])
    return run


def candidate_identity(cand):
    """候選的公開身份。不帶走 ladder、stop、target、entry 這類價格。"""
    if not isinstance(cand, dict):
        return None
    symbol = cand.get("symbol")
    setup = cand.get("setup")
    if not isinstance(symbol, str) or not isinstance(setup, str):
        return None
    symbol, setup = symbol.strip().upper(), setup.strip()
    if not symbol or not setup:
        return None
    bar = cand.get("bar")
    row = {"symbol": symbol, "setup": setup}
    if isinstance(bar, str) and bar.strip():
        row["bar"] = bar.strip()
    return row


def load_pending_catchup(state):
    """讀斷線期間記下來的候選身份。壞掉的項目略過，不把整份狀態判成不可信。"""
    raw = state.get("pending_catchup") if isinstance(state, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for item in raw:
        ident = candidate_identity(item)
        if ident is None:
            continue
        key = (ident["symbol"], ident["setup"])
        if key in seen:
            continue
        seen.add(key)
        seen_at = item.get("seen_at") if isinstance(item, dict) else None
        if isinstance(seen_at, str) and seen_at.strip():
            ident["seen_at"] = seen_at.strip()
        out.append(ident)
    return out


def _merge_pending(pending, candidates, seen_at):
    """只併入幣種、型態、訊號 K、看到的時間。呼叫端傳來的價格欄位在這裡丟掉。"""
    index = {(row["symbol"], row["setup"]): row for row in pending}
    out = list(pending)
    recorded = []
    for cand in candidates or []:
        ident = candidate_identity(cand)
        if ident is None:
            continue
        row = {"symbol": ident["symbol"], "setup": ident["setup"], "seen_at": seen_at}
        if ident.get("bar"):
            row["bar"] = ident["bar"]
        key = (row["symbol"], row["setup"])
        if key in index:
            index[key].clear()
            index[key].update(row)
        else:
            out.append(row)
            index[key] = row
        recorded.append(dict(row))
    return out, recorded


def _caught_candidates(prefilter):
    """prefilter 這輪抓到的候選。hardfilter 擋下的也算「曾抓到」，但價格不往外帶。"""
    caught = list(prefilter.get("candidates") or [])
    for item in prefilter.get("hardfilter_blocked") or []:
        caught.append(item)
    return caught


def settle_catchup(pending, observed, prefilter, seen_at, hardfilter_enabled, prefilter_fresh):
    """斷線時只記身份。帳戶可信且這輪 prefilter、hardfilter 都跑完，才做一次補審。

    review 裡的物件是這輪 kept 的原物件（含這輪現算的價格）。
    pending 裡永遠沒有價格。重跑後不在 kept 的，dropped，不交給 AI。
    prefilter 失敗、hardfilter 失敗、行情不新鮮、或 hardfilter 被關掉：名單留著，這輪不補審。
    """
    pending = list(pending)
    if observed == "unknown":
        if prefilter is None:
            return {"pending": pending, "review": [], "dropped": [], "held": False, "recorded": []}
        pending, recorded = _merge_pending(pending, _caught_candidates(prefilter), seen_at)
        return {"pending": pending, "review": [], "dropped": [], "held": False, "recorded": recorded}
    if not pending:
        return {"pending": [], "review": [], "dropped": [], "held": False, "recorded": []}
    can_recheck = (
        hardfilter_enabled
        and prefilter is not None
        and prefilter_fresh
        and not prefilter.get("hardfilter_failed")
    )
    if not can_recheck:
        return {"pending": pending, "review": [], "dropped": [], "held": True, "recorded": []}
    fresh = {}
    for cand in prefilter.get("candidates") or []:
        ident = candidate_identity(cand)
        if ident is None:
            continue
        fresh[(ident["symbol"], ident["setup"])] = cand
    review, dropped = [], []
    for row in pending:
        key = (row["symbol"], row["setup"])
        if key in fresh:
            review.append(fresh[key])
        else:
            dropped.append({"symbol": row["symbol"], "setup": row["setup"]})
    return {"pending": [], "review": review, "dropped": dropped, "held": False, "recorded": []}


def catchup_log(catchup, pending_before):
    """gate_log 用。只有這輪真的有記、有補審、有丟棄或有留著，才寫。不含價格。"""
    recorded = catchup.get("recorded") or []
    review = catchup.get("review") or []
    dropped = catchup.get("dropped") or []
    held = bool(catchup.get("held"))
    if not (pending_before or recorded or review or dropped or held):
        return None

    def brief(cand):
        ident = candidate_identity(cand) or {}
        row = {"symbol": ident.get("symbol"), "setup": ident.get("setup")}
        if ident.get("bar"):
            row["bar"] = ident["bar"]
        return row

    return {
        "recorded": recorded,
        "reviewed": [brief(cand) for cand in review],
        "dropped": dropped,
        "held": held,
    }


def load_state(path):
    """讀上一輪。檔案根本不存在是第一次跑（_absent）。

    檔案在但壞掉、不是物件、或 last 不是 flat/busy，都標成不可信。
    不把這些情況跟「第一次、還沒寫過狀態」混成同一種空字典。
    """
    if not os.path.exists(path):
        return {"_absent": True}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"_untrusted": True}
    if not isinstance(data, dict):
        return {"_untrusted": True}
    if data.get("last") not in ("flat", "busy"):
        data = dict(data)
        data["_untrusted"] = True
    return data


def save_state(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, path)


def append_log(path, row):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def book_observation(status, now):
    """這一輪實際看到的帳本。沒讀到、或時間過期／超前，都記 unknown，不沿用舊的 flat。"""
    if status is None or not reading_is_fresh(status.get("as_of"), now):
        return "unknown"
    if status["positions"] > 0 or status["orders"] > 0 or status["algo_orders"] > 0:
        return "busy"
    return "flat"


def decide(manage, status_run, status, prefilter_run, prefilter, previous, today, now=None):
    """回傳 (是否 QUIET, 理由清單)。只有全部條件都明確成立才 QUIET。"""
    if now is None:
        now = now_tpe()
    reasons = []
    if manage.get("code") != 0:
        reasons.append("manage_failed" if not manage.get("lock_busy") else "manage_lock_busy")
    else:
        text = manage.get("stdout", "") + manage.get("stderr", "")
        if any(word in text for word in MANAGE_ALERT_WORDS):
            reasons.append("manage_reported_action")
    if STATUS_ALGO_UNREADABLE in (status_run.get("stdout") or "") or STATUS_ALGO_UNREADABLE in (status_run.get("stderr") or ""):
        reasons.append("status_untrusted")
    elif status_run.get("code") != 0 or status is None:
        reasons.append("status_untrusted")
    else:
        if status["positions"] > 0:
            reasons.append("positions_open")
        if status["orders"] > 0 or status["algo_orders"] > 0:
            reasons.append("orders_open")
        if status["killed"]:
            reasons.append("kill_flag")
        if status["halted_new"] and previous.get("halt_reported_day") != today:
            reasons.append("halted_new_today")
        if not reading_is_fresh(status.get("as_of"), now):
            reasons.append("status_stale")
    if prefilter is None:
        reasons.append("prefilter_failed")
    else:
        if prefilter.get("hardfilter_failed"):
            reasons.append("hardfilter_failed")
        if prefilter["call_ai"] or prefilter["n_candidates"] > 0 or prefilter.get("candidates"):
            reasons.append("candidates")
        if not reading_is_fresh(prefilter.get("as_of"), now):
            reasons.append("prefilter_stale")
    if not previous.get("_absent"):
        last = previous.get("last")
        trusted_last = (not previous.get("_untrusted")) and last in ("flat", "busy")
        if not trusted_last:
            reasons.append("previous_state_untrusted")
        elif last == "busy" and not reasons:
            # 上一輪有倉或有單，這輪突然全空：可能剛停損或剛成交出場，要叫 AI 記錄與回報一次。
            reasons.append("book_just_went_flat")
    return (not reasons), reasons


def _combined_output(result):
    return "{0}\n{1}".format(result.get("stdout") or "", result.get("stderr") or "")


def identify_prefilter_rate_limit(result):
    """從 prefilter 的 stdout／stderr 讀 429 或 418。讀不到回 None。

    結束碼不夠：2 同時是限流和其他例外。只認上面常數註解裡的三種寫法。
    """
    if not isinstance(result, dict):
        return None
    match = _PREFILTER_RATE_LIMIT_RE.search(_combined_output(result))
    if not match:
        return None
    for group in match.groups():
        if group:
            return int(group)
    return None


def prefilter_retry_after(result):
    """stdout／stderr 裡最後一個正數的 Retry-After（秒）。沒有、不是數字、或 ≤ 0 就回 None。

    部署中的 prefilter 目前不會印這個 header；沒有時改走 PREFILTER_BACKOFF_S。
    """
    if not isinstance(result, dict):
        return None
    found = None
    for match in _RETRY_AFTER_RE.finditer(_combined_output(result)):
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if value > 0:
            found = value
    return found


def prefilter_retry_wait(retry_index, retry_after, waited):
    """第 retry_index 次重試（從 1 起）要睡幾秒。不該等就回 None，呼叫端直接走失敗路徑。"""
    if retry_index > PREFILTER_MAX_RETRIES or retry_index < 1:
        return None
    if retry_after is None:
        wait = float(PREFILTER_BACKOFF_S[retry_index - 1])
    else:
        wait = float(retry_after)
    if wait > PREFILTER_WAIT_CAP_S:
        return None
    if waited + wait > PREFILTER_TOTAL_WAIT_CAP_S:
        return None
    return wait


def _retry_wall_remaining(retry_t0, monotonic):
    """距離 120 秒額外預算還剩幾秒。retry_t0 是第一次 429／418 辨識到的時刻。"""
    if retry_t0 is None:
        return PREFILTER_RETRY_WALL_S
    return PREFILTER_RETRY_WALL_S - (monotonic() - retry_t0)


def run_prefilter_with_retry(runner, argv, cwd, sleep, monotonic=None):
    """呼叫 prefilter。只有辨識為 429／418 才退避重試，最多再 2 次。

    牆鐘從第一次辨識到 429／418 起算，不含第一次呼叫。因此重試的等待與後續呼叫
    合計不超過 PREFILTER_RETRY_WALL_S（120 秒）。第一次呼叫逾時仍是 240 秒。

    回傳最後一次的執行結果，並附上 rate_limit_* 欄位給 gate_log。
    monotonic 與 sleep 可注入；測試時 sleep 要推進同一支 monotonic。
    """
    if monotonic is None:
        monotonic = time.monotonic
    waits = []
    limited = False
    stopped = None
    attempts = 0
    result = {"code": None, "stdout": "", "stderr": ""}
    retry_t0 = None
    for attempt in range(1, PREFILTER_MAX_RETRIES + 2):
        if retry_t0 is None:
            timeout = PREFILTER_TIMEOUT_S
        else:
            remaining = _retry_wall_remaining(retry_t0, monotonic)
            if remaining <= 0:
                stopped = "budget_exhausted"
                break
            timeout = min(float(PREFILTER_TIMEOUT_S), remaining)
        attempts = attempt
        result = dict(runner(argv, timeout, cwd=cwd, lock_retries=0) or {})
        if parse_prefilter(result) is not None or identify_prefilter_rate_limit(result) is None:
            break
        limited = True
        if attempt > PREFILTER_MAX_RETRIES:
            stopped = "retries_exhausted"
            break
        wait = prefilter_retry_wait(len(waits) + 1, prefilter_retry_after(result), sum(waits))
        if wait is None:
            stopped = "wait_cap"
            break
        if retry_t0 is None:
            retry_t0 = monotonic()
        remaining = _retry_wall_remaining(retry_t0, monotonic)
        # 預估所需是這次等待。剩餘比等待還少（或已 <= 0）時，下一發逾時會是 0，不再重試。
        if remaining <= 0 or remaining <= wait:
            stopped = "budget_exhausted"
            break
        sleep(wait)
        waits.append(wait)
    result["rate_limit_limited"] = limited
    result["rate_limit_attempts"] = attempts
    result["rate_limit_retries"] = len(waits)
    result["rate_limit_wait_s"] = round(sum(waits), 3)
    result["rate_limit_waits_s"] = [round(item, 3) for item in waits]
    result["rate_limit_stopped"] = stopped
    return result


def _last_json_dict(text):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            body = json.loads(line)
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return None


def hardfilter_retry_log(run):
    """hardfilter stdout 最後一個 JSON 裡的重試統計。沒有就當 0。"""
    body = _last_json_dict((run or {}).get("stdout"))
    if not isinstance(body, dict):
        return {"retries": 0, "wait_s": 0, "waits_s": [], "stopped": None}
    retries = body.get("rate_limit_retries")
    wait_s = body.get("rate_limit_wait_s")
    waits = body.get("rate_limit_waits_s")
    if type(retries) is not int or retries < 0:
        retries = 0
    if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)):
        wait_s = 0
    if not isinstance(waits, list):
        waits = []
    stopped = body.get("rate_limit_stopped")
    if stopped is not None and not isinstance(stopped, str):
        stopped = None
    return {"retries": retries, "wait_s": wait_s, "waits_s": waits, "stopped": stopped}


def normalize_ip(text):
    """IPv4 或 IPv6。不像 IP 的字串回 None。不連網。"""
    if not isinstance(text, str):
        return None
    token = text.strip().strip(".,;\"'）)")
    if not token or len(token) > 64 or any(ch.isspace() for ch in token):
        return None
    try:
        return str(ipaddress.ip_address(token))
    except ValueError:
        return None


def is_ip_whitelist_rejection(text):
    """同一份輸出裡有 live_trader 的 `HTTP 401 code -2015`。

    不認分開的 401 與 -2015，也不認錢包 401.00、未實現 -2015.00。
    """
    if not isinstance(text, str) or not text:
        return False
    return _IP_WHITELIST_RE.search(text) is not None


def extract_request_ip(text):
    """錯誤文字裡最後一個合法的 request ip。沒有回 None。"""
    if not isinstance(text, str) or not text:
        return None
    found = None
    for match in _REQUEST_IP_RE.finditer(text):
        ip = normalize_ip(match.group(1))
        if ip:
            found = ip
    return found


def lookup_public_ip(timeout=PUBLIC_IP_TIMEOUT_S, url=PUBLIC_IP_URL, urlopen=None):
    """向 ipify 要一行公開 IP。不登入、不帶幣安簽章。失敗或看不懂回 None，不丟例外。

    選 ipify：HTTPS、不用帳號、回應是單行 IP，沒有 HTML。timeout 預設 2 秒。
    幣安錯誤裡已有 request ip 時，呼叫端不該再來這裡。
    """
    if urlopen is None:
        urlopen = urllib.request.urlopen
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bululau-gate"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(128)
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="replace")
        line = ""
        for part in raw.splitlines():
            if part.strip():
                line = part.strip()
                break
        return normalize_ip(line)
    except Exception:
        return None


def whitelist_from_account(manage, status_run):
    """回傳 (是否 401 -2015, request ip 或 None)。

    request ip 只從含 `HTTP 401 code -2015` 的那一份 manage／status 輸出拿。
    這裡不查網路。
    """
    rejected = False
    parsed = None
    for result in (manage, status_run):
        if not isinstance(result, dict):
            continue
        blob = _combined_output(result)
        if not is_ip_whitelist_rejection(blob):
            continue
        rejected = True
        ip = extract_request_ip(blob)
        if ip:
            parsed = ip
    return rejected, parsed


def _public_ip_checked_at(state):
    if not isinstance(state, dict):
        return None
    text = state.get("public_ip_checked_at")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return _as_tpe(parsed)


def public_ip_lookup_due(state, now, rejected, request_ip):
    """有 request ip 不查。401 -2015 但沒有 request ip 這輪就查。

    沒有 401 -2015 時最多每小時查一次，用來更新 ip_changed。
    """
    if request_ip:
        return False
    if rejected:
        return True
    checked = _public_ip_checked_at(state)
    if checked is None or now is None:
        return checked is None
    return _as_tpe(now) - checked >= PUBLIC_IP_LOOKUP_INTERVAL


def _stored_public_ip(state):
    if not isinstance(state, dict):
        return None
    return normalize_ip(state.get("public_ip"))


def main(argv=None, runner=run_cmd, clock=now_tpe, sleep=None, monotonic=None, ip_lookup=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="省 token 閘門：空輪不叫 AI")
    ap.add_argument("--root", default=os.path.expanduser("~/quantboy-live"), help="live_trader.py 所在目錄")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--live-trader", default="live_trader.py", help="相對 --root 的檔名")
    ap.add_argument("--prefilter", default="prefilter.py", help="相對 --root 的檔名，或絕對路徑")
    ap.add_argument("--state-dir", default=here, help="閘門自己的狀態與 log 目錄")
    ap.add_argument("--no-manage", action="store_true", help="不跑 manage --live（只讀測試用）")
    ap.add_argument("--hardfilter", default=os.path.join(here, "hardfilter.py"),
                    help="硬條件篩選的路徑；寫 off 則不跑（回到只看 prefilter 的行為）")
    args = ap.parse_args(argv)
    if sleep is None:
        sleep = time.sleep
    if monotonic is None:
        monotonic = time.monotonic
    if ip_lookup is None:
        ip_lookup = lookup_public_ip

    os.makedirs(args.state_dir, exist_ok=True)
    state_path = os.path.join(args.state_dir, "gate_state.json")
    log_path = os.path.join(args.state_dir, "gate_log.jsonl")
    started = clock()
    today = started.strftime("%Y-%m-%d")
    previous = load_state(state_path)
    pending = load_pending_catchup(previous)
    pending_before = len(pending)
    catchup = {"review": [], "dropped": [], "held": False, "recorded": []}

    trader = os.path.join(args.root, args.live_trader)
    prefilter_path = args.prefilter if os.path.isabs(args.prefilter) else os.path.join(args.root, args.prefilter)
    # 先擺空結果。後面某一步炸了，已經拿到的輸出留著，WAKE 仍印出來。
    # prompt 說 manage 不必重跑，所以不能在 except 裡把已經成功的 manage 清成空的。
    manage = {"code": None, "stdout": "", "stderr": ""}
    status_run = {"code": None, "stdout": "", "stderr": ""}
    prefilter_run = {"code": None, "stdout": "", "stderr": ""}
    status = prefilter = None
    hardfilter_run = None
    hf_retry = {"retries": 0, "wait_s": 0, "waits_s": [], "stopped": None}
    try:
        if args.no_manage:
            manage = {"code": 0, "stdout": "", "stderr": "", "skipped": True}
        else:
            manage = runner([args.python, trader, "manage", "--live"], MANAGE_TIMEOUT_S, cwd=args.root)
        status_run = runner([args.python, trader, "status"], STATUS_TIMEOUT_S, cwd=args.root)
        status = parse_status(status_run.get("stdout")) if status_run.get("code") == 0 else None
        prefilter_run = run_prefilter_with_retry(
            runner, [args.python, prefilter_path, "--quiet"], args.root, sleep, monotonic,
        )
        prefilter = parse_prefilter(prefilter_run)
        checked = clock()
        if prefilter is not None and args.hardfilter != "off":
            hardfilter_run = apply_hardfilter(prefilter, runner, args.python, args.hardfilter, args.root)
        quiet, reasons = decide(manage, status_run, status, prefilter_run, prefilter, previous, today, checked)
        if "prefilter_failed" in reasons and prefilter_run.get("rate_limit_retries"):
            reasons.append("prefilter_retries={0}".format(prefilter_run["rate_limit_retries"]))
        hf_retry = hardfilter_retry_log(hardfilter_run)
        if "hardfilter_failed" in reasons and hf_retry["retries"]:
            reasons.append("hardfilter_retries={0}".format(hf_retry["retries"]))
        # 補審用的是「帳戶現在信不信得過」，不是策略門檻。不可信只記身份；可信且重跑完成才交 AI。
        catchup = settle_catchup(
            pending,
            book_observation(status, checked),
            prefilter,
            started.isoformat(timespec="seconds"),
            args.hardfilter != "off",
            prefilter is not None and reading_is_fresh(prefilter.get("as_of"), checked),
        )
        pending = catchup["pending"]
        if catchup["review"]:
            reasons.append("catchup_review")
            quiet = False
    except Exception as exc:  # 閘門自己壞掉也只能叫 AI。已取得的輸出不要清掉。
        checked = clock()
        quiet, reasons = False, ["gate_error:{0}".format(type(exc).__name__)]

    # 新鮮度用的 checked 已經定案。這裡才看 IP，所以查詢的 2 秒不會把讀數算老，
    # 也不會改 manage／status／prefilter 的逾時。401 -2015 本來就 WAKE，只多一個理由。
    ip_rejected, request_ip = whitelist_from_account(manage, status_run)
    previous_ip = _stored_public_ip(previous)
    queried = False
    looked = None
    if request_ip:
        ip_value, ip_source = request_ip, PUBLIC_IP_SOURCE_BINANCE
    elif public_ip_lookup_due(previous, started, ip_rejected, request_ip):
        queried = True
        try:
            looked = normalize_ip(ip_lookup()) if ip_lookup is not None else None
        except Exception:
            looked = None
        ip_value = looked
        ip_source = PUBLIC_IP_SOURCE_LOOKUP if looked else None
    else:
        ip_value, ip_source = None, None
    if ip_rejected and IP_NOT_WHITELISTED not in reasons:
        reasons.append(IP_NOT_WHITELISTED)
        quiet = False
    observed_ip = request_ip or looked
    ip_changed = None
    if observed_ip and previous_ip and observed_ip != previous_ip:
        ip_changed = {"from": previous_ip, "to": observed_ip}
    if queried:
        checked_at = started.isoformat(timespec="seconds")
    else:
        previous_checked = previous.get("public_ip_checked_at") if isinstance(previous, dict) else None
        checked_at = previous_checked if isinstance(previous_checked, str) else None
    if observed_ip:
        logged_ip = observed_ip
    elif queried:
        logged_ip = None
    else:
        logged_ip = previous_ip

    observed = book_observation(status, checked)
    new_state = {
        "last": observed,
        "last_run": started.isoformat(timespec="seconds"),
        "last_decision": "QUIET" if quiet else "WAKE",
        "halt_reported_day": previous.get("halt_reported_day"),
        "pending_catchup": pending,
        "public_ip": observed_ip or previous_ip,
        "public_ip_checked_at": checked_at,
    }
    # 過期的 status 不能拿來銷掉「今天已經回報過停開新倉」，否則下一輪新鮮讀數會被靜音。
    if "halted_new_today" in reasons and observed != "unknown":
        new_state["halt_reported_day"] = today
    try:
        save_state(state_path, new_state)
        logged_status = None
        if status:
            logged_status = dict(status)
            as_of = logged_status.get("as_of")
            if isinstance(as_of, datetime):
                logged_status["as_of"] = as_of.isoformat(timespec="seconds")
        append_log(log_path, {
            "time": started.isoformat(timespec="seconds"),
            "decision": new_state["last_decision"],
            "reasons": reasons,
            "status": logged_status,
            "n_candidates": prefilter["n_candidates"] if prefilter else None,
            "prefilter_retry": None if not prefilter_run.get("rate_limit_limited") else {
                "attempts": prefilter_run.get("rate_limit_attempts"),
                "retries": prefilter_run.get("rate_limit_retries"),
                "wait_s": prefilter_run.get("rate_limit_wait_s"),
                "waits_s": prefilter_run.get("rate_limit_waits_s"),
                "stopped": prefilter_run.get("rate_limit_stopped"),
            },
            "hardfilter": None if not prefilter or hardfilter_run is None else {
                "failed": bool(prefilter.get("hardfilter_failed")),
                "before": prefilter.get("n_candidates_before_hardfilter"),
                "blocked": [
                    {"symbol": b.get("symbol"), "setup": b.get("setup"), "rule": b.get("blocked_by"), "why": b.get("why")}
                    for b in prefilter.get("hardfilter_blocked", [])
                ],
                "retries": hf_retry["retries"],
                "wait_s": hf_retry["wait_s"],
                "waits_s": hf_retry["waits_s"],
                "stopped": hf_retry["stopped"],
            },
            "catchup": catchup_log(catchup, pending_before),
            "public_ip": logged_ip if logged_ip else PUBLIC_IP_UNKNOWN,
            "public_ip_source": ip_source,
            "ip_changed": ip_changed,
        })
    except OSError as exc:
        if quiet:
            quiet, reasons = False, ["gate_state_write_failed:{0}".format(type(exc).__name__)]

    if quiet:
        blocked_n = len(prefilter.get("hardfilter_blocked", [])) if prefilter else 0
        suffix = " 硬條件擋{0}".format(blocked_n) if blocked_n else ""
        print("QUIET 0倉 0單 0候選 {0}{1}".format(started.strftime("%H:%M"), suffix))
        return QUIET

    print("WAKE " + ",".join(reasons))
    if ip_rejected:
        shown = ip_value if ip_value else PUBLIC_IP_UNKNOWN
        if ip_source == PUBLIC_IP_SOURCE_BINANCE:
            origin = "幣安錯誤文字的 request ip"
        elif ip_source == PUBLIC_IP_SOURCE_LOOKUP:
            origin = "公開查詢 ipify"
        else:
            origin = "沒有拿到"
        print("ip_not_whitelisted 對外 IP {0}（{1}）。".format(shown, origin))
    print("以下是閘門剛跑過的結果（台北 {0}）。manage --live 已經跑過，不必重跑。".format(started.strftime("%Y-%m-%d %H:%M")))
    if catchup.get("review"):
        print("補審：斷線期間 prefilter 曾抓到，且本輪以最新行情重跑 prefilter 與 hardfilter 後仍留下。只列幣種與型態。階梯、停損、目標用下面本輪 prefilter 的結果，不沿用斷線期間的舊掛單價。")
        for cand in catchup["review"]:
            ident = candidate_identity(cand) or {}
            suffix = " bar={0}".format(ident["bar"]) if ident.get("bar") else ""
            print("- {0} {1}{2}".format(ident.get("symbol"), ident.get("setup"), suffix))
    sections = (
        ("manage --live", manage),
        ("status", status_run),
        ("prefilter --quiet", prefilter_run),
    )
    if hardfilter_run is not None:
        sections = sections + (("hardfilter（硬條件篩選；被擋的候選與理由在 blocked，留下的在 kept）", hardfilter_run),)
    for title, result in sections:
        print("\n=== {0}（結束碼 {1}）===".format(title, result.get("code")))
        out = _tail(result.get("stdout"))
        err = _tail(result.get("stderr"), 1000)
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print("[stderr] " + err.rstrip())
    return WAKE


if __name__ == "__main__":
    sys.exit(main())
