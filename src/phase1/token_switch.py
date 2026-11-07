"""15 分鐘掃描閘門。沿用 prefilter 的 JSON，不重寫全市場掃描，也不下單。

沒有新候選就不要叫 AI。Webhook 的 URL 與認證文件未給，這裡不送 HTTP、不加密輪詢。
"""
import json
import sys
from datetime import datetime, timezone, timedelta

from .autoscan import ScanBook, TPE
from .session import Store
from .qty import dec_str, try_decimal

EXIT_QUIET = 0
EXIT_WAKE = 10
EXIT_FAILED = 2


class UnconfiguredWebhook:
    """正式介面要有 routine panel 的 URL 與 sender key。兩者與認證 header 都未在文件給出。"""

    def notify(self, event):
        return {
            "status": "blocked",
            "delivered": False,
            "http_sent": False,
            "reason": "webhook_url_and_sender_key_absent",
            "event_fingerprint": event.get("fingerprint"),
        }


def _ladder(value):
    if not isinstance(value, list):
        return []
    out = []
    for step in value:
        if isinstance(step, dict):
            item = {}
            for key in sorted(step):
                number = try_decimal(step[key])
                item[key] = dec_str(number) if number is not None and _numeric(step[key]) else step[key]
            out.append(item)
        else:
            number = try_decimal(step)
            out.append(dec_str(number) if number is not None and _numeric(step) else step)
    return out


def _numeric(value):
    text = str(value).strip()
    if text[:1] in "+-":
        text = text[1:]
    return bool(text) and text[0].isdigit()


def _bar_time(candidate):
    if candidate.get("bar_open_ms") not in (None, ""):
        opened = datetime.fromtimestamp(int(candidate["bar_open_ms"]) / 1000, TPE)
        return opened.isoformat(timespec="seconds")
    if candidate.get("bar_time"):
        return str(candidate["bar_time"])
    return None


def _identity(candidate):
    if not isinstance(candidate, dict):
        return None
    if candidate.get("fail") or candidate.get("blocked_by_btc_guard"):
        return None
    symbol = candidate.get("symbol")
    setup = candidate.get("setup")
    bar_time = _bar_time(candidate)
    if not symbol or not setup or not bar_time:
        return None
    return {
        "symbol": symbol,
        "setup": setup,
        "bar_time": bar_time,
        "ladder": _ladder(candidate.get("ladder") or []),
    }


def apply_prefilter(store, payload, *, scan_id, started_at, completed_at, notifier=None):
    """把現有 prefilter JSON 寫進持久 outbox。失敗不會偽裝成空成功。"""
    book = ScanBook(store)
    notifier = notifier or UnconfiguredWebhook()
    before_wake = int(store.state.get("ai_wake_count") or 0)
    before_keys = set(store.state.get("outbox") or {})
    if payload.get("error"):
        book.publish(
            scan_id=scan_id, started_at=started_at, completed_at=completed_at,
            status="failed", candidates=[], notify=False,
        )
        return _decision(EXIT_FAILED, False, [], {"status": "not_attempted", "http_sent": False, "reason": "scan_failed"}, "scan_failed")

    mapped = []
    skipped = []
    for candidate in payload.get("candidates") or []:
        ident = _identity(candidate)
        if ident is None:
            if isinstance(candidate, dict) and (candidate.get("fail") or candidate.get("blocked_by_btc_guard")):
                continue
            skipped.append(candidate.get("symbol") if isinstance(candidate, dict) else None)
            continue
        mapped.append(ident)
    if skipped:
        book.publish(
            scan_id=scan_id, started_at=started_at, completed_at=completed_at,
            status="failed", candidates=[], notify=False,
        )
        return _decision(EXIT_FAILED, False, [], {"status": "not_attempted", "http_sent": False, "reason": "cannot_dedup_without_bar_time"}, "cannot_dedup_without_bar_time")

    book.publish(
        scan_id=scan_id, started_at=started_at, completed_at=completed_at,
        status="ok", candidates=mapped, notify=bool(mapped),
    )
    new_fps = [fp for fp in store.state.get("outbox") or {} if fp not in before_keys]
    new_count = int(store.state.get("ai_wake_count") or 0) - before_wake
    notify = {"status": "not_attempted", "http_sent": False, "reason": "no_new_candidate"}
    if new_count:
        notify = notifier.notify({"fingerprints": new_fps, "scan_id": scan_id})
    exit_code = EXIT_WAKE if new_count else EXIT_QUIET
    reason = "new_candidates" if new_count else "no_new_candidates"
    return _decision(exit_code, new_count > 0, new_fps, notify, reason)


def _decision(exit_code, ai_wake, fingerprints, notify, reason):
    return {
        "exit_code": exit_code,
        "ai_wake": ai_wake,
        "new_fingerprints": fingerprints,
        "notify": notify,
        "http_sent": bool(notify.get("http_sent")),
        "manage_replaced": False,
        "watch_replaced": False,
        "reason": reason,
        "poll_interval_changed": False,
        "ai_execution_proven": False,
        "ai_judged_once": False,
    }


def main(argv=None):
    parser_args = argv if argv is not None else sys.argv[1:]
    import argparse
    ap = argparse.ArgumentParser(description="預檢 JSON 閘門。不下單、不叫模型、不送未配置的 webhook。")
    ap.add_argument("--input", required=True, help="prefilter 印出的 JSON 檔")
    ap.add_argument("--state-dir", required=True, help="這個閘門自己的狀態目錄，不要指到實盤 data/")
    ap.add_argument("--scan-id", default=None)
    args = ap.parse_args(parser_args)
    payload = json.loads(open(args.input, encoding="utf-8").read())
    now = datetime.now(timezone(timedelta(hours=8)))
    started = payload.get("time") or now.isoformat(timespec="seconds")
    scan_id = args.scan_id or ("prefilter-" + started)
    store = Store(args.state_dir)
    decision = apply_prefilter(
        store, payload, scan_id=scan_id, started_at=started, completed_at=started,
    )
    print(json.dumps(decision, ensure_ascii=False))
    return decision["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
