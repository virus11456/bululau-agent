#!/usr/bin/env python3
"""硬條件篩選：接在 prefilter 輸出之後，由 gate.py 呼叫。只把「程式算得出來、而且確定不符合」的候選拿掉。

原則（總經理boy 11/10 轉達使用者 23:44 原話「你依照原本的開單的條件跟頻率來撈單來做設定」）：
  - 只照現行規則（~/quantboy-gate/full_prompt.md 與 docs/strategy.md 11-06 定稿）判斷，不放寬也不加嚴。
  - 規則原文沒給數字、需要看型態、或資料不夠時，一律留下候選交給 AI（fail-open to AI）。
  - 429／418 限流或整體讀取失敗：結束碼 2，gate 會 WAKE 並保留全部候選。
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
# full_prompt.md：「瘸師弟15m連2–3長黑」；strategy.md：「大幣長黑大約 2%，小幣 4–5% 以上」「15m 連續 2–3 根長黑」
KUAI_MIN_CONSEC = 2
KUAI_LONG_BLACK_BIG = 2.0    # %
KUAI_LONG_BLACK_SMALL = 4.0  # %：原文 4–5%，取 4%（較寬，不加嚴）
# strategy.md 11-03 補充：「黑名單維持不做：程式裡的 INITUSDT、EWZUSDT，以及 lessons 點名的 CAP、TAG、APR、BREV、XAN、AIN、DOOD、INIT」
BLACKLIST_BASES = {"INIT", "EWZ", "CAP", "TAG", "APR", "BREV", "XAN", "AIN", "DOOD"}
# 布鹿流 RR 的目標價。prior_high＝prefilter 自己算 rr 用的前高（原文「回檔離前高很近仍然可以進。過不了前高再出場」）。
# plan_target＝prefilter 的 target 欄（被舊紙上參數 2R 截斷，strategy.md 說這不是核准參數）。預設 prior_high（較寬）。
BULU_RR_TARGET = "prior_high"

REQUEST_TIMEOUT_S = 10


class RateLimited(Exception):
    pass


class DataError(Exception):
    pass


def fetch_klines(symbol, interval, limit, end_ms=None):
    """幣安公開 K 線，只回已收 K。418/429 丟 RateLimited；其他錯誤丟 DataError。"""
    url = "{0}/fapi/v1/klines?symbol={1}&interval={2}&limit={3}".format(BASE, symbol, interval, limit)
    if end_ms:
        url += "&endTime={0}".format(end_ms)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gate-hardfilter"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            rows = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (418, 429):
            raise RateLimited("{0} on klines {1}".format(exc.code, symbol))
        raise DataError("HTTP {0} on klines {1}".format(exc.code, symbol))
    except Exception as exc:  # 逾時、DNS、JSON 壞掉
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


def rule_kuai_consecutive_blacks(cand, k15, ref_ms):
    """瘸師弟：訊號 K 必須和相鄰的已收 15m K 組成至少連 2 根長黑。

    原文：full_prompt.md「瘸師弟15m連2–3長黑、訊號量至少前10–20根3倍」；
    strategy.md「篩網：15m 連續 2–3 根長黑，而且量至少是前量的 3 倍」「大幣長黑大約 2%，小幣 4–5% 以上」。
    訊號 K 本身的 3 倍量 prefilter 已驗。相鄰那根只看長黑幅度，不另要求 3 倍量（原文沒說每根都要，取寬）。
    """
    if not k15 or len(k15) < 3:
        return "uncertain", "15m K 線不足"
    start = _bar_start_ms(cand.get("bar"), ref_ms)
    if start is None:
        return "uncertain", "訊號 K 時間讀不到"
    idx = next((i for i, b in enumerate(k15) if b["t"] == start), None)
    if idx is None:
        return "uncertain", "訊號 K 不在已收 K 線裡"
    big = cand.get("big")
    th = KUAI_LONG_BLACK_SMALL if big is False else KUAI_LONG_BLACK_BIG  # 欄位缺就用較寬的 2%
    if black_pct(k15[idx]) < th:
        return "uncertain", "訊號 K 重算不是長黑（資料與 prefilter 不一致）"
    run = 1
    j = idx - 1
    while j >= 0 and black_pct(k15[j]) >= th:
        run += 1
        j -= 1
    j = idx + 1
    while j < len(k15) and black_pct(k15[j]) >= th:
        run += 1
        j += 1
    if run >= KUAI_MIN_CONSEC:
        return "pass", None
    def desc(i):
        if i < 0 or i >= len(k15):
            return "尚未收／無"
        b = black_pct(k15[i])
        return "收黑 {0:.2f}%".format(b) if b > 0 else "收紅 {0:.2f}%".format(-b)
    return "block", "只有 1 根長黑（長黑門檻 {0}%；前一根{1}、後一根{2}）".format(th, desc(idx - 1), desc(idx + 1))


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


def rule_bulu_rr_after_widening(cand, rr_target=None):
    """布鹿流：停損先套 BTC/ETH 3%、小幣 5% 下限（取較寬），再算 RR，不到 1.5 不進。

    原文：full_prompt.md「停損取前低下1–2%容錯與BTC/ETH3%（小幣5%）較寬者，RR不足1.5不進」；
    strategy.md「取寬之後風報比不到 1.5，就不做」「禁止把停損收窄來湊」。
    不再額外往下加 1–2% 容錯（那會讓 RR 更低＝加嚴），所以這裡算出的 RR 只會偏高，不會誤擋。
    """
    mode = rr_target or BULU_RR_TARGET
    entry = _num(cand.get("entry"))
    stop = _num(cand.get("stop"))
    target = _num(cand.get("prior_high") if mode == "prior_high" else cand.get("target"))
    if None in (entry, stop, target) or entry <= 0 or stop <= 0 or stop >= entry:
        return "uncertain", "entry／stop／目標欄位缺或不合理"
    floor = BULU_MIN_STOP_MAJOR if cand.get("symbol") in MAJORS else BULU_MIN_STOP_SMALL
    wide_stop = min(stop, entry * (1 - floor))
    risk = entry - wide_stop
    rr = (target - entry) / risk
    if rr + 1e-9 >= RR_MIN:
        return "pass", None
    return "block", "停損 {0:.1f}% 套 {1:.0f}% 下限後 RR {2:.2f} <1.5（目標＝{3}）".format(
        (entry - stop) / entry * 100, floor * 100, rr, "前高" if mode == "prior_high" else "prefilter target")


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
        verdict, why = rule_kuai_consecutive_blacks(cand, k15, ref_ms)
        details.append(("kuai_consecutive_blacks", verdict, why))
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


def main(argv=None, stdin=None):
    ap = argparse.ArgumentParser(description="prefilter 之後的硬條件篩選（fail-open to AI）")
    ap.add_argument("--asof", help="回放時間 ISO 8601（含時區）。預設現在")
    ap.add_argument("--klines-fixture", help="回放用 K 線檔，不連網")
    ap.add_argument("--rr-target", choices=("prior_high", "plan_target"), default=BULU_RR_TARGET,
                    help="布鹿流 RR 的目標價（預設 prior_high；plan_target 需使用者確認後才可改成預設）")
    args = ap.parse_args(argv)
    stdin = stdin or sys.stdin
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
                    cache[key] = fetch_klines(symbol, interval, limit, end)
                return cache[key]
        out = run(body, getter, ref_ms, args.rr_target)
    except RateLimited as exc:
        print(json.dumps({"ok": False, "error": "rate_limited: {0}".format(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "{0}: {1}".format(type(exc).__name__, exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
