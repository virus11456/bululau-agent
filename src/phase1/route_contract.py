"""離線路由契約。只描述原順序，不呼叫 prompt、不交易、不宣稱 review-round 已等效。"""
import json

MISSING_JOINTS = (
    "original_prompt_not_in_package",
    "original_executor_unknown",
    "review_round_does_not_run_manage",
    "protection_ai_not_attached",
    "candidate_ai_prompt_not_attached",
    "failure_timeout_not_wired_to_live_routine",
    "thirty_minute_routine_not_switched",
    "watch_60s_not_switched",
    "daily_ma_undecided",
)


def _ok(step):
    return isinstance(step, dict) and step.get("ok") is True and step.get("timed_out") is not True


def _zero(value):
    return type(value) is int and value == 0


def _trusted_flat(status):
    if not isinstance(status, dict):
        return False
    return (
        status.get("status") == "ok"
        and status.get("status_trusted") is True
        and status.get("fresh") is True
        and status.get("manage_error") is False
        and status.get("manage_complete") is True
        and _zero(status.get("positions"))
        and _zero(status.get("open_orders"))
        and _zero(status.get("algo_orders"))
    )


def _result(reason, trace, *, quiet):
    return {
        "schema_version": 1,
        "event_type": "offline_route_contract",
        "reason": reason,
        "trace": list(trace),
        "quiet": quiet,
        "old_ai_poll": "keep",
        "orders_placed": False,
        "review_round_equivalent": False,
        "strategy_rewritten": False,
        "missing_joints": list(MISSING_JOINTS),
    }


def route_round(*, manage, status, protection=None, candidate=None):
    """manage → 狀態確認 → 有倉或任一類單才保護 AI → 有候選才新倉 AI。失敗或超時留原 AI。

    呼叫端傳入各步結果。這個函式不開網路、不讀秘密、不下單。
    quiet 只在 manage 成功、狀態可信且新鮮、0 倉、0 一般單、0 條件單、0 候選時成立。
    """
    trace = ["manage"]
    if not _ok(manage):
        return _result("manage_failed", trace, quiet=False)
    trace.append("status")
    if not isinstance(status, dict) or status.get("confirmed") is not True:
        return _result("status_unconfirmed", trace, quiet=False)
    candidates = status.get("candidates")
    if not isinstance(candidates, list):
        return _result("candidates_unconfirmed", trace, quiet=False)
    needs_protection = not (
        _zero(status.get("positions")) and _zero(status.get("open_orders")) and _zero(status.get("algo_orders"))
    )
    if needs_protection:
        trace.append("protection_ai")
        if not _ok(protection):
            return _result("protection_ai_failed", trace, quiet=False)
    if candidates:
        trace.append("candidate_ai")
        if not _ok(candidate):
            return _result("candidate_ai_failed", trace, quiet=False)
        return _result("candidate_ai_kept_original", trace, quiet=False)
    if needs_protection:
        return _result("protection_ai_kept_original", trace, quiet=False)
    if _trusted_flat(status):
        trace.append("quiet")
        return _result("quiet_flat_book", trace, quiet=True)
    return _result("not_quiet", trace, quiet=False)


def main():
    sample = route_round(
        manage={"ok": True},
        status={
            "confirmed": True, "status": "ok", "status_trusted": True, "fresh": True,
            "manage_error": False, "manage_complete": True,
            "positions": 0, "open_orders": 0, "algo_orders": 0, "candidates": [],
        },
    )
    print(json.dumps({
        "review_round_equivalent": False,
        "orders_placed": False,
        "sample_quiet": sample["quiet"],
        "missing_joints": list(MISSING_JOINTS),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
