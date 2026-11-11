#!/usr/bin/env python3
"""瘸師弟：舊版與新版 prefilter、hardfilter 對照。預設不連網。

讀 Mac prefilter 紀錄（2026-11-03 起的瘸師弟候選）、gate 紀錄，以及已存 K 線。
K 線有兩種：tests/fixtures/hardfilter_replay_20261110.json 裡 11/10 那批，
以及 tests/fixtures/kuai_single_black_klines_20261111.json。後者若缺，
加 --fetch 才會打幣安公開 /fapi/v1/klines（不簽章、不讀金鑰）。抓不到就標「未取得」。

python3 tests/replay_kuai_single_black.py
python3 tests/replay_kuai_single_black.py --fetch
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import hardfilter  # noqa: E402

PREFILTER_LOG = os.path.join(HERE, "fixtures", "mac_prefilter_log.jsonl")
GATE_LOG = os.path.join(HERE, "fixtures", "mac_gate_log.jsonl")
FIXTURE_1110 = os.path.join(HERE, "fixtures", "hardfilter_replay_20261110.json")
KLINE_STORE = os.path.join(HERE, "fixtures", "kuai_single_black_klines_20261111.json")
RESULT_JSON = os.path.join(HERE, "fixtures", "kuai_single_black_replay_20261111.json")
RESULT_MD = os.path.join(ROOT, "docs", "kuai_single_black_replay_20261111.md")

MAJORS = {"BTCUSDT", "ETHUSDT"}
BLACKLIST = {"INIT", "EWZ", "CAP", "TAG", "APR", "BREV", "XAN", "AIN", "DOOD"}


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def kuai_candidates(rows):
    out = []
    for row in rows:
        when = row.get("time") or ""
        if when < "2026-11-03":
            continue
        for cand in row.get("candidates") or []:
            if cand.get("setup") != "瘸師弟":
                continue
            out.append({"time": when, "candidate": cand})
    return out


def ref_ms(when):
    return int(datetime.fromisoformat(when).timestamp() * 1000)


def bar_label(ms):
    return datetime.fromtimestamp(ms / 1000, hardfilter.TPE).strftime("%m-%d %H:%M")


def black_pct(bar):
    return (bar["o"] / bar["c"] - 1) * 100 if bar["c"] else 0.0


def load_fixture_sets():
    with open(FIXTURE_1110, encoding="utf-8") as handle:
        data = json.load(handle)
    sets = []
    for rec in data.get("klines", []):
        if rec.get("interval") != "15m":
            continue
        sets.append({"symbol": rec["symbol"], "asof_ms": rec["asof_ms"], "rows": rec["rows"],
                     "source": "fixture（11/10 回放檔，當時已重建）"})
    return sets


def load_store():
    if not os.path.exists(KLINE_STORE):
        return []
    with open(KLINE_STORE, encoding="utf-8") as handle:
        data = json.load(handle)
    out = []
    for rec in data.get("series", []):
        out.append({"symbol": rec["symbol"], "asof_ms": rec["end_ms"], "rows": rec["rows"],
                    "source": rec.get("source") or "重建（本次公開 /fapi/v1/klines）"})
    return out


def save_store(fetched):
    payload = {
        "about": "2026-11-11 以幣安公開 /fapi/v1/klines 依各筆 prefilter 時間重抓的 15m。只補 hardfilter_replay_20261110.json 沒有的幣。不簽章。",
        "series": fetched,
    }
    with open(KLINE_STORE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.write("\n")


def fetch_klines_fapi(symbol, end_ms):
    url = "https://fapi.binance.com/fapi/v1/klines?symbol={0}&interval=15m&limit=80&endTime={1}".format(symbol, end_ms)
    req = urllib.request.Request(url, headers={"User-Agent": "gate-hardfilter-replay"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.loads(resp.read())
    if not isinstance(rows, list):
        raise hardfilter.DataError("klines 不是清單")
    return [[r[0], r[1], r[2], r[3], r[4], r[7], r[6]] for r in rows]


_VISION = {}
_FAPI_BLOCKED = {"reason": None}


def _vision_day(symbol, day):
    key = (symbol, day)
    if key in _VISION:
        return _VISION[key]
    url = "https://data.binance.vision/data/futures/um/daily/klines/{0}/15m/{0}-15m-{1}.zip".format(symbol, day)
    req = urllib.request.Request(url, headers={"User-Agent": "gate-hardfilter-replay"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        blob = resp.read()
    rows = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zipped:
        name = zipped.namelist()[0]
        text = zipped.read(name).decode("utf-8")
    for rec in csv.reader(io.StringIO(text)):
        if not rec or rec[0] == "open_time":
            continue
        rows.append([int(rec[0]), rec[1], rec[2], rec[3], rec[4], rec[7], int(rec[6])])
    _VISION[key] = rows
    return rows


def fetch_klines(symbol, end_ms):
    """先試公開 /fapi/v1/klines。這台機器若回 451，改讀 data.binance.vision 的日線檔。"""
    if _FAPI_BLOCKED["reason"] is None:
        try:
            rows = fetch_klines_fapi(symbol, end_ms)
            _FAPI_BLOCKED["reason"] = ""
            return rows, "重建（本次公開 /fapi/v1/klines）"
        except urllib.error.HTTPError as exc:
            if exc.code != 451:
                raise
            _FAPI_BLOCKED["reason"] = "HTTP 451"
        except Exception:
            raise
    elif _FAPI_BLOCKED["reason"] == "":
        return fetch_klines_fapi(symbol, end_ms), "重建（本次公開 /fapi/v1/klines）"
    utc = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    days = [(utc - timedelta(days=1)).date().isoformat(), utc.date().isoformat()]
    rows = []
    for day in days:
        try:
            rows.extend(_vision_day(symbol, day))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    closed = [r for r in rows if int(r[6]) < end_ms]
    if not closed:
        raise hardfilter.DataError("vision 沒有已收 K")
    last_ct = max(int(r[6]) for r in closed)
    if end_ms - last_ct > 16 * 60 * 1000:
        raise hardfilter.DataError("vision 日線還沒涵蓋當時（公開日檔通常次日才有）")
    closed.sort(key=lambda r: r[0])
    return closed[-80:], "重建（fapi 回 451，改以 data.binance.vision 日線檔）"


def pick_rows(symbol, when_ms, bar, sets):
    """優先用時間最接近、而且含該根 K 的序列。"""
    best = None
    for rec in sets:
        if rec["symbol"] != symbol:
            continue
        closed = hardfilter.normalize(rec["rows"], when_ms)
        if not any(bar_label(b["t"]) == bar for b in closed):
            continue
        dist = abs(rec["asof_ms"] - when_ms)
        if best is None or dist < best[0]:
            best = (dist, closed, rec["source"])
    if best is None:
        return None, None
    return best[1], best[2]


def find_idx(k15, bar):
    return next((i for i, b in enumerate(k15) if bar_label(b["t"]) == bar), None)


def prefilter_bar(k15, idx, body_min):
    if k15 is None or idx is None:
        return "未取得", None
    if idx < 20:
        return "未取得", None
    bar = k15[idx]
    avg = sum(x["v"] for x in k15[idx - 20:idx]) / 20
    body = black_pct(bar)
    vol = (bar["v"] / avg) if avg else None
    ok = bool(avg) and bar["v"] >= 3 * avg and body + 1e-6 >= body_min
    return ("放行" if ok else "不放行"), {"black": round(body, 2), "vol20": None if vol is None else round(vol, 2)}


def old_kuai(cand, k15, when_ms):
    """11-10 版：連續至少 2 根。門檻看 big，缺欄位用 2%。訊號根本身不夠則不確定。"""
    if not k15 or len(k15) < 3:
        return "uncertain", "15m K 線不足"
    start = hardfilter._bar_start_ms(cand.get("bar"), when_ms)
    if start is None:
        return "uncertain", "訊號 K 時間讀不到"
    idx = next((i for i, b in enumerate(k15) if b["t"] == start), None)
    if idx is None:
        return "uncertain", "訊號 K 不在已收 K 線裡"
    th = 4.0 if cand.get("big") is False else 2.0
    if black_pct(k15[idx]) + 1e-6 < th:
        return "uncertain", "訊號 K 重算不是長黑"
    run = 1
    j = idx - 1
    while j >= 0 and black_pct(k15[j]) + 1e-6 >= th:
        run += 1
        j -= 1
    j = idx + 1
    while j < len(k15) and black_pct(k15[j]) + 1e-6 >= th:
        run += 1
        j += 1
    if run >= 2:
        return "pass", None
    return "block", "只有 1 根長黑（舊門檻 {0:g}%）".format(th)


def old_overall(cand, k15, when_ms):
    sym = cand.get("symbol")
    if isinstance(sym, str) and sym.endswith("USDT") and sym[:-4] in BLACKLIST:
        return "擋", "黑名單 {0}".format(sym[:-4])
    verdict, why = old_kuai(cand, k15, when_ms)
    if verdict == "block":
        return "擋", why
    if verdict == "uncertain":
        return "不擋（不確定）", why
    return "不擋", why


def new_overall(cand, k15, when_ms):
    def getter(symbol, interval, limit):
        if interval != "15m" or symbol != cand.get("symbol"):
            raise hardfilter.DataError("回放沒有 {0} {1}".format(symbol, interval))
        return k15

    if k15 is None:
        return "未取得", "未取得", "未取得"
    verdict, details = hardfilter.evaluate(cand, getter, when_ms)
    rule, result, why = details[-1]
    if verdict == "block":
        return "擋", rule, why
    if result == "uncertain":
        return "不擋（不確定）", rule, why
    return "不擋", rule, why


def gate_index(rows):
    indexed = []
    for row in rows:
        blocked = ((row.get("hardfilter") or {}).get("blocked")) or []
        indexed.append((row.get("time"), blocked))
    return indexed


def gate_hit(indexed, when, symbol):
    when_ms = ref_ms(when)
    best = None
    for gtime, blocked in indexed:
        if not gtime:
            continue
        dist = abs(ref_ms(gtime) - when_ms)
        if dist > 180000:
            continue
        for item in blocked:
            if item.get("symbol") == symbol and item.get("setup") == "瘸師弟":
                if best is None or dist < best[0]:
                    best = (dist, item.get("rule"), item.get("why"))
    if best is None:
        return "未記錄", ""
    return best[1], best[2]


def compare(cands, sets, gate_rows):
    indexed = gate_index(gate_rows)
    rows = []
    for item in cands:
        cand = item["candidate"]
        when = item["time"]
        when_ms = ref_ms(when)
        symbol = cand.get("symbol")
        bar = cand.get("bar")
        k15, source = pick_rows(symbol, when_ms, bar, sets)
        idx = find_idx(k15, bar) if k15 is not None else None
        if k15 is None or idx is None:
            source = "未取得"
        th_old = 2.0 if cand.get("big") else 4.0
        th_new = 2.0 if symbol in MAJORS else 4.0
        old_pf, metrics = prefilter_bar(k15, idx, th_old)
        new_pf, _metrics = prefilter_bar(k15, idx, th_new)
        if k15 is None:
            old_hf, old_why = "未取得", "未取得"
            new_hf, new_rule, new_why = "未取得", "未取得", "未取得"
            meets = None
        else:
            old_hf, old_why = old_overall(cand, k15, when_ms)
            new_hf, new_rule, new_why = new_overall(cand, k15, when_ms)
            meets = None
            if metrics and metrics["vol20"] is not None:
                meets = metrics["black"] + 1e-9 >= th_new and metrics["vol20"] + 1e-9 >= 3
        live_rule, live_why = gate_hit(indexed, when, symbol)
        note = ""
        if source == "未取得":
            note = "K 線未取得，不猜判定"
        elif old_hf == "擋" and new_hf == "不擋":
            note = "新版多放行"
        elif old_pf == "放行" and new_pf == "不放行":
            note = "新版初篩改擋"
        elif meets is False and new_hf != "擋":
            note = "新版該擋沒擋"
        rows.append({
            "time": when,
            "symbol": symbol,
            "bar": bar,
            "big": cand.get("big"),
            "logged_black": cand.get("black_pct"),
            "logged_vol": cand.get("vol_x"),
            "recalc_black": None if not metrics else metrics["black"],
            "recalc_vol20": None if not metrics else metrics["vol20"],
            "old_prefilter": old_pf,
            "new_prefilter": new_pf,
            "old_hardfilter": old_hf,
            "old_why": old_why,
            "new_hardfilter": new_hf,
            "new_rule": new_rule,
            "new_why": new_why,
            "live_rule": live_rule,
            "live_why": live_why,
            "source": source,
            "meets_original": meets,
            "note": note,
        })
    return rows


def summarize(rows):
    released = [r for r in rows if r["note"] == "新版多放行"]
    tightened = [r for r in rows if r["note"] == "新版初篩改擋"]
    leaked = [r for r in rows if r["note"] == "新版該擋沒擋"]
    missing = [r for r in rows if r["source"] == "未取得"]
    old_prefilter_leak = []
    for r in rows:
        if r["meets_original"] is False and r["old_prefilter"] == "放行":
            old_prefilter_leak.append(r)
    return {
        "n": len(rows),
        "released": released,
        "tightened": tightened,
        "leaked": leaked,
        "missing": missing,
        "old_prefilter_leak": old_prefilter_leak,
    }


def fmt(value):
    if value is None:
        return "未取得"
    return str(value).replace("|", "/")


def write_md(rows, summary):
    lines = []
    lines.append("# 瘸師弟單根長黑回放（2026-11-11）")
    lines.append("")
    lines.append("比較 2026-11-03 起 Mac prefilter 紀錄裡的瘸師弟候選。舊版是連續至少 2 根、門檻看 `big`；新版是瞬間單根，BTCUSDT／ETHUSDT 2%、其餘 4%，長黑與爆量都要成立。爆量倍數沒有改。")
    lines.append("")
    lines.append("K 線來源寫在每一列。`fixture` 是 `hardfilter_replay_20261110.json` 裡已有的重建。其餘先試幣安公開 `/fapi/v1/klines`；這台機器回 HTTP 451，所以改讀 `data.binance.vision` 的 USDT-M 15m 日線檔，只留 `close_time` 早於該筆 prefilter 時間的已收 K。日線檔還沒公布、或沒有涵蓋當時的，列成「未取得」，沒有用紀錄裡的黑 K 幅度代替重算。")
    lines.append("")
    lines.append("## 摘要")
    lines.append("")
    lines.append("- 候選 {0} 筆。K 線未取得 {1} 筆。".format(summary["n"], len(summary["missing"])))
    lines.append("- 新版多放行（舊 hardfilter 擋、新 hardfilter 不擋）{0} 筆。".format(len(summary["released"])))
    lines.append("- 新版初篩改擋（舊 prefilter 會放、新 prefilter 不放）{0} 筆。".format(len(summary["tightened"])))
    lines.append("- 新版該擋沒擋（重算不符合「單根達門檻且前 20 根均量至少 3 倍」，新 hardfilter 卻沒擋）{0} 筆。".format(len(summary["leaked"])))
    lines.append("- 舊初篩放過、但重算不符合上述原文門檻的有 {0} 筆。這些是舊版用 24 小時成交額把小幣長黑放寬到 2% 的結果。".format(len(summary["old_prefilter_leak"])))
    lines.append("")
    if summary["released"]:
        lines.append("### 新版多放行")
        lines.append("")
        for r in summary["released"]:
            lines.append("- {time} {symbol} {bar}：舊硬篩「{old_why}」→ 新硬篩不擋。重算收黑 {recalc_black}% 、前 20 根 {recalc_vol20} 倍。K 線：{source}".format(**r))
        lines.append("")
    if summary["tightened"]:
        lines.append("### 新版初篩改擋")
        lines.append("")
        for r in summary["tightened"]:
            lines.append("- {time} {symbol} {bar}：紀錄收黑 {logged_black}%、量 {logged_vol} 倍、big={big}。重算收黑 {recalc_black}%。舊初篩放行，新初篩不放行。".format(**r))
        lines.append("")
    kept_both = [r for r in rows if r["old_hardfilter"] == "不擋" and r["new_hardfilter"] == "不擋"]
    if kept_both:
        lines.append("### 舊版硬篩本來就沒擋")
        lines.append("")
        lines.append("這些不是新版多放行。重算已符合單根門檻與前 20 根 3 倍量，舊版也沒有因連續根數擋下。")
        lines.append("")
        for r in kept_both:
            lines.append("- {time} {symbol} {bar}：重算收黑 {recalc_black}% 、前 20 根 {recalc_vol20} 倍。".format(**r))
        lines.append("")
    blocked_name = [r for r in rows if r["new_hardfilter"] == "擋" and "黑名單" in (r["new_why"] or "")]
    if blocked_name:
        lines.append("### 黑名單仍擋")
        lines.append("")
        lines.append("長黑條件不是這幾筆的阻擋理由。新舊硬篩都因黑名單擋下。")
        lines.append("")
        for r in blocked_name:
            lines.append("- {time} {symbol} {bar}：{new_why}".format(**r))
        lines.append("")
    if summary["missing"]:
        lines.append("### 未取得")
        lines.append("")
        lines.append("下面只列實盤 gate 當時寫下的舊理由。那是舊版 hardfilter 的紀錄，不是新版重算。")
        lines.append("")
        for r in summary["missing"]:
            live = r["live_why"] or r["live_rule"] or "gate 沒有這筆"
            lines.append("- {time} {symbol} {bar}：未取得。實盤舊紀錄：{live}".format(live=live, **r))
        lines.append("")
    lines.append("## 逐筆")
    lines.append("")
    lines.append("| 時間 | 幣種 | K棒 | 紀錄收黑% | 紀錄量倍 | 重算收黑% | 前20根量倍 | 舊初篩 | 新初篩 | 舊硬篩 | 新硬篩 | 新硬篩原因 | 實盤 gate | K線 | 備註 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append("| {time} | {symbol} | {bar} | {logged_black} | {logged_vol} | {black} | {vol} | {old_prefilter} | {new_prefilter} | {old_hardfilter} | {new_hardfilter} | {why} | {live} | {source} | {note} |".format(
            time=r["time"][5:16],
            symbol=r["symbol"],
            bar=r["bar"],
            logged_black=fmt(r["logged_black"]),
            logged_vol=fmt(r["logged_vol"]),
            black=fmt(r["recalc_black"]),
            vol=fmt(r["recalc_vol20"]),
            old_prefilter=r["old_prefilter"],
            new_prefilter=r["new_prefilter"],
            old_hardfilter=r["old_hardfilter"],
            new_hardfilter=r["new_hardfilter"],
            why=fmt(r["new_why"] or ("—" if r["new_hardfilter"] == "不擋" else r["new_rule"])),
            live=fmt(r["live_rule"]),
            source=r["source"],
            note=r["note"] or "",
        ))
    lines.append("")
    text = "\n".join(lines)
    with open(RESULT_MD, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text


def ensure_fetched(cands, sets, do_fetch):
    have = {(s["symbol"], s["asof_ms"]) for s in sets}
    fetched = []
    if os.path.exists(KLINE_STORE):
        with open(KLINE_STORE, encoding="utf-8") as handle:
            fetched = list(json.load(handle).get("series", []))
    if not do_fetch:
        return sets
    for item in cands:
        cand = item["candidate"]
        symbol = cand.get("symbol")
        when_ms = ref_ms(item["time"])
        bar = cand.get("bar")
        closed, _source = pick_rows(symbol, when_ms, bar, sets)
        if closed is not None:
            continue
        key = (symbol, when_ms)
        if key in have:
            continue
        try:
            rows, source = fetch_klines(symbol, when_ms)
        except Exception as exc:  # 公開資料失敗就留下未取得，不猜
            print("未取得 {0} {1}: {2}".format(symbol, item["time"], exc), file=sys.stderr)
            have.add(key)
            time.sleep(0.2)
            continue
        rec = {"symbol": symbol, "end_ms": when_ms, "rows": rows, "source": source}
        fetched.append(rec)
        sets.append({"symbol": symbol, "asof_ms": when_ms, "rows": rows, "source": rec["source"]})
        have.add(key)
        time.sleep(0.25)
    save_store(fetched)
    return sets


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="缺的 15m 才打幣安公開 K 線")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    cands = kuai_candidates(load_jsonl(PREFILTER_LOG))
    gate_rows = load_jsonl(GATE_LOG)
    sets = load_fixture_sets() + load_store()
    sets = ensure_fetched(cands, sets, args.fetch)
    rows = compare(cands, sets, gate_rows)
    summary = summarize(rows)
    if not args.no_write:
        with open(RESULT_JSON, "w", encoding="utf-8") as handle:
            json.dump({"rows": rows, "summary": {
                "n": summary["n"],
                "released": len(summary["released"]),
                "tightened": len(summary["tightened"]),
                "leaked": len(summary["leaked"]),
                "missing": len(summary["missing"]),
                "old_prefilter_leak": len(summary["old_prefilter_leak"]),
            }}, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        write_md(rows, summary)
    print("候選 {n}，多放行 {released}，初篩改擋 {tightened}，該擋沒擋 {leaked}，未取得 {missing}".format(**{
        "n": summary["n"],
        "released": len(summary["released"]),
        "tightened": len(summary["tightened"]),
        "leaked": len(summary["leaked"]),
        "missing": len(summary["missing"]),
    }))
    return rows, summary


if __name__ == "__main__":
    main()
