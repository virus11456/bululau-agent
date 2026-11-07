"""掃描快照、指紋、outbox／inbox。不送 HTTP，不把 notify 當成唯一送達條件。"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from .qty import try_decimal, dec_str

TPE = timezone(timedelta(hours=8))
MAX_AGE_SECONDS = 900
FINGERPRINT_VERSION = 1


def parse_time(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TPE)
    return dt


def canonical_value(value):
    if isinstance(value, dict):
        return {key: canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [canonical_value(item) for item in value]
        items.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return items
    if isinstance(value, str):
        number = try_decimal(value)
        if number is not None and value.strip() != "" and _looks_numeric(value):
            return dec_str(number)
        return value
    return value


def _looks_numeric(value):
    text = value.strip()
    if text[:1] in "+-":
        text = text[1:]
    if not text or not text[0].isdigit():
        return False
    return try_decimal(value) is not None


def fingerprint(symbol, setup, bar_time, ladder):
    """與 scan_id、notify 無關。同一候選跨輪相同。"""
    payload = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "symbol": symbol,
        "setup": setup,
        "bar_time": parse_time(bar_time).isoformat(timespec="seconds"),
        "ladder": canonical_value(list(ladder)),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def age_seconds(scan_completed_at, now):
    return int((parse_time(now) - parse_time(scan_completed_at)).total_seconds())


def freshness(scan_completed_at, now, max_age=MAX_AGE_SECONDS):
    age = age_seconds(scan_completed_at, now)
    if age < 0:
        return {"valid": False, "status": "blocked_clock_skew", "age_seconds": age, "age_basis": "scan_completed_at"}
    if age > max_age:
        return {"valid": False, "status": "stale", "age_seconds": age, "age_basis": "scan_completed_at"}
    return {"valid": True, "status": "fresh", "age_seconds": age, "age_basis": "scan_completed_at"}


class ScanBook:
    """生產者 outbox 與消費者 inbox。空快照不喚醒 AI，也不刪歷史。"""

    def __init__(self, store):
        self.store = store
        state = store.state
        state.setdefault("snapshot", None)
        state.setdefault("outbox", {})
        state.setdefault("inbox", {})
        state.setdefault("ai_wake_count", 0)
        state.setdefault("idempotency_keys", [])
        self.fail_next_send = 0

    def publish(self, *, scan_id, started_at, completed_at, status, candidates, notify=False):
        if status != "ok":
            self.store.trace({"kind": "scan_rejected", "reason": "scan_failed_not_empty_snapshot", "scan_id": scan_id})
            return {"replaced": False, "status": "failed"}
        current = self.store.state["snapshot"]
        if current is not None:
            if parse_time(completed_at) < parse_time(current["scan_completed_at"]):
                self.store.trace({"kind": "scan_rejected", "reason": "out_of_order_snapshot", "scan_id": scan_id})
                return {"replaced": False, "status": "blocked"}
        snap = {
            "schema_version": "scan-snapshot-v1",
            "scan_id": scan_id,
            "scan_started_at": parse_time(started_at).isoformat(timespec="seconds"),
            "scan_completed_at": parse_time(completed_at).isoformat(timespec="seconds"),
            "status": "ok",
            "notify": bool(notify),
            "candidates": [],
        }
        for candidate in candidates:
            fp = candidate.get("fingerprint") or fingerprint(
                candidate["symbol"], candidate["setup"], candidate["bar_time"], candidate.get("ladder") or []
            )
            snap["candidates"].append({
                "fingerprint": fp,
                "symbol": candidate.get("symbol"),
                "setup": candidate.get("setup"),
                "bar_time": candidate.get("bar_time"),
                "source_bar_time": candidate.get("source_bar_time") or candidate.get("bar_time"),
                "ladder": candidate.get("ladder") or [],
            })
            self._enqueue(fp, snap["scan_completed_at"], notify)
        self.store.state["snapshot"] = snap
        self.store.write_snapshot(snap)
        self.store.trace({"kind": "snapshot_replaced", "scan_id": scan_id, "candidates": len(snap["candidates"]), "notify": bool(notify)})
        return {"replaced": True, "status": "ok", "candidates": snap["candidates"]}

    def _enqueue(self, fp, completed_at, notify):
        outbox = self.store.state["outbox"]
        if fp in outbox:
            outbox[fp]["notify_seen"] = bool(outbox[fp].get("notify_seen") or notify)
            return
        outbox[fp] = {
            "fingerprint": fp,
            "status": "pending",
            "notify_seen": bool(notify),
            "scan_completed_at": completed_at,
            "evaluation_id": "eval:" + fp,
        }
        if fp:
            self.store.state["ai_wake_count"] += 1
        self.store.trace({"kind": "outbox_enqueue", "fingerprint": fp, "notify": bool(notify)})

    def attempt_send(self, fp):
        item = self.store.state["outbox"].get(fp)
        if item is None or item["status"] != "pending":
            return {"sent": False, "reason": "not_pending"}
        fresh = freshness(item["scan_completed_at"], self.store.state.get("now") or item["scan_completed_at"])
        if not fresh["valid"]:
            item["status"] = "stale"
            self._inbox(fp)["status"] = "stale"
            self._inbox(fp)["terminal_ack_status"] = "stale"
            self.store.trace({"kind": "stale", "fingerprint": fp, "age_seconds": fresh["age_seconds"]})
            return {"sent": False, "status": "stale"}
        if self.fail_next_send:
            self.fail_next_send -= 1
            self.store.trace({"kind": "send_failed", "fingerprint": fp, "notify": item.get("notify_seen")})
            return {"sent": False, "status": "pending"}
        inbox = self._inbox(fp)
        if inbox["status"] == "committed":
            self.store.trace({"kind": "ack_reissue", "fingerprint": fp})
            inbox["ack_reissued"] = True
            item["status"] = "acked"
            return {"sent": True, "ack_only": True}
        inbox["status"] = "persisted"
        self.store.trace({"kind": "inbox_persist", "fingerprint": fp})
        self.store.save()
        return {"sent": True, "ack_only": False}

    def _inbox(self, fp):
        inbox = self.store.state["inbox"]
        if fp not in inbox:
            inbox[fp] = {
                "fingerprint": fp,
                "status": "received",
                "evaluation_id": "eval:" + fp,
                "commit_count": 0,
                "process_count": 0,
                "ack_reissued": False,
                "terminal_ack_status": None,
                "leased": False,
            }
        return inbox[fp]

    def persist_inbox(self, fp):
        row = self._inbox(fp)
        if row["status"] not in ("committed", "stale"):
            row["status"] = "persisted"
        self.store.trace({"kind": "inbox_persist", "fingerprint": fp, "status": row["status"]})
        self.store.save()
        return row

    def claim_lease(self, fp):
        row = self._inbox(fp)
        if row["status"] == "committed":
            return row
        row["leased"] = True
        row["status"] = "leased"
        self.store.trace({"kind": "lease", "fingerprint": fp, "evaluation_id": row["evaluation_id"]})
        self.store.save()
        return row

    def expire_lease(self, fp):
        row = self._inbox(fp)
        row["leased"] = False
        if row["status"] == "leased":
            row["status"] = "persisted"
        self.store.trace({"kind": "lease_expired", "fingerprint": fp})
        self.store.save()
        return row

    def commit(self, fp):
        row = self._inbox(fp)
        if row["status"] == "committed":
            row["ack_reissued"] = True
            self.store.trace({"kind": "ack_reissue", "fingerprint": fp, "evaluation_id": row["evaluation_id"]})
            return row
        key = row["evaluation_id"] + ":record"
        keys = self.store.state["idempotency_keys"]
        if key not in keys:
            keys.append(key)
            self.store.trace({"kind": "side_effect", "key": key})
        row["status"] = "committed"
        row["commit_count"] += 1
        row["process_count"] += 1
        row["leased"] = False
        self.store.trace({"kind": "commit", "fingerprint": fp, "evaluation_id": row["evaluation_id"]})
        self.store.save()
        return row

    def processing_ack(self, fp):
        row = self._inbox(fp)
        if row["status"] != "committed":
            self.store.trace({"kind": "ack_refused", "fingerprint": fp, "reason": "not_committed"})
            return {"acked": False}
        item = self.store.state["outbox"].get(fp)
        if item is not None:
            item["status"] = "acked"
        row["terminal_ack_status"] = row.get("terminal_ack_status") or "acked"
        self.store.trace({"kind": "processing_ack", "fingerprint": fp})
        self.store.save()
        return {"acked": True}

    def mark_stale(self, fp):
        row = self._inbox(fp)
        row["status"] = "stale"
        row["terminal_ack_status"] = "stale"
        item = self.store.state["outbox"].get(fp)
        if item is not None:
            item["status"] = "stale"
        self.store.trace({"kind": "stale", "fingerprint": fp})
        self.store.save()
        return row


class WebhookGate:
    """未驗證的 webhook 不送出。不設定文件沒有寫的 HTTP header。"""

    ALLOWED_KEYS = ("schema_version", "scan_id", "fingerprint", "scan_completed_at")

    def consider(self, payload, verified_transport):
        if not isinstance(payload, dict):
            return {"status": "blocked", "evaluate_count": 0, "reason": "untrusted_payload"}
        unknown = [key for key in payload if key not in self.ALLOWED_KEYS and not key.startswith("_")]
        if verified_transport is None:
            return {
                "status": "blocked",
                "evaluate_count": 0,
                "reason": "must_verify_allowlisted_payload_or_supported_snapshot_transport",
                "ignored_keys": unknown,
            }
        for key in ("command", "shell", "exec", "url"):
            if key in payload:
                return {"status": "blocked", "evaluate_count": 0, "reason": "untrusted_payload"}
        allowed = {key: payload.get(key) for key in self.ALLOWED_KEYS}
        return {"status": "blocked", "evaluate_count": 0, "reason": "transport_adapter_not_verified", "allowed": allowed}
