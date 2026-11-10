#!/usr/bin/env python3
"""用 11/10 的實際候選回放 hardfilter，印出每個候選：AI 原判定 vs 新判定，以及每輪 QUIET/WAKE 變化。不連網。

python3 tests/replay_hardfilter.py [--rr-target prior_high|plan_target] [--json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
import hardfilter  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "hardfilter_replay_20261110.json")


def replay(rr_target="prior_high", fixture=FIXTURE):
    with open(fixture, encoding="utf-8") as handle:
        data = json.load(handle)
    rows, rounds = [], []
    for rnd in data["rounds"]:
        cands = [c["candidate"] for c in rnd["candidates"]]
        if rnd["prefilter_error"]:
            rounds.append({"time": rnd["gate_time"][11:16], "before": "WAKE(prefilter_failed)", "after": "WAKE(prefilter_failed)"})
            continue
        if not cands:
            rounds.append({"time": rnd["gate_time"][11:16], "before": "QUIET", "after": "QUIET"})
            continue
        from datetime import datetime
        ref_ms = int(datetime.fromisoformat(rnd["prefilter_time"]).timestamp() * 1000)
        getter = hardfilter._fixture_getter(fixture, ref_ms)
        out = hardfilter.run({"candidates": cands}, getter, ref_ms, rr_target)
        blocked = {b["symbol"]: b for b in out["blocked"]}
        for c in rnd["candidates"]:
            cand, ai = c["candidate"], c["ai"]
            b = blocked.get(cand["symbol"])
            new = "擋" if b else "交給AI"
            if ai["blocked_by_hard_rule"]:
                ok = bool(b)
            else:
                ok = not b  # AI 以型態擋、或有疑義：絕不能被程式擋
            rows.append({"time": rnd["gate_time"][11:16], "symbol": cand["symbol"], "setup": cand.get("setup"),
                         "ai": "不進", "ai_category": ai["category"], "ai_quote": ai["quote"],
                         "ai_hard": ai["blocked_by_hard_rule"], "ai_doubtful": ai["doubtful"],
                         "new": new, "new_rule": b["blocked_by"] if b else None, "new_why": b["why"] if b else None,
                         "consistent": ok})
        rounds.append({"time": rnd["gate_time"][11:16], "before": "WAKE(candidates)",
                       "after": "WAKE(candidates)" if out["kept"] else "QUIET（硬條件全擋）"})
    return rows, rounds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rr-target", default="prior_high", choices=("prior_high", "plan_target"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows, rounds = replay(a.rr_target)
    if a.json:
        print(json.dumps({"rows": rows, "rounds": rounds}, ensure_ascii=False, indent=1))
        return
    print("| 時間 | 幣種 | 型態 | AI 原判定與理由 | 新篩選 | 是否一致 |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        new = r["new"] if r["new"] == "交給AI" else "擋：{0}".format(r["new_why"])
        if r["consistent"]:
            agree = "一致"
        elif r["ai_hard"]:
            agree = "不一致（AI 以硬規則擋，新篩選放行給 AI）"
        else:
            agree = "不一致（誤擋）"
        print("| {time} | {symbol} | {setup} | 不進：{ai_quote} | {n} | {g} |".format(n=new, g=agree, **r))
    saved = sum(1 for r in rounds if r["after"].startswith("QUIET") and r["before"].startswith("WAKE"))
    print("\n輪次：", "；".join("{time} {before}→{after}".format(**r) for r in rounds))
    print("多省 {0} 輪 / 共 {1} 輪".format(saved, len(rounds)))


if __name__ == "__main__":
    main()
