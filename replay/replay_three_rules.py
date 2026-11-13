#!/usr/bin/env python3
"""2026-11-07 到 2026-11-12（台北）三項規則的歷史回放。

候選只從 prefilter_log、scan.jsonl、gate_log 來。decision_log 只用來對上閘門已經叫醒、
但 gate_log 沒留下幣種的那幾筆，並標明出處。K 線只打幣安公開 fapi klines，不簽章。
取不到的欄位寫「未取得」，不補、不內插。

用法：
  python3 replay/replay_three_rules.py
  python3 replay/replay_three_rules.py --no-network   # 只用 replay/cache 裡已經抓過的 K 線
"""
import argparse
import csv
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "replay", "fixtures")
CACHE = os.path.join(ROOT, "replay", "cache")
OUT_DIR = os.path.join(ROOT, "replay", "out")
TPE = timezone(timedelta(hours=8))
WINDOW_START = datetime(2026, 11, 7, tzinfo=TPE)
WINDOW_END = datetime(2026, 11, 13, tzinfo=TPE)  # 不含 11/13
FAPI = "https://fapi.binance.com/fapi/v1/klines"
BIG_QV = 3e8
RR_MIN = 1.5
KUAI_STEPS_NEW = (0.97, 0.96, 0.95)


def parse_time(text):
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TPE)
    return dt.astimezone(TPE)


def in_window(dt):
    return dt is not None and WINDOW_START <= dt < WINDOW_END


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def day_key(dt):
    return dt.astimezone(TPE).date().isoformat()


def coverage(rows, time_key="time"):
    days = {}
    for row in rows:
        dt = parse_time(row.get(time_key))
        if dt is None:
            continue
        days.setdefault(day_key(dt), 0)
        days[day_key(dt)] += 1
    return days


def kline_cache_path(symbol, interval):
    return os.path.join(CACHE, "{0}_{1}.json".format(symbol, interval))


def fetch_klines(symbol, interval, start_ms, limit=1500, sleep_s=0.35):
    """公開 K 線。429／418 退避後再試，最多 4 次。不帶簽章。"""
    os.makedirs(CACHE, exist_ok=True)
    path = kline_cache_path(symbol, interval)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            cached = json.load(handle)
        if cached.get("start_ms") == start_ms and cached.get("limit") == limit and "rows" in cached:
            return cached["rows"], None
    url = "{0}?symbol={1}&interval={2}&startTime={3}&limit={4}".format(
        FAPI, symbol, interval, start_ms, limit)
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bululau-replay"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                rows = json.loads(resp.read().decode())
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"start_ms": start_ms, "limit": limit, "rows": rows}, handle)
            time.sleep(sleep_s)
            return rows, None
        except urllib.error.HTTPError as exc:
            last_err = "HTTP {0}".format(exc.code)
            if exc.code in (418, 429):
                wait = 2.0 * (attempt + 1)
                header = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    if header and float(header) > 0:
                        wait = min(float(header), 30.0)
                except ValueError:
                    pass
                time.sleep(wait)
                continue
            return None, last_err
        except Exception as exc:
            last_err = "{0}: {1}".format(type(exc).__name__, exc)
            time.sleep(1.0)
    return None, last_err or "未取得"


def normalize(rows, end_ms=None):
    out = []
    for raw in rows or []:
        try:
            item = {"t": int(raw[0]), "o": float(raw[1]), "h": float(raw[2]), "l": float(raw[3]),
                    "c": float(raw[4]), "v": float(raw[7]), "ct": int(raw[6])}
        except (TypeError, ValueError, IndexError):
            continue
        if end_ms is None or item["ct"] < end_ms:
            out.append(item)
    return out


def load_prefilter_new():
    disc = os.path.join(ROOT, "backups", "mac", "quantboy-paper", "discretionary")
    sys.path.insert(0, disc)
    spec = importlib.util.spec_from_file_location("paper_prefilter_new", os.path.join(disc, "prefilter.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def old_kuai(k15, big, symbol):
    """改規則前的紙上 prefilter。參考點是 min(前 20 根最低, 訊號低點)。大幣 1/1.5/2%，其餘 3/4/5%。"""
    body_min = 2.0 if symbol in ("BTCUSDT", "ETHUSDT") else 4.0
    for i in (-1, -2):
        if len(k15) < 22:
            return None
        b = k15[i]
        prior = k15[i - 20:i]
        if len(prior) < 20:
            continue
        av = sum(x["v"] for x in prior) / 20
        body = pct(b["o"], b["c"])
        if av and b["v"] >= 3 * av and body >= body_min:
            prev_low = min(x["l"] for x in prior)
            steps = (0.99, 0.985, 0.98) if big else (0.97, 0.96, 0.95)
            ref = min(prev_low, b["l"])
            ladder = [round(ref * s, 8) for s in steps]
            avg = (ladder[0] * 2 + ladder[1] * 3 + ladder[2] * 5) / 10
            stop = ladder[2] * (0.985 if big else 0.97)
            risk = avg - stop
            return {"bar_ms": b["t"], "ladder": ladder, "entry": avg, "stop": round(stop, 8),
                    "target": round(avg + 1.5 * risk, 8), "ref": ref, "black_pct": round(body, 2)}
    return None


def ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def old_bulu(k1h, kd):
    """改規則前的 buluuliu：MA30/45/60、拉回 1.2%、錘子／吞噬，目標封在前高與 2R。"""
    if len(kd) < 25:
        return None, "daily history short"
    a = min(x["l"] for x in kd[-10:])
    b = min(x["l"] for x in kd[-20:-10])
    wild = any((x["h"] - x["l"]) / x["o"] > 0.5 for x in kd[-7:] if x.get("o"))
    if wild:
        return None, "daily range >50% (妖幣)"
    if not (a > b and kd[-1]["c"] > ma([x["c"] for x in kd], 20)):
        return None, "daily not higher-lows/above MA20"
    if len(k1h) < 70:
        return None, "1H history short"
    closes = [x["c"] for x in k1h]
    m = [ma(closes, n) for n in (30, 45, 60)]
    mp = [ma(closes[:-5], n) for n in (30, 45, 60)]
    if None in m or None in mp or not (m[0] > m[1] > m[2] and all(x > y for x, y in zip(m, mp))):
        return None, "1H MA30/45/60 not up-diverged"
    w = k1h[-48:]
    hi_i = max(range(len(w)), key=lambda i: w[i]["h"])
    high = w[hi_i]["h"]
    if hi_i > len(w) - 3:
        return None, "at highs (no pullback, no chase)"
    vb = max(x["v"] for x in w[:hi_i + 1])
    base = k1h[-90:-48]
    vbase = sum(x["v"] for x in base) / max(len(base), 1)
    if vbase and vb < 2 * vbase:
        return None, "no 2-3x volume expansion"
    last = w[-1]
    close = last["c"]
    near = min(abs(last["l"] / m[0] - 1), abs(last["l"] / m[1] - 1))
    if near > 0.012 and last["l"] > m[0]:
        return None, "not pulled back to MA30/45"
    if last["c"] < m[2]:
        return None, "closed below MA60"
    body = abs(last["c"] - last["o"])
    wick = min(last["o"], last["c"]) - last["l"]
    prev = w[-2]
    engulf = last["c"] > last["o"] and prev["c"] < prev["o"] and last["c"] >= prev["o"]
    if not (last["c"] > last["o"] and (wick >= body or engulf)):
        return None, "no green hammer/engulf confirmation"
    stop = min(min(x["l"] for x in w[-5:]), m[2]) * 0.995
    risk = close - stop
    if risk <= 0:
        return None, "bad risk"
    rr = (high - close) / risk
    if rr < RR_MIN:
        return None, "rr {0:.2f} to prior high".format(rr)
    target = close + min(rr, 2.0) * risk
    return {"bar_ms": last["t"], "entry": close, "stop": round(stop, 8), "target": round(target, 8),
            "prior_high": high, "rr": round(min(rr, 2), 2)}, None


def new_kuai_view(hit):
    if not hit:
        return None
    ladder = hit["ladder"]
    avg = (ladder[0] * 2 + ladder[1] * 3 + ladder[2] * 5) / 10
    return {"bar_ms": None, "ladder": ladder, "entry": avg, "stop": hit["stop"], "target": hit["target"],
            "ref": hit["ladder"][0] / 0.97, "black_pct": hit.get("black_pct"), "bar_label": hit.get("bar")}


def bar_ms_from_label(label, ref):
    if not label:
        return None
    best = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            dt = datetime.strptime("{0}-{1}".format(year, label), "%Y-%m-%d %H:%M").replace(tzinfo=TPE)
        except ValueError:
            return None
        if best is None or abs((dt - ref).total_seconds()) < abs((best - ref).total_seconds()):
            best = dt
    return int(best.timestamp() * 1000)


def position_r(filled, exit_px, stop):
    weight = sum(w for _p, w in filled)
    avg = sum(p * w for p, w in filled) / weight
    risk = avg - stop
    if risk <= 0:
        return None, avg
    return (exit_px - avg) / risk, avg


def simulate_kuai(future, plan):
    """限價只在訊號後 8 根 15m（2 小時）內掛。成交後繼續走到停損或目標。"""
    if not future:
        return {"fill": "未取得", "result": "未取得", "R": "未取得", "note": "訊號之後的 K 線未取得"}
    weights = (2, 3, 5)
    pending = list(zip(plan["ladder"], weights))
    filled = []
    for i, bar in enumerate(future):
        if i < 8:
            keep = []
            for price, weight in pending:
                if bar["l"] <= price:
                    filled.append((price, weight))
                else:
                    keep.append((price, weight))
            pending = keep
        if not filled:
            continue
        hit_stop = bar["l"] <= plan["stop"]
        hit_target = bar["h"] >= plan["target"]
        if hit_stop and hit_target:
            r, _avg = position_r(filled, plan["stop"], plan["stop"])
            return {"fill": "成交", "result": "停損", "R": r, "note": "同一根同時觸及停損和目標，以停損計"}
        if hit_stop:
            r, _avg = position_r(filled, plan["stop"], plan["stop"])
            return {"fill": "成交", "result": "停損", "R": r, "note": ""}
        if hit_target:
            r, _avg = position_r(filled, plan["target"], plan["stop"])
            return {"fill": "成交", "result": "目標", "R": r, "note": ""}
    if not filled:
        return {"fill": "沒成交", "result": "沒成交", "R": "", "note": "訊號後 2 小時沒有打到掛單"}
    return {"fill": "成交", "result": "未出場", "R": "未取得", "note": "區間內沒有打到停損或目標"}


def simulate_bulu(future, plan):
    """布鹿流的 entry 是訊號 1H 收盤，視作成交。之後的 K 線才算出場。"""
    if not future:
        return {"fill": "成交", "result": "未取得", "R": "未取得", "note": "進場之後的 K 線未取得"}
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = entry - stop
    if risk <= 0:
        return {"fill": "成交", "result": "未取得", "R": "未取得", "note": "停損不高於進場，無法算 R"}
    for bar in future:
        hit_stop = bar["l"] <= stop
        hit_target = bar["h"] >= target
        if hit_stop and hit_target:
            return {"fill": "成交", "result": "停損", "R": -1.0,
                    "note": "同一根同時觸及停損和目標，以停損計"}
        if hit_stop:
            return {"fill": "成交", "result": "停損", "R": -1.0, "note": ""}
        if hit_target:
            return {"fill": "成交", "result": "目標", "R": (target - entry) / risk, "note": ""}
    return {"fill": "成交", "result": "未出場", "R": "未取得", "note": "區間內沒有打到停損或目標"}


def widened_rr(symbol, entry, stop, prior):
    floor = 0.03 if symbol in ("BTCUSDT", "ETHUSDT") else 0.05
    wide = min(stop, entry * (1 - floor))
    risk = entry - wide
    if risk <= 0 or prior is None:
        return None
    return (prior - entry) / risk


def prompt_class(reason):
    """只把『唯一理由就是那兩條』算成提示詞多放行。還有別的理由就標出來，不算。"""
    text = reason or ""
    removed = any(k in text for k in ("妖幣急拉", "離好球帶", "不盲掛"))
    other_keys = ("日K均線", "日Ｋ均線", "1–2%", "1-2%", "結構停損", "RR", "續收黑", "下殺",
                  "底底高", "非首波", "5%", "均線參數")
    other = [k for k in other_keys if k in text]
    if removed and not other:
        return "多放行"
    if removed and other:
        return "仍有其他拒單理由"
    return ""


def rr_only(reason):
    """拒單文字裡只有 RR 不足，沒有同時寫底底高、波數或均線。這種才算布鹿流目標規則會多放行。"""
    text = reason or ""
    if "RR" not in text:
        return False
    return not any(k in text for k in ("底底高", "非首波", "3波", "三波", "均線", "錘子", "吞噬", "爆量"))


def logged_quote(reason):
    """只抄紀錄裡寫出來的數字，不反推進場價。"""
    import re
    text = reason or ""
    out = {}
    span = re.search(r"ladder\s*([0-9.]+)\s*[–\-]\s*([0-9.]+)", text)
    if span:
        out["entry"] = "紀錄階梯 {0}–{1}".format(span.group(1), span.group(2))
    else:
        one = re.search(r"ladder\s*([0-9.]+)", text)
        if one:
            out["entry"] = "紀錄階梯 {0}".format(one.group(1))
    stop = re.search(r"5%（約([0-9.]+)）", text)
    if stop:
        out["stop"] = "紀錄停損 " + stop.group(1)
    target = re.search(r"目標\s*([0-9.]+)", text)
    if target:
        out["target"] = "紀錄目標 " + target.group(1)
    return out


def fmt_num(value):
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    return "{0:.8g}".format(value)


def fmt_r(value):
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    return "{0:.4f}".format(value)


def collect_events():
    events = []
    prefilter_rows = load_jsonl(os.path.join(FIXTURE, "prefilter_log_20261113.jsonl"))
    scan_rows = load_jsonl(os.path.join(FIXTURE, "scan_20261113.jsonl"))
    gate_rows = load_jsonl(os.path.join(FIXTURE, "gate_log_20261113.jsonl"))
    decision_rows = load_jsonl(os.path.join(FIXTURE, "decision_log_20261113.jsonl"))
    for row in prefilter_rows:
        dt = parse_time(row.get("time"))
        if not in_window(dt):
            continue
        for cand in row.get("candidates") or []:
            events.append({"time": dt, "symbol": cand.get("symbol"), "setup": cand.get("setup"),
                           "source": "prefilter_log", "role": "candidate", "raw": cand})
        for near in row.get("near_misses") or []:
            events.append({"time": dt, "symbol": near.get("symbol"), "setup": near.get("setup"),
                           "source": "prefilter_log", "role": "near_miss", "fail": near.get("fail"), "raw": near})
    for row in gate_rows:
        dt = parse_time(row.get("time"))
        if not in_window(dt):
            continue
        hf = row.get("hardfilter") or {}
        if not isinstance(hf, dict):
            continue
        for blocked in hf.get("blocked") or []:
            events.append({"time": dt, "symbol": blocked.get("symbol"), "setup": blocked.get("setup"),
                           "source": "gate_log", "role": "hardfilter_blocked",
                           "rule": blocked.get("rule"), "why": blocked.get("why")})
    for row in decision_rows:
        dt = parse_time(row.get("round_time") or row.get("time"))
        if not in_window(dt):
            continue
        events.append({"time": dt, "symbol": row.get("symbol"), "setup": row.get("pattern"),
                       "source": "decision_log", "role": "ai", "decision": row.get("decision"),
                       "reason": row.get("reason") or ""})
    return events, prefilter_rows, scan_rows, gate_rows, decision_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    new_mod = load_prefilter_new()
    events, prefilter_rows, scan_rows, gate_rows, decision_rows = collect_events()
    symbols = sorted({e["symbol"] for e in events if e.get("symbol")})
    start_15 = int(datetime(2026, 10, 30, tzinfo=TPE).timestamp() * 1000)
    start_1h = int(datetime(2026, 10, 1, tzinfo=TPE).timestamp() * 1000)
    start_1d = int(datetime(2026, 8, 1, tzinfo=TPE).timestamp() * 1000)
    store = {}
    errors = {}
    if args.no_network:
        fetch = lambda *a, **k: (None, "no-network")
    else:
        fetch = fetch_klines
    for symbol in symbols:
        for interval, start in (("15m", start_15), ("1h", start_1h), ("1d", start_1d)):
            rows, err = fetch(symbol, interval, start)
            if err and not os.path.isfile(kline_cache_path(symbol, interval)):
                errors[(symbol, interval)] = err
                store[(symbol, interval)] = []
            elif rows is None and os.path.isfile(kline_cache_path(symbol, interval)):
                with open(kline_cache_path(symbol, interval), encoding="utf-8") as handle:
                    store[(symbol, interval)] = json.load(handle).get("rows") or []
            else:
                store[(symbol, interval)] = rows or []
                if err:
                    errors[(symbol, interval)] = err

    rows_out = []
    for event in events:
        symbol = event.get("symbol")
        setup = event.get("setup")
        if setup not in ("瘸師弟", "布鹿流"):
            continue
        if not symbol:
            continue
        end_ms = int(event["time"].timestamp() * 1000)
        k15 = normalize(store.get((symbol, "15m")), end_ms)
        k1h = normalize(store.get((symbol, "1h")), end_ms)
        kd = normalize(store.get((symbol, "1d")), end_ms)
        big = False
        if len(k15) >= 96:
            big = sum(x["v"] for x in k15[-96:]) >= BIG_QV
        missing = []
        if not k15:
            missing.append("15m")
        if setup == "布鹿流" and not k1h:
            missing.append("1h")
        if setup == "布鹿流" and not kd:
            missing.append("1d")
        note_bits = []
        if missing:
            note_bits.append("K線未取得: " + ",".join(missing))
            err = errors.get((symbol, missing[0] if missing else "15m"))
            if err:
                note_bits.append(err)
        change = ""
        old = new = None
        sim = {"fill": "未取得", "result": "未取得", "R": "未取得", "note": ""}
        if setup == "瘸師弟" and k15:
            old = old_kuai(k15, big, symbol)
            hit = new_mod.kuai_shidi(k15, {"big": big}, symbol)
            new = None
            if hit:
                bar_ms = bar_ms_from_label(hit.get("bar"), event["time"])
                new = new_kuai_view(hit)
                new["bar_ms"] = bar_ms
            if new and new.get("bar_ms"):
                future = [b for b in normalize(store.get((symbol, "15m"))) if b["t"] > new["bar_ms"]]
                sim = simulate_kuai(future, new)
            elif new:
                sim = {"fill": "未取得", "result": "未取得", "R": "未取得", "note": "訊號時間未取得"}
            else:
                sim = {"fill": "", "result": "", "R": "", "note": ""}
            if old and new and old["ladder"] != new["ladder"]:
                change = "掛單價改變"
            elif new and not old:
                change = "多放行"
            elif old and not new:
                change = "少放行"
            prompt = prompt_class(event.get("reason"))
            if prompt == "多放行":
                change = "多放行"
                if not new:
                    sim = {"fill": "未取得", "result": "未取得", "R": "未取得",
                           "note": "當下最後兩根沒有重算出瘸師弟"}
            elif prompt == "仍有其他拒單理由" and change != "多放行":
                note_bits.append("提示詞仍有其他拒單理由，不算多放行")
        elif setup == "布鹿流" and k1h and kd:
            old, old_why = old_bulu(k1h, kd)
            hit, new_why = new_mod.buluuliu(k1h, kd)
            new = None
            if hit:
                new = {"bar_ms": k1h[-1]["t"], "entry": hit["entry"], "stop": hit["stop"],
                       "target": hit["target"], "prior_high": hit.get("prior_high"), "rr": hit.get("rr")}
            # 舊硬篩：目標鎖前高，套 5%/3% 後 RR<1.5 就擋。新硬篩允許目標超過前高。
            old_gate = bool(old)
            new_gate = bool(new)
            if old:
                rr_old = widened_rr(symbol, old["entry"], old["stop"], old["prior_high"])
                if rr_old is not None and rr_old + 1e-9 < RR_MIN:
                    old_gate = False
                    note_bits.append("舊硬篩 RR {0:.2f}".format(rr_old))
            if new:
                if new["prior_high"] <= new["entry"]:
                    new_gate = False
                future = [b for b in normalize(store.get((symbol, "1h"))) if b["t"] > new["bar_ms"]]
                sim = simulate_bulu(future, new)
            else:
                sim = {"fill": "", "result": "", "R": "", "note": new_why or ""}
                note_bits.append(new_why or "")
            if (not old_gate) and new_gate:
                change = "多放行"
            elif old_gate and not new_gate:
                change = "少放行"
            elif old and new and (old["entry"] != new["entry"] or old["stop"] != new["stop"] or old["target"] != new["target"]):
                change = "價位改變"
            if event.get("role") == "near_miss" and event.get("fail"):
                note_bits.append("舊近失: " + event["fail"])
            if event.get("why"):
                note_bits.append("紀錄硬篩: " + event["why"])
        reason = event.get("reason") or ""
        if missing and change != "多放行":
            prompt = prompt_class(reason)
            if setup == "瘸師弟" and prompt == "多放行":
                change = "多放行"
                sim = {"fill": "未取得", "result": "未取得", "R": "未取得",
                       "note": "fapi klines HTTP 451，沒有事後模擬"}
            elif setup == "布鹿流" and rr_only(reason):
                change = "多放行"
                sim = {"fill": "未取得", "result": "未取得", "R": "未取得",
                       "note": "紀錄只因 RR 不足拒單。新規則目標可超過前高。fapi HTTP 451，模擬未取得"}
            else:
                change = "未取得"
                sim = {"fill": "未取得", "result": "未取得", "R": "未取得", "note": "K線未取得"}
                if prompt == "仍有其他拒單理由":
                    note_bits.append("提示詞仍有其他拒單理由，不算多放行")
        if reason:
            note_bits.append("AI: " + reason[:180].replace("\n", " "))
        if sim.get("note"):
            note_bits.append(sim["note"])
        plan = new or old or {}
        quoted = logged_quote(reason)
        rows_out.append({
            "change_item": "瘸師弟" if setup == "瘸師弟" else "布鹿流",
            "diff": change or "無差異",
            "time": event["time"].isoformat(timespec="seconds"),
            "symbol": symbol,
            "setup": setup,
            "source": event["source"],
            "role": event["role"],
            "entry": fmt_num(plan.get("entry")) or quoted.get("entry") or "",
            "stop": fmt_num(plan.get("stop")) or quoted.get("stop") or "",
            "target": fmt_num(plan.get("target")) or quoted.get("target") or "",
            "prior_high": fmt_num(plan.get("prior_high")),
            "fill": sim.get("fill") or "",
            "result": sim.get("result") or "",
            "R": fmt_r(sim.get("R")),
            "note": "；".join(x for x in note_bits if x),
        })

    # 波數：decision_log 裡出現「3 波／三波」的列。不從 K 線自算波數。
    for event in events:
        reason = event.get("reason") or ""
        if event.get("setup") != "布鹿流":
            continue
        if "3波" not in reason and "三波" not in reason and "約3波" not in reason:
            continue
        rows_out.append({
            "change_item": "波數",
            "diff": "未取得",
            "time": event["time"].isoformat(timespec="seconds"),
            "symbol": event.get("symbol") or "",
            "setup": "布鹿流",
            "source": event["source"],
            "role": event["role"],
            "entry": "未取得",
            "stop": "未取得",
            "target": "未取得",
            "prior_high": "未取得",
            "fill": "未取得",
            "result": "未取得",
            "R": "未取得",
            "note": "紀錄寫了約 3 波，但沒有可核對的波數，不從 K 線自算。AI: " + reason[:180],
        })

    # 同一幣、同一根、同一差異只留最早一筆進統計；明細仍全寫。
    def stat_key(row):
        return (row["change_item"], row["symbol"], row["diff"], row.get("entry"), row.get("stop"), row.get("target"))

    seen = set()
    unique = []
    for row in rows_out:
        key = stat_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    csv_path = os.path.join(OUT_DIR, "rule_replay_20261107_12.csv")
    fields = ["change_item", "diff", "time", "symbol", "setup", "source", "role",
              "entry", "stop", "target", "prior_high", "fill", "result", "R", "note"]
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows_out)

    def closed(rows):
        return [r for r in rows if r["result"] in ("停損", "目標") and r["R"] not in ("", "未取得")]

    def summarize(item):
        group = [r for r in unique if r["change_item"] == item and r["diff"] == "多放行"]
        done = closed(group)
        wins = [r for r in done if r["result"] == "目標"]
        total = sum(float(r["R"]) for r in done)
        nofill = [r for r in group if r["result"] == "沒成交"]
        days = sorted({r["time"][:10] for r in group})
        return group, done, wins, total, nofill, days

    lines = []
    lines.append("# 三項規則回放（2026-11-07 至 2026-11-12，台北）")
    lines.append("")
    lines.append("候選身份只來自日誌。K 線只打幣安公開 `https://fapi.binance.com/fapi/v1/klines`，沒有簽章。同一根同時打到停損和目標，算停損。")
    lines.append("沒有從缺日補幣，也沒有用 K 線自己數波。")
    if errors and all("451" in str(v) for v in errors.values()):
        lines.append("")
        lines.append("這個環境對 `fapi.binance.com` 的每一筆 klines 都是 HTTP 451（restricted location）。沒有改打別的主機，也沒有用 data.binance.vision。因此要靠 K 線重算的進場價、停損、目標和事後 R 全部是未取得。")
        lines.append("下面的「多放行」只來自 decision_log 的拒單原文：瘸師弟只因那兩條理由被拒，或布鹿流只因 RR 不足被拒。價位欄若寫「紀錄…」，是原文裡的數字，不是重算出來的。")
    lines.append("")
    lines.append("## 日誌涵蓋")
    lines.append("")
    lines.append("| 日期 | prefilter_log | scan.jsonl | gate_log | 幣種 |")
    lines.append("|---|---|---|---|---|")
    cov_p = coverage(prefilter_rows)
    cov_s = coverage(scan_rows)
    cov_g = coverage(gate_rows)
    scan_cand = {}
    for row in scan_rows:
        if row.get("event") == "candidates":
            scan_cand[row["time"][:10]] = scan_cand.get(row["time"][:10], 0) + 1
    for i in range(6):
        day = (WINDOW_START + timedelta(days=i)).date().isoformat()
        ident = "有" if (cov_p.get(day) or cov_g.get(day)) else "未取得"
        if day in ("2026-11-07", "2026-11-08", "2026-11-09"):
            ident = "未取得（scan 只有張數，沒有幣種）"
        lines.append("| {0} | {1} | 候選事件 {2}（無幣種） | {3} | {4} |".format(
            day,
            cov_p.get(day, "未取得"),
            scan_cand.get(day, "未取得"),
            cov_g.get(day, "未取得"),
            ident,
        ))
    lines.append("")
    lines.append("scan.jsonl 到 2026-11-11 為止，11/12 的掃描幣種未取得。gate_log 從 2026-11-10 15:04 起。prefilter_log 只有 2026-11-10 兩列，而且候選數是 0，只有 near miss。")
    lines.append("11/07、11/08、11/09 的幣種、進場價、停損、目標、模擬全部是未取得。這三天不放進分母，也不當成 0 筆。")
    lines.append("")
    lines.append("## 判定")
    lines.append("")
    lines.append("- 瘸師弟多放行：新舊掛單價不同不算多放行，另列「掛單價改變」。decision_log 裡拒單理由只有「妖幣急拉不追」或「離好球帶太遠／不盲掛」、而且新規則仍算得出掛單的，算多放行。還寫了別的理由（日 K 均線、停損、RR、底底高）的不算。")
    lines.append("- 布鹿流多放行：舊的錘子／均線／前高 RR 不過，新的結構條件與「目標可超過前高」過了。")
    lines.append("- 波數：日誌沒有波數，不模擬，勝率與總 R 都是未取得。")
    lines.append("- 瘸師弟限價只在訊號後 2 小時內算成交。布鹿流用訊號 1H 收盤當進場。")
    lines.append("- 統計用同一幣、同一進場價、同一停損、同一目標的第一筆，避免 15 分鐘重覆灌筆數。明細 CSV 保留每一輪。")
    lines.append("")

    def section(title, item):
        group, done, wins, total, nofill, days = summarize(item)
        lines.append("## {0}".format(title))
        lines.append("")
        if not group:
            lines.append("日誌裡沒有因此改判放行的列。K 線若是 HTTP 451，機械篩選會不會多放行也是未取得，沒有把它算成 0。")
            lines.append("")
            return
        lines.append("| 時間 | 幣 | 進場 | 停損 | 目標 | 成交 | 結果 | R | 出處 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for row in group:
            lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} |".format(
                row["time"], row["symbol"], row["entry"] or "未取得", row["stop"] or "未取得",
                row["target"] or "未取得", row["fill"] or "未取得", row["result"] or "未取得",
                row["R"] or "未取得", row["source"]))
        lines.append("")
        win_rate = "未取得" if not done else "{0:.1%}".format(len(wins) / len(done))
        span_days = 3  # 11/10–11/12 才有幣種
        lines.append("勝率（只算已平倉）{0}。已平倉 {1} 筆，總 R {2}。沒成交 {3} 筆。成交欄若是未取得，代表沒有模擬，不是沒成交，也不把未取得當成 0R。".format(
            win_rate, len(done), "未取得" if not done else "{0:.4f}".format(total), len(nofill)))
        lines.append("有幣種的日子是 11/10–11/12 共 {0} 天。多放行 {1} 筆，平均每天 {2:.2f} 筆。11/07–11/09 未取得，沒有拿來除。".format(
            span_days, len(group), len(group) / span_days))
        lines.append("")

    section("1. 瘸師弟：多放行", "瘸師弟")
    changed = [r for r in unique if r["change_item"] == "瘸師弟" and r["diff"] == "掛單價改變"]
    lines.append("### 瘸師弟掛單價改變（偵測仍通過，不是多放行）")
    lines.append("")
    if not changed:
        lines.append("未取得。fapi 沒有回 K 線，無法比較新舊掛單價。")
    else:
        lines.append("共 {0} 筆（去重後）。進場價是 2:3:5 的加權限價，只有三檔都成交才會等於這個價；上面的多放行表若為空，這些單的事後結果在 CSV。".format(len(changed)))
    lines.append("")
    section("2. 布鹿流：多放行", "布鹿流")
    less = [r for r in unique if r["diff"] == "少放行"]
    lines.append("### 少放行")
    lines.append("")
    if not less:
        lines.append("未取得。沒有 K 線，不能判斷新結構條件會不會把舊候選刷掉。")
    else:
        for row in less:
            lines.append("- {0} {1} {2}：{3}".format(row["time"], row["symbol"], row["setup"], row["note"][:160]))
    lines.append("")
    lines.append("## 3. 布鹿流波數")
    lines.append("")
    wave = [r for r in rows_out if r["change_item"] == "波數"]
    if not wave:
        lines.append("視窗內的 decision_log 沒有寫到約 3 波。勝率、總 R、每天筆數：未取得。")
    else:
        lines.append("有 {0} 列提到約 3 波。波數本身未取得，所以沒有改判放行，也沒有模擬。勝率、總 R、每天筆數：未取得。".format(len(wave)))
        for row in wave:
            lines.append("- {0} {1}：{2}".format(row["time"], row["symbol"], row["note"][:200]))
    lines.append("")
    lines.append("## 合計")
    lines.append("")
    all_pass = [r for r in unique if r["diff"] == "多放行"]
    done = closed(all_pass)
    wins = [r for r in done if r["result"] == "目標"]
    total = sum(float(r["R"]) for r in done)
    lines.append("| 項目 | 多放行筆數（去重） | 已平倉 | 勝率 | 總 R | 平均每天 |")
    lines.append("|---|---|---|---|---|---|")
    for item in ("瘸師弟", "布鹿流"):
        group, item_done, item_wins, item_total, _nofill, _days = summarize(item)
        rate = "未取得" if not item_done else "{0:.1%}".format(len(item_wins) / len(item_done))
        rtot = "未取得" if not item_done else "{0:.4f}".format(item_total)
        per = "未取得" if not group else "{0:.2f}".format(len(group) / 3)
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} |".format(item, len(group), len(item_done), rate, rtot, per))
    lines.append("| 波數 | 未取得 | 未取得 | 未取得 | 未取得 | 未取得 |")
    rate = "未取得" if not done else "{0:.1%}".format(len(wins) / len(done))
    rtot = "未取得" if not done else "{0:.4f}".format(total)
    per = "未取得" if not all_pass else "{0:.2f}".format(len(all_pass) / 3)
    lines.append("| 合計（不含波數） | {0} | {1} | {2} | {3} | {4} |".format(len(all_pass), len(done), rate, rtot, per))
    lines.append("")
    lines.append("平均每天的分母是 3（11/10–11/12）。使用者希望至少 1–2 筆。11/07–11/09 若其實有單，這張表看不到。")
    lines.append("")
    lines.append("明細：`replay/out/rule_replay_20261107_12.csv`。")
    md_path = os.path.join(OUT_DIR, "rule_replay_20261107_12.md")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("rows", len(rows_out), "unique", len(unique), "symbols", len(symbols))
    print(md_path)
    print(csv_path)


if __name__ == "__main__":
    main()
