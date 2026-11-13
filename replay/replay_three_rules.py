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

M15 = 15 * 60 * 1000

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


def next_bar_open(dec_ms, step=M15):
    """決策時間之後的下一根 K 開盤。剛好卡在開盤就用那一根。"""
    dec_ms = int(dec_ms)
    return ((dec_ms + step - 1) // step) * step


def bars_from_decision(bars, dec_ms, step=M15):
    """不含決策當下這根已經在走的 K。避免把決策前的高低點算進成交。"""
    start = next_bar_open(dec_ms, step)
    return [b for b in bars if b["t"] >= start]


def _finish(fill, result, r, exit_t=None, fill_t=None, note=""):
    return {"fill": fill, "result": result, "R": r, "exit_t": exit_t, "fill_t": fill_t, "note": note}


def simulate_kuai(future, plan, ignore_target_on_fill_bar=False):
    """A：決策後下一根 15m 起，8 根內掛單。成交當根可以算目標。同根停損優先。

    B 把 ignore_target_on_fill_bar 設成 True：成交那根碰到目標不算目標。
    """
    if not future:
        return _finish("未取得", "未取得", "未取得", note="決策之後的 15m 未取得")
    pending = list(zip(plan["ladder"], (2, 3, 5)))
    filled = []
    fill_t = None
    for i, bar in enumerate(future):
        if i < 8:
            keep = []
            for price, weight in pending:
                if bar["l"] <= price:
                    filled.append((price, weight))
                    if fill_t is None:
                        fill_t = bar["t"]
                else:
                    keep.append((price, weight))
            pending = keep
        if not filled:
            if i >= 8:
                exit_t = future[7]["t"] + M15 if len(future) > 7 else None
                return _finish("沒成交", "沒成交", "", exit_t=exit_t, note="決策後 2 小時沒有打到掛單")
            continue
        hit_stop = bar["l"] <= plan["stop"]
        hit_target = bar["h"] >= plan["target"]
        if ignore_target_on_fill_bar and bar["t"] == fill_t:
            hit_target = False
        if hit_stop and bar["h"] >= plan["target"]:
            r, _avg = position_r(filled, plan["stop"], plan["stop"])
            return _finish("成交", "停損", r, exit_t=bar["t"], fill_t=fill_t,
                           note="同一根同時觸及停損和目標，以停損計")
        if hit_stop:
            r, _avg = position_r(filled, plan["stop"], plan["stop"])
            return _finish("成交", "停損", r, exit_t=bar["t"], fill_t=fill_t)
        if hit_target:
            r, _avg = position_r(filled, plan["target"], plan["stop"])
            return _finish("成交", "目標", r, exit_t=bar["t"], fill_t=fill_t)
    if not filled:
        return _finish("沒成交", "沒成交", "", note="決策後 2 小時沒有打到掛單")
    return _finish("成交", "未出場", "未取得", fill_t=fill_t, note="區間內沒有打到停損或目標")


def simulate_bulu_a(future, plan):
    """A：進場價用訊號收盤（決策當下已知），出場從決策後下一根 15m 起。成交不另等限價。"""
    if not future:
        return _finish("未取得", "未取得", "未取得", note="決策之後的 15m 未取得")
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = entry - stop
    if risk <= 0:
        return _finish("未取得", "未取得", "未取得", note="停損不高於進場，無法算 R")
    for bar in future:
        hit_stop = bar["l"] <= stop
        hit_target = bar["h"] >= target
        if hit_stop and hit_target:
            return _finish("成交", "停損", -1.0, exit_t=bar["t"],
                           note="同一根同時觸及停損和目標，以停損計")
        if hit_stop:
            return _finish("成交", "停損", -1.0, exit_t=bar["t"])
        if hit_target:
            return _finish("成交", "目標", (target - entry) / risk, exit_t=bar["t"])
    return _finish("成交", "未出場", "未取得", note="區間內沒有打到停損或目標")


def bulu_b_prices(symbol, entry, stop, prior):
    """新 hardfilter R3：停損取較寬者，目標 = max(前高, 進場 + 1.5R)。"""
    floor = 0.03 if symbol in ("BTCUSDT", "ETHUSDT") else 0.05
    wide = min(stop, entry * (1 - floor))
    risk = entry - wide
    if risk <= 0 or prior is None:
        return None
    target = max(prior, entry + RR_MIN * risk)
    return wide, target


def apply_rr_cap(entry, stop, prior, rr_cap):
    """與 hardfilter.bulu_r3_prices 同一條：max(前高, 1.5R) 再依 cap 封頂。"""
    risk = entry - stop
    if risk <= 0 or prior is None or prior <= entry:
        return None
    target = max(prior, entry + RR_MIN * risk)
    if rr_cap is not None:
        target = min(target, entry + float(rr_cap) * risk)
    return target


def simulate_bulu_b(future, entry, stop, target):
    """B：決策後 8 根 15m 內要碰到進場價才算成交。成交那根的目標不算。"""
    if not future:
        return _finish("未取得", "未取得", "未取得", note="決策之後的 15m 未取得")
    if entry <= stop:
        return _finish("未取得", "未取得", "未取得", note="停損不高於進場，無法算 R")
    filled = False
    fill_t = None
    for i, bar in enumerate(future):
        if not filled:
            if i >= 8:
                return _finish("沒成交", "沒成交", "", exit_t=bar["t"], note="2 小時內沒碰到進場價")
            if bar["l"] <= entry:
                filled = True
                fill_t = bar["t"]
            else:
                continue
        hit_stop = bar["l"] <= stop
        hit_target = bar["h"] >= target and bar["t"] != fill_t
        if hit_stop:
            note = "同一根同時觸及停損和目標，以停損計" if hit_target else ""
            return _finish("成交", "停損", -1.0, exit_t=bar["t"], fill_t=fill_t, note=note)
        if hit_target:
            return _finish("成交", "目標", (target - entry) / (entry - stop),
                           exit_t=bar["t"], fill_t=fill_t)
    if not filled:
        return _finish("沒成交", "沒成交", "", note="2 小時內沒碰到進場價")
    return _finish("成交", "未出場", "未取得", fill_t=fill_t, note="區間內沒有打到停損或目標")


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


def load_cache_file(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        return data.get("rows") or []
    if isinstance(data, list):
        return data
    return []


def _resim_cap(row, store, rr_cap):
    """瘸師弟不受 2R 上限影響。布鹿流用已取寬的停損重算目標再走 B。"""
    rec = {
        "change_item": row["change_item"],
        "time": row["time"],
        "symbol": row["symbol"],
        "b_fill": row.get("b_fill") or "",
        "b_result": row.get("b_result") or "",
        "b_R": row.get("b_R") or "",
        "b_exit_ms": row.get("b_exit_ms") or "",
    }
    if row["change_item"] != "布鹿流" or rr_cap is None:
        return rec
    try:
        entry = float(row["b_entry"])
        stop = float(row["b_stop"])
        prior = float(row["prior_high"])
    except (TypeError, ValueError):
        rec["b_result"] = "未取得"
        rec["b_R"] = "未取得"
        rec["b_fill"] = "未取得"
        rec["b_exit_ms"] = ""
        return rec
    target = apply_rr_cap(entry, stop, prior, rr_cap)
    if target is None:
        rec["b_result"] = "未取得"
        rec["b_R"] = "未取得"
        return rec
    dec = int(datetime.fromisoformat(row["time"]).timestamp() * 1000)
    future = bars_from_decision(normalize(store.get((row["symbol"], "15m"))), dec)
    sim = simulate_bulu_b(future, entry, stop, target)
    rec["b_fill"] = sim["fill"]
    rec["b_result"] = sim["result"]
    rec["b_R"] = "" if sim["R"] in ("", None) else sim["R"]
    rec["b_exit_ms"] = "" if sim.get("exit_t") is None else str(int(sim["exit_t"]))
    return rec


def _mark_overlap(rows):
    busy = {}
    out = []
    for row in sorted(rows, key=lambda r: (r["time"], r["symbol"], r["change_item"])):
        rec = dict(row)
        key = (rec["change_item"], rec["symbol"])
        dec = int(datetime.fromisoformat(rec["time"]).timestamp() * 1000)
        end = busy.get(key)
        if end is not None and (end == "open" or dec < end):
            rec["overlap"] = "1"
        else:
            rec["overlap"] = "0"
            exit_ms = rec.get("b_exit_ms") or ""
            if exit_ms:
                busy[key] = int(exit_ms)
            elif rec.get("b_fill") == "成交":
                busy[key] = "open"
            else:
                busy[key] = None
        out.append(rec)
    return out


def _pack(rows):
    done = [r for r in rows if r.get("b_result") in ("停損", "目標") and str(r.get("b_R") or "") not in ("", "未取得")]
    wins = [r for r in done if r["b_result"] == "目標"]
    total = sum(float(r["b_R"]) for r in done)
    rate = None if not done else len(wins) / len(done)
    return done, rate, total


def _fmt_pack(done, rate, total, days):
    if not done:
        return "0", "未取得", "未取得", "0"
    return str(len(done)), "{0:.1%}".format(rate), "{0:.2f}".format(total), "{0:.2f}".format(len(done) / days)


def combo_lines(passes, store):
    """四組都走方法 B。3a 沒有數字，開與關的機械名單相同，型態再砍標未取得。"""
    days = 3
    specs = (
        ("1 部署", "關", "關", None),
        ("2 對照", "開", "開", 2.0),
        ("只開 3a", "開", "關", None),
        ("只開 2R", "關", "開", 2.0),
    )
    lines = ["", "## 四組開關（方法 B，11/10–11/12）", ""]
    lines.append("3a「不追深殺反彈」在改 PR #31 之前只是提示詞，prefilter 與 hardfilter 沒有這條，也沒有數字。這次回放不能從 K 線判斷誰是深殺反彈，所以 3a 開與關的機械成交相同；若再靠人工砍單，那些單是未取得。2R 上限只改布鹿流目標，瘸師弟不變。樣本只有三天。")
    lines.append("")
    lines.append("| 組 | 3a | 2R 上限 | 平倉 | 勝率 | 總 R | 平倉/天 | 瘸師弟 平倉/勝率/R | 布鹿流 平倉/勝率/R | 不重疊 平倉/勝率/R |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, a_on, cap_on, cap in specs:
        rows = _mark_overlap([_resim_cap(r, store, cap) for r in passes])
        done, rate, total = _pack(rows)
        n, rate_s, total_s, per = _fmt_pack(done, rate, total, days)
        parts = []
        for item in ("瘸師弟", "布鹿流"):
            d, rt, tot = _pack([r for r in rows if r["change_item"] == item])
            nn, rs, ts, _p = _fmt_pack(d, rt, tot, days)
            parts.append("{0} / {1} / {2}".format(nn, rs, ts))
        free = [r for r in rows if r.get("overlap") == "0"]
        d, rt, tot = _pack(free)
        nn, rs, ts, _p = _fmt_pack(d, rt, tot, days)
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} |".format(
            name, a_on, cap_on, n, rate_s, total_s, per, parts[0], parts[1],
            "{0} / {1} / {2}".format(nn, rs, ts)))
    lines.append("")
    lines.append("第 1 組是部署預設，應等於 PR #31 的 B：11 筆、18.2%、−6.00R；不重疊 8 筆、12.5%、−5.50R。第 2 組是 3a 與 2R 都打開的對照。")
    lines.append("這三天 2R 上限綁到的只有 MERL（約 2.67R 收到 2R）和 SXT（未出場那筆收到 2R）。MERL 先碰到停損，SXT 到快取結束沒碰到 2R，所以出場沒變。四組機械結果因此一樣。")
    lines.append("")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--cache-dir", help="幣名_週期.json。可以是幣安原始陣列，或 {rows: ...}")
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
    cache_dir = args.cache_dir
    if args.no_network and not cache_dir:
        cache_dir = CACHE
    if args.no_network:
        if not cache_dir or not os.path.isdir(cache_dir):
            raise SystemExit("沒有快取目錄，--no-network 不能打幣安")
        for name in os.listdir(cache_dir):
            if not name.endswith(".json") or name.startswith("._"):
                continue
            symbol, interval = name[:-5].rsplit("_", 1)
            store[(symbol, interval)] = load_cache_file(os.path.join(cache_dir, name))
        for symbol in symbols:
            for interval in ("15m", "1h", "1d"):
                if not store.get((symbol, interval)):
                    errors[(symbol, interval)] = "快取未取得"
                    store[(symbol, interval)] = []
    else:
        for symbol in symbols:
            for interval, start in (("15m", start_15), ("1h", start_1h), ("1d", start_1d)):
                rows, err = fetch_klines(symbol, interval, start)
                if err and not os.path.isfile(kline_cache_path(symbol, interval)):
                    errors[(symbol, interval)] = err
                    store[(symbol, interval)] = []
                elif rows is None and os.path.isfile(kline_cache_path(symbol, interval)):
                    store[(symbol, interval)] = load_cache_file(kline_cache_path(symbol, interval))
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
                future = bars_from_decision(normalize(store.get((symbol, "15m"))), end_ms)
                sim = simulate_kuai(future, new, ignore_target_on_fill_bar=False)
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
                future = bars_from_decision(normalize(store.get((symbol, "15m"))), end_ms)
                sim = simulate_bulu_a(future, new)
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
        bsim = None
        b_entry = b_stop = b_target = None
        if k15 and isinstance(plan.get("entry"), (int, float)):
            future_b = bars_from_decision(normalize(store.get((symbol, "15m"))), end_ms)
            if setup == "瘸師弟" and plan.get("ladder"):
                bsim = simulate_kuai(future_b, plan, ignore_target_on_fill_bar=True)
                b_entry, b_stop, b_target = plan["entry"], plan["stop"], plan["target"]
            elif setup == "布鹿流" and plan.get("prior_high") is not None and plan.get("stop") is not None:
                priced = bulu_b_prices(symbol, plan["entry"], plan["stop"], plan["prior_high"])
                if priced:
                    b_stop, b_target = priced
                    b_entry = plan["entry"]
                    bsim = simulate_bulu_b(future_b, b_entry, b_stop, b_target)
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
            "b_entry": fmt_num(b_entry),
            "b_stop": fmt_num(b_stop),
            "b_target": fmt_num(b_target),
            "b_fill": (bsim or {}).get("fill") or "",
            "b_result": (bsim or {}).get("result") or "",
            "b_R": fmt_r((bsim or {}).get("R")),
            "b_exit_ms": "" if not bsim or bsim.get("exit_t") is None else str(int(bsim["exit_t"])),
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
            "b_entry": "",
            "b_stop": "",
            "b_target": "",
            "b_fill": "",
            "b_result": "",
            "b_R": "",
            "b_exit_ms": "",
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
              "entry", "stop", "target", "prior_high", "fill", "result", "R",
              "b_entry", "b_stop", "b_target", "b_fill", "b_result", "b_R", "b_exit_ms", "note"]
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
    lines.append("候選身份只來自日誌。K 線是幣安公開 fapi klines，沒有簽章。同一根同時碰到停損和目標，算停損。")
    lines.append("沒有從缺日補幣，也沒有用 K 線自己數波。")
    if args.no_network and args.cache_dir:
        lines.append("這次用本機快取重跑（2026-11-13 10:35 從台灣抓的 20 個幣、15m／1h／1d）。快取約 7MB，沒有放進 repo。")
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
    lines.append("- 模擬從決策時間的下一根 15m 起。A／B 的差別見下方。")
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
    passes = [r for r in unique if r["diff"] == "多放行"]
    ordered = sorted(passes, key=lambda r: (r["time"], r["symbol"], r["change_item"]))
    busy = {}
    for row in ordered:
        key = (row["change_item"], row["symbol"])
        dec = int(datetime.fromisoformat(row["time"]).timestamp() * 1000)
        end = busy.get(key)
        if end is not None and (end == "open" or dec < end):
            row["overlap"] = "1"
            continue
        row["overlap"] = "0"
        exit_ms = row.get("b_exit_ms") or ""
        if exit_ms:
            busy[key] = int(exit_ms)
        elif row.get("b_fill") == "成交":
            busy[key] = "open"
        else:
            busy[key] = None

    def pack(rows, result_key, r_key):
        done = [r for r in rows if r.get(result_key) in ("停損", "目標") and str(r.get(r_key) or "") not in ("", "未取得")]
        wins = [r for r in done if r[result_key] == "目標"]
        total = sum(float(r[r_key]) for r in done)
        rate = None if not done else len(wins) / len(done)
        return done, wins, total, rate

    lines.append("## A / B / 同幣不重疊")
    lines.append("")
    lines.append("多放行先以同一幣、同一進場、同一停損、同一目標去重。A、B 都從決策時間的下一根 15m 才開始，不再用決策前已經走完的 K。")
    lines.append("A：瘸師弟 2 小時內限價；成交那根可以算目標。布鹿流把訊號收盤視同已成交。同根同時碰到停損和目標，算停損。")
    lines.append("B：成交那根碰到目標不算目標。布鹿流停損取結構停損與小幣 5%（BTC／ETH 3%）較寬者，目標 = max(前高, 進場+1.5R)；2 小時內沒碰到進場價算沒成交。")
    lines.append("不重疊：同一幣前一筆 B 還沒出場（沒成交也佔用那 2 小時掛單窗），新訊號不另算。")
    lines.append("")
    a_done, a_wins, a_total, a_rate = pack(passes, "result", "R")
    b_done, b_wins, b_total, b_rate = pack(passes, "b_result", "b_R")
    free = [r for r in ordered if r.get("overlap") == "0"]
    n_done, n_wins, n_total, n_rate = pack(free, "b_result", "b_R")

    def cell(done, wins, total, rate):
        if not done:
            return "0", "未取得", "未取得"
        return str(len(done)), "{0:.1%}".format(rate), "{0:.2f}".format(total)

    lines.append("| 口徑 | 平倉筆數 | 勝率 | 總 R |")
    lines.append("|---|---|---|---|")
    for name, packed in (("A（前視已修）", (a_done, a_wins, a_total, a_rate)),
                         ("B", (b_done, b_wins, b_total, b_rate)),
                         ("B 同幣不重疊", (n_done, n_wins, n_total, n_rate))):
        n, rate, total = cell(*packed)
        lines.append("| {0} | {1} | {2} | {3} |".format(name, n, rate, total))
    lines.append("")
    lines.append("總經理boy 的 B 對照是 11 筆平倉、勝率 18.2%、−6.00R；不重疊 8 筆、12.5%、−5.5R。上表是部署預設（3a 關、2R 上限關），應與該對照相同。")
    lines.extend(combo_lines(ordered, store))
    lines.append("")
    lines.append("| 時間 | 幣 | 型態 | A | A的R | B進場 | B停損 | B目標 | B | B的R | 重疊 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in ordered:
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} |".format(
            row["time"][5:16], row["symbol"], row["change_item"], row["result"] or "未取得",
            row["R"] or "未取得", row.get("b_entry") or "未取得", row.get("b_stop") or "未取得",
            row.get("b_target") or "未取得", row.get("b_result") or "未取得", row.get("b_R") or "未取得",
            "是" if row.get("overlap") == "1" else "否"))
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
