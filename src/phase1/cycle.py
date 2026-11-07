"""一輪整合：manage 先跑且不受候選限制；喚醒只用可信狀態。

不送正式持倉通道。HTTP 2xx 不會把 outbox 標成已處理。
processed 回條只經 apply_processed_ack 寫回這一個 cycle store。
"""
import json
import secrets

from .ack_contract import apply_processed_ack, cycle_key, load_ack
from .autoscan import fingerprint, freshness
from .manage_job import build_manage_argv
from .session import Store, refuse_foreign_state_dir
from .token_switch import _identity
from .wake_gate import decide


def _fingerprints(prefilter):
    found = []
    for item in prefilter.get("candidates") or []:
        ident = _identity(item)
        if ident is None:
            continue
        found.append(fingerprint(ident["symbol"], ident["setup"], ident["bar_time"], ident["ladder"]))
    return found


def _already_consumed(store, item):
    event_id = item.get("event_id")
    row = (store.state.get("test_events") or {}).get(event_id) or {}
    return (
        item.get("status") == "acked"
        and item.get("processing_ack") is True
        and row.get("status") == "processed"
        and row.get("process_count") == 1
    )


def ack_cycle(store, evaluation_id, *, processing_ack=None, document=None, expected_event_id=None, sent_at=None, now=None):
    """布林值不能把週期 outbox 標成已處理。"""
    if document is None or processing_ack is not True:
        return False
    if not isinstance(document, dict) or document.get("evaluation_id") != evaluation_id:
        return False
    result = apply_processed_ack(
        store, document, expected_event_id=expected_event_id, sent_at=sent_at, now=now,
    )
    return result.get("reason") == "processed"


def run_cycle(store, snapshot, prefilter, *, now, scan_id, manage_exit, notifier=None):
    manage_argv = build_manage_argv()
    manage_complete = (
        manage_exit == 0
        and snapshot.get("manage_complete") is True
        and not snapshot.get("manage_error")
    )
    gate_snapshot = dict(snapshot)
    if not manage_complete or snapshot.get("limit_older_than_2h"):
        gate_snapshot["manage_error"] = True
    seen = store.state.setdefault("seen_candidate_fingerprints", [])
    fingerprints = [] if prefilter.get("error") else _fingerprints(prefilter)
    new_fingerprints = [item for item in fingerprints if item not in seen]
    flat = (
        gate_snapshot.get("status") == "ok"
        and gate_snapshot.get("positions") == 0
        and gate_snapshot.get("orders") == 0
        and not gate_snapshot.get("manage_error")
    )
    prefilter_for_gate = prefilter
    if flat and not new_fingerprints:
        prefilter_for_gate = {"candidates": []}
    decision = decide(
        store, gate_snapshot, prefilter_for_gate, now=now, scan_id=scan_id, notifier=notifier,
    )
    sender = dict(decision["sender"])
    http_status = sender.get("http_status")
    if isinstance(http_status, int) and 200 <= http_status < 300:
        sender["processing_ack"] = False
        sender["http_ok_is_not_ack"] = True
    evaluation_id = decision.get("evaluation_id")
    item = None
    if evaluation_id:
        try:
            item = store.state.get("outbox", {}).get(cycle_key(evaluation_id))
        except ValueError:
            item = None
    if isinstance(item, dict) and not _already_consumed(store, item):
        item["status"] = "pending"
        item["processing_ack"] = False
        store.save()
    if not decision["cycle_needs_ai"]:
        for fp in fingerprints:
            if fp not in seen:
                seen.append(fp)
        store.save()
    elif new_fingerprints and decision["reason"] == "candidates_present":
        for fp in new_fingerprints:
            if fp not in seen:
                seen.append(fp)
        store.save()
    decision["sender"] = sender
    decision["new_candidate_fingerprints"] = new_fingerprints
    decision["protection_complete"] = False if not manage_complete else bool(decision["snapshot_fresh"])
    if decision["cycle_needs_ai"] and decision["reason"] in ("manage_or_scan_error", "snapshot_missing", "snapshot_stale", "snapshot_unknown"):
        decision["protection_complete"] = False
    decision["manage_argv"] = manage_argv
    decision["ai_suggestion"] = "skip" if not decision["cycle_needs_ai"] else "keep"
    decision["old_ai_poll"] = "keep"
    decision["routine_stopped"] = False
    decision["formal_channel"] = False
    return decision


def migrate_legacy_cycles(store):
    """舊鍵 cycle:cycle:<id> 複製到 cycle:<id>。舊 pending 留在原處。"""
    with store.exclusive():
        store.reload()
        outbox = store.state.setdefault("outbox", {})
        report = []
        changed = False
        for key, item in list(outbox.items()):
            if not isinstance(key, str) or not key.startswith("cycle:cycle:"):
                continue
            scan_id = key[len("cycle:cycle:"):]
            try:
                new_key = cycle_key(scan_id)
            except ValueError:
                report.append({"from": key, "action": "refuse", "reason": "rejected_id"})
                continue
            if not isinstance(item, dict) or item.get("status") == "acked":
                report.append({"from": key, "action": "refuse", "reason": "not_pending"})
                continue
            if new_key in outbox:
                report.append({"from": key, "action": "refuse", "reason": "destination_exists"})
                continue
            copied = dict(item)
            copied["evaluation_id"] = scan_id
            copied["status"] = "pending"
            copied["processing_ack"] = False
            copied["migrated_from"] = key
            if not isinstance(copied.get("event_id"), str):
                copied["event_id"] = "c-" + secrets.token_hex(16)
            try:
                cycle_key(copied["event_id"])
            except ValueError:
                copied["event_id"] = "c-" + secrets.token_hex(16)
            outbox[new_key] = copied
            store.state.setdefault("cycle_migration", []).append(
                {"from": key, "to": new_key, "event_id": copied["event_id"]}
            )
            report.append({"from": key, "to": new_key, "action": "copy", "event_id": copied["event_id"]})
            changed = True
        if changed:
            store.save()
        return report


def rollback_legacy_migration(store):
    """只刪仍是 pending 的遷移複本。舊鍵不動。"""
    with store.exclusive():
        store.reload()
        outbox = store.state.setdefault("outbox", {})
        kept = []
        report = []
        for record in list(store.state.get("cycle_migration") or []):
            item = outbox.get(record.get("to"))
            legacy = outbox.get(record.get("from"))
            reversible = (
                isinstance(item, dict)
                and item.get("status") == "pending"
                and item.get("migrated_from") == record.get("from")
                and isinstance(legacy, dict)
            )
            if not reversible:
                kept.append(record)
                report.append({"to": record.get("to"), "action": "refuse", "reason": "not_reversible"})
                continue
            del outbox[record["to"]]
            report.append({"to": record["to"], "action": "removed_copy"})
        store.state["cycle_migration"] = kept
        store.save()
        return report


def _print_result(result):
    print(json.dumps(
        {key: result.get(key) for key in ("applied", "reason", "pending", "old_ai_poll", "process_count")},
        ensure_ascii=False,
    ))


def main(argv):
    usage = "用法: python3 -m src.phase1.cycle apply --state-dir DIR --file FILE --expect EVENT_ID --sent-at TIME --now TIME"
    if len(argv) == 3 and argv[0] in ("migrate", "rollback-migration") and argv[1] == "--state-dir":
        try:
            refuse_foreign_state_dir(argv[2])
            store = Store(argv[2])
        except ValueError as exc:
            print(json.dumps({"applied": False, "reason": str(exc), "old_ai_poll": "keep"}, ensure_ascii=False))
            return 1
        report = migrate_legacy_cycles(store) if argv[0] == "migrate" else rollback_legacy_migration(store)
        print(json.dumps({"old_ai_poll": "keep", "routine_stopped": False, "report": report}, ensure_ascii=False))
        return 0
    if len(argv) != 11 or argv[0] != "apply" or argv[1:10:2] != ["--state-dir", "--file", "--expect", "--sent-at", "--now"]:
        print(usage)
        return 2
    directory, path, expect, sent_at, now = argv[2], argv[4], argv[6], argv[8], argv[10]
    try:
        refuse_foreign_state_dir(directory)
        store = Store(directory)
        result = apply_processed_ack(store, load_ack(path), expected_event_id=expect, sent_at=sent_at, now=now)
    except ValueError as exc:
        _print_result({"applied": False, "reason": str(exc), "pending": True, "old_ai_poll": "keep"})
        return 1
    result["old_ai_poll"] = "keep"
    _print_result(result)
    return 0 if result.get("reason") in ("processed", "duplicate") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
