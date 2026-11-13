#!/usr/bin/env python3
"""機械篩選：布鹿流 / 小師弟(ETH) / 瘸師弟 / 資金費率法。只讀幣安公開端點，不下單。
實盤最終判斷以 docs/strategy.md 為準（2026-11-03 例行流程）。亞洲盤的 confidence_hint 是舊提示，代理不得依它降份。
stdout：一個 JSON。結束碼：10＝有候選、要交給代理判斷；0＝沒有；2＝資料或限流錯誤，這輪不要判斷進場。
用法：python3 src/prefilter.py [--symbol XUSDT] [--asof 'YYYY-MM-DDTHH:MM'] [--quiet]
"""
import sys, json, time, datetime, argparse, os
from common import *

AVOID = {"INITUSDT", "EWZUSDT"}                 # hacked / manual avoid list (edit filters below)
MIN_QV = 5e6                         # 24h quote volume floor (liquidity)
BIG_QV = 3e8                         # >= this = "big coin"
RR_MIN = 1.5
LOG = os.path.join(DATA_DIR, "prefilter_log.jsonl")
DEC = os.path.join(DATA_DIR, "decisions.jsonl")

def stock_like(s):
    u = s.get("underlyingType", ""); sub = [x.upper() for x in s.get("underlyingSubType", [])]
    return u not in ("COIN", "INDEX") or any(x in ("STOCK", "TRADFI", "EQUITY", "COMMODITY", "PRE-MARKET") for x in sub)

def pct(a, b): return (a / b - 1) * 100

def chg(k15, bars):  # % change over N 15m bars
    return pct(k15[-1]["c"], k15[-1 - bars]["c"]) if len(k15) > bars else 0.0

def btc_guard(k15):
    """No longs within two 15m bars after BTC breaks a key level on volume (either direction)."""
    for i in (-1, -2):
        b = k15[i]; prior = k15[i - 20:i]
        av = sum(x["v"] for x in prior) / 20
        lo = min(x["l"] for x in prior); hi = max(x["h"] for x in prior)
        if b["v"] >= 2 * av and (b["c"] < lo or b["l"] < lo * 0.999):
            return {"blocked": True, "why": f"BTC broke 20-bar low {lo:.0f} on {b['v']/av:.1f}x vol at {ts(b['t'])}"}
    return {"blocked": False}

def kuai_shidi(k15, rs):
    """瘸師弟: strong coin, 15m volume spike + long black in last 2 bars -> ladder limit plan below prior low."""
    big = rs["big"]; body_min = 2.0 if big else 4.0
    for i in (-1, -2):
        b = k15[i]; prior = k15[i - 20:i]
        av = sum(x["v"] for x in prior) / 20
        body = pct(b["o"], b["c"])  # positive = black
        if av and b["v"] >= 3 * av and body >= body_min:
            # 004：參考點是這根訊號長黑打到的低點，往下 3%/4%/5%。不再用前 20 根最低點。
            # 原文例是 100 → 97/96/95，大幣小幣同一組深度；可以掛得很低。
            ref = b["l"]
            steps = (0.97, 0.96, 0.95)
            ladder = [round(ref * s, 8) for s in steps]
            avg = (ladder[0] * 2 + ladder[1] * 3 + ladder[2] * 5) / 10
            stop = ladder[2] * (0.985 if big else 0.97)
            risk = avg - stop
            return dict(setup="瘸師弟", tf="15m", bar=ts(b["t"]), vol_x=round(b["v"] / av, 1), black_pct=round(body, 2),
                        ladder=ladder, sizing="2:3:5", stop=round(stop, 8), target=round(avg + 1.5 * risk, 8), rr=1.5,
                        plan="ladder limit buys (paper); exit 1-1.5R or any bounce; max 2h")
    return None

def xiao_shidi(k15):
    """小師弟 (ETH only): slow 15m decline then volume flush; drop >=100pt; bounce >=40% of drop; last bar green."""
    w = k15[-40:]
    li = min(range(len(w)), key=lambda i: w[i]["l"]); L = w[li]["l"]
    if li < 4 or li > len(w) - 2: return None, "low too old/too fresh"
    hi_i = max(range(0, li), key=lambda i: w[i]["h"]); H = w[hi_i]["h"]
    drop = H - L
    if drop < 100: return None, f"drop {drop:.0f}pt <100"
    av = sum(x["v"] for x in k15[-60:-40]) / 20 or 1
    flush = max(w[max(li - 2, 0):li + 1], key=lambda x: x["v"])
    if flush["v"] < 2 * av or pct(flush["o"], flush["c"]) < 0.8: return None, "no volume flush long black"
    last = w[-1]; c = last["c"]
    if c <= last["o"]: return None, "last bar not green"
    bounce = (c - L) / drop
    if bounce < 0.4: return None, f"bounce {bounce:.0%} <40%"
    if bounce > 0.65: return None, f"bounce {bounce:.0%} -> chasing"
    stop = L * 0.999; risk = c - stop
    target = min(H, c + 2 * risk); rr = (target - c) / risk
    if rr < RR_MIN: return None, f"rr {rr:.2f}"
    return dict(setup="小師弟", tf="15m", entry=c, stop=round(stop, 2), target=round(target, 2), rr=round(rr, 2),
                drop_pt=round(drop, 1), bounce_pct=round(bounce * 100), plan="exit 1-1.5R / fading bounce / reversal K"), None

def maset(k):
    cl = [x["c"] for x in k]
    m = [ma(cl, n) for n in (30, 45, 60)]
    m_prev = [ma(cl[:-5], n) for n in (30, 45, 60)]
    return m, m_prev

def up_diverged(k):
    m, mp = maset(k)
    if None in m or None in mp: return False, m
    return (m[0] > m[1] > m[2] and all(a > b for a, b in zip(m, mp))), m

def daily_ok(kd):
    if len(kd) < 25: return False, "daily history short"
    a = min(x["l"] for x in kd[-10:]); b = min(x["l"] for x in kd[-20:-10])
    wild = any((x["h"] - x["l"]) / x["o"] > 0.5 for x in kd[-7:] if x.get("o"))
    if wild: return False, "daily range >50% (妖幣)"
    # 日 K 均線週期原文未定。不使用 MA20／MA30/45/60。只留底底高。
    if not (a > b): return False, "daily not higher-lows"
    return True, None

def buluuliu(k1h, kd):
    """布鹿流：均線向上發散、回測進場、不追價。

    均線週期原文沒有定成可擋單的數字（full_prompt：不可自行預設 MA30/45/60）。
    這裡用價格結構代理「向上發散」：最近 48 根 1H 對半，後段高點更高、低點也更高。
    這就是原文的底底高（後波低不破前波低、後波高會突破前波高），不填均線參數，
    也不要求拉回 1.2% 以內、不要求錘子或吞噬。
    不追價：最高點不在最後 3 根，而且收盤仍低於該高點。
    目標可以超過前高（003）。前高不到 1.5R 時，目標放到 1.5R，因此可以高於前高。
    """
    ok, why = daily_ok(kd)
    if not ok: return None, why
    if len(k1h) < 48: return None, "1H history short"
    w = k1h[-48:]
    first, second = w[:24], w[24:]
    if not (max(x["h"] for x in second) > max(x["h"] for x in first)
            and min(x["l"] for x in second) > min(x["l"] for x in first)):
        return None, "not higher-high higher-low (no up-divergence)"
    hi_i = max(range(len(w)), key=lambda i: w[i]["h"]); H = w[hi_i]["h"]
    if hi_i > len(w) - 3: return None, "at highs (no pullback, no chase)"
    last = w[-1]; c = last["c"]
    if not c or c >= H: return None, "at highs (no pullback, no chase)"
    vb = max(x["v"] for x in w[:hi_i + 1]); vbase = sum(x["v"] for x in k1h[-90:-48]) / max(len(k1h[-90:-48]), 1)
    if vbase and vb < 2 * vbase: return None, "no 2-3x volume expansion"
    pull_low = min(x["l"] for x in w[hi_i:])
    stop = pull_low * 0.995  # 沿用原本放在回檔低點下方 0.5% 的緩衝，不再綁 MA60
    risk = c - stop
    if risk <= 0: return None, "bad risk"
    rr_high = (H - c) / risk
    rr = max(RR_MIN, rr_high)
    target = c + rr * risk
    return dict(setup="布鹿流", tf="1H", entry=c, stop=round(stop, 8), target=round(target, 8), rr=round(rr, 2),
                prior_high=H, plan="pullback; target may exceed prior high; RR>=1.5"), None

def funding(k1h, fr):
    up, m = up_diverged(k1h)
    if not up: return None, "1H MAs not up-diverged"
    last = k1h[-1]; c = last["c"]
    if c <= last["o"]: return None, "last 1H not green"
    lvl = None
    if abs(last["l"] / m[0] - 1) <= 0.01: lvl = "MA30"
    elif abs(last["l"] / m[1] - 1) <= 0.01 or (last["l"] < m[0] and last["l"] >= m[1] * 0.99): lvl = "MA45"
    if not lvl: return None, "not at MA30/MA45"
    stop = m[2] * 0.98; risk = c - stop
    if risk <= 0: return None, "bad risk"
    target = c * 1.10; rr = (target - c) / risk
    if rr < RR_MIN: return None, f"rr {rr:.2f}"
    return dict(setup="funding_rate", tf="1H", entry=c, level=lvl, stop=round(stop, 8), target=round(target, 8),
                rr=round(rr, 2), funding_pct=round(fr * 100, 3), sizing="2:3:5 (MA30 then MA45)"), None

def kuai_today():
    d = datetime.datetime.now(TZ).strftime("%Y-%m-%d"); n = 0
    try:
        for l in open(DEC):
            if d in l[:40] and "瘸師弟" in l and any(x in l for x in ('"enter"', '"fill', '進場', '掛單')): n += 1
    except FileNotFoundError: pass
    return n

def run(args):
    t0 = time.time()
    end = None
    if args.asof:
        end = int(datetime.datetime.fromisoformat(args.asof).replace(tzinfo=TZ).timestamp() * 1000)
    info = get("/fapi/v1/exchangeInfo")
    perps = {s["symbol"]: s for s in info["symbols"] if s.get("contractType") == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
    tick = {t["symbol"]: t for t in get("/fapi/v1/ticker/24hr")}
    prem = {p["symbol"]: float(p.get("lastFundingRate") or 0) for p in get("/fapi/v1/premiumIndex")}
    try: fint = {f["symbol"]: int(f.get("fundingIntervalHours", 8)) for f in get("/fapi/v1/fundingInfo")}
    except Exception: fint = {}
    skipped = {"avoid": [], "stock": [], "illiquid": 0}
    uni = []
    for s, meta in perps.items():
        if s in AVOID: skipped["avoid"].append(s); continue
        if stock_like(meta): skipped["stock"].append(s); continue
        qv = float(tick.get(s, {}).get("quoteVolume", 0))
        if qv < MIN_QV and s != args.symbol: skipped["illiquid"] += 1; continue
        uni.append((s, qv))
    if args.limit: uni = sorted(uni, key=lambda x: -x[1])[:args.limit]
    k15 = dict(zip([u[0] for u in uni], pmap(lambda u: _safe(klines, u[0], "15m", 120, end), uni)))
    btc = k15.get("BTCUSDT") or klines("BTCUSDT", "15m", 120, end)
    guard = btc_guard(btc); btc24 = chg(btc, 96); btc4 = chg(btc, 16)
    hour = datetime.datetime.fromtimestamp((end or time.time() * 1000) / 1000, TZ).hour
    asia_day = 8 <= hour < 20
    cands, near, notes = [], [], []
    stage2 = []
    for s, qv in uni:
        k = k15.get(s)
        if not k or len(k) < 100: continue
        rs = dict(rs24=round(chg(k, 96) - btc24, 2), rs4h=round(chg(k, 16) - btc4, 2), big=qv >= BIG_QV, qv_m=round(qv / 1e6, 1))
        if s == "ETHUSDT":
            c, why = xiao_shidi(k)
            (cands if c else near).append(dict(symbol=s, **(c or {"setup": "小師弟", "fail": why}), **rs))
        strong = rs["rs24"] > 0 and rs["rs4h"] > -1
        if strong and kuai_today() < 2:
            c = kuai_shidi(k, rs)
            if c: cands.append(dict(symbol=s, **c, **rs))
        fr = prem.get(s, 0)
        hourly_extreme = fint.get(s) == 1 and abs(fr) >= 0.01 and not end
        # stage-2 (1H/1d) only for coins with 15m trend context or hourly extreme funding
        m30 = ma([x["c"] for x in k], 30)
        if hourly_extreme or (strong and m30 and k[-1]["c"] > m30 * 0.98) or s == args.symbol:
            stage2.append((s, rs, fr, hourly_extreme))
    def s2(item):
        s, rs, fr, he = item
        k1 = _safe(klines, s, "1h", 100, end); kd = _safe(klines, s, "1d", 40, end)
        out = []
        if not k1 or len(k1) < 70: return out
        if he:
            c, why = funding(k1, fr); out.append(dict(symbol=s, **(c or {"setup": "funding_rate", "fail": why}), **rs))
        if kd:
            c, why = buluuliu(k1, kd); out.append(dict(symbol=s, **(c or {"setup": "布鹿流", "fail": why}), **rs))
        return out
    for res in pmap(s2, stage2):
        for r in res:
            (near if "fail" in r else cands).append(r)
    if guard["blocked"]:
        for c in cands: c["blocked_by_btc_guard"] = True
        notes.append(guard["why"])
    live = [c for c in cands if not c.get("blocked_by_btc_guard")]
    for c in live: c["confidence_hint"] = "low (Asia daytime)" if asia_day else "normal"
    out = dict(time=now_iso(), asof=args.asof, call_ai=bool(live), n_candidates=len(live), candidates=live,
               blocked=[c for c in cands if c.get("blocked_by_btc_guard")], btc=dict(last=btc[-1]["c"], chg24=round(btc24, 2), guard=guard),
               asia_daytime=asia_day, universe=len(uni), stage2=len(stage2), skipped=dict(avoid=skipped["avoid"], stock=len(skipped["stock"]), illiquid=skipped["illiquid"]),
               near_misses=[n for n in near if n.get("setup") != "布鹿流" or "rr" in n.get("fail", "") or "confirmation" in n.get("fail", "")][:15],
               elapsed_s=round(time.time() - t0, 1), trigger_symbol=args.symbol)
    return out

def _safe(fn, *a):
    try: return fn(*a)
    except RateLimited: raise
    except Exception: return None

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--symbol"); ap.add_argument("--asof"); ap.add_argument("--limit", type=int)
    ap.add_argument("--nolog", action="store_true"); ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    try:
        o = run(a)
    except RateLimited as e:
        o = dict(time=now_iso(), call_ai=False, error=f"rate_limited: {e}", candidates=[])
        print(json.dumps(o, ensure_ascii=False)); append_jsonl(LOG, o); sys.exit(2)
    except Exception as e:
        o = dict(time=now_iso(), call_ai=False, error=f"{type(e).__name__}: {e}", candidates=[])
        print(json.dumps(o, ensure_ascii=False)); append_jsonl(LOG, o); sys.exit(2)
    if not a.nolog: append_jsonl(LOG, o)
    print(json.dumps(o if not a.quiet else {k: o[k] for k in ("time", "call_ai", "n_candidates", "candidates", "btc", "elapsed_s")}, ensure_ascii=False))
    sys.exit(10 if o["call_ai"] else 0)
