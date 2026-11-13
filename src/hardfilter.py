#!/usr/bin/env python3
"""硬條件篩選：接在 prefilter 輸出之後，由 gate.py 呼叫。只把「程式算得出來、而且確定不符合」的候選拿掉。

原則（總經理boy 11/10 轉達使用者 23:44 原話「你依照原本的開單的條件跟頻率來撈單來做設定」）：
  - 只照現行規則（~/quantboy-gate/full_prompt.md 與 docs/strategy.md 11-06 定稿）判斷，不放寬也不加嚴。
  - 規則原文沒給數字、需要看型態、或資料不夠時，一律留下候選交給 AI（fail-open to AI）。
    例外：布鹿流日 K。2026-11-13 13:58 使用者核定 ma7_25 為硬規則。已收日 K 少於 33 根就拒（理由含「日K資料不足」），不留給 AI。讀取失敗仍保留。
  - 429／418 限流：同一輪最多再試 2 次（有上限的退避）。等待前若 60 秒逾時的剩餘時間不夠再打一發，就不等。仍失敗：結束碼 2，gate 會 WAKE 並保留全部候選。
  - 其他整體讀取失敗：不重試，結束碼 2，同樣 WAKE。
  - 不下單、不讀金鑰、不做簽章請求，只讀幣安公開 K 線。

輸入（stdin）：prefilter --quiet 的那一行 JSON（至少要有 candidates）。
輸出（stdout）：一行 JSON {"ok", "kept", "blocked", "notes"}。
結束碼：0 = 篩完（kept 可能是空的）；2 = 讀取失敗或限流，呼叫端必須當成全部保留並 WAKE。

用法：
  python3 hardfilter.py < prefilter.json
  python3 hardfilter.py --klines-fixture replay.json --asof 2026-11-10T20:30:30+08:00 < candidates.json   # 回放，不連網
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))
BASE = "https://fapi.binance.com"

# ── 規則參數：每個數字都對應原文，見 docs/hardfilter.md 對照表 ──────────────────────────
# full_prompt.md：「最低RR1.5」；strategy.md：「風報比至少 1.5。取較寬停損之後若不到 1.5，不進場」
RR_MIN = 1.5
# full_prompt.md：「停損取前低下1–2%容錯與BTC/ETH3%（小幣5%）較寬者」
BULU_MIN_STOP_MAJOR = 0.03
BULU_MIN_STOP_SMALL = 0.05
MAJORS = {"BTCUSDT", "ETHUSDT"}
# full_prompt.md：「布鹿流…爆量至少3倍」；strategy.md：「爆量至少是前量的 3 倍…【布神原文】是前 10–20 根平均量的 3–10 倍。3 倍是最低」
BULU_VOL_X = 3.0
BULU_VOL_LOOKBACKS = (10, 20)  # 原文「前 10–20 根」：兩種都算，任一種達 3 倍就算有（取寬，不加嚴）
# user_notes2.txt：「長黑 = 瞬間單根較大跌勢，大幣（BTC、ETH）約 2–3%、小幣 4–5% 以上。」
# 「爆量 + 長黑，缺一不可。」「爆量 = 平均量 N 倍（機器人用前 20 根平均判定，成交量是相對值不是絕對值）。」
# four_strategies.md：「長黑為瞬間單根較大跌勢（大幣平均2%、小幣4-5%以上）」「爆量＋長黑，缺一不可」「爆量判定為平均量Ｎ倍」
# 2026-11-11 使用者決定取範圍下緣：BTCUSDT／ETHUSDT 2%，其餘小幣 4%。不再要求連續 2–3 根。
KUAI_LONG_BLACK_BIG = 2.0    # %：BTCUSDT、ETHUSDT。PDF 寫平均 2%，筆記寫約 2–3%，取 2%
KUAI_LONG_BLACK_SMALL = 4.0  # %：其餘。原文 4–5% 以上，取 4%（較寬，不加嚴）
# 爆量倍數與回看維持現狀，不改成別的 N、也不改成只看 20 根：前 10 或前 20 根均量，任一種達 3 倍即可
KUAI_VOL_X = 3.0
KUAI_VOL_LOOKBACKS = (10, 20)
# strategy.md 11-03 補充：「黑名單維持不做：程式裡的 INITUSDT、EWZUSDT，以及 lessons 點名的 CAP、TAG、APR、BREV、XAN、AIN、DOOD、INIT」
BLACKLIST_BASES = {"INIT", "EWZ", "CAP", "TAG", "APR", "BREV", "XAN", "AIN", "DOOD"}
# 布鹿流 RR 的目標價。prior_high 模式：目標 = max(前高, 進場 + 1.5R)。
# 003：後波高點會突破前波，所以前高不是天花板；前高不到 1.5R 時目標放在 1.5R，可以高於前高。
# 進場價已經不低於前高則是追價，擋。plan_target 仍用 prefilter 的 target 欄，不自動墊高。
BULU_RR_TARGET = "prior_high"
# 3b 開關。None＝不封頂，與 d06b651 相同。2.0 才恢復舊的 min(rr, 2.0)。
# 封頂後目標仍至少 1.5R，所以這支不會因為打開上限而多擋或少擋。
BULU_RR_CAP = None
_RR_CAP_DEFAULT = object()
# 布鹿流日 K 硬規則（2026-11-13 13:58 核定的 ma7_25）。算法與回放腳本的 ma_at／swing_lows／daily_ok 相同。
# 已收日 K 少於 25+3+5 根就拒。抓 K 線時 limit 至少 60，未收的那根由 normalize（close_time < 決策時間）丟掉。
DAILY_FAST = 7
DAILY_SLOW = 25
DAILY_MA_BACK = 3
DAILY_MIN_BARS = DAILY_SLOW + DAILY_MA_BACK + 5  # 33
DAILY_SWING_N = 2
DAILY_KLINE_LIMIT = 60

REQUEST_TIMEOUT_S = 10
# 公開 K 線 429／418 才重試。gate 給這支的逾時是 60 秒（gate.HARDFILTER_TIMEOUT_S），每次請求逾時 10 秒。
# 整輪（不是每個幣）最多再試 2 次。沒有正數 Retry-After 就等 2 秒、再 6 秒。
# 單次超過 8 秒，或這一輪累計睡眠會超過 15 秒：不等，直接 RateLimited（結束碼 2）。
# 另外在每次等待前看整輪還剩多少：必須蓋過「這次等待 + 一次請求逾時 + 餘裕」，否則不等。
# 沒有固定的「最壞 45 秒」；能不能等，看從這支開始起算的剩餘時間。
HARDFILTER_MAX_RETRIES = 2
HARDFILTER_BACKOFF_S = (2.0, 6.0)
HARDFILTER_WAIT_CAP_S = 8.0
HARDFILTER_TOTAL_WAIT_CAP_S = 15.0
HARDFILTER_RUN_BUDGET_S = 60.0
HARDFILTER_TIME_MARGIN_S = 5.0


class RateLimited(Exception):
    pass


class KlineRetryBudget:
    """一整輪 hardfilter 共用的 429／418 等待預算。sleep 與 monotonic 可注入，測試不真的睡。

    t0 在建立時記下，也就是 hardfilter 這輪的開頭。等待前用它看離 60 秒還剩多少。
    """

    def __init__(self, sleep, monotonic=None):
        self.sleep = sleep
        self.monotonic = time.monotonic if monotonic is None else monotonic
        self.t0 = self.monotonic()
        self.retries = 0
        self.wait_s = 0.0
        self.waits_s = []
        self.statuses = []
        self.stopped = None

    def remaining(self):
        return HARDFILTER_RUN_BUDGET_S - (self.monotonic() - self.t0)

    def plan(self, failure_count, retry_after):
        """這次請求的第 failure_count 次 429／418（從 1 起）要睡幾秒。不該等就回 None。"""
        if failure_count > HARDFILTER_MAX_RETRIES or self.retries >= HARDFILTER_MAX_RETRIES:
            self.stopped = "retries_exhausted"
            return None
        if failure_count < 1:
            self.stopped = "retries_exhausted"
            return None
        if retry_after is None:
            wait = float(HARDFILTER_BACKOFF_S[failure_count - 1])
        else:
            wait = float(retry_after)
        if wait > HARDFILTER_WAIT_CAP_S or self.wait_s + wait > HARDFILTER_TOTAL_WAIT_CAP_S:
            self.stopped = "wait_cap"
            return None
        # 睡完還要留得下一次請求逾時與餘裕，否則這次等待會把 gate 的 60 秒吃穿。
        reserve = REQUEST_TIMEOUT_S + HARDFILTER_TIME_MARGIN_S
        if self.remaining() < wait + reserve:
            self.stopped = "budget_exhausted"
            return None
        self.stopped = None
        return wait

    def spend(self, wait):
        self.sleep(wait)
        self.retries += 1
        self.wait_s = round(self.wait_s + wait, 3)
        self.waits_s.append(round(float(wait), 3))


def _retry_after_header(exc):
    """HTTP Retry-After，單位秒。沒有、不是數字、或 ≤ 0 就回 None（改走指數退避）。"""
    headers = getattr(exc, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _retry_fields(budget):
    return {
        "rate_limit_retries": budget.retries,
        "rate_limit_wait_s": budget.wait_s,
        "rate_limit_waits_s": list(budget.waits_s),
        "rate_limit_stopped": budget.stopped,
        "rate_limit_statuses": list(budget.statuses),
    }


class DataError(Exception):
    pass


def fetch_klines(symbol, interval, limit, end_ms=None, sleep=None, budget=None, monotonic=None):
    """幣安公開 K 線，只回已收 K。418/429 在預算內退避重試；超過就丟 RateLimited。其他錯誤丟 DataError，不重試。"""
    if budget is None:
        budget = KlineRetryBudget(time.sleep if sleep is None else sleep, monotonic)
    url = "{0}/fapi/v1/klines?symbol={1}&interval={2}&limit={3}".format(BASE, symbol, interval, limit)
    if end_ms:
        url += "&endTime={0}".format(end_ms)
    seen = 0
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gate-hardfilter"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                rows = json.loads(resp.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (418, 429):
                raise DataError("HTTP {0} on klines {1}".format(exc.code, symbol))
            budget.statuses.append(int(exc.code))
            seen += 1
            wait = budget.plan(seen, _retry_after_header(exc))
            if wait is None:
                raise RateLimited("{0} on klines {1}".format(exc.code, symbol))
            budget.spend(wait)
        except Exception as exc:  # 逾時、DNS、JSON 壞掉。不重試。
            raise DataError("{0}: {1}".format(type(exc).__name__, exc))
    now = end_ms or int(time.time() * 1000)
    if not isinstance(rows, list):
        raise DataError("klines 回傳不是清單")
    try:
        # 和 prefilter/common.py 一樣用 quote volume（第 8 欄）當量
        compact = [[r[0], r[1], r[2], r[3], r[4], r[7], r[6]] for r in rows]
    except (TypeError, IndexError, KeyError) as exc:
        raise DataError("bad kline row: {0}".format(exc))
    return normalize(compact, now)


def normalize(rows, now_ms):
    """[t, o, h, l, c, quote_vol, close_time] → dict；只留 close_time < now 的已收 K。格式不對丟 DataError。"""
    out = []
    try:
        for r in rows:
            out.append({"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                        "v": float(r[5]), "ct": int(r[6])})
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        raise DataError("bad kline row: {0}".format(exc))
    return [k for k in out if k["ct"] < now_ms]


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def black_pct(bar):
    """長黑幅度，正值＝收黑。和 prefilter 的 pct(o, c) 同一算法。"""
    return (bar["o"] / bar["c"] - 1) * 100 if bar["c"] else 0.0


# ── 規則 ───────────────────────────────────────────────────────────────────────
# 每條規則回傳 ("block", 理由) 或 ("pass", None) 或 ("uncertain", 理由)。uncertain 一律留給 AI。

def rule_blacklist(cand):
    sym = cand.get("symbol")
    if not isinstance(sym, str) or not sym.endswith("USDT"):
        return "uncertain", "symbol 讀不到"
    base = sym[:-4]
    if base in BLACKLIST_BASES:
        return "block", "黑名單 {0}（strategy.md 11-03 補充：黑名單維持不做）".format(base)
    return "pass", None


def _bar_start_ms(label, ref_ms):
    """prefilter 的 bar 欄是 'MM-DD HH:MM'（台北）。年份取參考時間那年；跨年取最接近的。"""
    try:
        ref = datetime.fromtimestamp(ref_ms / 1000, TPE)
        best = None
        for year in (ref.year - 1, ref.year, ref.year + 1):
            dt = datetime.strptime("{0}-{1}".format(year, label), "%Y-%m-%d %H:%M").replace(tzinfo=TPE)
            if best is None or abs((dt - ref).total_seconds()) < abs((best - ref).total_seconds()):
                best = dt
        return int(best.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def rule_kuai_long_black(cand, k15, ref_ms):
    """瘸師弟：訊號 K 本身是瞬間單根長黑，而且爆量達標。兩者缺一不可。不看相鄰根數。

    user_notes2.txt：「長黑 = 瞬間單根較大跌勢，大幣（BTC、ETH）約 2–3%、小幣 4–5% 以上。」
    「爆量 + 長黑，缺一不可。」
    four_strategies.md：「長黑為瞬間單根較大跌勢（大幣平均2%、小幣4-5%以上）」「爆量＋長黑，缺一不可」。
    門檻看幣種，不看 prefilter 的 big：BTCUSDT／ETHUSDT 用 2%，其餘用 4%。
    爆量倍數不改：訊號量 ≥ 前 10 根或前 20 根均量的 3 倍，任一種達標即可。
    """
    if not k15:
        return "uncertain", "15m K 線不足"
    sym = cand.get("symbol")
    if not isinstance(sym, str) or not sym.endswith("USDT"):
        return "uncertain", "symbol 讀不到"
    start = _bar_start_ms(cand.get("bar"), ref_ms)
    if start is None:
        return "uncertain", "訊號 K 時間讀不到"
    idx = next((i for i, b in enumerate(k15) if b["t"] == start), None)
    if idx is None:
        return "uncertain", "訊號 K 不在已收 K 線裡"
    th = KUAI_LONG_BLACK_BIG if sym in MAJORS else KUAI_LONG_BLACK_SMALL
    pct = black_pct(k15[idx])
    if pct + 1e-6 < th:
        return "block", "不是單根長黑（收黑 {0:.2f}% < {1:g}%）".format(pct, th)
    ratios = []
    for n in KUAI_VOL_LOOKBACKS:
        if idx < n:
            continue
        avg = sum(x["v"] for x in k15[idx - n:idx]) / n
        if avg > 0:
            ratios.append((n, k15[idx]["v"] / avg))
    if not ratios:
        return "uncertain", "爆量算不出來（前 10／20 根不足或均量為 0）"
    if max(r for _n, r in ratios) + 1e-9 >= KUAI_VOL_X:
        return "pass", None
    detail = "、".join("前{0}根 {1:.2f} 倍".format(n, r) for n, r in ratios)
    return "block", "長黑成立但爆量不足（{0}；要至少 3 倍）".format(detail)


def rule_bulu_volume(cand, k1h):
    """布鹿流：前高（第一波）之前要有一根 1H 量 ≥ 前 10 或前 20 根均量的 3 倍。

    原文：full_prompt.md「布鹿流…爆量至少3倍」；strategy.md「爆量至少是前量的 3 倍。…定稿的進場門檻是 3 倍」。
    找法和 prefilter 一樣取最近 48 根 1H 的最高點當前高；只要從資料起點到前高之間任何一根達標就算有（取寬）。
    """
    if not k1h or len(k1h) < 48 + max(BULU_VOL_LOOKBACKS):
        return "uncertain", "1H K 線不足"
    w0 = len(k1h) - 48
    hi = max(range(w0, len(k1h)), key=lambda i: k1h[i]["h"])
    best = 0.0
    for j in range(min(BULU_VOL_LOOKBACKS), hi + 1):
        for n in BULU_VOL_LOOKBACKS:
            if j - n < 0:
                continue
            avg = sum(x["v"] for x in k1h[j - n:j]) / n
            if avg > 0:
                best = max(best, k1h[j]["v"] / avg)
    if best >= BULU_VOL_X:
        return "pass", None
    return "block", "前高之前最大量只有前 10／20 根均量的 {0:.2f} 倍（<3 倍）".format(best)


def cap_bulu_target(entry, risk, target, rr_cap=_RR_CAP_DEFAULT):
    """rr_cap 省略時用 BULU_RR_CAP。None 不封頂。數字是 R 倍數，例如 2.0。"""
    cap = BULU_RR_CAP if rr_cap is _RR_CAP_DEFAULT else rr_cap
    if cap is None:
        return target, None
    ceiling = entry + float(cap) * risk
    if target > ceiling + 1e-12:
        return ceiling, cap
    return target, None


def bulu_r3_prices(symbol, entry, stop, prior, rr_cap=_RR_CAP_DEFAULT):
    """停損取結構與 3%/5% 較寬者。目標 = max(前高, 進場+1.5R)，再依 rr_cap 封頂。

    前高不高於進場、或風險不算得出來，回 None。
    """
    floor = BULU_MIN_STOP_MAJOR if symbol in MAJORS else BULU_MIN_STOP_SMALL
    wide = min(stop, entry * (1 - floor))
    risk = entry - wide
    if risk <= 0 or prior is None or prior <= entry:
        return None
    target, _cap = cap_bulu_target(entry, risk, max(prior, entry + RR_MIN * risk), rr_cap)
    return wide, target


def ma_at(closes, n, back=0):
    """簡單平均。back=0 是最後 n 根；back=3 是往前移 3 根、不含最後 3 根的那一段。"""
    end = len(closes) - back
    if end < n:
        return None
    return sum(closes[end - n:end]) / n


def swing_lows(bars, n=DAILY_SWING_N):
    """分形低點：第 i 根的低點嚴格低於左右各 n 根。右邊沒有 n 根已收 K 的不算。"""
    out = []
    for i in range(n, len(bars) - n):
        low = bars[i]["l"]
        if all(low < bars[j]["l"] for j in range(i - n, i)) and all(
            low < bars[j]["l"] for j in range(i + 1, i + n + 1)
        ):
            out.append(low)
    return out


def daily_trend_ok(daily_bars, now_ms=None):
    """布鹿流日 K ma7_25。回傳 (ok, reason)。

    daily_bars 是日 K，每根要有 "c" 與 "l"。now_ms 有給時，丟掉 close_time（"ct"）>= now_ms 的未收 K；
    沒有 "ct" 的留下。回放若已先濾成已收 K，呼叫 daily_trend_ok(bars) 即可，不要傳 now_ms。
    ok 為 True 才放行，reason 是 None。不過時 reason 為「日K資料不足」、「日K均線未向上發散」或「日K未底底高」。
    通過條件：收盤 > MA7、收盤 > MA25、MA7 > MA25、MA7 高於三根前的 MA7、MA25 高於三根前的 MA25，
    而且分形低點至少兩個、最近一個高於再前一個。比較都是嚴格大於。
    """
    bars = list(daily_bars or [])
    if now_ms is not None:
        kept = []
        for bar in bars:
            ct = bar.get("ct")
            if ct is None or ct < now_ms:
                kept.append(bar)
        bars = kept
    if len(bars) < DAILY_MIN_BARS:
        return False, "日K資料不足"
    closes = [bar["c"] for bar in bars]
    fast = ma_at(closes, DAILY_FAST)
    slow = ma_at(closes, DAILY_SLOW)
    fast_prev = ma_at(closes, DAILY_FAST, DAILY_MA_BACK)
    slow_prev = ma_at(closes, DAILY_SLOW, DAILY_MA_BACK)
    close = closes[-1]
    if not (close > fast and close > slow and fast > slow and fast > fast_prev and slow > slow_prev):
        return False, "日K均線未向上發散"
    lows = swing_lows(bars)
    if not (len(lows) >= 2 and lows[-1] > lows[-2]):
        return False, "日K未底底高"
    return True, None


def rule_bulu_rr_after_widening(cand, rr_target=None, rr_cap=_RR_CAP_DEFAULT):
    """布鹿流：停損先套 BTC/ETH 3%、小幣 5% 下限（取較寬），再算 RR，不到 1.5 不進。

    原文：full_prompt.md「停損取前低下1–2%容錯與BTC/ETH3%（小幣5%）較寬者，RR不足1.5不進」。
    不再額外往下加 1–2% 容錯（那會讓 RR 更低＝加嚴）。
    003：後波高點會突破前波。prior_high 模式的目標 = max(前高, 進場 + 1.5 × 取寬後風險)，
    前高不到 1.5R 時目標放在 1.5R，可以高於前高，不因此擋。進場價已經不低於前高是追價，擋。
    plan_target 模式仍用 prefilter 的 target 欄，不自動墊高。
    """
    mode = rr_target or BULU_RR_TARGET
    entry = _num(cand.get("entry"))
    stop = _num(cand.get("stop"))
    if None in (entry, stop) or entry <= 0 or stop <= 0 or stop >= entry:
        return "uncertain", "entry／stop／目標欄位缺或不合理"
    floor = BULU_MIN_STOP_MAJOR if cand.get("symbol") in MAJORS else BULU_MIN_STOP_SMALL
    wide_stop = min(stop, entry * (1 - floor))
    risk = entry - wide_stop
    if risk <= 0:
        return "uncertain", "entry／stop／目標欄位缺或不合理"
    if mode == "plan_target":
        target = _num(cand.get("target"))
        label = "prefilter target"
        if target is None or target <= 0:
            return "uncertain", "entry／stop／目標欄位缺或不合理"
    else:
        prior = _num(cand.get("prior_high"))
        if prior is None or prior <= 0:
            return "uncertain", "entry／stop／目標欄位缺或不合理"
        if prior <= entry:
            return "block", "進場價不低於前高（追價；目標可以超過前高，但不在前高之上追）"
        target = max(prior, entry + RR_MIN * risk)
        label = "1.5R（超過前高）" if target > prior + 1e-12 else "前高"
    target, used_cap = cap_bulu_target(entry, risk, target, rr_cap)
    if used_cap is not None:
        label = "{0:g}R 上限".format(used_cap)
    rr = (target - entry) / risk
    if rr + 1e-9 >= RR_MIN:
        return "pass", None
    return "block", "停損 {0:.1f}% 套 {1:.0f}% 下限後 RR {2:.2f} <1.5（目標＝{3}）".format(
        (entry - stop) / entry * 100, floor * 100, rr, label)


def evaluate(cand, get_klines, ref_ms, rr_target=None):
    """回傳 (verdict, details)。verdict = block / keep。任何 uncertain 都 keep。"""
    details = []
    verdict, why = rule_blacklist(cand)
    details.append(("blacklist", verdict, why))
    if verdict == "block":
        return "block", details
    setup = cand.get("setup")
    if setup == "瘸師弟":
        k15 = get_klines(cand["symbol"], "15m", 60)
        verdict, why = rule_kuai_long_black(cand, k15, ref_ms)
        details.append(("kuai_long_black", verdict, why))
        if verdict == "block":
            return "block", details
    elif setup == "布鹿流":
        verdict, why = rule_bulu_rr_after_widening(cand, rr_target)
        details.append(("bulu_rr_after_widening", verdict, why))
        if verdict == "block":
            return "block", details
        k1h = get_klines(cand["symbol"], "1h", 100)
        verdict, why = rule_bulu_volume(cand, k1h)
        details.append(("bulu_volume_3x", verdict, why))
        if verdict == "block":
            return "block", details
        k1d = get_klines(cand["symbol"], "1d", DAILY_KLINE_LIMIT)
        ok, why = daily_trend_ok(k1d, ref_ms)
        if not ok:
            details.append(("bulu_daily_trend", "block", why))
            return "block", details
        details.append(("bulu_daily_trend", "pass", None))
    else:
        # 小師弟、資金費率法等：這次沒有回放資料可驗證，不加篩，全部交給 AI。
        details.append(("no_hard_rule_for_setup", "uncertain", "setup {0} 不在本篩選範圍".format(setup)))
    return "keep", details


def run(body, get_klines, ref_ms, rr_target=None):
    cands = body.get("candidates")
    if not isinstance(cands, list):
        raise DataError("candidates 不是清單")
    kept, blocked, notes = [], [], []
    for cand in cands:
        if not isinstance(cand, dict) or not isinstance(cand.get("symbol"), str):
            kept.append(cand)
            notes.append("候選格式看不懂，保留給 AI")
            continue
        try:
            verdict, details = evaluate(cand, get_klines, ref_ms, rr_target)
        except RateLimited:
            raise
        except DataError as exc:
            verdict, details = "keep", [("data_error", "uncertain", str(exc))]
        rec = {"symbol": cand["symbol"], "setup": cand.get("setup"),
               "checks": [{"rule": r, "result": v, "why": w} for r, v, w in details]}
        if verdict == "block":
            rec["blocked_by"] = details[-1][0]
            rec["why"] = details[-1][2]
            blocked.append(rec)
        else:
            kept.append(cand)
            unc = [w for _r, v, w in details if v == "uncertain"]
            if unc:
                notes.append("{0} {1}：{2}".format(cand["symbol"], cand.get("setup"), "；".join(unc)))
    return {"ok": True, "kept": kept, "blocked": blocked, "notes": notes}


def _fixture_getter(path, asof_ms):
    """回放用：從檔案讀 K 線，不連網。檔案格式見 tests/fixtures/hardfilter_replay_20261110.json。"""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    table = {}
    for rec in data.get("klines", []):
        table[(rec["symbol"], rec["interval"], rec["asof_ms"])] = rec["rows"]

    def get(symbol, interval, limit):
        rows = table.get((symbol, interval, asof_ms))
        if rows is None:
            raise DataError("fixture 沒有 {0} {1} @ {2}".format(symbol, interval, asof_ms))
        return normalize(rows, asof_ms)[-limit:]
    return get


def main(argv=None, stdin=None, sleep=None, monotonic=None):
    ap = argparse.ArgumentParser(description="prefilter 之後的硬條件篩選（fail-open to AI）")
    ap.add_argument("--asof", help="回放時間 ISO 8601（含時區）。預設現在")
    ap.add_argument("--klines-fixture", help="回放用 K 線檔，不連網")
    ap.add_argument("--rr-target", choices=("prior_high", "plan_target"), default=BULU_RR_TARGET,
                    help="布鹿流 RR 的目標價（預設 prior_high；plan_target 需使用者確認後才可改成預設）")
    args = ap.parse_args(argv)
    stdin = stdin or sys.stdin
    if sleep is None:
        sleep = time.sleep
    if monotonic is None:
        monotonic = time.monotonic
    # t0 在這輪一開始就記下，後面每次退避都用它看離 gate 的 60 秒還剩多少。
    budget = KlineRetryBudget(sleep, monotonic)
    try:
        body = json.loads(stdin.read())
        if not isinstance(body, dict):
            raise DataError("輸入不是 JSON 物件")
        if args.asof:
            ref_ms = int(datetime.fromisoformat(args.asof).timestamp() * 1000)
        else:
            ref_ms = int(time.time() * 1000)
        if args.klines_fixture:
            getter = _fixture_getter(args.klines_fixture, ref_ms)
        else:
            end = ref_ms if args.asof else None
            cache = {}

            def getter(symbol, interval, limit):
                key = (symbol, interval)
                if key not in cache:
                    cache[key] = fetch_klines(symbol, interval, limit, end, budget=budget)
                return cache[key]
        out = run(body, getter, ref_ms, args.rr_target)
    except RateLimited as exc:
        payload = {"ok": False, "error": "rate_limited: {0}".format(exc)}
        payload.update(_retry_fields(budget))
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "{0}: {1}".format(type(exc).__name__, exc)}, ensure_ascii=False))
        return 2
    if budget.retries:
        out.update(_retry_fields(budget))
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
