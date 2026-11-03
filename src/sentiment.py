#!/usr/bin/env python3
"""幣安 USDT-M 公開端點情緒檢查（不需金鑰、不下單）。
caution（含資金費率 0.03%–0.05%）只供註明。2026-11-03 起實盤不再因 caution 降份，見 docs/strategy.md。
用法：python3 src/sentiment.py SYMBOL [SYMBOL...]
"""
import sys, json, urllib.request, urllib.parse
BASE = "https://fapi.binance.com"

def get(path, **p):
    try:
        with urllib.request.urlopen(BASE + path + "?" + urllib.parse.urlencode(p), timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_err": str(e)[:120]}
def ok(x): return not (isinstance(x, dict) and "_err" in x)
def chg(new, old): return None if new is None or not old else round((new - old) / old * 100, 2)

def ratio(path, sym):
    d = get(path, symbol=sym, period="15m", limit=17)   # 最新 + 4h 前 (16 根 15m)
    if not ok(d) or not d: return {"latest": None, "chg_4h_pct": None, "err": d.get("_err") if isinstance(d, dict) else "empty"}
    new, old = float(d[-1]["longShortRatio"]), float(d[0]["longShortRatio"])
    return {"latest": round(new, 4), "4h_ago": round(old, 4), "chg_4h_pct": chg(new, old)}

def analyze(sym):
    out = {"symbol": sym}
    pi = get("/fapi/v1/premiumIndex", symbol=sym)
    fr = float(pi["lastFundingRate"]) if ok(pi) and "lastFundingRate" in pi else None
    out["funding_rate_pct"] = None if fr is None else round(fr * 100, 4)
    fh = get("/fapi/v1/fundingRate", symbol=sym, limit=3)
    out["funding_last3_pct"] = [round(float(x["fundingRate"]) * 100, 4) for x in fh] if ok(fh) and isinstance(fh, list) else None
    out["global_ls_account"] = ratio("/futures/data/globalLongShortAccountRatio", sym)
    out["top_ls_position"] = ratio("/futures/data/topLongShortPositionRatio", sym)
    oi = get("/futures/data/openInterestHist", symbol=sym, period="1h", limit=25)
    oi_chg = chg(float(oi[-1]["sumOpenInterest"]), float(oi[0]["sumOpenInterest"])) if ok(oi) and isinstance(oi, list) and len(oi) > 1 else None
    out["oi_chg_24h_pct"] = oi_chg
    t = get("/fapi/v1/ticker/24hr", symbol=sym)
    pc = float(t["priceChangePercent"]) if ok(t) and "priceChangePercent" in t else None
    out["price_chg_24h_pct"] = pc
    ls = out["global_ls_account"]["latest"]

    def verdict(side):
        s = 1 if side == "long" else -1; block, caution = [], []
        if fr is not None and fr * s > 0.0005: block.append(f"資金費率 {fr*100:.4f}% {'>' if s>0 else '<'} {'' if s>0 else '-'}0.05%")
        tp = (out.get("top_ls_position") or {}).get("latest")
        if ls is not None and tp is not None and ((ls > 3.0 and tp > 2.0) if s > 0 else (ls < 0.33 and tp < 0.5)): caution.append(f"多空帳戶比 {ls} 且大戶持倉比 {tp} 偏{'多' if s>0 else '空'}（僅提醒）")
        if fr is not None and 0.0003 < fr * s <= 0.0005: caution.append(f"資金費率 {fr*100:.4f}% 超過 {'' if s>0 else '-'}0.03%")
        if oi_chg is not None and pc is not None and oi_chg > 30 and pc * s > 0:
            caution.append(f"持倉量 24h +{oi_chg}% 且價格同向 {pc}%")
        if block: return f"{side}_block", block
        if caution: return f"{side}_caution", caution
        if fr is None or ls is None: return "unknown", ["資金費率或多空比讀取失敗"]
        return f"{side}_ok", []
    for side in ("long", "short"):
        v, why = verdict(side); out[side] = {"verdict": v, "reasons": why}
    return out

if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit(__doc__)
    for s in sys.argv[1:]:
        try: r = analyze(s.upper())
        except Exception as e: r = {"symbol": s.upper(), "long": {"verdict": "unknown"}, "short": {"verdict": "unknown"}, "err": str(e)[:120]}
        print(json.dumps(r, ensure_ascii=False))
