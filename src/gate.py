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
只用標準庫，Python 3.8+。不讀金鑰、不改 live_trader 的檔案、不自己下單。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
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


def main(argv=None, runner=run_cmd, clock=now_tpe, sleep=None, monotonic=None):
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

    os.makedirs(args.state_dir, exist_ok=True)
    state_path = os.path.join(args.state_dir, "gate_state.json")
    log_path = os.path.join(args.state_dir, "gate_log.jsonl")
    started = clock()
    today = started.strftime("%Y-%m-%d")
    previous = load_state(state_path)

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
    except Exception as exc:  # 閘門自己壞掉也只能叫 AI。已取得的輸出不要清掉。
        checked = clock()
        quiet, reasons = False, ["gate_error:{0}".format(type(exc).__name__)]

    observed = book_observation(status, checked)
    new_state = {
        "last": observed,
        "last_run": started.isoformat(timespec="seconds"),
        "last_decision": "QUIET" if quiet else "WAKE",
        "halt_reported_day": previous.get("halt_reported_day"),
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
    print("以下是閘門剛跑過的結果（台北 {0}）。manage --live 已經跑過，不必重跑。".format(started.strftime("%Y-%m-%d %H:%M")))
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
