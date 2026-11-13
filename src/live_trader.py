#!/usr/bin/env python3
"""幣安 USDT-M 實盤下單。金鑰只從環境變數或本機 env 檔讀取，絕不寫進日誌或輸出。
預設 dry-run；真實下單需加 --live 參數。資料目錄見 config.py。"""
import os, sys, json, time, hmac, hashlib, argparse, fcntl, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from config import data_dir, events_path, load_api_keys

BASE = "https://fapi.binance.com"
HOME = data_dir()
LOG = os.path.join(HOME, "live_log.jsonl")
STATE = os.path.join(HOME, "state.json")
EVENTS = events_path()                            # [{"time_tpe":"2026-10-31T20:30","title":"..."}]
HALT_FLAG = os.path.join(HOME, "HALT_REPORT_USER.flag")
LOCK = os.path.join(HOME, ".lock")
TPE = timezone(timedelta(hours=8))
CFG = dict(slices=20, max_positions=5, max_lev=5, risk_pct=0.01, daily_stop=0.07, daily_kill=0.30, event_window_min=30)

def now_tpe(): return datetime.now(TPE)
def log(kind, **kw):
    kw.update(ts=now_tpe().isoformat(timespec="seconds"), kind=kind)
    with open(LOG, "a") as f: f.write(json.dumps(kw, ensure_ascii=False, default=str) + "\n")

def _keys():
    return load_api_keys()

def tpe_ms(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, TPE).strftime("%m-%d %H:%M")
    except Exception:
        return "-"

class BinanceError(Exception):
    def __init__(self, status, code, msg): self.status, self.code, self.msg = status, code, msg; super().__init__(f"HTTP {status} code {code}: {msg}")

def req(method, path, params=None, signed=False, retries=5):
    params = dict(params or {})
    for attempt in range(retries):
        p = dict(params); headers = {}
        if signed:
            key, sec = _keys()
            p["timestamp"] = int(time.time() * 1000); p["recvWindow"] = 5000
            q = urllib.parse.urlencode(p)
            q += "&signature=" + hmac.new(sec.encode(), q.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = key
        else:
            q = urllib.parse.urlencode(p)
        url = BASE + path + ("?" + q if q else "")
        r = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return json.loads(resp.read() or "null")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try: j = json.loads(body); code, msg = j.get("code"), j.get("msg")
            except Exception: code, msg = None, body[:200]
            if e.code in (429, 418) or code == -1003:
                wait = int(e.headers.get("Retry-After") or 0) or min(60, 2 ** (attempt + 1))
                log("backoff", path=path, status=e.code, code=code, wait=wait); time.sleep(wait); continue
            if e.code >= 500 and attempt < retries - 1: time.sleep(2 ** attempt); continue
            raise BinanceError(e.code, code, msg)
        except urllib.error.URLError as e:
            if attempt < retries - 1: time.sleep(2 ** attempt); continue
            raise
    raise BinanceError(429, -1003, "rate limit retries exhausted")

# ---------- 精度 ----------
_info = None
def sym_info(symbol):
    global _info
    if _info is None:
        cache = os.path.join(HOME, "exchangeInfo.json")
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3600:
            _info = json.load(open(cache))
        else:
            _info = req("GET", "/fapi/v1/exchangeInfo"); json.dump(_info, open(cache, "w"))
    for s in _info["symbols"]:
        if s["symbol"] == symbol:
            f = {x["filterType"]: x for x in s["filters"]}
            return dict(tick=Decimal(f["PRICE_FILTER"]["tickSize"]), step=Decimal(f["LOT_SIZE"]["stepSize"]),
                        mstep=Decimal(f["MARKET_LOT_SIZE"]["stepSize"]), minqty=Decimal(f["LOT_SIZE"]["minQty"]),
                        maxmqty=Decimal(f["MARKET_LOT_SIZE"]["maxQty"]),
                        minnot=Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "5")), status=s["status"],
                        ctype=s.get("contractType"))
    raise SystemExit(f"找不到幣種 {symbol}")
def rnd(v, step, mode=ROUND_DOWN):
    v = Decimal(str(v)); return (v / step).to_integral_value(rounding=mode) * step
def fmt(d): return format(d.normalize(), "f")

# ---------- 狀態與鎖 ----------
class Lock:
    def __enter__(self):
        self.f = open(LOCK, "w")
        try: fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit("另一個 live_trader 正在執行（檔案鎖），稍後再試")
        return self
    def __exit__(self, *a): fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()

def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}
def save_state(s):
    tmp = STATE + ".tmp"; json.dump(s, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, STATE)

def account():
    return req("GET", "/fapi/v2/account", signed=True)
def equity_of(acc):
    return float(acc["totalWalletBalance"]) + float(acc["totalUnrealizedProfit"])

def day_state(acc):
    """每日 00:00 台北重算份額與日初淨值。"""
    s = load_state(); today = now_tpe().strftime("%Y-%m-%d")
    if s.get("day") != today:
        eq = equity_of(acc)
        s = dict(day=today, day_start_equity=eq, slice_margin=round(eq / CFG["slices"], 4),
                 halted_new=False, killed=False, created=now_tpe().isoformat(timespec="seconds"))
        save_state(s); log("day_reset", **s)
    return s

def daily_loss_pct(acc, s):
    return max(0.0, (s["day_start_equity"] - equity_of(acc)) / s["day_start_equity"]) if s["day_start_equity"] > 0 else 0.0

def positions(acc):
    return [p for p in acc["positions"] if float(p["positionAmt"]) != 0]

# ---------- 保護單（新版 Algo 端點，失敗退回舊端點） ----------
# status 讀取失敗時印這一行再往上拋。gate 看到這句就 WAKE，不能把它當成 0 張條件單。
ALGO_ORDERS_UNREADABLE = "條件單讀取失敗"

def open_algo(symbol=None, *, fail_closed=False):
    """讀條件單。

    manage／watch／flatten 維持舊行為（fail_closed=False）：幣安錯誤只記 log 並回 []。
    這次不改那些路徑，避免動到風控與 60 秒 watch。
    status 必須 fail_closed=True：讀取失敗要往上拋，呼叫端不可印成 0 張。
    """
    p = {"symbol": symbol} if symbol else {}
    try:
        data = req("GET", "/fapi/v1/openAlgoOrders", p, signed=True) or []
    except BinanceError as e:
        log("warn", msg="openAlgoOrders 失敗", err=str(e))
        if fail_closed:
            raise
        return []
    if fail_closed and not isinstance(data, list):
        log("warn", msg="openAlgoOrders 不是清單")
        raise BinanceError(None, None, "openAlgoOrders 不是清單")
    return data
def open_orders(symbol=None):
    p = {"symbol": symbol} if symbol else {}
    return req("GET", "/fapi/v1/openOrders", p, signed=True)

def place_protect(symbol, close_side, otype, trigger, live, qty=None):
    """qty=None → closePosition（需已有倉位）；qty 有值 → 指定數量 + reduceOnly（可在成交前預掛）。"""
    size = dict(closePosition="true") if qty is None else dict(quantity=qty, reduceOnly="true")
    algo = dict(algoType="CONDITIONAL", symbol=symbol, side=close_side, type=otype, triggerPrice=trigger,
                workingType="MARK_PRICE", priceProtect="TRUE", **size)
    if not live:
        print("  [dry-run] POST /fapi/v1/algoOrder", algo); return {"dry": True}
    try:
        r = req("POST", "/fapi/v1/algoOrder", algo, signed=True)
    except BinanceError as e:
        # -2021 = 會立即觸發（如止盈低於現價）；舊端點已不收條件單(-4120)，不再退回，直接拋出
        if e.code in (-2021, -2022, -4120, -1102, -1111, -4014, -4164) or e.status != 404: raise
        log("warn", msg="algoOrder 端點不存在，改用舊端點", err=str(e))
        legacy = dict(symbol=symbol, side=close_side, type=otype, stopPrice=trigger,
                      workingType="MARK_PRICE", priceProtect="TRUE", **size)
        r = req("POST", "/fapi/v1/order", legacy, signed=True)
    log("protect_placed", symbol=symbol, type=otype, trigger=trigger, qty=qty, resp=r); return r

def protect_orders(symbol, algo=None, oo=None):
    """回傳該幣種所有停損/止盈單 [(kind, id_key, id, qty, closePosition)]。"""
    algo = open_algo(symbol) if algo is None else algo
    oo = open_orders(symbol) if oo is None else oo
    out = []
    for o in algo:
        if o.get("symbol") != symbol: continue
        t = o.get("orderType") or o.get("type")
        if t in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
            out.append(dict(t=t, algo=True, id=o["algoId"], qty=o.get("quantity") or o.get("origQty"),
                            cp=str(o.get("closePosition")).lower() == "true", trig=o.get("triggerPrice")))
    for o in oo:
        if o["symbol"] != symbol: continue
        if o["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
            out.append(dict(t=o["type"], algo=False, id=o["orderId"], qty=o.get("origQty"),
                            cp=bool(o.get("closePosition")), trig=o.get("stopPrice")))
    return out

def cancel_protect(symbol, po, live):
    if not live: print("  [dry-run] 取消保護單", symbol, po["t"], po["id"]); return
    if po["algo"]: req("DELETE", "/fapi/v1/algoOrder", {"algoId": po["id"]}, signed=True)
    else: req("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": po["id"]}, signed=True)
    log("cancel_protect", symbol=symbol, order=po)

def market_close(symbol, amt, live, reason):
    side = "SELL" if amt > 0 else "BUY"
    o = dict(symbol=symbol, side=side, type="MARKET", quantity=fmt(Decimal(str(abs(amt)))), reduceOnly="true")
    if not live: print("  [dry-run] 市價平倉", o); return
    r = req("POST", "/fapi/v1/order", o, signed=True); log("market_close", reason=reason, order=o, resp=r); return r

def upcoming_event():
    if not os.path.exists(EVENTS): return None
    n = now_tpe(); w = timedelta(minutes=CFG["event_window_min"])
    for ev in json.load(open(EVENTS)):
        t = datetime.fromisoformat(ev["time_tpe"])
        if t.tzinfo is None: t = t.replace(tzinfo=TPE)
        if t - w <= n <= t + w: return ev
    return None

# --- kuai ladder (paste start) ---
# 這一段不讀金鑰、不呼叫 config.load_api_keys。下單時才沿用本檔既有的 account／req／sym_info。
# Mac 的 live_trader.py 沒有 config.py，貼這一段即可，不要連 _keys 一起貼。
LADDER_WEIGHTS = (2, 3, 5)  # 由淺到深。第一個價格權重 2，最深那檔權重 5。
LADDER_RR_MIN = 1.5         # 與現行 RR 門檻相同，不另設一個數字。

def parse_ladder(text):
    """'97,96,95' → 三個價格。做多必須一個比一個低（3%、4%、5%）。"""
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 3 or any(not p for p in parts):
        raise SystemExit("階梯要三個價格，用逗號隔開，例如 --ladder 97,96,95")
    try:
        prices = [Decimal(p) for p in parts]
    except Exception:
        raise SystemExit("階梯價格讀不懂：{0}".format(text))
    if any(p <= 0 for p in prices):
        raise SystemExit("階梯價格必須是正數")
    if not (prices[0] > prices[1] > prices[2]):
        raise SystemExit("做多階梯要由淺到深、一個比一個低（權重 2、3、5 對應該順序）")
    return prices

def weighted_entry(prices, weights=LADDER_WEIGHTS):
    """加權均價。權重預設 2:3:5。"""
    prices = [Decimal(p) for p in prices]
    weights = [Decimal(w) for w in weights]
    return sum(p * w for p, w in zip(prices, weights)) / sum(weights)

def split_ladder_qty(total, step, weights=LADDER_WEIGHTS):
    """把已經依 step 取整的總量拆成 2:3:5。四捨的餘數用最大餘數法補回，合計仍等於總量。
    餘數相同時補給較深的那一檔。"""
    total = Decimal(str(total)); step = Decimal(str(step))
    if step <= 0:
        raise SystemExit("stepSize 不是正數")
    units = int((total / step).to_integral_value(rounding=ROUND_DOWN))
    weight_sum = sum(weights)
    raw = [Decimal(units) * Decimal(w) / Decimal(weight_sum) for w in weights]
    base = [int(x.to_integral_value(rounding=ROUND_DOWN)) for x in raw]
    left = units - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (raw[i] - Decimal(base[i]), i), reverse=True)
    for i in order:
        if left <= 0:
            break
        base[i] += 1
        left -= 1
    return [Decimal(b) * step for b in base]

def order_size(entry, stop, equity, slice_margin, portion, leverage, step):
    """與 cmd_open 同一條式子：風險 1%、每份保證金、槓桿上限都不改。entry 用呼叫端給的單一價格。"""
    entry = float(entry); stop = float(stop)
    dist = abs(entry - stop) / entry
    risk_usdt = equity * CFG["risk_pct"]
    margin = slice_margin * portion
    notional = min(risk_usdt / dist, margin * leverage)
    qty = rnd(notional / entry, step)
    return qty, risk_usdt, margin

def ladder_target(text, avg, stop):
    """'1.5R' 用加權均價算。其他文字當絕對價格。"""
    if str(text).strip().upper() == "1.5R":
        return avg + Decimal("1.5") * (avg - stop)
    return Decimal(str(text))

def ladder_rr(avg, stop, target):
    risk = avg - stop
    if risk <= 0:
        return None
    return (target - avg) / risk

def limit_order(symbol, side, qty, price):
    return dict(symbol=symbol, side=side, type="LIMIT", quantity=fmt(qty), price=fmt(price),
                timeInForce="GTC", newOrderRespType="RESULT")

def check_ladder_levels(prices, qtys, minqty, minnot):
    """任一檔低於最小量或最小名目，整筆不送。回傳理由清單，空的代表可以送。"""
    reasons = []
    for i, (price, qty) in enumerate(zip(prices, qtys), 1):
        if qty < minqty:
            reasons.append("第 {0} 檔數量 {1} 低於最小量 {2}".format(i, fmt(qty), fmt(minqty)))
        notional = qty * price
        if notional < minnot:
            reasons.append("第 {0} 檔名目 {1} 低於最小名目 {2}".format(i, format(notional, "f"), fmt(minnot)))
    return reasons

def cancel_entry_order(symbol, order_id, live):
    if not live:
        print("  [dry-run] 取消進場單", symbol, order_id); return
    req("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True)
    log("cancel_entry", symbol=symbol, orderId=order_id)

def resize_protects(symbol, close_side, qty, live):
    """把停損與止盈的數量改成 qty。價格沿用掛著的觸發價，不重算。先掛新再撤舊。"""
    changed = False
    for po in protect_orders(symbol):
        if po["cp"] or po["qty"] is None or Decimal(str(po["qty"])) == qty:
            continue
        kind = "STOP_MARKET" if str(po["t"]).startswith("STOP") else "TAKE_PROFIT_MARKET"
        print("  調整 {0} 數量 {1} → {2}".format(kind, po["qty"], fmt(qty)))
        place_protect(symbol, close_side, kind, po["trig"], live, qty=fmt(qty))
        cancel_protect(symbol, po, live)
        changed = True
    return changed

def _filled_qty(order):
    try:
        return Decimal(str(order.get("executedQty") or "0"))
    except Exception:
        return Decimal(0)

def manage_ladder_pending(sym, pd, held_amt, live):
    """三檔限價的待成交處理。

    沿用單張限價：未終結前不把這筆移出 pending；全部終結且有持倉才轉 meta；
    終結且沒有持倉就留給後面的孤兒保護單清理。
    持倉已歸零但還有未成交的檔：撤掉那些進場單（停損或平倉之後不要留著）。
    有成交且還有檔掛著：保護單數量改成實際持倉，止盈價不重算。
    """
    rows = []
    for item in pd.get("orders") or []:
        oid = item.get("orderId")
        if not oid:
            continue
        try:
            rows.append(req("GET", "/fapi/v1/order", {"symbol": sym, "orderId": oid}, signed=True))
        except BinanceError as e:
            log("warn", msg="查詢階梯單失敗", symbol=sym, orderId=oid, err=str(e))
            print("{0} 階梯單查詢失敗，這輪不動".format(sym))
            return False
    if len(rows) != len(pd.get("orders") or []):
        return False
    filled = sum((_filled_qty(o) for o in rows), Decimal(0))
    resting = [o for o in rows if o.get("status") in ("NEW", "PARTIALLY_FILLED")]
    amt = held_amt.get(sym, 0.0)
    if resting and amt == 0 and filled == 0:
        print("{0} 階梯仍掛著 {1} 檔，尚未成交".format(sym, len(resting)))
        return False
    if not resting and (filled > 0 or amt != 0):
        # 跟單張限價一樣：狀態已 FILLED 就轉成持倉，即使這輪快照還沒看到數量。
        print("{0} 階梯已全部終結，持倉 {1}，轉為一般持倉管理".format(sym, amt))
        return "meta"
    if resting and amt != 0:
        close_side = "SELL" if amt > 0 else "BUY"
        print("{0} 階梯部分成交，持倉 {1}，還有 {2} 檔未完成".format(sym, amt, len(resting)))
        try:
            resize_protects(sym, close_side, Decimal(str(abs(amt))), live)
        except Exception as e:
            log("error", msg="調整階梯保護單數量失敗", symbol=sym, err=str(e))
        return False
    if resting and filled > 0 and amt == 0:
        # 快照是 0。保護單兩張都還在：可能只是成交還沒反映，這輪不撤。
        # 停損或止盈已經不在：視為已經平倉，撤掉剩下的進場單。
        protects = protect_orders(sym)
        has_sl = any(str(po["t"]).startswith("STOP") for po in protects)
        has_tp = any(str(po["t"]).startswith("TAKE") for po in protects)
        if has_sl and has_tp:
            print("{0} 持倉快照為 0 但保護單都還在，這輪不撤未成交檔".format(sym))
            return False
        for o in resting:
            print("{0} 持倉已平，撤未成交階梯 {1}".format(sym, o.get("orderId")))
            try:
                cancel_entry_order(sym, o.get("orderId"), live)
            except Exception as e:
                log("error", msg="撤階梯進場單失敗", symbol=sym, err=str(e))
        for po in protects:
            cancel_protect(sym, po, live)
        return "drop"
    print("{0} 階梯結束且無持倉，撤掉孤兒保護單".format(sym))
    for po in protect_orders(sym):
        cancel_protect(sym, po, live)
    return "drop"

def open_ladder(a):
    """三檔限價。總量用加權均價走 cmd_open 的同一條風險式子，再拆 2:3:5。預設不走這條。"""
    live = a.live; sym = a.symbol.upper(); side = a.side.upper()
    if side != "BUY":
        raise SystemExit("階梯目前只做多。空單請用原本的單張下單")
    if a.entry_type == "market":
        raise SystemExit("階梯是限價，不能跟 --entry-type market 一起用")
    acc = account(); s = day_state(acc); eq = equity_of(acc)
    if os.path.exists(HALT_FLAG) or s["killed"]:
        raise SystemExit("已觸發 30% 全平，需回報使用者，禁止開倉")
    dl = daily_loss_pct(acc, s)
    if dl >= CFG["daily_stop"] or s["halted_new"]:
        raise SystemExit("當日虧損 {0:.2f}% ≥ 7%，今日停開新倉".format(dl * 100))
    ev = upcoming_event()
    if ev:
        raise SystemExit("重大事件 ±30 分鐘內禁止開倉：{0}".format(ev))
    pos = positions(acc)
    if len(pos) >= CFG["max_positions"]:
        raise SystemExit("已達同時 5 倉上限")
    if any(p["symbol"] == sym for p in pos):
        raise SystemExit("{0} 已有倉位（單幣最多 1 份）".format(sym))
    if req("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"):
        if live:
            raise SystemExit("帳戶是雙向持倉模式，本模組只支援單向，請先改成單向")
        print("  ⚠ 帳戶目前是雙向持倉模式，--live 會被拒絕（dry-run 繼續）")
    info = sym_info(sym)
    if info["status"] != "TRADING":
        raise SystemExit("{0} 狀態 {1}".format(sym, info["status"]))
    prices = [rnd(p, info["tick"]) for p in parse_ladder(a.ladder)]
    stop = rnd(Decimal(str(a.stop)), info["tick"], ROUND_DOWN)
    avg = weighted_entry(prices)
    asked_r = str(a.target).strip().upper() == "1.5R"
    try:
        target = ladder_target(a.target, avg, stop)
    except Exception:
        raise SystemExit("止盈讀不懂：{0}".format(a.target))
    target = rnd(target, info["tick"], ROUND_DOWN)
    # 1.5R 取整後若矮於 1.5，補一個 tick。明示的較低止盈不補。
    if asked_r and avg > stop and ladder_rr(avg, stop, target) < Decimal(str(LADDER_RR_MIN)):
        target += info["tick"]
    if not (stop < prices[2] and avg < target):
        raise SystemExit("停損/止盈方向錯誤：加權均價 {0} 停損 {1} 止盈 {2}".format(fmt(avg), fmt(stop), fmt(target)))
    if prices[0] >= target:
        raise SystemExit("第 1 檔 {0} 不低于止盈 {1}，整筆不送出".format(fmt(prices[0]), fmt(target)))
    rr = ladder_rr(avg, stop, target)
    # 止盈 ROUND_DOWN 最多矮一個 tick。用這個寬度避免剛好 1.5R 被取整拒掉。0.83 這種仍會拒。
    tick_rr = info["tick"] / (avg - stop) if avg > stop else Decimal(0)
    if rr is None or rr + tick_rr + Decimal("1e-9") < Decimal(str(LADDER_RR_MIN)):
        raise SystemExit("加權進場 RR {0} < {1}，整筆不送出".format(rr, LADDER_RR_MIN))
    lev = min(CFG["max_lev"], int(a.leverage or CFG["max_lev"]))
    qty, risk_usdt, margin = order_size(avg, stop, eq, s["slice_margin"], a.portion, lev, info["step"])
    if qty < info["minqty"] or qty * Decimal(str(avg)) < info["minnot"]:
        raise SystemExit("數量 {0} 低於最小量/最小名目 {1}（停損太遠或份額太小）".format(qty, info["minnot"]))
    qtys = split_ladder_qty(qty, info["step"])
    reasons = check_ladder_levels(prices, qtys, info["minqty"], info["minnot"])
    if reasons:
        raise SystemExit("階梯有一檔低於交易所下限，整筆不送出：" + "；".join(reasons))
    if sum(qtys) != qty:
        raise SystemExit("階梯拆分合計 {0} 不等於總量 {1}，整筆不送出".format(sum(qtys), qty))
    close_side = "SELL"
    stop_p, tp_p = fmt(stop), fmt(target)
    orders = [limit_order(sym, side, q, p) for p, q in zip(prices, qtys)]
    shallow = prices[0]
    shallow_rr = ladder_rr(shallow, stop, target)
    plan = dict(symbol=sym, side=side, setup=a.setup, ladder=True, weights="2:3:5",
                prices=[fmt(p) for p in prices], qtys=[fmt(q) for q in qtys],
                entry=fmt(avg), stop=stop_p, target=tp_p, qty=fmt(qty),
                rr=round(float(rr), 4), level1_rr=None if shallow_rr is None else round(float(shallow_rr), 4),
                notional=round(float(qty) * float(avg), 2), lev=lev,
                est_loss=round(float(qty) * abs(float(avg) - float(stop)), 2),
                risk_cap=round(risk_usdt, 2), slice_margin=s["slice_margin"], portion=a.portion,
                margin=round(margin, 4), live=live)
    print("計畫:", json.dumps(plan, ensure_ascii=False))
    log("open_plan", **plan)
    for order in orders:
        req("POST", "/fapi/v1/order/test", order, signed=True)
    print("  /order/test 通過（三檔）")
    if not live:
        print("  [dry-run] 設定 {0} ISOLATED、槓桿 {1}x".format(sym, lev))
        for order in orders:
            print("  [dry-run] POST /fapi/v1/order", order)
        place_protect(sym, close_side, "STOP_MARKET", stop_p, False, qty=fmt(qty))
        place_protect(sym, close_side, "TAKE_PROFIT_MARKET", tp_p, False, qty=fmt(qty))
        return plan
    try:
        req("POST", "/fapi/v1/marginType", dict(symbol=sym, marginType="ISOLATED"), signed=True)
    except BinanceError as e:
        if e.code != -4046:
            raise
    req("POST", "/fapi/v1/leverage", dict(symbol=sym, leverage=lev), signed=True)
    placed = []
    try:
        for order in orders:
            r = req("POST", "/fapi/v1/order", order, signed=True)
            log("entry_order", order=order, resp=r)
            placed.append(dict(orderId=r.get("orderId"), price=order["price"], qty=order["quantity"]))
    except Exception as e:
        log("error", msg="階梯有一檔失敗，撤掉已送出的進場單", symbol=sym, err=str(e))
        for item in placed:
            try:
                cancel_entry_order(sym, item["orderId"], True)
            except Exception as e2:
                log("error", msg="撤銷階梯進場單失敗", err=str(e2))
        for p in positions(account()):
            if p["symbol"] == sym:
                market_close(sym, float(p["positionAmt"]), True, "ladder_entry_failed")
        raise SystemExit("階梯沒有全部掛上，已撤銷已送出的進場單: {0}".format(e))
    try:
        place_protect(sym, close_side, "STOP_MARKET", stop_p, True, qty=fmt(qty))
    except Exception as e:
        log("error", msg="階梯預掛停損失敗，撤銷進場單", symbol=sym, err=str(e))
        for item in placed:
            try:
                cancel_entry_order(sym, item["orderId"], True)
            except Exception as e2:
                log("error", msg="撤銷階梯進場單失敗", err=str(e2))
        for p in positions(account()):
            if p["symbol"] == sym:
                market_close(sym, float(p["positionAmt"]), True, "ladder_stop_place_failed")
        raise SystemExit("預掛停損失敗，階梯進場單已撤銷: {0}".format(e))
    try:
        place_protect(sym, close_side, "TAKE_PROFIT_MARKET", tp_p, True, qty=fmt(qty))
    except BinanceError as e:
        if e.code == -2021:
            log("info", msg="止盈價在現價錯邊，暫不預掛；成交後由 watch/manage 立即補掛", symbol=sym, target=tp_p)
            print("  止盈暫不預掛（會立即觸發），成交後 watch 會在 1 分鐘內補掛")
        else:
            log("error", msg="階梯預掛止盈失敗（停損已在）", err=str(e))
    except Exception as e:
        log("error", msg="階梯預掛止盈失敗（停損已在）", err=str(e))
    st = load_state()
    st.setdefault("pending", {})[sym] = dict(stop=stop_p, target=tp_p, setup=a.setup, qty=fmt(qty), side=side,
                                              ladder=True, orders=placed)
    save_state(st)
    print("三檔限價已掛，停損/止盈已同步預掛（reduceOnly 合計數量）", [x["orderId"] for x in placed])
    return plan
# --- kuai ladder (paste end) ---

# ---------- 指令 ----------
def cmd_status(a):
    acc = account(); s = day_state(acc); eq = equity_of(acc)
    pos = positions(acc)
    print(f"時間 {now_tpe():%Y-%m-%d %H:%M} 台北")
    print(f"錢包 {float(acc['totalWalletBalance']):.2f}  未實現 {float(acc['totalUnrealizedProfit']):+.2f}  淨值 {eq:.2f}  可用 {float(acc['availableBalance']):.2f} USDT")
    print(f"日初淨值 {s['day_start_equity']:.2f}  當日損益 {eq - s['day_start_equity']:+.2f} ({-daily_loss_pct(acc, s)*100:.2f}% 虧損側)  每份保證金 {s['slice_margin']:.2f}")
    print(f"停開新倉 {s['halted_new']}  全平旗標 {s['killed'] or os.path.exists(HALT_FLAG)}")
    print(f"持倉 {len(pos)}/{CFG['max_positions']}")
    for p in pos: print(f"  {p['symbol']} 數量 {p['positionAmt']} 進場 {p['entryPrice']} 未實現 {float(p['unrealizedProfit']):+.2f} 槓桿 {p['leverage']} 逐倉 {p['isolated']}")
    oo = open_orders()
    try:
        ao = open_algo(fail_closed=True)
    except BinanceError:
        # 先印標記再拋出。main() 會以非 0 結束；即使有人忽略結束碼，這一行也不是「條件單 0」。
        print(ALGO_ORDERS_UNREADABLE)
        raise
    print(f"一般掛單 {len(oo)}  條件單 {len(ao)}")
    for o in oo: print(f"  {o['symbol']} {o['side']} {o['type']} 價 {o.get('price')} 觸發 {o.get('stopPrice')} 掛於 {tpe_ms(o.get('time'))} id {o.get('orderId')}")
    for o in ao:
        created = o.get("createTime") or o.get("bookTime") or o.get("time") or o.get("updateTime")
        print(f"  [algo] {o.get('symbol')} {o.get('side')} {o.get('orderType') or o.get('type')} 觸發 {o.get('triggerPrice')} 掛於 {tpe_ms(created)} id {o.get('algoId')}")
    dual = req("GET", "/fapi/v1/positionSide/dual", signed=True)
    try: multi = req("GET", "/fapi/v1/multiAssetsMargin", signed=True)
    except BinanceError as e: multi = {"err": str(e)}
    print(f"雙向持倉 {dual.get('dualSidePosition')}  多資產模式 {multi.get('multiAssetsMargin', multi)}")

def cmd_open(a):
    live = a.live; sym = a.symbol.upper(); side = a.side.upper()
    acc = account(); s = day_state(acc); eq = equity_of(acc)
    # 風控閘門
    if os.path.exists(HALT_FLAG) or s["killed"]: raise SystemExit("已觸發 30% 全平，需回報使用者，禁止開倉")
    dl = daily_loss_pct(acc, s)
    if dl >= CFG["daily_stop"] or s["halted_new"]: raise SystemExit(f"當日虧損 {dl*100:.2f}% ≥ 7%，今日停開新倉")
    ev = upcoming_event()
    if ev: raise SystemExit(f"重大事件 ±30 分鐘內禁止開倉：{ev}")
    pos = positions(acc)
    if len(pos) >= CFG["max_positions"]: raise SystemExit("已達同時 5 倉上限")
    if any(p["symbol"] == sym for p in pos): raise SystemExit(f"{sym} 已有倉位（單幣最多 1 份）")
    if req("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"):
        if live: raise SystemExit("帳戶是雙向持倉模式，本模組只支援單向，請先改成單向")
        print("  ⚠ 帳戶目前是雙向持倉模式，--live 會被拒絕（dry-run 繼續）")
    info = sym_info(sym)
    if info["status"] != "TRADING": raise SystemExit(f"{sym} 狀態 {info['status']}")
    mark = float(req("GET", "/fapi/v1/premiumIndex", {"symbol": sym})["markPrice"])
    entry = float(a.price) if a.entry_type == "limit" else mark
    stop, target = float(a.stop), float(a.target)
    if side == "BUY" and not (stop < entry < target) or side == "SELL" and not (target < entry < stop):
        raise SystemExit(f"停損/止盈方向錯誤：entry {entry} stop {stop} target {target}")
    dist = abs(entry - stop) / entry
    lev = min(CFG["max_lev"], int(a.leverage or CFG["max_lev"]))
    risk_usdt = eq * CFG["risk_pct"]
    margin = s["slice_margin"] * a.portion
    notional = min(risk_usdt / dist, margin * lev)
    qty = rnd(notional / entry, info["mstep"] if a.entry_type == "market" else info["step"])
    if qty < info["minqty"] or qty * Decimal(str(entry)) < info["minnot"]:
        raise SystemExit(f"數量 {qty} 低於最小量/最小名目 {info['minnot']}（停損太遠或份額太小）")
    close_side = "SELL" if side == "BUY" else "BUY"
    # 停損朝不利方向取整、止盈朝保守方向取整
    stop_p = fmt(rnd(stop, info["tick"], ROUND_DOWN if side == "BUY" else ROUND_UP))
    tp_p = fmt(rnd(target, info["tick"], ROUND_DOWN if side == "BUY" else ROUND_UP))
    order = dict(symbol=sym, side=side, type="MARKET" if a.entry_type == "market" else "LIMIT", quantity=fmt(qty), newOrderRespType="RESULT")
    if a.entry_type == "limit": order.update(price=fmt(rnd(entry, info["tick"])), timeInForce="GTC")
    plan = dict(symbol=sym, side=side, setup=a.setup, entry=entry, stop=stop_p, target=tp_p, qty=fmt(qty),
                notional=round(float(qty) * entry, 2), lev=lev, est_loss=round(float(qty) * abs(entry - float(stop_p)), 2),
                risk_cap=round(risk_usdt, 2), slice_margin=s["slice_margin"], portion=a.portion, margin=round(margin, 4), live=live)
    print("計畫:", json.dumps(plan, ensure_ascii=False))
    log("open_plan", **plan)
    # 交易所參數驗證
    req("POST", "/fapi/v1/order/test", order, signed=True); print("  /order/test 通過")
    if not live:
        print(f"  [dry-run] 設定 {sym} ISOLATED、槓桿 {lev}x"); print("  [dry-run] POST /fapi/v1/order", order)
        place_protect(sym, close_side, "STOP_MARKET", stop_p, False)
        place_protect(sym, close_side, "TAKE_PROFIT_MARKET", tp_p, False); return
    try: req("POST", "/fapi/v1/marginType", dict(symbol=sym, marginType="ISOLATED"), signed=True)
    except BinanceError as e:
        if e.code != -4046: raise   # -4046 = 已是逐倉
    req("POST", "/fapi/v1/leverage", dict(symbol=sym, leverage=lev), signed=True)
    r = req("POST", "/fapi/v1/order", order, signed=True); log("entry_order", order=order, resp=r)
    if a.entry_type == "limit":
        oid = r.get("orderId")
        # 同步預掛：指定數量 + reduceOnly 停損/止盈（單向模式，成交前即生效）
        try:
            place_protect(sym, close_side, "STOP_MARKET", stop_p, True, qty=fmt(qty))
        except Exception as e:
            log("error", msg="限價預掛停損失敗，撤銷進場單", symbol=sym, err=str(e))
            try: req("DELETE", "/fapi/v1/order", {"symbol": sym, "orderId": oid}, signed=True)
            except Exception as e2: log("error", msg="撤銷進場單失敗", err=str(e2))
            acc2 = account()
            for p in positions(acc2):
                if p["symbol"] == sym: market_close(sym, float(p["positionAmt"]), True, "limit_stop_place_failed")
            raise SystemExit(f"預掛停損失敗，進場單已撤銷: {e}")
        try: place_protect(sym, close_side, "TAKE_PROFIT_MARKET", tp_p, True, qty=fmt(qty))
        except BinanceError as e:
            if e.code == -2021:
                log("info", msg="止盈價在現價錯邊，暫不預掛；成交後由 watch/manage 立即補掛", symbol=sym, target=tp_p)
                print("  止盈暫不預掛（會立即觸發），成交後 watch 會在 1 分鐘內補掛")
            else: log("error", msg="限價預掛止盈失敗（停損已在）", err=str(e))
        except Exception as e: log("error", msg="限價預掛止盈失敗（停損已在）", err=str(e))
        st = load_state(); st.setdefault("pending", {})[sym] = dict(stop=stop_p, target=tp_p, setup=a.setup, orderId=oid,
                                                                     qty=fmt(qty), side=side); save_state(st)
        print("限價單已掛，停損/止盈已同步預掛（reduceOnly 指定數量）", oid); return
    try:
        place_protect(sym, close_side, "STOP_MARKET", stop_p, True)
    except Exception as e:
        log("error", msg="停損掛單失敗，立即市價平倉", err=str(e))
        market_close(sym, float(qty) * (1 if side == "BUY" else -1), True, "stop_place_failed"); raise SystemExit(f"停損失敗已平倉: {e}")
    try: place_protect(sym, close_side, "TAKE_PROFIT_MARKET", tp_p, True)
    except Exception as e: log("error", msg="止盈掛單失敗（停損已在）", err=str(e))
    st = load_state(); st.setdefault("meta", {})[sym] = dict(stop=stop_p, target=tp_p, setup=a.setup); save_state(st)
    print("已進場並掛好保護單", r.get("avgPrice"), r.get("executedQty"))

def flatten_all(live, reason):
    acc = account()
    for p in positions(acc):
        market_close(p["symbol"], float(p["positionAmt"]), live, reason)
    for o in open_orders():
        if live: req("DELETE", "/fapi/v1/allOpenOrders", {"symbol": o["symbol"]}, signed=True)
        else: print("  [dry-run] 取消", o["symbol"], o["orderId"])
    for o in open_algo():
        if live: req("DELETE", "/fapi/v1/algoOrder", {"algoId": o["algoId"]}, signed=True)
        else: print("  [dry-run] 取消 algo", o.get("symbol"), o.get("algoId"))

def cmd_manage(a):
    live = a.live; acc = account(); s = day_state(acc); dl = daily_loss_pct(acc, s)
    if dl >= CFG["daily_kill"] and not s["killed"]:
        log("kill", loss_pct=dl); flatten_all(live, "daily_kill_30pct")
        if live:
            s["killed"] = True; s["halted_new"] = True; save_state(s)
            open(HALT_FLAG, "w").write(json.dumps(dict(ts=now_tpe().isoformat(), loss_pct=dl)))
        print(f"!!! 當日虧損 {dl*100:.2f}% ≥ 30%，已全平，必須回報使用者"); return
    if dl >= CFG["daily_stop"] and not s["halted_new"]:
        s["halted_new"] = True; save_state(s); log("daily_stop", loss_pct=dl); print(f"當日虧損 {dl*100:.2f}% ≥ 7%，今日停開新倉")
    st = load_state()
    # ---- 限價待成交單：成交/部分成交/取消/過期處理 ----
    held_amt = {p["symbol"]: float(p["positionAmt"]) for p in positions(acc)}
    for sym, pd in list(st.get("pending", {}).items()):
        if pd.get("ladder"):
            done = manage_ladder_pending(sym, pd, held_amt, live)
            if done:
                if done == "meta":
                    st.setdefault("meta", {})[sym] = {k: pd[k] for k in ("stop", "target", "setup")}
                st["pending"].pop(sym, None)
                log("ladder_done", symbol=sym, pos=held_amt.get(sym, 0.0))
                if live:
                    save_state(st)
            continue
        if not pd.get("orderId"): continue
        try: o = req("GET", "/fapi/v1/order", {"symbol": sym, "orderId": pd["orderId"]}, signed=True)
        except BinanceError as e: log("warn", msg="查詢限價單失敗", symbol=sym, err=str(e)); continue
        stt = o["status"]; amt = held_amt.get(sym, 0.0)
        if stt in ("NEW", "PARTIALLY_FILLED"):
            # 未終結：保護單維持整單數量（reduceOnly 不會反向開倉），確保後續成交也受保護
            print(f"{sym} 限價單 {stt} 已成交 {o.get('executedQty')}/{o.get('origQty')}"); continue
        st["pending"].pop(sym)
        if stt == "FILLED" or amt != 0:
            st.setdefault("meta", {})[sym] = {k: pd[k] for k in ("stop", "target", "setup")}
            log("limit_done", symbol=sym, status=stt, executed=o.get("executedQty"), pos=amt)
            print(f"{sym} 限價單 {stt}，持倉 {amt}，轉為一般持倉管理")
        else:
            log("limit_dead", symbol=sym, status=stt); print(f"{sym} 限價單 {stt} 且無持倉，撤掉孤兒保護單")
            for po in protect_orders(sym): cancel_protect(sym, po, live)
        if live: save_state(st)
    algo = open_algo(); oo = open_orders(); meta = {**st.get("pending", {}), **st.get("meta", {})}
    for p in positions(acc):
        sym = p["symbol"]; amt = float(p["positionAmt"]); close_side = "SELL" if amt > 0 else "BUY"
        types = {(o.get("orderType") or o.get("type")) for o in algo if o.get("symbol") == sym}
        types |= {o["type"] for o in oo if o["symbol"] == sym}
        has_sl = "STOP_MARKET" in types or "STOP" in types
        has_tp = "TAKE_PROFIT_MARKET" in types or "TAKE_PROFIT" in types
        print(f"{sym} {amt} 停損 {'有' if has_sl else '缺'} 止盈 {'有' if has_tp else '缺'}")
        m = meta.get(sym)
        # 指定數量的保護單：若限價單已終結，數量需等於實際持倉
        if sym not in st.get("pending", {}):
            for po in protect_orders(sym, algo, oo):
                if po["cp"] or po["qty"] is None or Decimal(str(po["qty"])) == Decimal(str(abs(amt))): continue
                kind = "STOP_MARKET" if po["t"].startswith("STOP") else "TAKE_PROFIT_MARKET"
                print(f"  調整 {kind} 數量 {po['qty']} → {abs(amt)}")
                try:
                    place_protect(sym, close_side, kind, po["trig"], live, qty=fmt(Decimal(str(abs(amt)))))
                    cancel_protect(sym, po, live)   # 先掛新再撤舊，避免空窗
                except Exception as e:
                    log("error", msg="調整保護單數量失敗", symbol=sym, err=str(e))
        if not has_sl:
            if not m: log("error", msg="缺停損且無紀錄停損價，市價平倉", symbol=sym); market_close(sym, amt, live, "no_stop_no_meta"); continue
            try: place_protect(sym, close_side, "STOP_MARKET", m["stop"], live)
            except Exception as e:
                log("error", msg="補掛停損失敗，市價平倉", symbol=sym, err=str(e)); market_close(sym, amt, live, "restop_failed"); continue
        if not has_tp and m:
            try: place_protect(sym, close_side, "TAKE_PROFIT_MARKET", m["target"], live)
            except Exception as e: log("error", msg="補掛止盈失敗", symbol=sym, err=str(e))
    # 已無持倉的幣種：清掉孤兒保護單
    held = {p["symbol"] for p in positions(acc)}
    for o in algo:
        if o.get("symbol") not in held and o.get("symbol") not in st.get("pending", {}):
            print("孤兒條件單", o.get("symbol"), o.get("algoId"))
            if live: req("DELETE", "/fapi/v1/algoOrder", {"algoId": o["algoId"]}, signed=True); log("cancel_orphan", order=o)
    for o in oo:
        if o["symbol"] not in held and o["symbol"] not in st.get("pending", {}) and o["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            print("孤兒條件單(舊端點)", o["symbol"], o["orderId"])
            if live: req("DELETE", "/fapi/v1/order", {"symbol": o["symbol"], "orderId": o["orderId"]}, signed=True); log("cancel_orphan", order=o)
    print(f"當日虧損 {dl*100:.2f}%  持倉 {len(held)}")

def cmd_watch(a):
    """輕量成交偵測（launchd 每 60 秒）：無待成交限價單 → 零 API 呼叫；
    有成交（含部分成交）且缺止盈/停損 → 立即跑一次 manage 補掛與調整數量。"""
    st = load_state(); pend = st.get("pending", {})
    if not pend: return
    need = False
    for sym, pd in pend.items():
        if pd.get("ladder"):
            types = {po["t"] for po in protect_orders(sym)}
            has_sl = any(str(t).startswith("STOP") for t in types)
            has_tp = any(str(t).startswith("TAKE") for t in types)
            for item in pd.get("orders") or []:
                if not item.get("orderId"):
                    continue
                try:
                    o = req("GET", "/fapi/v1/order", {"symbol": sym, "orderId": item["orderId"]}, signed=True, retries=2)
                except BinanceError as e:
                    log("warn", msg="watch 查單失敗", symbol=sym, err=str(e)); continue
                executed = float(o.get("executedQty") or 0)
                resting = o["status"] in ("NEW", "PARTIALLY_FILLED")
                if (not resting) or executed > 0 or not has_sl or not has_tp:
                    log("watch_trigger", symbol=sym, status=o["status"], executed=o.get("executedQty"), ladder=True)
                    need = True
                    break
            continue
        if not pd.get("orderId"): continue
        try: o = req("GET", "/fapi/v1/order", {"symbol": sym, "orderId": pd["orderId"]}, signed=True, retries=2)
        except BinanceError as e: log("warn", msg="watch 查單失敗", symbol=sym, err=str(e)); continue
        if o["status"] not in ("NEW", "PARTIALLY_FILLED") or float(o.get("executedQty") or 0) > 0:
            types = {po["t"] for po in protect_orders(sym)}
            filled = float(o.get("executedQty") or 0) > 0
            if o["status"] not in ("NEW", "PARTIALLY_FILLED") or not any(t.startswith("TAKE") for t in types) or not any(t.startswith("STOP") for t in types):
                log("watch_trigger", symbol=sym, status=o["status"], executed=o.get("executedQty"), filled=filled); need = True
    if need: cmd_manage(a)

def cmd_flatten(a):
    log("flatten", live=a.live); flatten_all(a.live, "manual_flatten"); print("完成" if a.live else "dry-run 完成")

def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    o = sub.add_parser("open"); o.add_argument("--symbol", required=True); o.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell", "long", "short"])
    o.add_argument("--entry-type", default="market", choices=["market", "limit"]); o.add_argument("--price")
    o.add_argument("--ladder", help="三檔限價，由淺到深，例如 97,96,95。權重 2:3:5。不給就維持單張")
    o.add_argument("--stop", required=True); o.add_argument("--target", required=True); o.add_argument("--setup", required=True)
    o.add_argument("--leverage", type=int); o.add_argument("--portion", type=float, default=1.0, choices=[0.25, 0.5, 1.0]); o.add_argument("--live", action="store_true")
    for n in ("manage", "flatten", "watch"):
        x = sub.add_parser(n); x.add_argument("--live", action="store_true")
    a = ap.parse_args()
    if a.cmd == "open": a.side = {"long": "BUY", "short": "SELL"}.get(a.side.lower(), a.side.upper())
    with Lock():
        try:
            # 有 --ladder 才走三檔。沒有就仍是 cmd_open，單張行為不變。
            if a.cmd == "open" and getattr(a, "ladder", None):
                open_ladder(a)
            else:
                {"status": cmd_status, "open": cmd_open, "manage": cmd_manage, "watch": cmd_watch, "flatten": cmd_flatten}[a.cmd](a)
        except BinanceError as e: log("error", cmd=a.cmd, err=str(e)); raise SystemExit(f"幣安錯誤: {e}")

if __name__ == "__main__": main()
