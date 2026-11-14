#!/usr/bin/env python3
"""最近 30 天小師弟、大師兄公開 K 線回放。只讀 data.binance.vision，不下單、不登入。

小師弟在訊號收盤進場，停損或 2R 先碰到的那個算結果。
大師兄在二次測前低的收盤進場，2R 平一半、3R 平餘倉；同一根碰到 3R 和 4R 時算 3R。
"""
import csv
import io
import json
import os
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

os.environ.setdefault("QUANTBOY_DATA_DIR", "/tmp/quantboy-replay")
os.makedirs(os.environ["QUANTBOY_DATA_DIR"], exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import prefilter  # noqa: E402

START = int(datetime(2026, 10, 14, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp() * 1000)
MID = START + 15 * 24 * 60 * 60 * 1000
MIN_QV = 5e6
CACHE = os.environ.get("REPLAY_CACHE", "/tmp/vision-cache")
UA = {"User-Agent": "bululau-replay"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read()


def symbols():
    import xml.etree.ElementTree as ET
    base = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    prefix = "data/futures/um/monthly/klines/"
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    marker = ""
    out = []
    while True:
        url = base + "?prefix=" + prefix + "&delimiter=/&max-keys=1000"
        if marker:
            url += "&marker=" + urllib.request.quote(marker, safe="")
        root = ET.fromstring(get(url))
        prefs = root.findall("s:CommonPrefixes", ns)
        for cp in prefs:
            name = cp.find("s:Prefix", ns).text[len(prefix):].strip("/")
            if name.endswith("USDT"):
                out.append(name)
        truncated = root.find("s:IsTruncated", ns)
        if truncated is None or truncated.text != "true":
            break
        nxt = root.find("s:NextMarker", ns)
        marker = nxt.text if nxt is not None else prefs[-1].find("s:Prefix", ns).text
    return out


def parse_zip(blob):
    zf = zipfile.ZipFile(io.BytesIO(blob))
    rows = []
    with zf.open(zf.namelist()[0]) as handle:
        for row in csv.reader(io.TextIOWrapper(handle, encoding="utf-8")):
            if not row or not row[0].isdigit():
                continue
            rows.append(dict(t=int(row[0]), o=float(row[1]), h=float(row[2]), l=float(row[3]),
                             c=float(row[4]), v=float(row[7]), ct=int(row[6])))
    return rows


def fetch(url):
    os.makedirs(CACHE, exist_ok=True)
    name = url.replace("https://", "").replace("/", "_")
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as handle:
            blob = handle.read()
    else:
        try:
            blob = get(url)
        except Exception:
            return []
        with open(path, "wb") as handle:
            handle.write(blob)
    try:
        return parse_zip(blob)
    except Exception:
        return []


def daily_url(sym, interval, day):
    return ("https://data.binance.vision/data/futures/um/daily/klines/{0}/{1}/{0}-{1}-{2}.zip"
            .format(sym, interval, day))


def monthly_url(sym, interval):
    return ("https://data.binance.vision/data/futures/um/monthly/klines/{0}/{1}/{0}-{1}-2026-10.zip"
            .format(sym, interval))


def quote_ok(sym):
    rows = fetch(daily_url(sym, "15m", "2026-11-12"))
    return sym, sum(r["v"] for r in rows)


def load_interval(sym, interval):
    rows = fetch(monthly_url(sym, interval))
    for day in range(1, 13):
        rows.extend(fetch(daily_url(sym, interval, "2026-11-{0:02d}".format(day))))
    rows.sort(key=lambda r: r["t"])
    out, seen = [], set()
    for row in rows:
        if row["t"] in seen:
            continue
        seen.add(row["t"])
        out.append(row)
    return out


def walk(bars, i, entry, stop, target):
    risk = entry - stop
    if risk <= 0:
        return "open", 0.0
    for bar in bars[i + 1:]:
        if bar["t"] >= END:
            break
        if bar["l"] <= stop:
            return "loss", -1.0
        if bar["h"] >= target:
            return "win", 2.0
    last = bars[min(len(bars) - 1, i)]
    return "open", (last["c"] - entry) / risk


def walk_da(bars, i, hit):
    entry, stop = hit["entry"], hit["stop"]
    risk = entry - stop
    if risk <= 0:
        return "open", 0.0
    t2, t3 = hit["target"], hit["target_3r"]
    scaled = False
    for bar in bars[i + 1:]:
        if bar["t"] >= END:
            break
        if bar["l"] <= stop:
            return ("win", 0.5) if scaled else ("loss", -1.0)
        if bar["h"] >= t3:
            return "win", 2.5
        if bar["h"] >= t2:
            scaled = True
    last = bars[min(len(bars) - 1, i)]
    held = (last["c"] - entry) / risk
    if scaled:
        held = 0.5 * 2 + 0.5 * held
    return "open", held


# 訊號那根 15m 已經收完才下單。IOC 用收盤後這幾分鐘的 1m 判斷，不用那根自己的高點。
IOC_FOLLOW_MS = 5 * 60 * 1000
IOC_SLIP = 0.003


def ioc_missed(minutes, close_ms, entry, slip=IOC_SLIP):
    """下單價是 15m 收盤。收盤後 5 分鐘的 1m 最低價仍高於收盤 × 1.003，這筆買不進。"""
    cap = float(entry) * (1 + slip)
    window = [b for b in minutes if close_ms <= b["t"] < close_ms + IOC_FOLLOW_MS]
    if not window:
        return False
    return min(b["l"] for b in window) > cap


def scan_xiao(sym, bars):
    seen = set()
    out = []
    for i in range(40, len(bars)):
        if bars[i]["t"] < START or bars[i]["t"] >= END:
            continue
        hit, _why = prefilter.xiao_shidi(bars[:i + 1], sym)
        if not hit or hit.get("low_t") in seen:
            continue
        seen.add(hit.get("low_t"))
        kind, r = walk(bars, i, hit["entry"], hit["stop"], hit["target"])
        close_ms = int(bars[i].get("ct") or (bars[i]["t"] + 15 * 60 * 1000))
        out.append(dict(kind=kind, r=r, ioc_abandoned=False, t=bars[i]["t"],
                        symbol=sym, ct=close_ms, entry=hit["entry"]))
    return out


def apply_ioc(rows, minutes):
    """用收盤後的 1m 覆寫。超過上限的筆數之後不進勝、敗、未結和 R。"""
    for row in rows:
        if ioc_missed(minutes, row["ct"], row["entry"]):
            row["kind"] = "abandoned"
            row["r"] = 0.0
            row["ioc_abandoned"] = True


def scan_da(sym, bars):
    seen = set()
    out = []
    for i in range(prefilter.DA_MA_SLOW + 10, len(bars)):
        if bars[i]["t"] < START or bars[i]["t"] >= END:
            continue
        hit, _why = prefilter.da_shixiong(bars[:i + 1], sym)
        if not hit or hit.get("black_t") in seen:
            continue
        seen.add(hit.get("black_t"))
        kind, r = walk_da(bars, i, hit)
        close_ms = int(bars[i].get("ct") or (bars[i]["t"] + 4 * 60 * 60 * 1000))
        out.append(dict(kind=kind, r=r, black_t=hit.get("black_t"), t=bars[i]["t"], ct=close_ms))
    return out


def load_btc_daily():
    """9 月整月加上 10 月、11 月，訊號日前才湊得齊 33 根已收日 K。"""
    rows = fetch(("https://data.binance.vision/data/futures/um/monthly/klines/"
                  "BTCUSDT/1d/BTCUSDT-1d-2026-09.zip"))
    rows.extend(load_interval("BTCUSDT", "1d"))
    rows.sort(key=lambda r: r["t"])
    out, seen = [], set()
    for row in rows:
        if row["t"] in seen:
            continue
        seen.add(row["t"])
        out.append(row)
    return out


def apply_da_pause(rows, daily):
    """BTC 日 K 沒有收盤 > MA7 > MA25，或已收日 K 少於 33 根，這筆不進勝敗和 R。"""
    import live_trader as lt
    if not daily:
        return
    for row in rows:
        closed = [b for b in daily if int(b.get("ct") or 0) < int(row.get("ct") or 0)]
        why = lt.da_btc_pause_reason(closed)
        if why:
            row["da_paused"] = True
            row["pause_why"] = why
            row["kind"] = "paused"
            row["r"] = 0.0


def summarize(rows):
    traded = [r for r in rows if not r.get("ioc_abandoned") and not r.get("da_paused")]
    wins = sum(1 for r in traded if r["kind"] == "win")
    losses = sum(1 for r in traded if r["kind"] == "loss")
    opens = sum(1 for r in traded if r["kind"] == "open")
    total = round(sum(r["r"] for r in traded), 2)
    n = len(rows)
    closed = wins + losses
    first = round(sum(r["r"] for r in traded if r.get("t", 0) < MID), 2)
    second = round(sum(r["r"] for r in traded if r.get("t", 0) >= MID), 2)
    return dict(episodes=n, wins=wins, losses=losses, open=opens, r=total,
                win_rate=round(wins / closed, 3) if closed else None,
                ioc_abandoned=sum(1 for r in rows if r.get("ioc_abandoned")),
                da_paused=sum(1 for r in rows if r.get("da_paused")),
                r_first_15d=first, r_second_15d=second)


def main():
    syms = symbols()
    print("symbols", len(syms), flush=True)
    kept = []
    with ThreadPoolExecutor(16) as pool:
        for n, fut in enumerate(as_completed([pool.submit(quote_ok, s) for s in syms]), 1):
            sym, qv = fut.result()
            if qv >= MIN_QV:
                kept.append((sym, qv))
            if n % 200 == 0:
                print("volume", n, "kept", len(kept), flush=True)
    print("liquid", len(kept), flush=True)
    eth, other, da = [], [], []

    def one(item):
        sym, _qv = item
        return sym, scan_xiao(sym, load_interval(sym, "15m")), scan_da(sym, load_interval(sym, "4h"))

    with ThreadPoolExecutor(8) as pool:
        for n, fut in enumerate(as_completed([pool.submit(one, item) for item in kept]), 1):
            sym, xs, ds = fut.result()
            (eth if sym == "ETHUSDT" else other).extend(xs)
            for row in ds:
                row["symbol"] = sym
            da.extend(ds)
            if n % 25 == 0:
                print("scanned", n, flush=True)
    touched = {}
    for row in eth + other:
        touched.setdefault(row["symbol"], []).append(row)
    print("xiao symbols", len(touched), flush=True)
    with ThreadPoolExecutor(8) as pool:
        loaded = pool.map(lambda sym: (sym, load_interval(sym, "1m")), list(touched))
        for sym, minutes in loaded:
            apply_ioc(touched[sym], minutes)
    apply_da_pause(da, load_btc_daily())
    summary = dict(
        window="2026-10-14 .. 2026-11-12 UTC",
        source="data.binance.vision",
        fapi="HTTP 451",
        liquid_symbols=len(kept),
        xiao_eth=summarize(eth),
        xiao_other=summarize(other),
        da=summarize(da),
        da_unique_blacks=len({(r.get("symbol"), r.get("black_t")) for r in da}),
        ioc_price="下單價用 15m 收盤。放棄與否看收盤後 5 分鐘的 1m 最低價是否仍高於收盤 × 1.003。不用訊號那根的高點。成交的 R 仍用 15m 收盤。",
        note="IOC 0.3% 不變。收盤後 5 分鐘內 1m 最低價仍高於收盤 × 1.003 才算放棄，並從勝、敗、未結和 R 扣掉。大師兄另看 BTC 已收日 K：沒有收盤 > MA7 > MA25，或少於 33 根，記 da_paused，同樣不進勝敗和 R。勝率是勝/(勝+敗)。前半是 2026-10-14 到 10-28，後半是 10-29 到 11-12（UTC）。",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return summary


if __name__ == "__main__":
    main()
