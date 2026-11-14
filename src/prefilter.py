#!/usr/bin/env python3
"""機械篩選：布鹿流 / 小師弟 / 瘸師弟 / 大師兄 / 資金費率法。只讀幣安公開端點，不下單。
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

# ETH 起跌要遠大於 100 點。原文只寫至少超過 100 點。這次用大於 100 點。
# BTC 歸在其他幣：起跌跌幅 / 起跌高點至少 5%，急殺那一根至少佔整段跌幅 60%。
# 停損不跟著這兩個門檻放寬。BTC、ETH 仍是最低點下方 0.5%，並且至少離進場 3%。
XIAO_ETH_DROP_MIN = 100
XIAO_ALT_DROP_PCT = 0.05
XIAO_SLOW_BARS = 6
XIAO_SLOW_VOL_MAX = 1.5
XIAO_FLUSH_VOL = 3.0
XIAO_FLUSH_SHARE = 0.60
XIAO_BOUNCE_MIN = 0.40
# 最低點下方的緩衝。預設 0.5%，可調。
XIAO_STOP_CUSHION = 0.005
XIAO_UNCONFIRMED = [
    "ETH 起跌要遠大於 100 點。原文只寫至少超過 100 點，沒有更嚴的第二個數字。這次用大於 100 點，更嚴的門檻未確認。",
    "停損在最低點下方再留緩衝。0.5%（XIAO_STOP_CUSHION）是預設、可調。BTC、ETH 另外至少離進場 3%。這條 3% 是停損距離，不跟著其他幣 5% 的起跌門檻改。",
    "第二次下殺「回落到彈幅一半左右」的誤差帶未確認。這次用最後一根的低點碰到彈幅中線、且不破最低點。",
]

def xiao_drop_ok(symbol, high, low):
    drop = high - low
    if symbol == "ETHUSDT":
        if drop <= XIAO_ETH_DROP_MIN:
            return False, "ETH drop {0:.0f}pt <=100".format(drop)
        return True, None
    if not high:
        return False, "bad high"
    if drop / high < XIAO_ALT_DROP_PCT:
        return False, "drop {0:.1%} <5%".format(drop / high)
    return True, None

def xiao_shidi(k15, symbol="ETHUSDT"):
    """小師弟：緩跌、急殺爆量、彈回之後等第二次下殺。限價 IOC，不在反彈途中追。"""
    if not k15 or len(k15) < 40:
        return None, "15m 資料不足"
    last_i = len(k15) - 1
    search_from = max(26, last_i - 36)
    if last_i - 1 <= search_from:
        return None, "15m 資料不足"
    li = min(range(search_from, last_i), key=lambda i: k15[i]["l"])
    if li < 26:
        return None, "low too old/too fresh"
    slow = k15[li - XIAO_SLOW_BARS:li]
    base = k15[li - XIAO_SLOW_BARS - 20:li - XIAO_SLOW_BARS]
    if len(slow) < XIAO_SLOW_BARS or len(base) < 20:
        return None, "緩跌不夠"
    if slow[-1]["c"] >= slow[0]["o"]:
        return None, "急殺前不是緩跌"
    base_v = sum(x["v"] for x in base) / float(len(base)) or 1
    slow_v = sum(x["v"] for x in slow) / float(XIAO_SLOW_BARS)
    if slow_v > XIAO_SLOW_VOL_MAX * base_v:
        return None, "緩跌量不平穩"
    Lbar = k15[li]
    L = Lbar["l"]
    if Lbar["c"] >= Lbar["o"]:
        return None, "急殺不是長黑"
    prior20 = k15[li - 20:li]
    av20 = sum(x["v"] for x in prior20) / 20.0 or 1
    if Lbar["v"] < XIAO_FLUSH_VOL * av20:
        return None, "急殺量不到 3 倍"
    H = max(x["h"] for x in slow)
    drop = H - L
    ok, why = xiao_drop_ok(symbol, H, L)
    if not ok:
        return None, why
    if drop <= 0 or (Lbar["o"] - Lbar["l"]) / drop < XIAO_FLUSH_SHARE:
        return None, "急殺佔跌幅不到 60%"
    between = k15[li + 1:last_i]
    if len(between) < 2:
        return None, "還沒有彈回"
    peak = max(x["h"] for x in between)
    if (peak - L) / drop < XIAO_BOUNCE_MIN:
        return None, "彈回不到 40%"
    half = L + 0.5 * (peak - L)
    last = k15[last_i]
    if last["l"] <= L:
        return None, "第二次下殺破了最低點"
    if last["l"] > half:
        return None, "還沒回落到彈幅一半"
    if last["c"] >= peak or last["h"] >= peak:
        return None, "反彈途中不追"
    entry = last["c"]
    stop = L * (1 - XIAO_STOP_CUSHION)
    if symbol in ("BTCUSDT", "ETHUSDT"):
        stop = min(stop, entry * 0.97)
    risk = entry - stop
    if risk <= 0:
        return None, "bad risk"
    target = entry + 2 * risk
    rr = (target - entry) / risk
    if rr < RR_MIN:
        return None, "rr {0:.2f}".format(rr)
    return dict(setup="小師弟", tf="15m", entry=round(entry, 8), stop=round(stop, 8),
                target=round(target, 8), rr=round(rr, 2),
                drop_pt=round(drop, 1), drop_pct=round(drop / H * 100, 2) if H else None,
                bounce_pct=round((peak - L) / drop * 100, 1), entry_type="ioc",
                low_t=Lbar.get("t"), cushion=XIAO_STOP_CUSHION,
                plan="第二次下殺限價 IOC。停損在最低點下方。到 2R 一次全出。試算進場價是這一根收盤，實際止盈用成交均價重算。上線用正常倉位。",
                unconfirmed=list(XIAO_UNCONFIRMED)), None

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

# 使用者 2026-11-14 確認。出處 user_notes2「講師常用參數 30/45/60」。只給大師兄，布鹿流日 K 不用這組。
DA_MA_PERIODS = (30, 45, 60)
DA_MA_SLOW = DA_MA_PERIODS[-1]
DA_BLACK_BODY_MIN = 2.0
DA_BLACK_BODY_MAX = 5.0
DA_BLACK_VOL = 2.0
DA_REBOUND_MIN = 10
DA_REBOUND_MAX = 40
DA_RECOVER = 0.50
DA_RETEST_BAND = 0.03
# 限價單掛著超過幾根 4H 就撤。預設 2 根，可調。
DA_LIMIT_EXPIRE_4H = 2
DA_SHIXIONG_UNCONFIRMED = [
    "限價單超過幾根 4H 就撤。2 根是預設、可調。",
    "前低下方再留多少未確認。停損取前低與 BTC/ETH 3%、小幣 10% 的較低者。",
    "8H 要不要跟 4H 各掃一輪未確認。這次只在新的 4H 收盤評估。",
]

def _down_ma_at(closes, close):
    """收盤在 MA30、MA45、MA60 下方，而且 MA30 < MA45 < MA60。"""
    vals = [ma(closes, n) for n in DA_MA_PERIODS]
    if any(v is None for v in vals):
        return False
    return close < vals[0] and close < vals[1] and close < vals[2] and vals[0] < vals[1] < vals[2]

def _down_ma(k4):
    return _down_ma_at([x["c"] for x in k4], k4[-1]["c"])

def da_shixiong(k4, symbol):
    """大師兄。只在最近一根已收 4H 是二次測前低時出候選。同一根長黑由呼叫端去重。"""
    if not k4 or len(k4) < DA_MA_SLOW + DA_REBOUND_MIN + 10:
        return None, "4H 資料不足"
    if not _down_ma(k4):
        return None, "均線未下發散"
    last_i = len(k4) - 1
    last = k4[last_i]
    found = None
    start = last_i - DA_REBOUND_MIN
    stop_i = max(10, last_i - DA_REBOUND_MAX)
    for i in range(start, stop_i - 1, -1):
        bar = k4[i]
        if bar["c"] >= bar["o"]:
            continue
        prior = k4[i - 10:i]
        if len(prior) < 10:
            continue
        avg_body = sum(abs(x["o"] - x["c"]) for x in prior) / 10.0
        body = bar["o"] - bar["c"]
        if avg_body <= 0 or body < DA_BLACK_BODY_MIN * avg_body or body > DA_BLACK_BODY_MAX * avg_body:
            continue
        avg_v = sum(x["v"] for x in prior) / 10.0
        if avg_v and bar["v"] < DA_BLACK_VOL * avg_v:
            continue
        if bar["c"] >= min(x["l"] for x in prior):
            continue
        closes = [x["c"] for x in k4[:i + 1]]
        if not _down_ma_at(closes, bar["c"]):
            continue
        after = k4[i + 1:]
        if not (DA_REBOUND_MIN <= len(after) <= DA_REBOUND_MAX):
            continue
        if any(x["c"] >= bar["o"] for x in after[:3]):
            continue
        span = bar["h"] - bar["l"]
        peak = max(x["h"] for x in after)
        if span <= 0 or peak < bar["l"] + DA_RECOVER * span:
            continue
        band = bar["l"] * (1 + DA_RETEST_BAND)
        if any(bar["l"] < x["l"] <= band for x in after[:-1]):
            continue
        if not (bar["l"] < last["l"] <= band):
            continue
        if last["c"] <= bar["l"]:
            continue
        found = i
        break
    if found is None:
        return None, "沒有符合的長黑或不是二次測前低"
    black = k4[found]
    after = k4[found + 1:]
    entry = last["c"]
    min_pct = 0.03 if symbol in ("BTCUSDT", "ETHUSDT") else 0.10
    stop = min(black["l"], entry * (1 - min_pct))
    risk = entry - stop
    if risk <= 0:
        return None, "停損距離不對"
    return dict(setup="大師兄", tf="4H", entry=round(entry, 8), stop=round(stop, 8),
                target=round(entry + 2 * risk, 8), target_3r=round(entry + 3 * risk, 8),
                flat_before_4r=round(entry + 4 * risk, 8), rr=2.0, entry_mode="二次測前低",
                entry_type="limit", black_low=black["l"], black_t=black.get("t"),
                rebound_bars=len(after), min_stop_pct=min_pct * 100,
                limit_expire_4h=DA_LIMIT_EXPIRE_4H,
                ma_periods=DA_MA_PERIODS,
                ma_rule="收盤 < MA30 < MA45 < MA60。使用者 2026-11-14 確認。",
                exit="2R 平 50%，剩下 50% 在 3R 平，4R 前全出。2R 後停損不動。",
                unconfirmed=list(DA_SHIXIONG_UNCONFIRMED), plan="二次測前低"), None

SEEN_NAME = "signal_seen.jsonl"
DA_NEAR_SKIP = {"4H 資料不足", "均線未下發散", "沒有符合的長黑或不是二次測前低"}

def signal_id(cand):
    setup = cand.get("setup")
    if setup == "小師弟" and cand.get("low_t") is not None:
        return "xiao|{0}|{1}".format(cand.get("symbol"), cand.get("low_t"))
    if setup == "大師兄" and cand.get("black_t") is not None:
        return "da|{0}|{1}".format(cand.get("symbol"), cand.get("black_t"))
    return None

def record_signals(args):
    """回放 --asof 和探測用的 --nolog 不寫去重紀錄，避免把歷史或試掃記成已看過。"""
    return not (getattr(args, "asof", None) or getattr(args, "nolog", False))

def filter_new_signals(cands, path=None, record=True):
    """同一個（幣、最低點或長黑）只留下一次。已看過的不再送進 gate，所以不會再 WAKE。"""
    path = path or os.path.join(DATA_DIR, SEEN_NAME)
    seen = set()
    try:
        with open(path) as handle:
            for line in handle:
                text = line.strip()
                if text:
                    seen.add(text)
    except FileNotFoundError:
        pass
    kept, fresh = [], []
    for cand in cands:
        key = signal_id(cand)
        if key and key in seen:
            continue
        kept.append(cand)
        if key:
            fresh.append(key)
            seen.add(key)
    if record and fresh:
        with open(path, "a") as handle:
            for key in fresh:
                handle.write(key + "\n")
    return kept

def cap_near(near, limit=15):
    """其他型態先留，小師弟和大師兄的未中籤不要把名額佔滿。"""
    other = [n for n in near if n.get("setup") not in ("小師弟", "大師兄")]
    screen = [n for n in near if n.get("setup") in ("小師弟", "大師兄")]
    return (other + screen)[:limit]

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
        c, why = xiao_shidi(k, s)
        if c:
            cands.append(dict(symbol=s, **c, **rs))
        elif why and not str(why).startswith("drop ") and not str(why).startswith("ETH drop") and why not in (
                "15m 資料不足", "緩跌不夠", "急殺前不是緩跌", "緩跌量不平穩", "急殺不是長黑",
                "急殺量不到 3 倍", "急殺佔跌幅不到 60%", "還沒有彈回", "彈回不到 40%",
                "第二次下殺破了最低點", "還沒回落到彈幅一半", "反彈途中不追", "low too old/too fresh"):
            near.append(dict(symbol=s, setup="小師弟", fail=why, **rs))
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
    # 大師兄不限 stage2 強勢幣。每輪用已收 4H，同一根長黑只出一次。
    k4map = dict(zip([u[0] for u in uni], pmap(lambda u: _safe(klines, u[0], "4h", 80, end), uni)))
    rs_by = {s: dict(rs24=round(chg(k15[s], 96) - btc24, 2), rs4h=round(chg(k15[s], 16) - btc4, 2),
                     big=qv >= BIG_QV, qv_m=round(qv / 1e6, 1))
             for s, qv in uni if k15.get(s) and len(k15[s]) >= 100}
    for s, _qv in uni:
        k4 = k4map.get(s)
        if not k4:
            continue
        c, why = da_shixiong(k4, s)
        rs = rs_by.get(s) or {}
        if c:
            cands.append(dict(symbol=s, **c, **rs))
        elif why not in DA_NEAR_SKIP:
            near.append(dict(symbol=s, setup="大師兄", fail=why, **rs))
    if guard["blocked"]:
        for c in cands: c["blocked_by_btc_guard"] = True
        notes.append(guard["why"])
    live = filter_new_signals([c for c in cands if not c.get("blocked_by_btc_guard")],
                              record=record_signals(args))
    for c in live: c["confidence_hint"] = "low (Asia daytime)" if asia_day else "normal"
    screened = [n for n in near if n.get("setup") != "布鹿流" or "rr" in n.get("fail", "") or "confirmation" in n.get("fail", "")]
    out = dict(time=now_iso(), asof=args.asof, call_ai=bool(live), n_candidates=len(live), candidates=live,
               blocked=[c for c in cands if c.get("blocked_by_btc_guard")], btc=dict(last=btc[-1]["c"], chg24=round(btc24, 2), guard=guard),
               asia_daytime=asia_day, universe=len(uni), stage2=len(stage2), skipped=dict(avoid=skipped["avoid"], stock=len(skipped["stock"]), illiquid=skipped["illiquid"]),
               near_misses=cap_near(screened),
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
