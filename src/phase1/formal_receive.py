"""未啟用的正式接收核對。不建立 routine、不發 HTTP、不填判斷。

父判斷截止是本地狀態。沒有 scheduler，不會自己準時跳。
本地終止不代表平台 AI process 已被殺，也不送告警、不呼叫原 AI。

五欄 trade_candidate_test 是舊測試入口，不是正式 wake 授權。
可驗收的是這個 handler。平台上的 prompt 要人自己存。有界 PUT／GET／POST 仍待批准。
"""
import fcntl
import json
import os
from datetime import datetime, timezone

from .ack_contract import MAX_ACK_AGE, cycle_key
from .box_receiver import assert_wake_bound
from .production_transport import CONTEXT_MAX_BYTES, _outside_window, _moment
from .review_context import _recomputed_context, exact_schema_version
from .sender_setup import validate_event

LEGACY_TEST_ROUTINE = {
    "owner_label": "總經理boy",
    "owner_id": "ef5f0556-2d5c-49a4-8b61-a4c5eef57780",
    "automation_id": "automation-1794023232755",
    "name": "交易候選接收測試（不下單）",
    "state": "paused_webhook",
    "contract_path_on_mac": "/workspace/trading-webhook-e2e-test-20261107",
    "contract_readable_here": False,
    "expires_taipei": "2026-11-08T00:00:00+08:00",
    "budget_exhausted": {"ids": 2, "posts": 3},
    "formal_wake_authorized": False,
}

LAYERS = {
    "handler_verifiable": True,
    "prompt_saved_by_platform": False,
    "bounded_trial_approved": False,
}

# 沿用既有 MAX_ACK_AGE。不另編試跑秒數，也不把它當成已批准的平台 AI 期限。
PARENT_JUDGMENT_DEADLINE = MAX_ACK_AGE

PLATFORM_PARENT_AI = {
    "wake_connected": False,
    "context_read_connected": False,
    "persistent_submit_connected": False,
    "bounded_deadline_trigger_connected": False,
    "desensitized_alert_delivery_connected": False,
    "original_ai_fallback_connected": False,
}


def _base(reason):
    return {
        "schema_version": 1,
        "event_type": "formal_receive_check",
        "reason": reason,
        "formal_wake_authorized": False,
        "orders_placed": False,
        "http_sent": False,
        "http_200_is_ack": False,
        "program_fills_judgment": False,
        "routine_created": False,
        "routine_modified": False,
        "parent_must_read_context": True,
        "layers": dict(LAYERS),
        "legacy_test_routine": dict(LEGACY_TEST_ROUTINE),
    }


def classify_legacy_test(document):
    """舊五欄測試。本機沒有那份 CONTRACT 檔，不猜內容，只套 repo 已有的欄位契約。"""
    if not isinstance(document, dict) or document.get("event_type") != "trade_candidate_test":
        return None
    try:
        validate_event(document)
        schema_valid = True
    except ValueError:
        schema_valid = False
    verdict = _base("legacy_test_not_formal_wake")
    verdict.update({
        "class": "legacy_trade_candidate_test",
        "schema_valid": schema_valid,
        "accepted": False,
        "budget_reusable": False,
    })
    return verdict


def _checked_delivery(document, now):
    """沿用 publisher 的完整 context 驗證。摘要一致不能代替 snapshot／行情契約。"""
    legacy = classify_legacy_test(document)
    if legacy is not None:
        return None, legacy
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > CONTEXT_MAX_BYTES:
        raise ValueError("context_too_large")
    if not isinstance(document, dict) or set(document) != {"schema_version", "event_type", "wake", "context"}:
        raise ValueError("rejected_context")
    if not exact_schema_version(document.get("schema_version")) or document.get("event_type") != "evaluation_delivery":
        raise ValueError("rejected_context")
    context = document.get("context")
    digest, problem = _recomputed_context(context)
    if problem:
        raise ValueError(problem)
    if _outside_window(_moment(context.get("sent_at")), now):
        raise ValueError("expired")
    assert_wake_bound(document.get("wake"), context)
    return {
        "event_id": context["event_id"],
        "evaluation_id": context["evaluation_id"],
        "envelope_sha256": digest,
    }, None


def verify_formal_delivery(document, now, seen=None):
    """離線核對 {wake, context}。不呼叫父 AI，也不把 HTTP 200 當成 ACK。"""
    checked, legacy = _checked_delivery(document, now)
    if legacy is not None:
        return legacy
    duplicate = False
    if isinstance(seen, dict) and checked["event_id"] in seen:
        if seen[checked["event_id"]] != checked["envelope_sha256"]:
            raise ValueError("immutable_conflict")
        duplicate = True
    verdict = _base("delivery_schema_accepted")
    verdict.update({
        "class": "evaluation_delivery",
        "accepted": True,
        "duplicate": duplicate,
        "event_id": checked["event_id"],
        "evaluation_id": checked["evaluation_id"],
        "envelope_sha256": checked["envelope_sha256"],
        "live_send": False,
        "trade_authorized": False,
        "durable": False,
        "ack": False,
        "timeout_executor": False,
    })
    return verdict


def _inbox_lock(root, event_id):
    cycle_key(event_id)
    path = os.path.join(str(root), "formal-" + event_id + ".claim")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _unlock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_formal(store, event_id):
    with store.exclusive():
        store.reload()
        row = (store.state.get("formal_inbox") or {}).get(event_id)
    return row if isinstance(row, dict) else None


def trusted_judgment_clock(injected=None):
    """生產不傳 injected，用系統 UTC。測試才傳入受控時鐘。payload 的 now 不進這裡。

    injected 可以是字串，或每次取時都呼叫的零引數函式，用來讓測試把時鐘往前推。
    """
    if callable(injected):
        injected = injected()
    if injected is None:
        moment = datetime.now(timezone.utc)
        source = "system"
    else:
        moment = _moment(injected)
        source = "injected"
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), source


def _deadline_text(clock_text):
    moment = _moment(clock_text) + PARENT_JUDGMENT_DEADLINE
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_deadline(deadline_text, clock_text):
    if not isinstance(deadline_text, str):
        return True
    return _moment(clock_text) > _moment(deadline_text)


def _alert_path(root, event_id):
    return os.path.join(str(root), "formal-alerts", event_id + ".json")


def _alert_body(event_id, deadline_text, clock_text):
    return {
        "schema_version": 1,
        "event_type": "local_parent_deadline_alert",
        "event_id": event_id,
        "status": "parent_deadline_expired",
        "judgment_deadline": deadline_text,
        "checked_at": clock_text,
        "alert_delivered": False,
        "platform_ai_process_killed": False,
        "original_ai_fallback_called": False,
        "http_sent": False,
        "scheduler_present": False,
        "contains_context": False,
        "contains_judgment": False,
        "contains_secret": False,
    }


def _alert_durable(path, event_id):
    if not os.path.isfile(path):
        return False
    try:
        body = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(body, dict)
        and body.get("event_type") == "local_parent_deadline_alert"
        and body.get("event_id") == event_id
        and body.get("status") == "parent_deadline_expired"
        and body.get("alert_delivered") is False
        and body.get("platform_ai_process_killed") is False
        and body.get("original_ai_fallback_called") is False
        and body.get("http_sent") is False
        and body.get("contains_context") is False
        and body.get("contains_judgment") is False
        and body.get("contains_secret") is False
    )


def _ensure_local_alert(store, event_id, deadline_text, checked_at):
    """終止列要有去敏告警檔。缺檔就補。寫失敗如實拋出，不記成已交付。"""
    path = _alert_path(store.root, event_id)
    if not _alert_durable(path, event_id):
        _write_json(path, _alert_body(event_id, deadline_text, checked_at))
        if not _alert_durable(path, event_id):
            raise OSError("alert_not_durable")
    relative = os.path.join("formal-alerts", event_id + ".json")
    with store.exclusive():
        store.reload()
        row = store.state["formal_inbox"][event_id]
        if row.get("status") != "parent_deadline_expired":
            return row
        if row.get("local_alert") != relative:
            row["local_alert"] = relative
            store.save()
    return read_formal(store, event_id)


def _mark_expired(store, event_id, clock_text):
    """只在已過持久截止且尚未成功時寫終止。不改 deadline，不覆寫 publish_ready。

    已經是終止狀態時仍冪等補建告警。告警寫失敗不把 alert_delivered 改成 true。
    """
    with store.exclusive():
        store.reload()
        row = (store.state.get("formal_inbox") or {}).get(event_id)
        if not isinstance(row, dict):
            raise ValueError("not_received")
        if row.get("status") == "publish_ready":
            return row
        if row.get("status") != "parent_deadline_expired":
            if not _past_deadline(row.get("judgment_deadline"), clock_text):
                return row
            row["status"] = "parent_deadline_expired"
            row["expired_at"] = clock_text
            store.save()
        deadline_text = row.get("judgment_deadline")
        checked_at = row.get("expired_at") or clock_text
    return _ensure_local_alert(store, event_id, deadline_text, checked_at)


def _row_verdict(row, *, duplicate, parent_called, claim_held=False, accepted=True):
    status = row.get("status") if isinstance(row, dict) else "pending"
    if status == "publish_ready":
        reason = "publish_ready"
    elif status == "parent_deadline_expired":
        reason = "parent_deadline_expired"
    else:
        reason = "parent_pending"
    verdict = _base(reason)
    verdict.update({
        "class": "evaluation_delivery",
        "accepted": accepted and not claim_held,
        "duplicate": duplicate,
        "claim_held": claim_held,
        "status": status,
        "event_id": row.get("event_id") if isinstance(row, dict) else None,
        "evaluation_id": row.get("evaluation_id") if isinstance(row, dict) else None,
        "envelope_sha256": row.get("digest") if isinstance(row, dict) else None,
        "parent_called": parent_called,
        "judgment_deadline": row.get("judgment_deadline") if isinstance(row, dict) else None,
        "ack": status == "publish_ready",
        "live_send": False,
        "trade_authorized": False,
        "durable": True,
        "timeout_executor": False,
        "platform_ai_process_killed": False,
        "scheduler_present": False,
        "expiry_requires_platform_trigger": True,
        "platform_parent_ai": dict(PLATFORM_PARENT_AI),
    })
    return verdict


def _formal_dir(root, event_id):
    cycle_key(event_id)
    return os.path.join(str(root), "formal-files", event_id)


def _formal_paths(root, event_id):
    directory = _formal_dir(root, event_id)
    return {
        "context": os.path.join(directory, "context.json"),
        "judgment": os.path.join(directory, "judgment.json"),
        "trace": os.path.join(directory, "trace.json"),
    }


def _write_json(path, document):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_parent_files(store, event_id):
    paths = _formal_paths(store.root, event_id)
    if not all(os.path.isfile(path) for path in paths.values()):
        return None
    return {name: _read_json(path) for name, path in paths.items()}


def _offline_publisher_config():
    return {"installation_id": "local-offline", "enabled": False, "live_send": False}


def readback_put(store, event_id):
    """只從 Store 的三個固定檔重組 PUT。缺檔或驗證失敗都拒絕。不發網路。"""
    cycle_key(event_id)
    if read_parent_files(store, event_id) is None:
        raise ValueError("files_missing")
    document = _publish_files(store, event_id)
    document = dict(document)
    document["sent"] = False
    document["http_sent"] = False
    document["publish_ready_is_not_a_publish"] = True
    document["mac_ack"] = False
    return document


def _publish_files(store, event_id):
    """讀已寫入的檔，走既有 publisher_document。不發 HTTP。"""
    from .gateway_client import publisher_document
    paths = _formal_paths(store.root, event_id)
    return publisher_document(
        _offline_publisher_config(), event_id, paths["judgment"], paths["trace"], paths["context"],
    )


def receive_delivery(store, document, now, parent=None, judgment_clock=None):
    """寫進 Store。沒有父 callback 不假 ACK。程式不生成 judgment。

    now 只核對 delivery 時效。父判斷截止用 judgment_clock；生產省略它，改取系統 UTC。
    重收不改已寫入的截止，也不在截止後再喚父 AI。
    """
    checked, legacy = _checked_delivery(document, now)
    if legacy is not None:
        return legacy
    clock_text, clock_source = trusted_judgment_clock(judgment_clock)
    fd = _inbox_lock(store.root, checked["event_id"])
    if fd is None:
        return _busy_delivery(store, checked)
    try:
        with store.exclusive():
            store.reload()
            inbox = store.state.setdefault("formal_inbox", {})
            prior = inbox.get(checked["event_id"])
            if isinstance(prior, dict) and (
                prior.get("digest") != checked["envelope_sha256"] or prior.get("evaluation_id") != checked["evaluation_id"]
            ):
                raise ValueError("immutable_conflict")
            if not isinstance(prior, dict):
                inbox[checked["event_id"]] = {
                    "status": "pending",
                    "event_id": checked["event_id"],
                    "evaluation_id": checked["evaluation_id"],
                    "digest": checked["envelope_sha256"],
                    "parent_invoked": False,
                    "judgment_deadline": _deadline_text(clock_text),
                    "judgment_clock_source": clock_source,
                }
                store.save()
                _write_json(_formal_paths(store.root, checked["event_id"])["context"], document["context"])
                prior = inbox[checked["event_id"]]
                duplicate = False
            else:
                duplicate = True
            status = prior.get("status")
            invoked = prior.get("parent_invoked") is True
            stored_deadline = prior.get("judgment_deadline")
        if status == "pending" and _past_deadline(stored_deadline, clock_text):
            _mark_expired(store, checked["event_id"], clock_text)
            return _row_verdict(read_formal(store, checked["event_id"]), duplicate=duplicate, parent_called=False)
        if status in ("publish_ready", "parent_deadline_expired") or invoked or not callable(parent):
            return _row_verdict(read_formal(store, checked["event_id"]), duplicate=duplicate, parent_called=False)
        with store.exclusive():
            store.reload()
            row = store.state["formal_inbox"][checked["event_id"]]
            if row.get("judgment_deadline") != stored_deadline:
                row["judgment_deadline"] = stored_deadline
            if row.get("parent_invoked") is True or row.get("status") != "pending" or _past_deadline(row.get("judgment_deadline"), clock_text):
                expired = row.get("status") == "pending" and _past_deadline(row.get("judgment_deadline"), clock_text)
            else:
                expired = False
                row["parent_invoked"] = True
                store.save()
        if expired:
            _mark_expired(store, checked["event_id"], clock_text)
            return _row_verdict(read_formal(store, checked["event_id"]), duplicate=True, parent_called=False)
        returned = parent(document) if callable(parent) else None
        if isinstance(returned, dict) and isinstance(returned.get("judgment"), dict) and isinstance(returned.get("trace"), dict):
            current, _source = trusted_judgment_clock(judgment_clock)
            if _past_deadline(stored_deadline, current):
                _mark_expired(store, checked["event_id"], current)
                return _row_verdict(read_formal(store, checked["event_id"]), duplicate=duplicate, parent_called=True)
            _install_parent_files(
                store, checked["event_id"], returned["judgment"], returned["trace"], judgment_clock=judgment_clock,
            )
        return _row_verdict(read_formal(store, checked["event_id"]), duplicate=duplicate, parent_called=True)
    finally:
        _unlock(fd)


def _busy_delivery(store, checked):
    """claim 還在時也要核對已記住的 evaluation／digest。衝突不能接受。"""
    row = read_formal(store, checked["event_id"])
    if isinstance(row, dict) and (
        row.get("digest") != checked["envelope_sha256"] or row.get("evaluation_id") != checked["evaluation_id"]
    ):
        raise ValueError("immutable_conflict")
    held = row or {
        "status": "pending",
        "event_id": checked["event_id"],
        "evaluation_id": None,
        "digest": None,
    }
    verdict = _row_verdict(held, duplicate=False, parent_called=False, claim_held=True, accepted=False)
    verdict["ack"] = False
    return verdict


def _discard_uncommitted_parent_files(paths):
    """驗證用的 judgment／trace 還沒變成 publish_ready 時，不能留成可發布結果。"""
    for name in ("judgment", "trace"):
        if os.path.isfile(paths[name]):
            os.remove(paths[name])


def _install_parent_files(store, event_id, judgment, trace, judgment_clock=None):
    """呼叫端已持有 claim。不生成 judgment，只驗證並保存父層交來的檔。

    截止以寫入 publish_ready 之前的那一次可信時鐘為準。這不表示磁碟提交耗時為零。
    已是 publish_ready 則不寫檔。
    """
    paths = _formal_paths(store.root, event_id)
    with store.exclusive():
        store.reload()
        row = (store.state.get("formal_inbox") or {}).get(event_id)
        if not isinstance(row, dict) or row.get("status") != "pending":
            return _row_verdict(row or {"status": "pending", "event_id": event_id}, duplicate=True, parent_called=False)
    if not os.path.isfile(paths["context"]):
        raise ValueError("rejected_context")
    _write_json(paths["judgment"], judgment)
    _write_json(paths["trace"], trace)
    try:
        _publish_files(store, event_id)
    except ValueError:
        _discard_uncommitted_parent_files(paths)
        verdict = _row_verdict(read_formal(store, event_id), duplicate=False, parent_called=False)
        verdict["ack"] = False
        verdict["reason"] = "parent_package_not_ready"
        return verdict
    with store.exclusive():
        store.reload()
        row = store.state["formal_inbox"][event_id]
        if row.get("status") != "pending":
            return _row_verdict(row, duplicate=True, parent_called=False)
        clock_text, _source = trusted_judgment_clock(judgment_clock)
        if _past_deadline(row.get("judgment_deadline"), clock_text):
            expired_clock = clock_text
        else:
            expired_clock = None
            row["status"] = "publish_ready"
            row["judgment_path"] = paths["judgment"]
            row["trace_path"] = paths["trace"]
            row["context_path"] = paths["context"]
            store.save()
    if expired_clock is not None:
        _discard_uncommitted_parent_files(paths)
        _mark_expired(store, event_id, expired_clock)
        raise ValueError("parent_deadline_expired")
    return _row_verdict(read_formal(store, event_id), duplicate=False, parent_called=False)


def submit_parent_files(store, event_id, judgment, trace, now, judgment_clock=None):
    """pending 補交父層已經寫好的 judgment／trace。不重新呼叫父 AI，也不假完成。

    now 是 payload 時鐘，刪掉不用。開頭的時鐘只做提早拒絕。
    能不能 ready，以 _install_parent_files 持久寫入前的可信時鐘為準。
    已是 publish_ready 則不覆寫。過期則終止並拒絕。
    """
    del now
    cycle_key(event_id)
    if not isinstance(judgment, dict) or not isinstance(trace, dict):
        raise ValueError("parent_package_not_ready")
    clock_text, _source = trusted_judgment_clock(judgment_clock)
    fd = _inbox_lock(store.root, event_id)
    if fd is None:
        raise ValueError("claim_held")
    try:
        row = read_formal(store, event_id)
        if not isinstance(row, dict):
            raise ValueError("not_received")
        if row.get("status") != "publish_ready" and (
            row.get("status") == "parent_deadline_expired" or _past_deadline(row.get("judgment_deadline"), clock_text)
        ):
            _mark_expired(store, event_id, clock_text)
            raise ValueError("parent_deadline_expired")
        if row.get("status") == "publish_ready" and _past_deadline(row.get("judgment_deadline"), clock_text):
            raise ValueError("parent_deadline_expired")
        return _install_parent_files(store, event_id, judgment, trace, judgment_clock=judgment_clock)
    finally:
        _unlock(fd)


def formal_status(store, event_id, judgment_clock=None):
    """離線讀狀態。不改列、不寫告警、不喚 AI。"""
    cycle_key(event_id)
    clock_text, source = trusted_judgment_clock(judgment_clock)
    row = read_formal(store, event_id)
    if not isinstance(row, dict):
        raise ValueError("not_received")
    past = row.get("status") != "publish_ready" and _past_deadline(row.get("judgment_deadline"), clock_text)
    return {
        "status": row.get("status"),
        "parent_invoked": row.get("parent_invoked") is True,
        "judgment_deadline": row.get("judgment_deadline"),
        "judgment_clock_source": row.get("judgment_clock_source"),
        "checked_at": clock_text,
        "checked_clock_source": source,
        "past_deadline": past,
        "mutated": False,
        "claim_held": False,
        "ack": False,
        "http_sent": False,
        "scheduler_present": False,
        "expiry_requires_platform_trigger": True,
        "platform_ai_process_killed": False,
        "platform_parent_ai": dict(PLATFORM_PARENT_AI),
    }


def expire_parent(store, event_id, judgment_clock=None):
    """平台或人明示觸發才檢查到期。沒有 scheduler，不會自己準時發生。

    活躍 flock 不搶。已持久成功不覆寫。本地終止不殺平台 AI process。
    """
    cycle_key(event_id)
    clock_text, source = trusted_judgment_clock(judgment_clock)
    fd = _inbox_lock(store.root, event_id)
    if fd is None:
        row = read_formal(store, event_id) or {"status": "pending", "event_id": event_id}
        verdict = _row_verdict(row, duplicate=False, parent_called=False, claim_held=True, accepted=False)
        verdict["reason"] = "claim_held"
        verdict["mutated"] = False
        verdict["checked_clock_source"] = source
        return verdict
    try:
        before = read_formal(store, event_id)
        if not isinstance(before, dict):
            raise ValueError("not_received")
        if before.get("status") == "publish_ready" or not _past_deadline(before.get("judgment_deadline"), clock_text):
            verdict = _row_verdict(before, duplicate=False, parent_called=False)
            verdict["mutated"] = False
            verdict["checked_clock_source"] = source
            verdict["ack"] = False
            return verdict
        after = _mark_expired(store, event_id, clock_text)
        verdict = _row_verdict(after, duplicate=False, parent_called=False)
        verdict["mutated"] = before.get("status") != "parent_deadline_expired" and after.get("status") == "parent_deadline_expired"
        verdict["checked_clock_source"] = source
        verdict["local_alert_only"] = True
        return verdict
    finally:
        _unlock(fd)
