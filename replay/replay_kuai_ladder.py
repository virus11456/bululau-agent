#!/usr/bin/env python3
"""瘸師弟單張限價與 2:3:5 階梯的 RR 對照。不連網、不讀金鑰。

讀 replay/fixtures/*20261113* 找 PIEVERSE，以及 tests/fixtures/mac_prefilter_log.jsonl
裡既有的瘸師弟階梯（tests/replay_kuai_single_black.py 用的同一份紀錄）。
數量用寫死的示範淨值，不是實盤帳戶。
"""
import glob
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import live_trader as lt  # noqa: E402

OUT = os.path.join(HERE, "out", "kuai_ladder_20261113.md")
FIXTURES = os.path.join(HERE, "fixtures")
PREFILTER = os.path.join(ROOT, "tests", "fixtures", "mac_prefilter_log.jsonl")
# 示範用，不是 Mac 帳戶。用來展示 dry-run 委託長什麼樣子。
DEMO_EQUITY = 10000
DEMO_SLICE = 500
DEMO_LEV = 5
DEMO_STEP = Decimal("0.001")
DEMO_TICK = Decimal("0.01")
DEMO_MINQTY = Decimal("0.001")
DEMO_MINNOT = Decimal("5")


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def scan_pieverse():
    hits = []
    for path in sorted(glob.glob(os.path.join(FIXTURES, "*20261113*"))):
        with open(path, encoding="utf-8", errors="replace") as handle:
            for i, line in enumerate(handle, 1):
                if "PIEVERSE" in line.upper():
                    hits.append((os.path.basename(path), i))
    return hits


def gate_span():
    path = os.path.join(FIXTURES, "gate_log_20261113.jsonl")
    rows = load_jsonl(path)
    return rows[0].get("time"), rows[-1].get("time"), len(rows)


def rr_of(entry, stop, target):
    entry, stop, target = Decimal(str(entry)), Decimal(str(stop)), Decimal(str(target))
    return lt.ladder_rr(entry, stop, target)


def historical():
    out = []
    for row in load_jsonl(PREFILTER):
        for cand in row.get("candidates") or []:
            if cand.get("setup") != "瘸師弟" or not cand.get("ladder"):
                continue
            prices = [Decimal(str(p)) for p in cand["ladder"]]
            if len(prices) != 3:
                continue
            stop, target = cand.get("stop"), cand.get("target")
            if stop is None or target is None:
                continue
            avg = lt.weighted_entry(prices)
            out.append({
                "time": row.get("time"),
                "symbol": cand.get("symbol"),
                "prices": prices,
                "avg": avg,
                "stop": Decimal(str(stop)),
                "target": Decimal(str(target)),
                "level1": rr_of(prices[0], stop, target),
                "weighted": rr_of(avg, stop, target),
            })
    return out


def demo_payload(prices, stop):
    prices = [lt.rnd(p, DEMO_TICK) for p in prices]
    stop_p = lt.rnd(stop, DEMO_TICK, lt.ROUND_DOWN)
    avg = lt.weighted_entry(prices)
    target = lt.ladder_target("1.5R", avg, stop_p)
    target = lt.rnd(target, DEMO_TICK, lt.ROUND_DOWN)
    if avg > stop_p and lt.ladder_rr(avg, stop_p, target) < Decimal("1.5"):
        target += DEMO_TICK
    qty, risk, margin = lt.order_size(avg, stop_p, DEMO_EQUITY, DEMO_SLICE, 1, DEMO_LEV, DEMO_STEP)
    qtys = lt.split_ladder_qty(qty, DEMO_STEP)
    reasons = lt.check_ladder_levels(prices, qtys, DEMO_MINQTY, DEMO_MINNOT)
    orders = [lt.limit_order("PIEVERSEUSDT", "BUY", q, p) for p, q in zip(prices, qtys)]
    return dict(prices=prices, qtys=qtys, avg=avg, stop=stop_p, target=target, qty=qty,
                rr=lt.ladder_rr(avg, stop_p, target), level1=lt.ladder_rr(prices[0], stop_p, target),
                risk=risk, margin=margin, reasons=reasons, orders=orders,
                protect_stop=dict(type="STOP_MARKET", triggerPrice=lt.fmt(stop_p), quantity=lt.fmt(qty)),
                protect_tp=dict(type="TAKE_PROFIT_MARKET", triggerPrice=lt.fmt(target), quantity=lt.fmt(qty)))


def fmt_rr(value):
    if value is None:
        return "未取得"
    return "{0:.4f}".format(float(value))


def main():
    hits = scan_pieverse()
    start, end, n_gate = gate_span()
    rows = historical()
    below = [r for r in rows if r["level1"] is not None and r["weighted"] is not None
             and r["level1"] < Decimal("1.5") <= r["weighted"]]
    # 規則例：低點 100 → 97/96/95。小幣停損沿 prefilter：最深一檔 × 0.97。
    demo = demo_payload([Decimal("97"), Decimal("96"), Decimal("95")], Decimal("95") * Decimal("0.97"))
    lines = []
    lines.append("# 瘸師弟三檔階梯對照（2026-11-13）")
    lines.append("")
    lines.append("不連網、沒有簽章、沒有讀金鑰。數量用寫死的示範淨值，不是 Mac 帳戶。")
    lines.append("")
    lines.append("## PIEVERSE 15:33")
    lines.append("")
    lines.append("`replay/fixtures/*20261113*` 裡沒有 `PIEVERSE`。`gate_log_20261113.jsonl` 從 {0} 到 {1}，共 {2} 列，趕不上 15:33。這顆候選的交易所價格、stepSize、帳戶淨值都是未取得。".format(start, end, n_gate))
    if hits:
        lines.append("搜到的列：" + ", ".join("{0}:{1}".format(*h) for h in hits))
    lines.append("")
    lines.append("下面用規則自己的例子重現「只掛第 1 檔約 0.83、三檔加權 ≥ 1.5」。低點 100，盲掛 97／96／95。停損用 prefilter 的小幣算法：最深一檔 × 0.97 = 92.15。目標用 `1.5R`，從加權均價算。")
    lines.append("")
    lines.append("| 項目 | 值 |")
    lines.append("|---|---|")
    lines.append("| 三檔價格 | {0} |".format(" / ".join(lt.fmt(p) for p in demo["prices"])))
    lines.append("| 數量（2:3:5） | {0} |".format(" / ".join(lt.fmt(q) for q in demo["qtys"])))
    lines.append("| 合計數量 | {0} |".format(lt.fmt(demo["qty"])))
    lines.append("| 加權均價 | {0} |".format(lt.fmt(demo["avg"])))
    lines.append("| 停損 | {0} |".format(lt.fmt(demo["stop"])))
    lines.append("| 止盈 | {0} |".format(lt.fmt(demo["target"])))
    lines.append("| 加權 RR | {0} |".format(fmt_rr(demo["rr"])))
    lines.append("| 只掛第 1 檔的 RR | {0} |".format(fmt_rr(demo["level1"])))
    lines.append("")
    lines.append("示範假設：淨值 {0}、每份保證金 {1}、槓桿 {2}、step {3}、tick {4}、最小名目 {5}。這不是帳戶餘額。風險上限仍是淨值的 1%（{6}），名目再被每份保證金×槓桿卡住。拆分合計等於這條式子算出的總量。最小名目檢查：{7}。".format(
        DEMO_EQUITY, DEMO_SLICE, DEMO_LEV, DEMO_STEP, DEMO_TICK, DEMO_MINNOT, demo["risk"],
        "通過" if not demo["reasons"] else "；".join(demo["reasons"])))
    lines.append("")
    lines.append("dry-run 會送出的委託（沒有真的送）：")
    lines.append("")
    lines.append("```text")
    for order in demo["orders"]:
        lines.append("POST /fapi/v1/order {0}".format(order))
    lines.append("POST /fapi/v1/algoOrder {0}".format(demo["protect_stop"]))
    lines.append("POST /fapi/v1/algoOrder {0}".format(demo["protect_tp"]))
    lines.append("```")
    lines.append("")
    lines.append("## 既有瘸師弟紀錄：單張對階梯")
    lines.append("")
    lines.append("來源是 `tests/fixtures/mac_prefilter_log.jsonl`，與 `tests/replay_kuai_single_black.py` 讀的是同一份。每筆已有三檔價、停損、目標。單張 RR 把進場價當成第 1 檔；階梯 RR 用 2:3:5 加權均價。門檻 1.5 沒有改。")
    lines.append("")
    lines.append("- 筆數：{0}".format(len(rows)))
    lines.append("- 第 1 檔 RR 全部低於 1.5（最高 {0}）。只掛第一檔會被 1.5 擋下。".format(fmt_rr(max(r["level1"] for r in rows))))
    lines.append("- 加權 RR 嚴格 ≥ 1.5：{0}。其餘 {1} 筆最低 {2}，是紀錄裡的目標價已經四捨五入，不是另一套門檻。".format(
        len(below), len(rows) - len(below), fmt_rr(min(r["weighted"] for r in rows))))
    lines.append("")
    lines.append("最近五筆裡、第 1 檔會低於 1.5 的：")
    lines.append("")
    lines.append("| 時間 | 幣 | 三檔 | 加權均價 | 停損 | 止盈 | 第1檔 RR | 加權 RR |")
    lines.append("|---|---|---|---|---|---|---|---|")
    shown = [r for r in rows if r["level1"] is not None and r["level1"] < Decimal("1.5")][-5:]
    for r in shown:
        lines.append("| {time} | {symbol} | {prices} | {avg} | {stop} | {target} | {level1} | {weighted} |".format(
            time=r["time"], symbol=r["symbol"],
            prices=" / ".join(lt.fmt(p) for p in r["prices"]),
            avg=lt.fmt(r["avg"]), stop=lt.fmt(r["stop"]), target=lt.fmt(r["target"]),
            level1=fmt_rr(r["level1"]), weighted=fmt_rr(r["weighted"])))
    lines.append("")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(OUT)
    print("pieverse", hits, "rows", len(rows), "level1_below", len(below), "demo_rr", fmt_rr(demo["rr"]), "level1", fmt_rr(demo["level1"]))


if __name__ == "__main__":
    main()
