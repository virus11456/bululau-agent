"""只用已去重的實際成交與已換算費用。缺資料保持 null，不估算成實績。"""
from .qty import D, dec_str, try_decimal

SLIPPAGE_SIGN = "positive_is_adverse"
MISSING = "未取得"


def _fee_quote(fill, fx_rate):
    if fill.get("fee_quote") not in (None, ""):
        return D(fill["fee_quote"])
    amount = fill.get("fee_amount")
    asset = fill.get("fee_asset")
    if amount in (None, "") and asset in (None, ""):
        return D(0)
    if fx_rate is None:
        return None
    converted = try_decimal(fx_rate)
    if converted is None:
        return None
    return D(amount) * converted


def summarize_position(given):
    """線性、報價幣結算。其他合約不套這個公式。"""
    instrument = given.get("instrument")
    result = {
        "gross_pnl_quote": None,
        "fee_quote": None,
        "net_pnl_quote": None,
        "net_R": None,
        "exit_vwap": None,
        "adverse_slippage_quote": None,
        "slippage": None,
        "slippage_sign": None,
        "realized_trade_count": 0,
        "realized_pnl": None,
        "display": MISSING,
        "must_not_estimate_as_actual": True,
        "entry_fee_allocated_quote": None,
        "entry_fee_unallocated_quote": None,
        "fills_used": [],
    }
    fills = list(given.get("fills") or [])
    entry_fills = list(given.get("entry_fills") or [])
    exit_fills = list(given.get("exit_fills") or [])
    if not entry_fills and not exit_fills:
        if not fills:
            result["realized_trade_count"] = 0
            result["realized_pnl"] = None
            result["display"] = MISSING
            return result
        result["display"] = MISSING
        return result

    if instrument not in (None, "synthetic_linear_quote_settled"):
        result["display"] = MISSING
        result["status"] = "blocked"
        result["reason"] = "non_linear_needs_confirmed_basis"
        return result

    if given.get("side") not in (None, "long"):
        result["display"] = MISSING
        result["status"] = "blocked"
        result["reason"] = "side_not_explicit_long"
        return result

    seen = set()
    def keep(fill, bucket):
        fill_id = fill.get("id")
        if fill_id is not None:
            if fill_id in seen:
                return None
            seen.add(fill_id)
        qty = try_decimal(fill.get("qty"))
        price = try_decimal(fill.get("price"))
        if qty is None or price is None or qty <= 0:
            return None
        return {"bucket": bucket, "qty": qty, "price": price, "raw": fill}

    entries = [x for x in (keep(f, "entry") for f in entry_fills) if x]
    exits = [x for x in (keep(f, "exit") for f in exit_fills) if x]
    if not exits:
        result["display"] = MISSING
        return result

    entry_qty = sum((x["qty"] for x in entries), D(0))
    if entry_qty <= 0:
        result["display"] = MISSING
        return result
    entry_vwap = sum((x["price"] * x["qty"] for x in entries), D(0)) / entry_qty
    exit_qty = sum((x["qty"] for x in exits), D(0))
    gross = sum(((x["price"] - entry_vwap) * x["qty"] for x in exits), D(0))
    exit_vwap = sum((x["price"] * x["qty"] for x in exits), D(0)) / exit_qty

    fx = given.get("fee_fx")
    fees = []
    fees_known = True
    for fill in entry_fills + exit_fills:
        fee = _fee_quote(fill, fx)
        if fee is None:
            fees_known = False
            break
        fees.append(fee)
    entry_fee = None
    if fees_known:
        entry_fee = sum((_fee_quote(f, fx) for f in entry_fills), D(0))
        exit_fee = sum((_fee_quote(f, fx) for f in exit_fills), D(0))
        allocated = entry_fee * exit_qty / entry_qty
        unallocated = entry_fee - allocated
        fee_total = allocated + exit_fee
        result["entry_fee_allocated_quote"] = dec_str(allocated)
        result["entry_fee_unallocated_quote"] = dec_str(unallocated)
        result["fee_quote"] = dec_str(fee_total)
        result["gross_pnl_quote"] = dec_str(gross)
        result["net_pnl_quote"] = dec_str(gross - fee_total)
        result["realized_pnl"] = result["net_pnl_quote"]
        result["realized_trade_count"] = 1
    else:
        result["gross_pnl_quote"] = dec_str(gross)
        result["fee_quote"] = None
        result["net_pnl_quote"] = None
        result["realized_pnl"] = None

    risk = try_decimal(given.get("initial_risk_quote"))
    if result["net_pnl_quote"] is not None and risk is not None and risk > 0:
        result["net_R"] = dec_str(D(result["net_pnl_quote"]) / risk)
    else:
        result["net_R"] = None

    planned = given.get("planned_exit_price", given.get("planned_price"))
    planned_d = try_decimal(planned)
    if planned_d is None:
        result["adverse_slippage_quote"] = None
        result["slippage"] = None
    else:
        slip = sum(((planned_d - x["price"]) * x["qty"] for x in exits), D(0))
        result["adverse_slippage_quote"] = dec_str(slip)
        result["slippage"] = result["adverse_slippage_quote"]
        result["slippage_sign"] = SLIPPAGE_SIGN

    result["exit_vwap"] = dec_str(exit_vwap)
    result["fills_used"] = [
        {"bucket": x["bucket"], "qty": dec_str(x["qty"]), "price": dec_str(x["price"])}
        for x in entries + exits
    ]
    missing_actual = (
        result["net_pnl_quote"] is None
        or result["net_R"] is None
        or result["slippage"] is None
    )
    result["display"] = MISSING if missing_actual else None
    return result
