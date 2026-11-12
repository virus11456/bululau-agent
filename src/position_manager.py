#!/usr/bin/env python3
"""紙上倉位檢查。讀資料目錄的 positions.json，用已收盤的 15m K 棒檢查停損、止盈、時間停損。
止盈（高點碰到 target 就平）是 2026-10 參數，未接現行監控。
RULES 是 2026-10 紙上參數，和 2026-11-03 實盤出場不一致。實盤不要用這支決定減倉或移動停損，請看 docs/strategy.md。
有事件時結束碼 10，沒有是 0，資料錯誤是 2。用法：python3 src/position_manager.py [--dry]
"""
import sys, json, os, time, datetime, argparse, shutil
from common import *
POS = os.path.join(DATA_DIR, "positions.json"); DEC = os.path.join(DATA_DIR, "decisions.jsonl")
RULES = {  # be_at_r: move stop to entry at this R; time_bars: 15m bars to reach be_at_r else exit; max_bars: hard exit
    "瘸師弟": dict(be_at_r=0.5, time_bars=4, max_bars=8),
    "小師弟": dict(be_at_r=0.5, time_bars=12, max_bars=48),
    "布鹿流": dict(be_at_r=1.0, time_bars=None, max_bars=None, review_bars_1h=24),
    "funding_rate": dict(be_at_r=1.0, time_bars=None, max_bars=None, review_bars_1h=24),
}
PENDING_EXPIRY_BARS = 16  # 4h for unfilled limit plans unless pos has 'expires'

def iso_ms(s): return int(datetime.datetime.fromisoformat(s).timestamp() * 1000)
def ms_iso(m): return datetime.datetime.fromtimestamp(m / 1000, TZ).isoformat(timespec="seconds")

def manage(p, events):
    r = RULES.get(p.get("pattern"), RULES["布鹿流"])
    since = p.get("pm_last_bar") or iso_ms(p.get("filled") or p["opened"])
    ks = [k for k in klines(p["symbol"], "15m", 200) if k["t"] + 15 * 60000 > since]
    if not ks: return None
    entry, stop0 = p["entry"], p.get("initial_stop", p["stop"]); p.setdefault("initial_stop", stop0)
    R = entry - stop0
    if p.get("status") == "pending":
        for k in ks:
            if k["l"] <= entry:
                p["status"] = "open"; p["filled"] = ms_iso(k["t"]); events.append(ev(p, "fill", entry, k)); since = k["t"]; break
        else:
            n = len([k for k in klines(p["symbol"], "15m", 200) if k["t"] >= iso_ms(p["opened"])])
            exp = p.get("expires")
            if (exp and time.time() * 1000 > iso_ms(exp)) or (not exp and n >= PENDING_EXPIRY_BARS):
                events.append(ev(p, "cancel_unfilled", None, ks[-1])); return "remove"
            p["pm_last_bar"] = ks[-1]["t"]; return None
        ks = [k for k in ks if k["t"] >= since]
    t_open = iso_ms(p.get("filled") or p["opened"])
    for k in ks:
        bars = int((k["t"] - t_open) / 900000) + 1
        if k["l"] <= p["stop"]:
            return close(p, events, p["stop"], "breakeven_stop" if p["stop"] >= entry else "stop", k)
        if p.get("target") and k["h"] >= p["target"]:
            return close(p, events, p["target"], "target", k)
        best = max(p.get("high_seen", entry), k["h"]); p["high_seen"] = best
        if R > 0 and r.get("be_at_r") and best >= entry + r["be_at_r"] * R and p["stop"] < entry:
            p["stop"] = entry; events.append(ev(p, "move_stop_breakeven", entry, k))
        if r.get("time_bars") and bars >= r["time_bars"] and best < entry + r["be_at_r"] * R:
            return close(p, events, k["c"], f"time_stop_{r['time_bars']}bars", k)
        if r.get("max_bars") and bars >= r["max_bars"]:
            return close(p, events, k["c"], f"max_hold_{r['max_bars']}bars", k)
        if r.get("review_bars_1h") and bars >= r["review_bars_1h"] * 4 and not p.get("review_flagged"):
            p["review_flagged"] = True; events.append(ev(p, "time_review_24x1H", k["c"], k))
    p["pm_last_bar"] = ks[-1]["t"]; return None

def ev(p, kind, price, k):
    R = p["entry"] - p.get("initial_stop", p["stop"])
    return dict(time=now_iso(), trigger="position_manager", setup=p.get("pattern"), action=kind, symbol=p["symbol"], id=p.get("id"),
                price=price, bar=ms_iso(k["t"]), stop=p["stop"], target=p.get("target"),
                r=round((price - p["entry"]) / R, 2) if price and R > 0 and kind.startswith(("stop", "target", "time", "max", "breakeven_stop")) else None)

def close(p, events, px, why, k):
    e = ev(p, why, px, k); events.append(e)
    p.update(closed=ms_iso(k["t"]), exit=px, exit_reason=why, r=e["r"] if e["r"] is not None else round((px - p["entry"]) / (p["entry"] - p["initial_stop"]), 2))
    return "close"

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dry", action="store_true"); a = ap.parse_args()
    if not os.path.exists(POS):
        raise SystemExit(f"找不到紙上倉位檔 {POS}。實盤出場不要用這支，請用 live_trader.py。格式見 docs/operation.md。")
    t0 = time.time(); db = json.load(open(POS)); events = []; keep = []
    for p in db.get("open", []):
        try: res = manage(p, events)
        except RateLimited as e: print(json.dumps(dict(error=str(e), events=[]))); sys.exit(2)
        if res == "close": db.setdefault("closed", []).append(p)
        elif res != "remove": keep.append(p)
    db["open"] = keep
    if not a.dry:
        bk = POS + ".pre_prefilter"
        if not os.path.exists(bk): shutil.copy2(POS, bk)
        tmp = POS + ".tmp"; json.dump(db, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, POS)
        for e in events: append_jsonl(DEC, e)
    print(json.dumps(dict(time=now_iso(), events=events, open=len(keep), call_ai=bool(events), elapsed_s=round(time.time() - t0, 1)), ensure_ascii=False))
    sys.exit(10 if events else 0)

if __name__ == "__main__": main()
