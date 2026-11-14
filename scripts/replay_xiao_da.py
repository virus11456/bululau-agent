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
        out.append(dict(kind=kind, r=r))
    return out


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
        out.append(dict(kind=kind, r=r, black_t=hit.get("black_t")))
    return out


def summarize(rows):
    wins = sum(1 for r in rows if r["kind"] == "win")
    losses = sum(1 for r in rows if r["kind"] == "loss")
    opens = sum(1 for r in rows if r["kind"] == "open")
    total = round(sum(r["r"] for r in rows), 2)
    n = len(rows)
    return dict(episodes=n, wins=wins, losses=losses, open=opens, r=total,
                win_rate=round(wins / n, 3) if n else None)


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
    summary = dict(
        window="2026-10-14 .. 2026-11-12 UTC",
        source="data.binance.vision",
        fapi="HTTP 451",
        liquid_symbols=len(kept),
        xiao_eth=summarize(eth),
        xiao_other=summarize(other),
        da=summarize(da),
        da_unique_blacks=len({(r.get("symbol"), r.get("black_t")) for r in da}),
        note="小師弟 R：贏 +2、輸 -1。大師兄：3R 出場 +2.5R，2R 平半後停損 +0.5R，沒平半就停損 -1R。",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return summary


if __name__ == "__main__":
    main()
