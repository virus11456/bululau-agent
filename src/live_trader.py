#!/usr/bin/env python3
"""幣安 USDT-M 實盤下單。金鑰只從環境變數或本機 env 檔讀取，絕不寫進日誌或輸出。
預設 dry-run；真實下單需加 --live 參數。資料目錄見 config.py。"""
import os, sys, json, time, hmac, hashlib, argparse, fcntl, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
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

# 跨程序共用。Mac 上是 ~/quantboy-live/.ratelimit_until。壞掉或沒有檔就當作沒有退避。
RATELIMIT_NAME = ".ratelimit_until"
WEIGHT_HIGH = 1800
WEIGHT_1M_MAX = 0
ORPHAN_SCAN_EVERY = 4
ACCOUNT_MODE_NAME = ".account_mode_cache.json"
ACCOUNT_MODE_TTL_S = 3600

def _ratelimit_path():
    return os.path.join(HOME, RATELIMIT_NAME)

def read_ratelimit():
    """回傳 (status, until_epoch)。檔案不在、空的、或格式不對就回 None。"""
    try:
        with open(_ratelimit_path()) as handle:
            text = handle.read().strip()
        status_s, until_s = text.split()
        status = int(status_s)
        until = float(until_s)
    except Exception:
        return None
    if status not in (418, 429) or until != until:
        return None
    return status, until

def write_ratelimit(status, until):
    path = _ratelimit_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write("{0} {1}\n".format(int(status), float(until)))
    os.replace(tmp, path)

def _note_weight(headers):
    """記下本程序看過的最高 X-MBX-USED-WEIGHT-1M。超過 1800 只記 log，不改請求。"""
    global WEIGHT_1M_MAX
    if headers is None or not hasattr(headers, "get"):
        return
    raw = headers.get("X-MBX-USED-WEIGHT-1M")
    if raw is None:
        raw = headers.get("x-mbx-used-weight-1m")
    if raw is None:
        return
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return
    if value > WEIGHT_1M_MAX:
        WEIGHT_1M_MAX = value
    if value > WEIGHT_HIGH:
        log("weight_high", weight=value)

def _honor_ratelimit(path):
    gate = read_ratelimit()
    if not gate:
        return
    status, until = gate
    remain = until - time.time()
    if remain <= 0:
        return
    if status == 418:
        log("backoff_418", path=path, wait=round(remain, 3), weight=WEIGHT_1M_MAX)
        raise BinanceError(418, -1003, "shared 418 backoff")
    time.sleep(remain)

def req(method, path, params=None, signed=False, retries=5):
    params = dict(params or {})
    for attempt in range(retries):
        _honor_ratelimit(path)
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
                _note_weight(getattr(resp, "headers", None))
                return json.loads(resp.read() or "null")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            _note_weight(getattr(e, "headers", None))
            try: j = json.loads(body); code, msg = j.get("code"), j.get("msg")
            except Exception: code, msg = None, body[:200]
            if e.code == 418:
                wait = int(e.headers.get("Retry-After") or 0) or 60
                write_ratelimit(418, time.time() + wait)
                log("backoff_418", path=path, status=418, code=code, wait=wait, weight=WEIGHT_1M_MAX)
                raise BinanceError(418, code, msg)
            if e.code == 429 or code == -1003:
                wait = int(e.headers.get("Retry-After") or 0) or min(60, 2 ** (attempt + 1))
                write_ratelimit(429, time.time() + wait)
                log("backoff", path=path, status=e.code, code=code, wait=wait, weight=WEIGHT_1M_MAX)
                time.sleep(wait); continue
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
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close()
            raise SystemExit("另一個 live_trader 正在執行（檔案鎖），稍後再試")
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

# 跨日只換這幾欄。pending、meta、watch_heartbeat 等不能在 00:00 被清掉。
_DAY_FIELDS = ("day", "day_start_equity", "slice_margin", "halted_new", "killed", "created")

def day_state(acc):
    """每日 00:00 台北重算份額與日初淨值。只重設日欄，其餘原樣留著。"""
    s = load_state(); today = now_tpe().strftime("%Y-%m-%d")
    if s.get("day") != today:
        eq = equity_of(acc)
        fresh = dict(day=today, day_start_equity=eq, slice_margin=round(eq / CFG["slices"], 4),
                     halted_new=False, killed=False, created=now_tpe().isoformat(timespec="seconds"))
        kept = {k: v for k, v in s.items() if k not in _DAY_FIELDS}
        s = dict(kept); s.update(fresh)
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

def cancel_symbol_orphans(sym, live):
    """清掉這個幣上殘留的停損／止盈。平倉後、以及同一幣再開倉前都要做，避免舊單套到新倉。讀不到就不撤。"""
    try:
        algo = open_algo(sym, fail_closed=True) or []
    except BinanceError as e:
        log("warn", msg="條件單讀取失敗，本輪不撤孤兒單", symbol=sym, err=str(e))
        algo = []
    try:
        oo = open_orders(sym) or []
    except BinanceError as e:
        log("warn", msg="掛單讀取失敗，本輪不撤孤兒單", symbol=sym, err=str(e))
        oo = []
    for o in algo:
        if o.get("symbol") not in (None, sym):
            continue
        print("孤兒條件單", sym, o.get("algoId"))
        if live:
            try:
                req("DELETE", "/fapi/v1/algoOrder", {"algoId": o["algoId"]}, signed=True)
                log("cancel_orphan", order=o)
            except BinanceError as e:
                log("error", msg="撤孤兒條件單失敗", symbol=sym, err=str(e))
    for o in oo:
        if o.get("symbol") not in (None, sym):
            continue
        if o.get("type") not in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
            continue
        print("孤兒條件單(舊端點)", sym, o.get("orderId"))
        if live:
            try:
                req("DELETE", "/fapi/v1/order", {"symbol": sym, "orderId": o["orderId"]}, signed=True)
                log("cancel_orphan", order=o)
            except BinanceError as e:
                log("error", msg="撤孤兒條件單失敗", symbol=sym, err=str(e))

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

LADDER_EXPIRE_MS = 2 * 60 * 60 * 1000

def resize_ladder_partial(symbol, close_side, position_qty, planned_qty, live):
    """部分成交：止盈數量改成實際持倉，觸發價不重算。

    停損維持計畫總量，而且一定是 reduceOnly（有數量就不能同時 closePosition）。
    幣安的 reduceOnly 只會減倉，數量大於現倉也不會開出反向倉。
    closePosition=true 不能帶 quantity，所以這裡不用它，才能把停損數量留在計畫總量。
    """
    for po in protect_orders(symbol):
        is_stop = str(po["t"]).startswith("STOP")
        if is_stop and po.get("cp"):
            continue
        target = planned_qty if is_stop else position_qty
        if po["qty"] is not None and Decimal(str(po["qty"])) == target:
            continue
        if (not is_stop) and (po.get("cp") or po["qty"] is None):
            continue
        kind = "STOP_MARKET" if is_stop else "TAKE_PROFIT_MARKET"
        print("  調整 {0} 數量 {1} → {2}".format(kind, po["qty"], fmt(target)))
        place_protect(symbol, close_side, kind, po["trig"], live, qty=fmt(target))
        cancel_protect(symbol, po, live)


def ladder_anchor_ms(pd, orders):
    """掛出時間。優先用 pending 裡的 placed_at，否則用交易所訂單的 time。"""
    raw = pd.get("placed_at")
    if raw:
        try:
            return int(datetime.fromisoformat(str(raw)).timestamp() * 1000)
        except (TypeError, ValueError):
            pass
    times = []
    for order in orders or []:
        if order.get("time"):
            try:
                times.append(int(order["time"]))
            except (TypeError, ValueError):
                continue
    return min(times) if times else None


def expire_unfilled_ladder(sym, pd, rows, held_amt, live, now_ms=None):
    """超過 2 小時仍未成交的進場單撤掉。已成交的倉與它的停損止盈不動。

    任何一張進場單撤不掉，或撤完再查仍有成交，都不撤預掛停損，pending 也留著。
    停損只在每一張的 executedQty 重新查過、確認是 0、而且沒有持倉時才撤。
    回傳 drop / partial / None。
    """
    anchor = ladder_anchor_ms(pd, rows)
    if anchor is None:
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if now_ms - anchor < LADDER_EXPIRE_MS:
        return None
    resting = [o for o in rows if o.get("status") in ("NEW", "PARTIALLY_FILLED")]
    if not resting:
        return None
    amt = held_amt.get(sym, 0.0) if held_amt else 0.0
    cancel_failed = False
    for order in resting:
        log("ladder_expire", symbol=sym, orderId=order.get("orderId"), status=order.get("status"))
        try:
            cancel_entry_order(sym, order.get("orderId"), live)
        except BinanceError as e:
            # -2011 代表單已經不在，不能當成「成交量是 0」。下面再查一次。
            if e.code == -2011:
                log("ladder_expire", symbol=sym, orderId=order.get("orderId"), gone=True)
            else:
                cancel_failed = True
                log("error", msg="逾時撤階梯進場單失敗", symbol=sym, err=str(e))
        except Exception as e:
            cancel_failed = True
            log("error", msg="逾時撤階梯進場單失敗", symbol=sym, err=str(e))
    if cancel_failed:
        return None
    filled = Decimal(0)
    unconfirmed = False
    for order in rows:
        oid = order.get("orderId")
        if not oid:
            unconfirmed = True
            continue
        try:
            fresh = req("GET", "/fapi/v1/order", {"symbol": sym, "orderId": oid}, signed=True)
        except BinanceError as e:
            # 查不到就不能證明成交量是 0，停損留著。
            log("warn", msg="逾時撤單後再查失敗", symbol=sym, orderId=oid, code=e.code)
            unconfirmed = True
            continue
        except Exception as e:
            log("warn", msg="逾時撤單後再查失敗", symbol=sym, orderId=oid, err=str(e))
            unconfirmed = True
            continue
        if not isinstance(fresh, dict):
            unconfirmed = True
            continue
        filled += _filled_qty(fresh)
        if fresh.get("status") in ("NEW", "PARTIALLY_FILLED"):
            unconfirmed = True
    if filled > 0 or amt:
        return "partial"
    if unconfirmed or not live:
        return None
    for po in protect_orders(sym):
        if not str(po["t"]).startswith("STOP"):
            continue
        log("ladder_expire", symbol=sym, cancel="stop", id=po.get("id"))
        try:
            cancel_protect(sym, po, live)
        except Exception as e:
            log("error", msg="逾時撤預掛停損失敗", symbol=sym, err=str(e))
    return "drop"


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
    expired = expire_unfilled_ladder(sym, pd, rows, held_amt, live)
    if expired == "drop":
        return "drop"
    if expired == "partial":
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
        planned = Decimal(str(pd["qty"])) if pd.get("qty") else sum(
            (Decimal(str(o.get("origQty") or "0")) for o in rows), Decimal(0))
        try:
            resize_ladder_partial(sym, close_side, Decimal(str(abs(amt))), planned, live)
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
    if sym not in (load_state().get("pending") or {}):
        cancel_symbol_orphans(sym, live)
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
                                              ladder=True, orders=placed, placed_at=now_tpe().isoformat(timespec="seconds"))
    save_state(st)
    print("三檔限價已掛，停損/止盈已同步預掛（reduceOnly 合計數量）", [x["orderId"] for x in placed])
    return plan
# --- kuai ladder (paste end) ---

def _account_mode_path():
    return os.path.join(HOME, ACCOUNT_MODE_NAME)

def read_account_mode():
    """status 用的 dual／multiAssets 快取。壞掉、過期、或不在就回 None。"""
    try:
        with open(_account_mode_path()) as handle:
            data = json.load(handle)
        if time.time() - float(data.get("ts") or 0) > ACCOUNT_MODE_TTL_S:
            return None
        return data.get("dual"), data.get("multi")
    except Exception:
        return None

def write_account_mode(dual, multi):
    path = _account_mode_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump({"ts": time.time(), "dual": dual, "multi": multi}, handle)
    os.replace(tmp, path)

def fetch_account_mode():
    dual = req("GET", "/fapi/v1/positionSide/dual", signed=True)
    try:
        multi = req("GET", "/fapi/v1/multiAssetsMargin", signed=True)
    except BinanceError as e:
        multi = {"err": str(e)}
    try:
        write_account_mode(dual, multi)
    except Exception:
        pass
    return dual, multi

def status_account_mode():
    """只給 status（以及 manage --with-status）用。open 不走這裡。"""
    cached = read_account_mode()
    if cached is not None:
        return cached
    return fetch_account_mode()

def print_status_body(acc, s, pos, oo, ao, dual, multi):
    eq = equity_of(acc)
    print(f"時間 {now_tpe():%Y-%m-%d %H:%M} 台北")
    print(f"錢包 {float(acc['totalWalletBalance']):.2f}  未實現 {float(acc['totalUnrealizedProfit']):+.2f}  淨值 {eq:.2f}  可用 {float(acc['availableBalance']):.2f} USDT")
    print(f"日初淨值 {s['day_start_equity']:.2f}  當日損益 {eq - s['day_start_equity']:+.2f} ({-daily_loss_pct(acc, s)*100:.2f}% 虧損側)  每份保證金 {s['slice_margin']:.2f}")
    print(f"停開新倉 {s['halted_new']}  全平旗標 {s['killed'] or os.path.exists(HALT_FLAG)}")
    print(f"持倉 {len(pos)}/{CFG['max_positions']}")
    for p in pos: print(f"  {p['symbol']} 數量 {p['positionAmt']} 進場 {p['entryPrice']} 未實現 {float(p['unrealizedProfit']):+.2f} 槓桿 {p['leverage']} 逐倉 {p['isolated']}")
    print(f"一般掛單 {len(oo)}  條件單 {len(ao)}")
    for o in oo: print(f"  {o['symbol']} {o['side']} {o['type']} 價 {o.get('price')} 觸發 {o.get('stopPrice')} 掛於 {tpe_ms(o.get('time'))} id {o.get('orderId')}")
    for o in ao:
        created = o.get("createTime") or o.get("bookTime") or o.get("time") or o.get("updateTime")
        print(f"  [algo] {o.get('symbol')} {o.get('side')} {o.get('orderType') or o.get('type')} 觸發 {o.get('triggerPrice')} 掛於 {tpe_ms(created)} id {o.get('algoId')}")
    print(f"雙向持倉 {dual.get('dualSidePosition')}  多資產模式 {multi.get('multiAssetsMargin', multi)}")

# ---------- 指令 ----------
def cmd_status(a):
    acc = account(); s = day_state(acc)
    pos = positions(acc)
    oo = open_orders()
    try:
        ao = open_algo(fail_closed=True)
    except BinanceError:
        # 先印標記再拋出。main() 會以非 0 結束；即使有人忽略結束碼，這一行也不是「條件單 0」。
        print(ALGO_ORDERS_UNREADABLE)
        raise
    dual, multi = status_account_mode()
    print_status_body(acc, s, pos, oo, ao, dual, multi)

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
    if sym not in (load_state().get("pending") or {}):
        cancel_symbol_orphans(sym, live)
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
    held = list(positions(acc))
    for p in held:
        market_close(p["symbol"], float(p["positionAmt"]), live, reason)
    for o in open_orders():
        if live: req("DELETE", "/fapi/v1/allOpenOrders", {"symbol": o["symbol"]}, signed=True)
        else: print("  [dry-run] 取消", o["symbol"], o["orderId"])
    for o in open_algo():
        if live: req("DELETE", "/fapi/v1/algoOrder", {"algoId": o["algoId"]}, signed=True)
        else: print("  [dry-run] 取消 algo", o.get("symbol"), o.get("algoId"))
    if reason == "manual_flatten":
        _record_manual_flat(live, held)

def _record_manual_flat(live, held):
    """手動全平要記 position_closed（reason=manual）並清掉 meta。dry-run 只記 log，不存檔。"""
    st = load_state()
    meta = st.get("meta") or {}
    changed = False
    for p in held:
        sym = p.get("symbol")
        if sym not in meta:
            continue
        m = meta.get(sym) or {}
        try:
            qty = fmt(abs(Decimal(str(p.get("positionAmt") or "0"))))
        except Exception:
            qty = None
        try:
            amt = float(p.get("positionAmt") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        side = "SELL" if amt > 0 else "BUY"
        log("position_closed", symbol=sym, setup=m.get("setup"), stop=m.get("stop"), target=m.get("target"),
            exit_price=None, exit_side=side, qty=qty, reason="manual", pnl=None)
        print("{0} 已平倉 出場價 null 觸發=手動 已實現 null".format(sym))
        meta.pop(sym, None)
        changed = True
    if not changed:
        return
    st["meta"] = meta
    if live:
        save_state(st)

def _price_gap(vwap, ref):
    try:
        ref_d = Decimal(str(ref))
        if ref_d == 0:
            return None
        return abs(vwap - ref_d) / abs(ref_d)
    except Exception:
        return None


# 1.5%。SHELL 止盈滑價約 1%（0.03908 / 目標 0.03869），0.3% 會被記成「其他」。
EXIT_MATCH_BAND = Decimal("0.015")

def _kind_reason(kind):
    kind = str(kind or "").upper()
    if "TAKE" in kind:
        return "tp"
    if "STOP" in kind:
        return "stop"
    return None

def classify_exit(vwap, meta, order_type, algo_type=None):
    """條件單或原單類型先算（closePosition 的止盈／停損觸發後實單常是 MARKET）。
    類型對不上時，再用 1.5% 對 meta.stop / meta.target。都沒有就是其他。"""
    for kind in (algo_type, order_type):
        reason = _kind_reason(kind)
        if reason:
            return reason
    meta = meta or {}
    ds = _price_gap(vwap, meta.get("stop"))
    dt = _price_gap(vwap, meta.get("target"))
    sl_ok = ds is not None and ds <= EXIT_MATCH_BAND
    tp_ok = dt is not None and dt <= EXIT_MATCH_BAND
    if sl_ok and (not tp_ok or ds <= dt):
        return "stop"
    if tp_ok:
        return "tp"
    return "other"

def format_exit_price(symbol, price):
    """出場價對到該幣 tick。查不到 tick 就維持原來的字串。回傳 (顯示, 原始或 None)。"""
    raw = Decimal(str(price))
    shown = raw
    try:
        tick = sym_info(symbol)["tick"]
        if tick and tick > 0:
            shown = raw.quantize(tick)
    except Exception:
        shown = raw
    text = fmt(shown)
    raw_text = fmt(raw)
    return text, (raw_text if raw_text != text else None)

def format_pnl(pnl):
    """已實現損益固定 4 位小數。回傳 (顯示字串, 原始值或 None)。"""
    original = Decimal(str(pnl))
    shown = original.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    text = format(shown, "f")
    return text, (pnl if shown != original else None)

def _algo_type_for_order(symbol, order_id, start_ms):
    """唯讀。用條件單歷史把觸發後的 orderId 對回 TAKE_PROFIT／STOP。對不到就 None。"""
    try:
        rows = req("GET", "/fapi/v1/allAlgoOrders",
                   {"symbol": symbol, "startTime": start_ms, "limit": 100}, signed=True)
    except Exception:
        return None
    if isinstance(rows, dict):
        rows = rows.get("orders") or rows.get("data") or []
    if not isinstance(rows, list):
        return None
    want = str(order_id)
    for row in rows:
        if not isinstance(row, dict):
            continue
        ids = (row.get("actualOrderId"), row.get("actualId"))
        if not any(i is not None and str(i) == want for i in ids):
            continue
        kind = row.get("orderType") or row.get("type") or ""
        # closePosition 的止盈／停損一樣算止盈／停損，不因為觸發單是 MARKET 就變成其他。
        if _kind_reason(kind):
            return kind
    return None


def lookup_close(symbol, meta):
    """近 24 小時 userTrades 裡最後一串減倉成交的加權均價。查不到就 exit_price=None、reason=unknown。"""
    unknown = {"exit_price": None, "exit_side": None, "qty": None, "reason": "unknown"}
    start_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
    try:
        trades = req("GET", "/fapi/v1/userTrades",
                     {"symbol": symbol, "startTime": start_ms, "limit": 1000}, signed=True)
    except Exception:
        return unknown
    if not isinstance(trades, list) or not trades:
        return unknown
    def _rp(t):
        try:
            return float(t.get("realizedPnl") or 0)
        except (TypeError, ValueError):
            return 0.0
    reducing = [t for t in trades if _rp(t) != 0]
    use = reducing or list(trades)
    try:
        use.sort(key=lambda t: int(t.get("time") or 0))
    except (TypeError, ValueError):
        return unknown
    side = str(use[-1].get("side") or "")
    trail = []
    for t in reversed(use):
        if str(t.get("side") or "") != side:
            break
        trail.append(t)
    trail.reverse()
    try:
        qty = sum((Decimal(str(t.get("qty") or "0")) for t in trail), Decimal(0))
        notion = sum((Decimal(str(t.get("price") or "0")) * Decimal(str(t.get("qty") or "0")) for t in trail), Decimal(0))
    except Exception:
        return unknown
    if qty <= 0:
        return unknown
    vwap = notion / qty
    order_type = None
    algo_type = None
    oid = trail[-1].get("orderId")
    if oid:
        try:
            order = req("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": oid}, signed=True)
            if isinstance(order, dict):
                order_type = order.get("origType") or order.get("type") or order.get("orderType")
        except Exception:
            order_type = None
        if not _kind_reason(order_type):
            algo_type = _algo_type_for_order(symbol, oid, start_ms)
    shown, raw_px = format_exit_price(symbol, vwap)
    out = {"exit_price": shown, "exit_side": side or None, "qty": fmt(qty),
           "reason": classify_exit(vwap, meta, order_type, algo_type=algo_type)}
    if raw_px is not None:
        out["exit_price_raw"] = raw_px
    return out


def _exit_label(reason):
    return {"stop": "停損", "tp": "止盈", "other": "其他", "unknown": "未知", "manual": "手動"}.get(reason, reason)

def meta_from_pending(old, pd):
    """階梯或限價轉成持倉時，已標記的減倉、成本停損、no_tp 不能被蓋掉。"""
    fresh = {k: pd[k] for k in ("stop", "target", "setup") if k in pd}
    old = old or {}
    if old.get("reduced"):
        fresh["reduced"] = True
        if old.get("stop") not in (None, ""):
            fresh["stop"] = old["stop"]
        if old.get("no_tp"):
            fresh["no_tp"] = True
    return fresh


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
    # 這一輪才從 pending 寫進 meta 的先留著。帳戶快照可能還沒看到成交，不能馬上當已平倉。
    meta_at_start = set(st.get("meta") or {})
    for sym, pd in list(st.get("pending", {}).items()):
        if pd.get("ladder"):
            done = manage_ladder_pending(sym, pd, held_amt, live)
            if done:
                if done == "meta":
                    old = (st.get("meta") or {}).get(sym) or {}
                    st.setdefault("meta", {})[sym] = meta_from_pending(old, pd)
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
            old = (st.get("meta") or {}).get(sym) or {}
            st.setdefault("meta", {})[sym] = meta_from_pending(old, pd)
            log("limit_done", symbol=sym, status=stt, executed=o.get("executedQty"), pos=amt)
            print(f"{sym} 限價單 {stt}，持倉 {amt}，轉為一般持倉管理")
        else:
            log("limit_dead", symbol=sym, status=stt); print(f"{sym} 限價單 {stt} 且無持倉，撤掉孤兒保護單")
            for po in protect_orders(sym): cancel_protect(sym, po, live)
        if live: save_state(st)
    scan_n = int(st.get("orphan_scan_n") or 0) + 1
    st["orphan_scan_n"] = scan_n
    known = set(held_amt) | set(st.get("pending") or {}) | set(st.get("meta") or {})
    oo_known, algo_known = [], []
    order_unread = set()
    for sym in sorted(known):
        try:
            oo_known.extend(open_orders(sym) or [])
        except BinanceError as e:
            log("warn", msg="openOrders 失敗，本輪跳過", symbol=sym, err=str(e))
            order_unread.add(sym)
        try:
            algo_known.extend(open_algo(sym, fail_closed=True) or [])
        except BinanceError as e:
            log("warn", msg="條件單讀取失敗，本輪跳過", symbol=sym, err=str(e))
            order_unread.add(sym)
    # 第 1、5、9… 輪做全量孤兒掃描，和以前每次都掃的結果在這些輪相同。
    # 平倉那一輪也全量掃，避免剛平掉的幣上的保護單要等下一輪。
    full_orphan = (scan_n % ORPHAN_SCAN_EVERY == 1)
    meta = {**st.get("pending", {}), **st.get("meta", {})}
    algo = algo_known
    oo = oo_known
    for p in positions(acc):
        sym = p["symbol"]; amt = float(p["positionAmt"]); close_side = "SELL" if amt > 0 else "BUY"
        if sym in order_unread:
            log("warn", msg="條件單讀取失敗，本輪不判斷停損、不平倉", symbol=sym)
            print("{0} {1}".format(sym, ALGO_ORDERS_UNREADABLE))
            continue
        types = {(o.get("orderType") or o.get("type")) for o in algo if o.get("symbol") == sym}
        types |= {o["type"] for o in oo if o["symbol"] == sym}
        has_sl = "STOP_MARKET" in types or "STOP" in types
        has_tp = "TAKE_PROFIT_MARKET" in types or "TAKE_PROFIT" in types
        print(f"{sym} {amt} 停損 {'有' if has_sl else '缺'} 止盈 {'有' if has_tp else '缺'}")
        m = meta.get(sym)
        no_tp = bool(m and m.get("no_tp"))
        # 指定數量的保護單：若限價單已終結，數量需等於實際持倉。no_tp 的止盈不調整，下面直接撤。
        if sym not in st.get("pending", {}):
            for po in protect_orders(sym, algo, oo):
                if no_tp and str(po["t"]).startswith("TAKE"):
                    continue
                if po["cp"] or po["qty"] is None or Decimal(str(po["qty"])) == Decimal(str(abs(amt))): continue
                kind = "STOP_MARKET" if po["t"].startswith("STOP") else "TAKE_PROFIT_MARKET"
                print(f"  調整 {kind} 數量 {po['qty']} → {abs(amt)}")
                try:
                    place_protect(sym, close_side, kind, po["trig"], live, qty=fmt(Decimal(str(abs(amt)))))
                    cancel_protect(sym, po, live)   # 先掛新再撤舊，避免空窗
                except Exception as e:
                    log("error", msg="調整保護單數量失敗", symbol=sym, err=str(e))
        if no_tp:
            for po in protect_orders(sym, algo, oo):
                if not str(po["t"]).startswith("TAKE"):
                    continue
                print("no_tp_cancel {0}".format(sym))
                log("no_tp_cancel", symbol=sym, id=po.get("id"))
                try:
                    cancel_protect(sym, po, live)
                except Exception as e:
                    log("error", msg="撤止盈失敗", symbol=sym, err=str(e))
            has_tp = False
        if not has_sl:
            if not m: log("error", msg="缺停損且無紀錄停損價，市價平倉", symbol=sym); market_close(sym, amt, live, "no_stop_no_meta"); continue
            # reduced 之後 meta.stop 已是成本價，缺停損就補掛在這個價，不是原來的結構停損。
            try: place_protect(sym, close_side, "STOP_MARKET", m["stop"], live)
            except Exception as e:
                log("error", msg="補掛停損失敗，市價平倉", symbol=sym, err=str(e)); market_close(sym, amt, live, "restop_failed"); continue
        if not has_tp and m and not no_tp:
            try: place_protect(sym, close_side, "TAKE_PROFIT_MARKET", m["target"], live)
            except Exception as e: log("error", msg="補掛止盈失敗", symbol=sym, err=str(e))
    # 已無持倉的幣種：先把 meta 裡已平的記下來，再清孤兒保護單（清理本身不改）。
    held = {p["symbol"] for p in positions(acc)}
    closed_note = False
    for sym in list(st.get("meta") or {}):
        if sym not in meta_at_start or sym in held or sym in st.get("pending", {}):
            continue
        m = st["meta"].get(sym) or {}
        pnl = None
        try:
            start_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
            rows = req("GET", "/fapi/v1/income",
                       {"symbol": sym, "incomeType": "REALIZED_PNL", "startTime": start_ms, "limit": 1000},
                       signed=True)
            if isinstance(rows, list):
                pnl = sum(float(r.get("income") or 0) for r in rows)
        except Exception:
            pnl = None
        closed = lookup_close(sym, m)
        pnl_text, pnl_raw = (None, None) if pnl is None else format_pnl(pnl)
        fields = dict(symbol=sym, setup=m.get("setup"), stop=m.get("stop"), target=m.get("target"),
                      exit_price=closed["exit_price"], exit_side=closed["exit_side"], qty=closed["qty"],
                      reason=closed["reason"], pnl=pnl_text)
        if closed.get("exit_price_raw") is not None:
            fields["exit_price_raw"] = closed["exit_price_raw"]
        if pnl_raw is not None:
            fields["pnl_raw"] = pnl_raw
        log("position_closed", **fields)
        px = "null" if closed["exit_price"] is None else closed["exit_price"]
        py = "null" if pnl_text is None else pnl_text
        print("{0} 已平倉 出場價 {1} 觸發={2} 已實現 {3}".format(sym, px, _exit_label(closed["reason"]), py))
        st["meta"].pop(sym, None)
        closed_note = True
        cancel_symbol_orphans(sym, live)
    if closed_note:
        full_orphan = True
    if full_orphan:
        try:
            oo = open_orders() or []
        except BinanceError as e:
            log("warn", msg="openOrders 全量失敗", err=str(e)); oo = oo_known
        try:
            algo = open_algo() or []
        except BinanceError as e:
            log("warn", msg="openAlgoOrders 全量失敗", err=str(e)); algo = algo_known
    if live:
        save_state(st)
    for o in algo:
        if o.get("symbol") not in held and o.get("symbol") not in st.get("pending", {}):
            print("孤兒條件單", o.get("symbol"), o.get("algoId"))
            if live: req("DELETE", "/fapi/v1/algoOrder", {"algoId": o["algoId"]}, signed=True); log("cancel_orphan", order=o)
    for o in oo:
        if o["symbol"] not in held and o["symbol"] not in st.get("pending", {}) and o["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            print("孤兒條件單(舊端點)", o["symbol"], o["orderId"])
            if live: req("DELETE", "/fapi/v1/order", {"symbol": o["symbol"], "orderId": o["orderId"]}, signed=True); log("cancel_orphan", order=o)
    print(f"當日虧損 {dl*100:.2f}%  持倉 {len(held)}")
    if getattr(a, "with_status", False):
        try:
            status_oo = open_orders() or []
            status_ao = open_algo(fail_closed=True)
        except BinanceError:
            print(ALGO_ORDERS_UNREADABLE)
            raise
        dual, multi = status_account_mode()
        print_status_body(acc, s, positions(acc), status_oo, status_ao, dual, multi)

def _user_stream_fresh():
    """Mac 還沒放 user_stream.py 時，60 秒 watch 照舊輪詢，不要因 ImportError 停擺。"""
    try:
        from user_stream import heartbeat_fresh
    except ImportError:
        return False
    try:
        return bool(heartbeat_fresh())
    except Exception:
        return False


def cmd_watch(a):
    """輕量成交偵測（launchd 每 60 秒）：無待成交限價單 → 零 API 呼叫，每小時最多印一行；
    有成交（含部分成交）且缺止盈/停損 → 立即跑一次 manage 補掛與調整數量。
    使用者資料流的心跳還新的時候，這支不打 API，當備援。沒有那個檔就照舊輪詢。"""
    if _user_stream_fresh():
        return
    st = load_state(); pend = st.get("pending", {})
    if not pend:
        now = now_tpe()
        last = st.get("watch_heartbeat")
        due = True
        if last:
            try:
                prev = datetime.fromisoformat(last)
                if prev.tzinfo is None:
                    prev = prev.replace(tzinfo=TPE)
                due = (now - prev).total_seconds() >= 3600
            except (TypeError, ValueError):
                due = True
        if due:
            line = "[watch] 無待成交限價單 {0}".format(now.strftime("%H:%M"))
            if WEIGHT_1M_MAX:
                line += " weight={0}".format(WEIGHT_1M_MAX)
            print(line)
            st["watch_heartbeat"] = now.isoformat(timespec="seconds")
            save_state(st)
        return
    need = False
    for sym, pd in pend.items():
        if pd.get("ladder"):
            types = {po["t"] for po in protect_orders(sym)}
            has_sl = any(str(t).startswith("STOP") for t in types)
            has_tp = any(str(t).startswith("TAKE") for t in types)
            total_exec = 0.0
            done_n = 0
            fetched = []
            for item in pd.get("orders") or []:
                if not item.get("orderId"):
                    continue
                try:
                    o = req("GET", "/fapi/v1/order", {"symbol": sym, "orderId": item["orderId"]}, signed=True, retries=2)
                except BinanceError as e:
                    log("warn", msg="watch 查單失敗", symbol=sym, err=str(e)); continue
                fetched.append(o)
                total_exec += float(o.get("executedQty") or 0)
                if o["status"] not in ("NEW", "PARTIALLY_FILLED"):
                    done_n += 1
            prev = pd.get("last_exec")
            try:
                prev_f = float(prev) if prev is not None else None
            except (TypeError, ValueError):
                prev_f = None
            changed = prev_f is None or abs(total_exec - prev_f) > 1e-9
            try:
                prev_done = int(pd["last_done"]) if pd.get("last_done") is not None else None
            except (TypeError, ValueError):
                prev_done = None
            done_increased = (prev_done is None and done_n > 0) or (prev_done is not None and done_n > prev_done)
            pd["last_exec"] = total_exec
            pd["last_done"] = done_n
            save_state(st)
            # 已結束的檔數變多才再叫。超過 2 小時仍有未成交檔，也要叫 manage 去撤。
            expired = False
            anchor = ladder_anchor_ms(pd, fetched)
            if anchor is not None and int(time.time() * 1000) - anchor >= LADDER_EXPIRE_MS:
                expired = any(o.get("status") in ("NEW", "PARTIALLY_FILLED") for o in fetched)
            if done_increased or (total_exec > 0 and ((not has_sl or not has_tp) or changed)) or (not has_sl) or expired:
                log("watch_trigger", symbol=sym, executed=total_exec, ladder=True, has_sl=has_sl, has_tp=has_tp)
                need = True
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

def cmd_mark_reduced(a):
    """手動標記已減倉。不掛單、不撤單、不自動平一半。布鹿流同時標記不再補掛止盈。"""
    live = a.live
    sym = a.symbol.upper()
    st = load_state()
    meta = st.setdefault("meta", {})
    cur = dict(meta.get(sym) or {})
    if cur.get("reduced"):
        raise SystemExit("已減過倉，不重複標記")
    rows = req("GET", "/fapi/v2/positionRisk", {"symbol": sym}, signed=True)
    if isinstance(rows, dict):
        rows = [rows]
    pos = None
    for row in rows or []:
        if not isinstance(row, dict) or row.get("symbol") not in (None, sym):
            continue
        try:
            amt = float(row.get("positionAmt") or 0)
        except (TypeError, ValueError):
            continue
        if amt != 0:
            pos = row
            break
    if not pos or pos.get("entryPrice") in (None, ""):
        raise SystemExit("沒有持倉，不能標記減倉")
    info = sym_info(sym)
    entry = fmt(rnd(Decimal(str(pos["entryPrice"])), info["tick"]))
    new = dict(cur)
    new["reduced"] = True
    new["stop"] = entry
    if new.get("setup") == "布鹿流":
        new["no_tp"] = True
    print("mark-reduced {0} stop={1} reduced=true no_tp={2}".format(sym, entry, bool(new.get("no_tp"))))
    log("mark_reduced", symbol=sym, stop=entry, setup=new.get("setup"), no_tp=bool(new.get("no_tp")), live=live)
    if not live:
        return
    meta[sym] = new
    st["meta"] = meta
    save_state(st)


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
        if n == "manage":
            x.add_argument("--with-status", action="store_true")
    mr = sub.add_parser("mark-reduced"); mr.add_argument("--symbol", required=True); mr.add_argument("--live", action="store_true")
    a = ap.parse_args()
    if a.cmd == "open": a.side = {"long": "BUY", "short": "SELL"}.get(a.side.lower(), a.side.upper())
    with Lock():
        try:
            # 有 --ladder 才走三檔。沒有就仍是 cmd_open，單張行為不變。
            if a.cmd == "open" and getattr(a, "ladder", None):
                open_ladder(a)
            else:
                {"status": cmd_status, "open": cmd_open, "manage": cmd_manage, "watch": cmd_watch,
                 "flatten": cmd_flatten, "mark-reduced": cmd_mark_reduced}[a.cmd](a)
        except BinanceError as e: log("error", cmd=a.cmd, err=str(e)); raise SystemExit(f"幣安錯誤: {e}")

if __name__ == "__main__": main()
