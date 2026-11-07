# 1800 秒入口遷移（未安裝）

現有伺服器 routine 仍是 `*/30`、彈性 ±7、enabled。這包不暫停它，也不把命令改成 `review-round`。只改成 `review-round` 仍會先喚 AI，省不了那一次喚醒。

低成本入口是 `python3 -m src.phase1.runner orchestrate --state-dir DIR --config config/orchestrator.example.json --now TIME --scan-id ID`。間隔固定 1800 秒。`schedule_installed` 是 false。`enabled` 與 `execute` 不能改成 true，載入會拒絕。

## 呼叫順序

注入的函式才會被呼叫：`manage` → `status` → `prefilter`。三者都成功、可信、新鮮、0 倉、0 一般單、0 條件單、0 候選，才 quiet，不叫 AI。有倉或任一類單先叫保護 callback。有已知候選且 context 足夠，再叫新倉 callback。同一 `event_id` 可同時帶這兩個需求，`orders_placed` 維持 false。未知 setup、失敗或超時改叫 fallback callback。同一個 `scan_id` 再進一次不重複呼叫。

`manage --live`、`status`、`open --live` 只寫在未啟用契約裡，指向 Mac 的 `~/quantboy-live/live_trader.py`。HALT、401、日虧 7%、累虧 30% 仍由那支 manage 負責。這包不執行。未驗 autoscan 不下單。paper 程式不改。60 秒 watch 與 2 小時老限價規則不改：老限價仍是叫醒原 AI，不撤單。

## 這包沒有的接點

- 沒有函式能喚回 Mac 上的原 prompt／執行器。沒注入 fallback 就只記 `original_ai_callback_unwired`，不印一個假的 keep 當作已經叫過。
- 沒有原 AI 的超時政策，也沒有持倉例外的處理入口。
- notifier 與 gateway 預設不呼叫。不能說平台 AI 已能執行，也不能說已經省下真喚醒。
