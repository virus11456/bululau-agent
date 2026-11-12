#!/usr/bin/env python3
"""布鹿流 RR 1.2–1.5 的紙上彙整。只讀 gate_log、decision_log，不改實盤門檻。

實盤仍是 RR ≥ 1.5 才留下。這支把 hardfilter 擋下、且 RR 落在 [1.2, 1.5) 的布鹿流
候選整理成表。進場、停損、前高不在紀錄裡就寫「未取得」，不猜。

選擇性的 K 線檔用來事後模擬：同一根先看停損。低點碰到停損且高點碰到目標，算停損。
沒有 K 線或沒有價格時，勝率與期望值寫「未取得」。

不進排程，不呼叫 gate，不連網，不讀金鑰。

用法：
  python3 src/bulu_rr_paper.py --gate-log ~/quantboy-gate/gate_log.jsonl
  python3 src/bulu_rr_paper.py --gate-log gate_log.jsonl --decision-log decision_log.jsonl --bars bars.json
"""
import argparse
import json
import re
import sys

LIVE_RR = 1.5
BAND_LOW = 1.2
UNKNOWN = "未取得"
_RR_RE = re.compile(r"RR\s+([0-9]+(?:\.[0-9]+)?)")


def _load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def parse_rr(item):
    direct = _num(item.get("rr") if isinstance(item, dict) else None)
    if direct is not None:
        return direct
    why = item.get("why") if isinstance(item, dict) else None
    if not isinstance(why, str):
        return None
    match = _RR_RE.search(why)
    if not match:
        return None
    return float(match.group(1))


def in_paper_band(rr):
    return rr is not None and BAND_LOW <= rr < LIVE_RR


def _price(item, *keys):
    for key in keys:
        number = _num(item.get(key))
        if number is not None:
            return number
    return None


def iter_blocked(gate_rows):
    for row in gate_rows:
        if not isinstance(row, dict):
            continue
        hardfilter = row.get("hardfilter") or {}
        if not isinstance(hardfilter, dict):
            continue
        for item in hardfilter.get("blocked") or []:
            if isinstance(item, dict):
                yield row.get("time"), item


def collect(gate_rows, decisions):
    """回傳紙上列。decision_log 只附註，不改 RR。"""
    notes = {}
    for row in decisions or []:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        when = row.get("round_time")
        if isinstance(symbol, str) and isinstance(when, str):
            notes[(symbol.upper(), when)] = {
                "decision": row.get("decision"),
                "reason": row.get("reason"),
            }
    out = []
    for when, item in iter_blocked(gate_rows):
        setup = item.get("setup")
        if setup != "布鹿流":
            continue
        rr = parse_rr(item)
        if not in_paper_band(rr):
            continue
        symbol = item.get("symbol")
        entry = _price(item, "entry")
        stop = _price(item, "stop")
        target = _price(item, "prior_high", "target")
        note = notes.get((str(symbol).upper(), when), {}) if isinstance(symbol, str) and isinstance(when, str) else {}
        out.append({
            "time": when if isinstance(when, str) else UNKNOWN,
            "symbol": symbol if isinstance(symbol, str) else UNKNOWN,
            "setup": "布鹿流",
            "rule": item.get("rule") or UNKNOWN,
            "entry": entry if entry is not None else UNKNOWN,
            "stop": stop if stop is not None else UNKNOWN,
            "prior_high": target if target is not None else UNKNOWN,
            "rr": rr,
            "decision": note.get("decision", UNKNOWN),
            "decision_reason": note.get("reason", UNKNOWN),
            "outcome": UNKNOWN,
            "r_result": UNKNOWN,
        })
    return out


def simulate(row, bars):
    """同一根同時碰到停損與目標，算停損。沒有價格或 K 線就留未取得。"""
    stop = row.get("stop")
    target = row.get("prior_high")
    symbol = row.get("symbol")
    if stop == UNKNOWN or target == UNKNOWN or not isinstance(symbol, str):
        return
    series = (bars or {}).get(symbol) or []
    for bar in series:
        if not isinstance(bar, dict):
            continue
        low = _num(bar.get("l"))
        high = _num(bar.get("h"))
        if low is None or high is None:
            continue
        hit_stop = low <= stop
        hit_target = high >= target
        if hit_stop:
            row["outcome"] = "stop"
            row["r_result"] = -1.0
            return
        if hit_target:
            row["outcome"] = "target"
            row["r_result"] = row["rr"]
            return


def summarize(rows):
    done = [row for row in rows if isinstance(row.get("r_result"), float)]
    wins = [row for row in done if row["outcome"] == "target"]
    if not done:
        win_rate = UNKNOWN
        expectancy = UNKNOWN
    else:
        win_rate = round(len(wins) / len(done), 4)
        expectancy = round(sum(row["r_result"] for row in done) / len(done), 4)
    return {
        "band": [BAND_LOW, LIVE_RR],
        "live_threshold": LIVE_RR,
        "n": len(rows),
        "simulated": len(done),
        "outcomes_missing": len(rows) - len(done),
        "win_rate": win_rate,
        "expectancy_r": expectancy,
        "rows": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="布鹿流 RR 1.2–1.5 紙上彙整（只讀）")
    parser.add_argument("--gate-log", required=True)
    parser.add_argument("--decision-log")
    parser.add_argument("--bars", help="JSON：{幣種: [{h, l}, ...]}。不讀就不模擬。")
    args = parser.parse_args(argv)
    try:
        gate_rows = _load_jsonl(args.gate_log)
        decisions = _load_jsonl(args.decision_log) if args.decision_log else []
        bars = {}
        if args.bars:
            with open(args.bars, encoding="utf-8") as handle:
                bars = json.load(handle)
    except (OSError, ValueError) as exc:
        print("bulu_rr_paper: {0}".format(exc), file=sys.stderr)
        return 2
    rows = collect(gate_rows, decisions)
    for row in rows:
        simulate(row, bars if isinstance(bars, dict) else {})
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
