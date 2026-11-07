"""只讀舊 state.json。不建 data/、不寫 state、不開鎖、不查帳戶。

日切是否到期只報告。舊 day_state 會整份換掉 state，pending 與 meta 不會留下；這裡不執行那件事。
"""
import json
import os
import stat
import sys
from datetime import timedelta, timezone

from .timeparse import parse_instant

TPE = timezone(timedelta(hours=8))


def _read_json(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("rejected_symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError("bad_state")
    return loaded


def snapshot_file(path, now):
    """缺檔不是空倉。餘額數字不輸出。"""
    moment = parse_instant(now)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TPE)
    today = moment.astimezone(TPE).strftime("%Y-%m-%d")
    base = {
        "schema_version": 1,
        "command": "legacy-snapshot",
        "ok": False,
        "account_queried": False,
        "positions": None,
        "orders": None,
        "status_trusted": False,
        "pending_count": None,
        "meta_count": None,
        "halted_new": None,
        "killed": None,
        "day": None,
        "day_rollover_due": None,
        "day_rollover_drops_pending": True,
        "writes": 0,
        "lock_taken": False,
        "balance_included": False,
        "data_dir_created": False,
    }
    if not os.path.lexists(path):
        base["reason"] = "missing_state"
        return base
    try:
        state = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        base["reason"] = "bad_state"
        return base
    pending = state.get("pending", {})
    meta = state.get("meta", {})
    if ("pending" in state and not isinstance(pending, dict)) or ("meta" in state and not isinstance(meta, dict)):
        base["reason"] = "bad_state"
        return base
    day = state.get("day")
    base.update({
        "ok": True,
        "reason": "read",
        "pending_count": len(pending) if isinstance(pending, dict) else None,
        "meta_count": len(meta) if isinstance(meta, dict) else None,
        "halted_new": state.get("halted_new") if isinstance(state.get("halted_new"), bool) else None,
        "killed": state.get("killed") if isinstance(state.get("killed"), bool) else None,
        "day": day if isinstance(day, str) else None,
        "day_rollover_due": day != today if isinstance(day, str) else None,
        "has_day_start_equity": "day_start_equity" in state,
    })
    return base


def main(argv):
    found = {}
    index = 0
    while index < len(argv):
        key = argv[index]
        if key not in ("--state", "--now") or index + 1 >= len(argv):
            print("用法: python3 -m src.phase1.legacy_snapshot --state FILE --now TIME")
            return 2
        found[key] = argv[index + 1]
        index += 2
    if "--state" not in found or "--now" not in found:
        print("用法: python3 -m src.phase1.legacy_snapshot --state FILE --now TIME")
        return 2
    try:
        report = snapshot_file(found["--state"], found["--now"])
    except ValueError as exc:
        print(json.dumps({"ok": False, "reason": str(exc), "writes": 0, "account_queried": False}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
