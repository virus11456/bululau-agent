"""1800 秒低成本入口。只呼叫注入的函式。不執行 manage --live，不裝排程，不喚平台 AI。

原 prompt／執行器、超時政策、持倉例外在這個 repo 沒有可接的函式。缺的 callback 記成缺口，不造 echo。
"""
import json
import secrets

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
    if document.get("enabled") is not False or document.get("execute") is not False:
        raise ValueError("orchestrator_live_refused")
    if document.get("execute_manage") is not False or document.get("open_live_enabled") is not False:
        raise ValueError("orchestrator_live_refused")
    if document.get("interval_seconds") != INTERVAL_SECONDS:
        raise ValueError("scan_interval_too_fast")
    if document.get("schedule_installed") is not False or document.get("replaces_routine") is not False:
        raise ValueError("schedule_refused")
    if document.get("live_callbacks") is not False or document.get("live_http") is not False:
        raise ValueError("orchestrator_live_refused")
    contract = disabled_mac_contract()
    contract["loaded"] = True
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


def _invoke(name, fn, payload, order, gaps):
    if not callable(fn):
        gaps.append(name + "_absent")
        return {"called": False, "ok": False, "absent": True}
    order.append(name)
    try:
        value = fn(payload)
    except TimeoutError:
        return {"called": True, "ok": False, "timed_out": True}
    except Exception:
        return {"called": True, "ok": False, "failed": True}
    if not isinstance(value, dict) or value.get("ok") is not True or value.get("timed_out") is True:
        timed_out = isinstance(value, dict) and value.get("timed_out") is True
        return {"called": True, "ok": False, "timed_out": timed_out, "value": value if isinstance(value, dict) else None}
    return {"called": True, "ok": True, "value": value}


def _count(document, key):
    if not isinstance(document, dict):
        return None
    value = document.get(key)
    if type(value) is not int:
        return None
    return value


def _result(reason, order, gaps, *, quiet, event_id, duplicate=False, needs=None, fallback_called=False):
    return {
        "schema_version": 1,
        "event_type": "orchestrator_round",
        "reason": reason,
        "order": list(order),
        "quiet": quiet,
        "duplicate": duplicate,
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
        "gaps": list(GAPS) + [item for item in gaps if item not in GAPS],
    }


def run_orchestrator(store, *, now, scan_id, callbacks=None, notifier=None):
    """manage → status → prefilter。quiet 只在三者都真的成功且空倉。AI 步驟只呼叫注入的函式。"""
    del now
    callbacks = callbacks or {}
    with store.exclusive():
        store.reload()
        seen = store.state.setdefault("orchestrator_seen", {})
        prior = seen.get(scan_id)
        if isinstance(prior, dict) and prior.get("event_id"):
            return _result("duplicate_event", [], [], quiet=False, event_id=prior["event_id"], duplicate=True)
        event_id = "c-" + secrets.token_hex(16)
        seen[scan_id] = {"event_id": event_id}
        store.save()
    order = []
    extra_gaps = []
    payload = {"event_id": event_id, "evaluation_id": scan_id, "orders_placed": False, "live": False}
    manage = _invoke("manage", callbacks.get("manage"), payload, order, extra_gaps)
    status = _invoke("status", callbacks.get("status"), payload, order, extra_gaps)
    prefilter = _invoke("prefilter", callbacks.get("prefilter"), payload, order, extra_gaps)
    status_value = status.get("value") if status.get("ok") else None
    prefilter_value = prefilter.get("value") if prefilter.get("ok") else None
    positions = _count(status_value, "positions")
    open_orders = _count(status_value, "open_orders")
    algo_orders = _count(status_value, "algo_orders")
    counts_known = None not in (positions, open_orders, algo_orders)
    exposure = counts_known and (positions > 0 or open_orders > 0 or algo_orders > 0)
    candidates = prefilter_value.get("candidates") if isinstance(prefilter_value, dict) else None
    if not isinstance(candidates, list):
        candidates = None
    uncovered = uncovered_candidates(candidates or []) if candidates is not None else []
    context_ok = isinstance(prefilter_value, dict) and prefilter_value.get("context_sufficient") is True
    failed = (
        not manage.get("ok") or not status.get("ok") or not prefilter.get("ok")
        or manage.get("timed_out") or status.get("timed_out") or prefilter.get("timed_out")
    )
    manage_value = manage.get("value") if isinstance(manage.get("value"), dict) else {}
    trusted = (
        manage.get("ok") and manage_value.get("trusted") is True and manage_value.get("halted") is not True
        and isinstance(status_value, dict) and status_value.get("fresh") is True and status_value.get("status_trusted") is True
    )
    flat = counts_known and positions == 0 and open_orders == 0 and algo_orders == 0 and candidates == []
    if trusted and flat and not failed and not uncovered:
        return _result("quiet_flat_book", order, extra_gaps, quiet=True, event_id=event_id)
    planned_candidate = bool(candidates) and not uncovered and context_ok and not failed and trusted
    needs = []
    if exposure:
        needs.append("protection_ai")
    if planned_candidate:
        needs.append("candidate_ai")
    shared = dict(payload, needs=list(needs))
    if exposure:
        protection = _invoke("protection", callbacks.get("protection"), shared, order, extra_gaps)
        if not protection.get("ok"):
            failed = True
            planned_candidate = False
    if planned_candidate:
        candidate = _invoke("candidate", callbacks.get("candidate"), shared, order, extra_gaps)
        if not candidate.get("ok"):
            failed = True
    elif candidates:
        failed = True
    need_fallback = failed or bool(uncovered) or not trusted
    fallback_called = False
    if need_fallback or (needs and any(item.endswith("_absent") for item in extra_gaps)):
        fallback = _invoke("fallback", callbacks.get("fallback"), dict(payload, needs=list(needs)), order, extra_gaps)
        fallback_called = fallback.get("called") is True
        if not fallback_called:
            extra_gaps.append("original_ai_callback_unwired")
    if notifier is not None and callable(getattr(notifier, "notify", None)):
        order.append("notifier")
        notifier.notify(dict(payload, needs=list(needs), http_sent=False))
    reason = "fallback" if fallback_called else ("callbacks_unwired" if extra_gaps else "routed")
    if uncovered and fallback_called:
        reason = "unknown_setup_fallback"
    return _result(reason, order, extra_gaps, quiet=False, event_id=event_id, needs=needs, fallback_called=fallback_called)


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
