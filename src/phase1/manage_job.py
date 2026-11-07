"""不含 AI、不讀候選。只組既有 live_trader manage --live 的指令。

不在這裡送單。排程要不要裝，由使用者自己決定。
"""
import json


def build_manage_argv(python="python3"):
    return [python, "src/live_trader.py", "manage", "--live"]


TWO_H_MS = 7_200_000


def discretion_wakes(orders, now_ms):
    """只標記需要原 AI 決定的事，不撤單、不移停損。"""
    wakes = []
    for order in orders:
        if order.get("type") != "LIMIT":
            continue
        opened = order.get("time")
        if opened is None:
            continue
        if int(now_ms) - int(opened) > TWO_H_MS:
            wakes.append({
                "kind": "limit_older_than_2h",
                "symbol": order.get("symbol"),
                "order_id": order.get("orderId"),
                "action": "wake_original_ai",
                "cancel": False,
            })
    wakes.append({
        "kind": "structure_or_trail_stop",
        "action": "keep_original_ai_poll",
        "detector": None,
        "reason": "no_non_ai_equivalent",
    })
    return wakes


def run_manage(argv, runner, alert_path):
    if any("token_switch" in part or "candidates" in part for part in argv):
        raise RuntimeError("manage 風控不得依賴候選通知")
    code = runner(argv)
    if code != 0:
        with open(alert_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"exit_code": code, "reads_candidates": False}, ensure_ascii=False) + "\n")
    return code
