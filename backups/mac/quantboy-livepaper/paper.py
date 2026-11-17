#!/usr/bin/env python3
"""實盤新規則的紙上帳本。只打幣安公開端點（K 線），不碰金鑰、不下單。
add     記一筆模擬開單（限價可三檔 2:3:5；market 以當下價成交）
update  用 1 分 K 推進：限價成交、2 小時撤未成交檔、停損／止盈（同根 K 兩邊都碰到算停損）、布鹿流 1.5R 平一半移保本
move-stop  手動移停損（布鹿流沿 1H 更高低點上移時用）
status  列持倉與今日（台北日、系統日）合計
btc     BTC 已收日K 30/45/60 均線排列（多：收>MA30>MA45>MA60；空：反向；其餘維持），維護布鹿流暫停旗標
1R 的 U 數：開單當下紙上淨值 × 1%（portion 0.25 就 ×0.25）。手續費以 taker 0.05% 進出各一次估算。
"""
import argparse, json, os, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
TPE = timezone(timedelta(hours=8))
D = os.path.expanduser("~/quantboy-livepaper")
LEDGER = os.path.join(D, "ledger.json"); LOG = os.path.join(D, "events.jsonl")
START_EQ = 1294.76; FEE = 0.0005; EXPIRE_MS = 2 * 3600 * 1000

def now_ms(): return int(time.time() * 1000)
def tpe(ms): return datetime.fromtimestamp(ms / 1000, TPE).strftime("%m-%d %H:%M")
def get(path):
    with urllib.request.urlopen("https://fapi.binance.com" + path, timeout=15) as r: return json.load(r)
def load():
    if os.path.exists(LEDGER): return json.load(open(LEDGER))
    return {"equity": START_EQ, "trades": [], "next_id": 1, "bulu_paused": True}
def save(L):
    tmp = LEDGER + ".tmp"; json.dump(L, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, LEDGER)
def log(kind, **kw):
    kw.update(kind=kind, ts=datetime.now(TPE).isoformat(timespec="seconds"))
    open(LOG, "a").write(json.dumps(kw, ensure_ascii=False) + "\n")

def cmd_add(a):
    L = load()
    if a.setup == "布鹿流" and L.get("bulu_paused", True):
        print("拒絕：布鹿流暫停中（BTC 日K 未轉多）。先跑 btc。"); sys.exit(2)
    if any(t["symbol"] == a.symbol and t["state"] in ("pending", "open") for t in L["trades"]):
        print("拒絕：", a.symbol, "已有紙上持倉或掛單"); sys.exit(2)
    long = a.side.lower() in ("buy", "long")
    t0 = now_ms()
    if a.ladder:
        prices = [float(x) for x in a.ladder.split(",")]; weights = [0.2, 0.3, 0.5][:len(prices)]
        s = sum(weights); weights = [w / s for w in weights]
    else:
        p = float(a.price) if a.price else float(get("/fapi/v1/premiumIndex?symbol=" + a.symbol)["markPrice"])
        prices, weights = [p], [1.0]
    stop, target = float(a.stop), float(a.target)
    avg = sum(p * w for p, w in zip(prices, weights))
    risk_px = (avg - stop) if long else (stop - avg)
    if risk_px <= 0: print("拒絕：停損方向錯"); sys.exit(2)
    rr = ((target - avg) if long else (avg - target)) / risk_px
    one_r = L["equity"] * 0.01 * a.portion
    market = a.entry_type == "market"
    legs = [{"price": p, "w": w, "filled": market, "fill_ms": t0 if market else None, "dead": False} for p, w in zip(prices, weights)]
    t = {"id": L["next_id"], "symbol": a.symbol, "side": "long" if long else "short", "setup": a.setup,
         "created_ms": t0, "last_ms": t0 - 60000 if not market else t0, "legs": legs, "stop": stop, "stop_orig": stop,
         "target": target, "one_r_u": one_r, "plan_avg": avg, "rr_plan": round(rr, 3), "portion": a.portion,
         "state": "open" if market else "pending", "half_done": False, "realized_r": 0.0, "note": a.note or ""}
    L["next_id"] += 1; L["trades"].append(t); save(L); log("add", trade=t)
    print("紙上開單 #%d %s %s %s 進場 %s 停損 %s 止盈 %s 計畫RR %.2f 1R=%.2fU" % (t["id"], a.symbol, a.setup, t["side"], prices, stop, target, rr, one_r))

def risk_unit(t):
    long = t["side"] == "long"
    return sum(l["w"] * ((l["price"] - t["stop_orig"]) if long else (t["stop_orig"] - l["price"])) for l in t["legs"])

def close_part(L, t, px, frac, reason, ms):
    """frac：佔已成交部位的比例。回傳這部分的 R。"""
    long = t["side"] == "long"; ru = risk_unit(t)
    filled = [l for l in t["legs"] if l["filled"]]
    rem = 1.0 - (0.5 if t["half_done"] else 0.0)
    gross = sum(l["w"] * ((px - l["price"]) if long else (l["price"] - px)) for l in filled) * frac * rem
    notional = sum(l["w"] * l["price"] for l in filled) * frac * rem
    fee_r = (notional * FEE + sum(l["w"] for l in filled) * frac * rem * px * FEE) / ru
    r = gross / ru - fee_r
    t["realized_r"] += r
    log("close_part", id=t["id"], symbol=t["symbol"], px=px, frac=frac, reason=reason, r=round(r, 4), at=tpe(ms))
    return r

def finish(L, t, px, reason, ms):
    close_part(L, t, px, 1.0, reason, ms)
    t.update(state="closed", exit_px=px, exit_reason=reason, close_ms=ms, pnl_u=t["realized_r"] * t["one_r_u"])
    L["equity"] += t["pnl_u"]
    log("closed", id=t["id"], symbol=t["symbol"], setup=t["setup"], reason=reason, exit=px, r=round(t["realized_r"], 4), u=round(t["pnl_u"], 4), equity=round(L["equity"], 4))
    print("紙上平倉 #%d %s %s %s 出場 %s R %+.3f U %+.2f" % (t["id"], t["symbol"], t["setup"], reason, px, t["realized_r"], t["pnl_u"]))

def step(L, t):
    long = t["side"] == "long"; end = now_ms() - 60000
    start = t["last_ms"] + 60000
    while start <= end and t["state"] in ("pending", "open"):
        ks = get("/fapi/v1/klines?symbol=%s&interval=1m&startTime=%d&limit=1000" % (t["symbol"], start))
        ks = [k for k in ks if k[0] + 60000 <= now_ms()]
        if not ks: break
        for k in ks:
            ot, hi, lo = k[0], float(k[2]), float(k[3])
            newly = False
            for l in t["legs"]:
                if l["filled"] or l["dead"]: continue
                if ot - t["created_ms"] >= EXPIRE_MS: l["dead"] = True; log("leg_expired", id=t["id"], price=l["price"]); continue
                if (lo <= l["price"]) if long else (hi >= l["price"]): l["filled"] = True; l["fill_ms"] = ot; newly = True; log("leg_fill", id=t["id"], price=l["price"], at=tpe(ot))
            if t["state"] == "pending":
                if any(l["filled"] for l in t["legs"]): t["state"] = "open"
                elif all(l["dead"] for l in t["legs"]):
                    t.update(state="cancelled", close_ms=ot); log("cancelled_2h", id=t["id"]); print("紙上撤單 #%d %s 2小時未成交" % (t["id"], t["symbol"])); break
                else:
                    tgt_hit = (hi >= t["target"]) if long else (lo <= t["target"])
                    if tgt_hit: t.update(state="cancelled", close_ms=ot); log("target_hit_before_fill", id=t["id"]); print("紙上撤單 #%d %s 未成交前已過止盈" % (t["id"], t["symbol"])); break
                    t["last_ms"] = ot; continue
            # 持倉中：先看停損
            if (lo <= t["stop"]) if long else (hi >= t["stop"]):
                finish(L, t, t["stop"], "breakeven" if t["half_done"] else "stop", ot); break
            if t["setup"] == "布鹿流" and not t["half_done"]:
                ru = risk_unit(t); filled_w = sum(l["w"] for l in t["legs"] if l["filled"])
                avg = sum(l["w"] * l["price"] for l in t["legs"] if l["filled"]) / filled_w
                r15 = avg + 1.5 * ru / filled_w if long else avg - 1.5 * ru / filled_w
                if (hi >= r15) if long else (lo <= r15):
                    close_part(L, t, r15, 0.5, "bulu_1.5R_half", ot); t["half_done"] = True; t["stop"] = avg; t["target"] = None
                    log("bulu_half", id=t["id"], px=r15, new_stop=avg); print("紙上布鹿流 #%d %s 1.5R 平一半，停損移保本 %s" % (t["id"], t["symbol"], avg))
            if t["target"] is not None and ((hi >= t["target"]) if long else (lo <= t["target"])):
                finish(L, t, t["target"], "target", ot); break
            t["last_ms"] = ot
        start = t["last_ms"] + 60000
        if len(ks) < 1000: break

def cmd_update(a):
    L = load()
    for t in L["trades"]:
        if t["state"] in ("pending", "open"):
            try: step(L, t)
            except Exception as e: print("更新失敗", t["symbol"], e)
    save(L); cmd_status(a, L)

def cmd_move(a):
    L = load()
    for t in L["trades"]:
        if t["id"] == a.id and t["state"] == "open":
            log("move_stop", id=t["id"], old=t["stop"], new=float(a.stop)); t["stop"] = float(a.stop); save(L); print("已移停損", t["symbol"], a.stop); return
    print("找不到持倉中的 #", a.id); sys.exit(2)

def cmd_status(a, L=None):
    L = L or load(); n = datetime.now(TPE)
    d0 = int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    s0 = n.replace(hour=8, minute=0, second=0, microsecond=0)
    if n.hour < 8: s0 -= timedelta(days=1)
    s0 = int(s0.timestamp() * 1000)
    print("紙上淨值 %.2f（起始 %.2f）布鹿流暫停=%s" % (L["equity"], START_EQ, L.get("bulu_paused")))
    for t in L["trades"]:
        if t["state"] in ("pending", "open"):
            f = [l["price"] for l in t["legs"] if l["filled"]]
            print("  #%d %s %s %s 狀態=%s 已成交檔=%s 停損=%s 止盈=%s 已實現R=%+.3f 建立 %s" % (t["id"], t["symbol"], t["setup"], t["side"], t["state"], f, t["stop"], t["target"], t["realized_r"], tpe(t["created_ms"])))
    for name, z in (("台北日", d0), ("系統日", s0)):
        c = [t for t in L["trades"] if t["state"] == "closed" and t["close_ms"] >= z]
        print("  %s 平倉 %d 筆 合計 %+.2fU / %+.3fR" % (name, len(c), sum(t["pnl_u"] for t in c), sum(t["realized_r"] for t in c)))

def cmd_btc(a):
    # 2026-11-17 使用者規則：BTC 已收日K 收盤>MA30>MA45>MA60 轉多（布鹿流恢復），收盤<MA30<MA45<MA60 轉空（布鹿流暫停），其餘維持上一個狀態
    L = load()
    k = get("/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=400")
    closed = [x for x in k if x[6] < now_ms()]
    c = [float(x[4]) for x in closed]
    st = None
    for i in range(59, len(c)):
        m30, m45, m60 = sum(c[i-29:i+1]) / 30, sum(c[i-44:i+1]) / 45, sum(c[i-59:i+1]) / 60
        if c[i] > m30 > m45 > m60: st = "bull"
        elif c[i] < m30 < m45 < m60: st = "bear"
    paused = (st != "bull")
    print("BTC 日K 收盤 %.1f MA30 %.1f MA45 %.1f MA60 %.1f 趨勢狀態=%s 布鹿流暫停=%s" % (c[-1], m30, m45, m60, st, paused))
    if paused != L.get("bulu_paused", True):
        L["bulu_paused"] = paused; save(L)
        log("bulu_paused" if paused else "bulu_resumed", close=c[-1], ma30=m30, ma45=m45, ma60=m60, state=st)
        print("布鹿流已%s（紙上）" % ("暫停" if paused else "恢復"))

ap = argparse.ArgumentParser(); sp = ap.add_subparsers(dest="cmd", required=True)
p = sp.add_parser("add"); p.add_argument("--symbol", required=True); p.add_argument("--side", default="long")
p.add_argument("--setup", required=True); p.add_argument("--entry-type", choices=["market", "limit"], default="limit")
p.add_argument("--price"); p.add_argument("--ladder"); p.add_argument("--stop", required=True); p.add_argument("--target", required=True)
p.add_argument("--portion", type=float, default=1.0); p.add_argument("--note")
sp.add_parser("update"); sp.add_parser("status"); sp.add_parser("btc")
p = sp.add_parser("move-stop"); p.add_argument("--id", type=int, required=True); p.add_argument("--stop", required=True)
a = ap.parse_args()
{"add": cmd_add, "update": cmd_update, "status": cmd_status, "btc": cmd_btc, "move-stop": cmd_move}[a.cmd](a)
