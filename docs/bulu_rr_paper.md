# 布鹿流 RR 1.2–1.5 紙上彙整

實盤門檻仍是 RR ≥ 1.5。這支只讀紀錄，不進排程，不改 `gate.py`。

它從 `gate_log.jsonl` 的 `hardfilter.blocked` 找出型態是布鹿流、而且 RR 在 1.2 以上、不到 1.5 的列。RR 讀 `rr` 欄，沒有就從理由裡的 `RR 1.31` 這類文字讀。進場、停損、前高紀錄裡沒有就寫「未取得」。

`decision_log.jsonl` 用幣種與 `round_time`（對 gate 的 `time`）附上 AI 的決策，不拿它改 RR。

`--bars` 是 `{幣種: [{"h": 高, "l": 低}, ...]}`。同一根低點碰到停損、高點也碰到目標，算停損。沒有這份檔，或沒有價格，勝率與期望值是「未取得」。期望值只對有模擬結果的列：停損記 -1R，碰到目標記 +RR。

```bash
python3 src/bulu_rr_paper.py --gate-log ~/quantboy-gate/gate_log.jsonl \
  --decision-log ~/quantboy-gate/decision_log.jsonl
```

約 2026-11-25 用這支彙整。不部署到 routine。
