#!/usr/bin/env python3
"""省 token 閘門：每輪先跑這支，只有「真的需要判斷」才交給 Grok。

做法跟現行 routine 每輪的機械步驟一樣，只是改由程式跑：
  1. live_trader.py manage --live   （原本每輪就會跑）
  2. live_trader.py status          （只讀）
  3. prefilter.py --quiet           （只讀公開行情）

第一行輸出：
  QUIET  → 新鮮、可信的 0 倉 0 單 0 候選，上一輪也是空的。本輪結束，不叫 AI。
  WAKE   → 其他所有情況（有倉、有單、有候選、狀態變化、風控旗標、任何錯誤或看不懂）。
           後面附上三支命令的輸出，Grok 直接用，不必重跑。

結束碼：0 = QUIET，10 = WAKE。閘門本身出錯也一律 WAKE，絕不把未知當成空倉。
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

# manage 輸出出現這些字，代表這輪有動作或風控，要叫 AI 回報。
MANAGE_ALERT_WORDS = (
    "!!!", "停開新倉", "全平", "成交", "平倉", "撤", "取消", "補掛", "錯誤", "Error", "error", "失敗", "Traceback",
)


def now_tpe():
    return datetime.now(TPE)


def _tail(text, limit=MAX_TAIL):
    text = text or ""
    return text if len(text) <= limit else "…(前略)…\n" + text[-limit:]


def run_cmd(argv, timeout, cwd=None, env=None, lock_retries=5, lock_wait=3.0, sleep=time.sleep):
    """跑一支命令。碰到 live_trader 檔案鎖（60 秒 watch 正在跑）就等一下重試。"""
    attempt = 0
    while True:
        try:
            proc = subprocess.run(
                argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False,
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


def parse_status(text):
    """解析 live_trader.py status 的文字輸出。任何一項讀不到就回 None（＝不可信）。

    除了表頭數字，也核對表頭下面列出的明細行數，數字和明細對不上一律不可信。
    """
    lines = [line.rstrip() for line in (text or "").splitlines()]
    positions = orders = algo = halted = killed = None
    pos_rows = order_rows = 0
    section = None
    for line in lines:
        stripped = line.strip()
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
    if None in (positions, orders, algo, halted, killed):
        return None
    if pos_rows != positions or order_rows != orders + algo:
        return None
    return {"positions": positions, "orders": orders, "algo_orders": algo, "halted_new": halted, "killed": killed}


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
    if result["code"] == 0 and (body["call_ai"] or n != 0):
        return None
    if result["code"] == 10 and not body["call_ai"]:
        return None
    return {"call_ai": body["call_ai"], "n_candidates": n, "candidates": body.get("candidates") or []}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, path)


def append_log(path, row):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def decide(manage, status_run, status, prefilter_run, prefilter, previous, today):
    """回傳 (是否 QUIET, 理由清單)。只有全部條件都明確成立才 QUIET。"""
    reasons = []
    if manage.get("code") != 0:
        reasons.append("manage_failed" if not manage.get("lock_busy") else "manage_lock_busy")
    else:
        text = manage.get("stdout", "") + manage.get("stderr", "")
        if any(word in text for word in MANAGE_ALERT_WORDS):
            reasons.append("manage_reported_action")
    if status_run.get("code") != 0 or status is None:
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
    if prefilter is None:
        reasons.append("prefilter_failed")
    elif prefilter["call_ai"] or prefilter["n_candidates"] > 0:
        reasons.append("candidates")
    if previous.get("last") == "busy" and not reasons:
        # 上一輪有倉或有單，這輪突然全空：可能剛停損或剛成交出場，要叫 AI 記錄與回報一次。
        reasons.append("book_just_went_flat")
    return (not reasons), reasons


def main(argv=None, runner=run_cmd, clock=now_tpe):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="省 token 閘門：空輪不叫 AI")
    ap.add_argument("--root", default=os.path.expanduser("~/quantboy-live"), help="live_trader.py 所在目錄")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--live-trader", default="live_trader.py", help="相對 --root 的檔名")
    ap.add_argument("--prefilter", default="prefilter.py", help="相對 --root 的檔名，或絕對路徑")
    ap.add_argument("--state-dir", default=here, help="閘門自己的狀態與 log 目錄")
    ap.add_argument("--no-manage", action="store_true", help="不跑 manage --live（只讀測試用）")
    args = ap.parse_args(argv)

    os.makedirs(args.state_dir, exist_ok=True)
    state_path = os.path.join(args.state_dir, "gate_state.json")
    log_path = os.path.join(args.state_dir, "gate_log.jsonl")
    started = clock()
    today = started.strftime("%Y-%m-%d")
    previous = load_state(state_path)

    trader = os.path.join(args.root, args.live_trader)
    prefilter_path = args.prefilter if os.path.isabs(args.prefilter) else os.path.join(args.root, args.prefilter)
    try:
        if args.no_manage:
            manage = {"code": 0, "stdout": "", "stderr": "", "skipped": True}
        else:
            manage = runner([args.python, trader, "manage", "--live"], 180, cwd=args.root)
        status_run = runner([args.python, trader, "status"], 90, cwd=args.root)
        status = parse_status(status_run.get("stdout")) if status_run.get("code") == 0 else None
        prefilter_run = runner([args.python, prefilter_path, "--quiet"], 240, cwd=args.root, lock_retries=0)
        prefilter = parse_prefilter(prefilter_run)
        quiet, reasons = decide(manage, status_run, status, prefilter_run, prefilter, previous, today)
    except Exception as exc:  # 閘門自己壞掉也只能叫 AI
        manage = status_run = prefilter_run = {"code": None, "stdout": "", "stderr": ""}
        status = prefilter = None
        quiet, reasons = False, ["gate_error:{0}".format(type(exc).__name__)]

    busy = status is not None and (status["positions"] > 0 or status["orders"] > 0 or status["algo_orders"] > 0)
    new_state = {
        "last": "busy" if busy else ("flat" if status is not None else previous.get("last", "unknown")),
        "last_run": started.isoformat(timespec="seconds"),
        "last_decision": "QUIET" if quiet else "WAKE",
        "halt_reported_day": previous.get("halt_reported_day"),
    }
    if "halted_new_today" in reasons:
        new_state["halt_reported_day"] = today
    try:
        save_state(state_path, new_state)
        append_log(log_path, {
            "time": started.isoformat(timespec="seconds"),
            "decision": new_state["last_decision"],
            "reasons": reasons,
            "status": status,
            "n_candidates": prefilter["n_candidates"] if prefilter else None,
        })
    except OSError as exc:
        if quiet:
            quiet, reasons = False, ["gate_state_write_failed:{0}".format(type(exc).__name__)]

    if quiet:
        print("QUIET 0倉 0單 0候選 {0}".format(started.strftime("%H:%M")))
        return QUIET

    print("WAKE " + ",".join(reasons))
    print("以下是閘門剛跑過的結果（台北 {0}）。manage --live 已經跑過，不必重跑。".format(started.strftime("%Y-%m-%d %H:%M")))
    sections = (
        ("manage --live", manage),
        ("status", status_run),
        ("prefilter --quiet", prefilter_run),
    )
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
