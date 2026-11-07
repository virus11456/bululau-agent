"""數量用十進位，只向下取整，不為了最小量向上湊。"""
from decimal import Decimal, ROUND_DOWN, InvalidOperation


def D(value):
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def dec_str(value):
    text = format(D(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def try_decimal(value):
    if value is None or value == "":
        return None
    try:
        return D(value)
    except (InvalidOperation, ValueError):
        return None


def floor_step(qty, step):
    qty = D(qty)
    step = D(step)
    if step <= 0:
        raise ValueError("qty_step_invalid")
    units = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def normalize_exit_qty(requested, step, min_qty, actual=None):
    """回傳 (qty_or_None, blocked_reason, remainder, never_exceed).

    step 或 min_qty 未知時不送單。低於最小量不向上湊。
    """
    if step is None or min_qty is None:
        return None, "precision_unknown", None, False
    requested = D(requested)
    floored = floor_step(requested, step)
    never_exceed = True
    if actual is not None:
        actual = D(actual)
        if floored > actual:
            floored = floor_step(actual, step)
        never_exceed = floored <= actual
    remainder = requested - floored
    if floored <= 0 or floored < D(min_qty):
        return None, "below_minimum_no_round_up", remainder, never_exceed
    return floored, None, remainder, never_exceed


def half_baseline(baseline, step=None):
    """基準量的一半。步進未知且無法整除時不猜。"""
    if baseline is None:
        return None, "baseline_unspecified"
    raw = D(baseline) / Decimal(2)
    if step is None:
        if raw == raw.to_integral_value():
            return raw, None
        return None, "qty_step_unknown"
    floored = floor_step(raw, step)
    if floored <= 0:
        return None, "below_minimum_no_round_up"
    return floored, None
