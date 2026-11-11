#!/usr/bin/env python3
"""AI 判斷紀錄。每輪審完一個候選後單獨執行，把判斷追加成一行 jsonl。

這支程式自己跑。gate.py、hardfilter.py、prefilter.py 不匯入它，它也不匯入它們。
不下單、不讀金鑰、不讀 env 檔、不連網、不做簽章請求。只用標準庫。Python 3.9 相容
（Mac 上用 /usr/bin/python3）。

用法：
  python3 src/decision_log.py --symbol ONEUSDT --pattern 瘸師弟 --decision reject \\
      --reason "只有1根長黑" --round-time 2026-11-11T09:30:00+08:00
"""
import errno
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# 固定偏移。Python 3.9 沒有 datetime.timezone.utc，所以 UTC 用 timedelta(0)。
TPE = timezone(timedelta(hours=8))
UTC = timezone(timedelta(0))

REASON_MAX = 500
PATTERN_MAX = 40
DECISIONS = ("reject", "enter", "skip_other")
DEFAULT_LOG = "~/quantboy-gate/decision_log.jsonl"
EXIT_OK = 0
EXIT_IO = 1
EXIT_USAGE = 2

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,32}$")
_ROUND_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:\d{2})$"
)
_FLAGS = {
    "--symbol": "symbol",
    "--pattern": "pattern",
    "--decision": "decision",
    "--reason": "reason",
    "--round-time": "round_time",
    "--log": "log",
}
_REQUIRED = (
    ("symbol", "--symbol"),
    ("pattern", "--pattern"),
    ("decision", "--decision"),
    ("reason", "--reason"),
)
_IO_MSG = {
    errno.ENOENT: "路徑不存在（上一層目錄要先在）",
    errno.EACCES: "沒有寫入權限",
    errno.EPERM: "沒有寫入權限",
    errno.EISDIR: "路徑是目錄，不是檔案",
    errno.EROFS: "檔案系統唯讀",
    errno.ENOSPC: "磁碟沒有空間",
}

HELP = """\
用法：
  python3 decision_log.py --symbol 幣種 --pattern 型態 --decision 決策 --reason 理由 \\
      [--round-time 時間] [--log 路徑]

把 AI 對一個候選的判斷追加成一行 JSON。一個候選一行。
不下單、不讀金鑰、不連網，也不被閘門匯入。

決策只接受這三個（小寫）：
  reject      否決，不進場
  enter       進場
  skip_other  其他原因略過（不是否決這個型態，也不是進場）

預設寫入 ~/quantboy-gate/decision_log.jsonl（與 gate_log.jsonl 同一目錄）。
reason 前後空白會去掉；超過 500 字只留前 500 字，不改寫、不加刪節號。
round-time 用來對回 gate_log.jsonl 的 time，格式 2026-11-11T09:30:00+08:00。
沒給就在紀錄裡寫 null。給了會換算成台北時間再存。

結束碼：0 成功；2 參數不合法（檔案不會被改）；1 寫入失敗。
"""


def _has_control(text):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def parse_args(argv):
    """回傳 ('help', None)、('error', 訊息) 或 ('ok', dict)。"""
    if argv in (["-h"], ["--help"]):
        return "help", None
    opts = {}
    index = 0
    while index < len(argv):
        item = argv[index]
        name = item
        inline = None
        if item.startswith("--") and "=" in item:
            name, inline = item.split("=", 1)
        key = _FLAGS.get(name)
        if key is None:
            return "error", "看不懂的參數：{0}".format(item)
        if key in opts:
            return "error", "{0} 重複了".format(name)
        if inline is None:
            if index + 1 >= len(argv) or str(argv[index + 1]).startswith("--"):
                return "error", "{0} 缺少值".format(name)
            inline = argv[index + 1]
            index += 2
        else:
            index += 1
        opts[key] = inline
    missing = [flag for key, flag in _REQUIRED if key not in opts]
    if missing:
        return "error", "缺少 {0}".format("、".join(missing))
    return "ok", opts


def normalize_symbol(text):
    trimmed = (text or "").strip()
    if not _SYMBOL_RE.fullmatch(trimmed):
        raise ValueError("symbol 請用 1 到 32 碼大寫英數，例如 ONEUSDT")
    return trimmed


def normalize_pattern(text):
    trimmed = (text or "").strip()
    if not trimmed:
        raise ValueError("pattern 是空的")
    if _has_control(trimmed):
        raise ValueError("pattern 含有控制字元")
    if len(trimmed) > PATTERN_MAX:
        raise ValueError("pattern 請在 1 到 {0} 字以內".format(PATTERN_MAX))
    return trimmed


def normalize_decision(text):
    trimmed = (text or "").strip()
    if trimmed in DECISIONS:
        return trimmed
    shown = ""
    if trimmed and len(trimmed) <= 40 and not _has_control(trimmed):
        shown = "（收到 {0}）".format(trimmed)
    raise ValueError("decision 只接受 reject、enter、skip_other{0}".format(shown))


def normalize_reason(text):
    """去掉前後空白。超過 500 字只留前面，不改寫剩下的字，也不加刪節號。"""
    trimmed = (text or "").strip()
    if not trimmed:
        raise ValueError("reason 是空的")
    if len(trimmed) > REASON_MAX:
        return trimmed[:REASON_MAX]
    return trimmed


def parse_round_time(text):
    """收 ISO 時間（必須帶時區），存成台北時間、秒，方便跟 gate_log 的 time 對字串。"""
    raw = (text or "").strip()
    if raw.endswith(("z", "Z")):
        raw = raw[:-1] + "Z"
    match = _ROUND_RE.fullmatch(raw)
    if match is None:
        raise ValueError("round-time 要像 2026-11-11T09:30:00+08:00（要有時區）")
    year, month, day, hour, minute, second, zone = match.groups()
    if zone == "Z":
        offset = UTC
    else:
        sign = 1 if zone[0] == "+" else -1
        hours = int(zone[1:3])
        minutes = int(zone[4:6])
        if hours > 23 or minutes > 59:
            raise ValueError("round-time 的時區偏移不正確")
        try:
            offset = timezone(timedelta(hours=sign * hours, minutes=sign * minutes))
        except ValueError:
            raise ValueError("round-time 的時區偏移不正確")
    try:
        when = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=offset,
        )
    except ValueError:
        raise ValueError("round-time 不是有效的時間")
    return when.astimezone(TPE).isoformat(timespec="seconds")


def format_now(now):
    if now is None:
        now = datetime.now(TPE)
    if not isinstance(now, datetime):
        raise ValueError("內部時間不正確")
    if now.tzinfo is None:
        raise ValueError("時間必須帶時區")
    return now.astimezone(TPE).isoformat(timespec="seconds")


def build_record(symbol, pattern, decision, reason, round_time, now):
    return {
        "time": format_now(now),
        "symbol": normalize_symbol(symbol),
        "pattern": normalize_pattern(pattern),
        "decision": normalize_decision(decision),
        "reason": normalize_reason(reason),
        "round_time": None if round_time is None else parse_round_time(round_time),
    }


def _json_line(record):
    """編成單一物理行。json 已跳脫換行；U+2028／U+2029 再換成 \\uXXXX，讀回仍是原字。"""
    text = json.dumps(record, ensure_ascii=False)
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\u0085", "\\u0085")
    if "\n" in text or "\r" in text:
        raise ValueError("紀錄沒辦法編成單行")
    return text + "\n"


def append_record(path, record):
    """獨佔鎖住之後，把整行寫進檔尾。鎖住期間別人不會插進半行。"""
    data = _json_line(record).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        pending = data
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError(errno.EIO, "write")
            pending = pending[written:]
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _io_message(exc):
    code = getattr(exc, "errno", None)
    if code in _IO_MSG:
        return _IO_MSG[code]
    if code is None:
        return "寫入失敗"
    return "寫入失敗（錯誤碼 {0}）".format(code)


def _usage(message):
    print("decision_log: {0}".format(message), file=sys.stderr)
    print("說明：python3 decision_log.py --help", file=sys.stderr)
    return EXIT_USAGE


def _log_path(opts):
    if "log" in opts:
        path = os.path.expanduser(opts["log"]).strip()
    else:
        path = os.path.expanduser(DEFAULT_LOG)
    if not path or _has_control(path):
        raise ValueError("log 路徑不正確")
    return path


def main(argv=None, now=None):
    if argv is None:
        argv = sys.argv[1:]
    kind, payload = parse_args(argv)
    if kind == "help":
        print(HELP, end="")
        return EXIT_OK
    if kind == "error":
        return _usage(payload)
    try:
        path = _log_path(payload)
        record = build_record(
            payload["symbol"],
            payload["pattern"],
            payload["decision"],
            payload["reason"],
            payload.get("round_time"),
            now,
        )
        append_record(path, record)
    except ValueError as exc:
        return _usage(str(exc))
    except OSError as exc:
        print(
            "decision_log: 寫不進去 {0}：{1}".format(path, _io_message(exc)),
            file=sys.stderr,
        )
        return EXIT_IO
    print("已記錄 {0} {1}".format(record["symbol"], record["decision"]))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
