"""1800 秒低成本入口。只呼叫注入的函式。不執行 manage --live，不裝排程，不喚平台 AI。

原 prompt／執行器、超時政策、持倉例外在這個 repo 沒有可接的函式。缺的 callback 記成缺口，不造 echo。
TimeoutError 只代表這次同步 callback 自己丟出，不是執行器真的可超時。
"""
import fcntl
import json
import os
import signal
import subprocess
import secrets

from .ack_contract import cycle_key
from .market_adapter import uncovered_candidates

MAC_FLAT = "/Users/frank/quantboy-live/live_trader.py"
INTERVAL_SECONDS = 1800
GAPS = (
    "original_ai_invoke_absent",
    "original_ai_timeout_policy_absent",
    "position_exception_handler_absent",
    "platform_ai_not_executable",
    "manage_live_not_executed",
    "schedule_not_installed",
    "routine_not_replaced",
    "watch_60s_unchanged",
    "notifier_default_off",
    "gateway_live_send_off",
    "wakes_not_saved",
    "review_round_does_not_save_the_wake",
    "platform_ai_timeout_not_connected",
)
DEADLINE_MIN = 1
DEADLINE_MAX = 60
DEFAULT_DEADLINE_SECONDS = 20
ISOLATION_LIMITS = (
    "isolated_callbacks_are_argv_only",
    "pickle_not_used",
    "in_process_callable_timeout_is_self_raised_not_executor",
    "timeout_kills_the_process_group",
    "processes_that_leave_the_session_are_not_covered",
    "live_commands_refused",
    "platform_ai_timeout_not_connected",
)
COUNT_KEYS = ("positions", "open_orders", "algo_orders")
REFUSED_TRUE_FLAGS = (
    "enabled", "execute", "execute_manage", "open_live_enabled", "live_callbacks", "live_http",
    "gateway_enabled", "notifier_verified", "schedule_installed", "replaces_routine",
)


def disabled_mac_contract():
    """未啟用的命令契約。manage --live 會動實盤，這裡不執行。"""
    python = "python3"
    return {
        "enabled": False,
        "execute": False,
        "interval_seconds": INTERVAL_SECONDS,
        "schedule_installed": False,
        "replaces_routine": False,
        "watch_seconds": 60,
        "watch_modified": False,
        "manage_argv": [python, MAC_FLAT, "manage", "--live"],
        "status_argv": [python, MAC_FLAT, "status"],
        "open_live_argv": [python, MAC_FLAT, "open", "--live"],
        "open_live_enabled": False,
        "halt_rules_owned_by": "live_trader.py manage --live",
        "halt_rules": ["HALT", "401", "daily_loss_7pct", "cumulative_loss_30pct"],
        "paper_program_modified": False,
        "autoscan_orders_allowed": False,
        "two_hour_limit_rule": "wake_original_ai_do_not_cancel",
    }


def load_orchestrator_config(document):
    if not isinstance(document, dict):
        raise ValueError("malformed_config")
    for key in REFUSED_TRUE_FLAGS:
        if document.get(key) is True:
            raise ValueError("orchestrator_live_refused")
    if document.get("interval_seconds") != INTERVAL_SECONDS:
        raise ValueError("scan_interval_too_fast")
    deadline = document.get("callback_deadline_seconds", DEFAULT_DEADLINE_SECONDS)
    _bounded_deadline(deadline)
    contract = disabled_mac_contract()
    contract["callback_deadline_seconds"] = deadline
    contract["loaded"] = True
    contract["refused_true_flags"] = list(REFUSED_TRUE_FLAGS)
    return contract


def run_disabled_manage(contract, runner=None):
    """就算傳入 runner 也不呼叫。live 命令不能從這包啟動。"""
    del runner
    if not isinstance(contract, dict) or contract.get("execute") is not False or contract.get("enabled") is not False:
        raise ValueError("manage_live_refused")
    argv = contract.get("manage_argv") or []
    if "--live" in argv or any(str(part).endswith("live_trader.py") for part in argv):
        return {"executed": False, "called": False, "reason": "manage_live_not_executed"}
    return {"executed": False, "called": False, "reason": "command_disabled"}


def _bounded_deadline(value):
    if isinstance(value, bool) or not isinstance(value, int) or not DEADLINE_MIN <= value <= DEADLINE_MAX:
        raise ValueError("deadline_out_of_bounds")
    return value


def _refuse_live_argv(argv):
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part.strip() and "\n" not in part and "\r" not in part for part in argv):
        raise ValueError("malformed_command")
    if "--live" in argv or any(os.path.basename(part) == "live_trader.py" for part in argv):
        raise ValueError("manage_live_refused")


def run_isolated(argv, deadline_seconds, payload):
    """子行程加新 session。超時對整個 process group 送 SIGKILL，不等 thread 自己停。"""
    _refuse_live_argv(argv)
    deadline = _bounded_deadline(deadline_seconds)
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    try:
        stdout, _stderr = proc.communicate(input=json.dumps(payload, ensure_ascii=False), timeout=deadline)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return {"called": True, "ok": False, "timed_out": True, "isolated": True, "process_killed": True}
    if proc.returncode != 0:
        return {"called": True, "ok": False, "failed": True, "isolated": True, "process_killed": False}
    try:
        value = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict) or value.get("ok") is not True:
        return {"called": True, "ok": False, "failed": True, "isolated": True, "process_killed": False, "value": value if isinstance(value, dict) else None}
    value = dict(value)
    value.update({"called": True, "ok": True, "isolated": True, "process_killed": False, "timed_out": False})
    return value


def _invoke(name, fn, payload, order, gaps, deadline_seconds):
    if isinstance(fn, dict) and isinstance(fn.get("argv"), list):
        order.append(name)
        try:
            limit = _bounded_deadline(fn.get("deadline_seconds", deadline_seconds))
            return run_isolated(fn["argv"], limit, payload)
        except ValueError:
            return {"called": False, "ok": False, "failed": True}
    if not callable(fn):
        gaps.append(name + "_absent")
        return {"called": False, "ok": False, "absent": True}
    order.append(name)
    try:
        value = fn(payload)
    except TimeoutError:
        return {"called": True, "ok": False, "timed_out": True, "isolated": False, "process_killed": False}
    except Exception:
        return {"called": True, "ok": False, "failed": True, "isolated": False, "process_killed": False}
    if not isinstance(value, dict) or value.get("ok") is not True or value.get("timed_out") is True:
        timed_out = isinstance(value, dict) and value.get("timed_out") is True
        return {"called": True, "ok": False, "timed_out": timed_out, "value": value if isinstance(value, dict) else None}
    return {"called": True, "ok": True, "value": value}


def _explicit_count(document, key):
    """只有非布林的 int 且 >= 0 才算數。False、None、缺欄都是未知。"""
    if not isinstance(document, dict) or key not in document:
        return None
    value = document[key]
    if type(value) is not int or value < 0:
        return None
    return value


def _counts(document):
    return {key: _explicit_count(document, key) for key in COUNT_KEYS}


def _result(reason, order, gaps, *, quiet, event_id, duplicate=False, needs=None, fallback_called=False, claim_held=False):
    return {
        "schema_version": 1,
        "event_type": "orchestrator_round",
        "reason": reason,
        "order": list(order),
        "quiet": quiet,
        "duplicate": duplicate,
        "claim_held": claim_held,
        "event_id": event_id,
        "needs": list(needs or []),
        "fallback_called": fallback_called,
        "orders_placed": False,
        "platform_ai_executed": False,
        "review_round_equivalent": False,
        "wakes_saved": False,
        "http_sent": False,
        "gateway_called": False,
        "manage_executed": False,
        "schedule_installed": False,
        "routine_replaced": False,
        "watch_modified": False,
        "old_ai_poll": "keep",
        "isolation_limits": list(ISOLATION_LIMITS),
        "gaps": list(GAPS) + [item for item in gaps if item not in GAPS],
    }


def _claim_path(root, scan_id):
    cycle_key(scan_id)
    return os.path.join(str(root), "orchestrator-" + scan_id + ".claim")


def _try_claim(root, scan_id):
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(_claim_path(root, scan_id), flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _release_claim(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _peek_event(store, scan_id):
    with store.exclusive():
        store.reload()
        item = (store.state.get("orchestrator_seen") or {}).get(scan_id)
    if isinstance(item, dict):
        return item.get("event_id")
    return None


def _begin(store, scan_id):
    with store.exclusive():
        store.reload()
        seen = store.state.setdefault("orchestrator_seen", {})
        prior = seen.get(scan_id)
        if isinstance(prior, dict) and prior.get("status") in ("completed", "recovered") and prior.get("event_id"):
            return "done", prior["event_id"]
        if isinstance(prior, dict) and prior.get("status") == "running" and prior.get("event_id"):
            return "incomplete", prior["event_id"]
        event_id = "c-" + secrets.token_hex(16)
        seen[scan_id] = {"status": "running", "event_id": event_id, "phase": "started"}
        store.save()
        return "fresh", event_id


def _finish(store, scan_id, event_id, result):
    """fallback 沒接上或失敗時維持 running。不能把 pending 寫成 recovered。"""
    reason = result.get("reason")
    if result.get("fallback_required") and result.get("fallback_settled") is not True:
        return
    status = "recovered" if reason == "incomplete_restart" else "completed"
    with store.exclusive():
        store.reload()
        seen = store.state.setdefault("orchestrator_seen", {})
        current = seen.get(scan_id)
        if not isinstance(current, dict) or current.get("event_id") != event_id:
            return
        if current.get("status") == "running":
            seen[scan_id] = {"status": status, "event_id": event_id, "reason": reason}
            store.save()


def _recover(callbacks, event_id, scan_id, deadline_seconds):
    """中斷後不重跑 manage。只明確 fallback，不當成已成功完成。"""
    order = []
    gaps = ["incomplete_restart"]
    payload = {"event_id": event_id, "evaluation_id": scan_id, "orders_placed": False, "live": False, "resume": True}
    fallback = _invoke("fallback", callbacks.get("fallback"), payload, order, gaps, deadline_seconds)
    settled = fallback.get("called") is True and fallback.get("ok") is True
    if not settled:
        gaps.append("original_ai_callback_unwired" if fallback.get("called") is not True else "fallback_not_settled")
    result = _result(
        "incomplete_restart", order, gaps, quiet=False, event_id=event_id, fallback_called=fallback.get("called") is True,
    )
    result["fallback_settled"] = settled
    result["fallback_required"] = True
    return result


def _execute(callbacks, notifier, event_id, scan_id, deadline_seconds):
    order = []
    extra_gaps = []
    payload = {"event_id": event_id, "evaluation_id": scan_id, "orders_placed": False, "live": False}
    manage = _invoke("manage", callbacks.get("manage"), payload, order, extra_gaps, deadline_seconds)
    status = _invoke("status", callbacks.get("status"), payload, order, extra_gaps, deadline_seconds)
    prefilter = _invoke("prefilter", callbacks.get("prefilter"), payload, order, extra_gaps, deadline_seconds)
    status_value = status.get("value") if isinstance(status.get("value"), dict) else None
    prefilter_value = prefilter.get("value") if prefilter.get("ok") and isinstance(prefilter.get("value"), dict) else None
    counts = _counts(status_value)
    counts_complete = all(value is not None for value in counts.values())
    exposure = any(value is not None and value > 0 for value in counts.values())
    candidates = prefilter_value.get("candidates") if isinstance(prefilter_value, dict) else None
    if not isinstance(candidates, list):
        candidates = None
    uncovered = uncovered_candidates(candidates) if isinstance(candidates, list) else []
    context_ok = isinstance(prefilter_value, dict) and prefilter_value.get("context_sufficient") is True
    failed = (
        not manage.get("ok") or not status.get("ok") or not prefilter.get("ok")
        or manage.get("timed_out") or status.get("timed_out") or prefilter.get("timed_out")
    )
    manage_value = manage.get("value") if isinstance(manage.get("value"), dict) else {}
    halted_clear = manage_value.get("halted") is False
    halted_on = manage_value.get("halted") is True
    status_clear = (
        status.get("ok") is True and isinstance(status_value, dict)
        and status_value.get("fresh") is True and status_value.get("status_trusted") is True
    )
    manage_clear = manage.get("ok") is True and manage_value.get("trusted") is True and halted_clear
    flat = counts_complete and all(value == 0 for value in counts.values()) and candidates == []
    if manage_clear and status_clear and prefilter.get("ok") and flat and not failed and not uncovered:
        return _result("quiet_flat_book", order, extra_gaps, quiet=True, event_id=event_id)
    candidate_allowed = (
        isinstance(candidates, list) and len(candidates) > 0 and not uncovered and context_ok
        and counts_complete and manage_clear and status_clear and halted_clear and not failed
    )
    needs = []
    if exposure:
        needs.append("protection_ai")
    if candidate_allowed:
        needs.append("candidate_ai")
    shared = dict(payload, needs=list(needs))
    if exposure:
        protection = _invoke("protection", callbacks.get("protection"), shared, order, extra_gaps, deadline_seconds)
        if not protection.get("ok"):
            failed = True
            candidate_allowed = False
    if candidate_allowed:
        candidate = _invoke("candidate", callbacks.get("candidate"), shared, order, extra_gaps, deadline_seconds)
        if not candidate.get("ok"):
            failed = True
    unknown = (not counts_complete) or (not halted_clear) or halted_on or bool(uncovered) or candidates is None
    need_fallback = failed or unknown or (isinstance(candidates, list) and candidates and not candidate_allowed)
    fallback_called = False
    if need_fallback or (needs and any(item.endswith("_absent") for item in extra_gaps)):
        fallback = _invoke("fallback", callbacks.get("fallback"), dict(payload, needs=list(needs)), order, extra_gaps, deadline_seconds)
        fallback_called = fallback.get("called") is True
        if not fallback_called:
            extra_gaps.append("original_ai_callback_unwired")
    if notifier is not None and callable(getattr(notifier, "notify", None)):
        order.append("notifier")
        notifier.notify(dict(payload, needs=list(needs), http_sent=False))
    if halted_on and fallback_called:
        reason = "halt_fallback"
    elif uncovered and fallback_called:
        reason = "unknown_setup_fallback"
    elif fallback_called:
        reason = "fallback"
    elif extra_gaps:
        reason = "callbacks_unwired"
    else:
        reason = "routed"
    result = _result(reason, order, extra_gaps, quiet=False, event_id=event_id, needs=needs, fallback_called=fallback_called)
    result["fallback_required"] = bool(need_fallback or (needs and any(item.endswith("_absent") for item in extra_gaps)))
    result["fallback_settled"] = (not result["fallback_required"]) or (fallback_called and fallback.get("ok") is True)
    return result


def run_orchestrator(store, *, now, scan_id, callbacks=None, notifier=None, deadline_seconds=DEFAULT_DEADLINE_SECONDS):
    """manage → status → prefilter。quiet 只在欄位齊、halted 恰為 false、三個 count 都是 0。"""
    del now
    callbacks = callbacks or {}
    deadline_seconds = _bounded_deadline(deadline_seconds)
    fd = _try_claim(store.root, scan_id)
    if fd is None:
        return _result(
            "claim_held", [], ["active_claim"], quiet=False, event_id=_peek_event(store, scan_id), claim_held=True,
        )
    try:
        phase, event_id = _begin(store, scan_id)
        if phase == "done":
            return _result("duplicate_event", [], [], quiet=False, event_id=event_id, duplicate=True)
        result = _recover(callbacks, event_id, scan_id, deadline_seconds) if phase == "incomplete" else _execute(
            callbacks, notifier, event_id, scan_id, deadline_seconds,
        )
        _finish(store, scan_id, event_id, result)
        return result
    finally:
        _release_claim(fd)


def main(argv=None):
    contract = disabled_mac_contract()
    print(json.dumps({
        "enabled": False,
        "execute": False,
        "interval_seconds": contract["interval_seconds"],
        "schedule_installed": False,
        "manage_executed": False,
        "platform_ai_executed": False,
        "wakes_saved": False,
        "gaps": list(GAPS),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(None))
