# 1800 秒入口遷移（未安裝）

現有伺服器 routine 仍是 `*/30`、彈性 ±7、enabled。這包不暫停它，也不把命令改成 `review-round`。只改成 `review-round` 仍會先喚 AI，省不了那一次喚醒。

低成本入口是 `python3 -m src.phase1.runner orchestrate --state-dir DIR --config config/orchestrator.example.json --now TIME --scan-id ID`。間隔固定 1800 秒。`schedule_installed` 是 false。`enabled` 與 `execute` 不能改成 true，載入會拒絕。

## 呼叫順序

注入的函式才會被呼叫：`manage` → `status` → `prefilter`。quiet 之前，`positions`、`open_orders`、`algo_orders` 都要在，而且是非布林的 int、大於等於 0，三個都是 0，候選是空清單，`halted` 必須剛好是 false。缺欄、None、False 或負數都是未知，改叫 fallback，不開新倉。任何一個明寫的 count 大於 0 就先保護，再 fallback；不因為別的欄未知，或 status 整體失敗，就把那個數字丟掉。`halted` 是 true 時不開新倉，有倉或有單仍先保護。

同一個 `scan_id` 先記 `running`。執行中的第二個 worker 拿不到 claim，不另叫 AI。行程在 manage callback 中途退出後，新的行程看到 `running` 只補 fallback，不重跑 manage。fallback 沒接上或失敗就維持 `running`，不能記成 `recovered`。fallback 成功之後才記 `recovered`。

`TimeoutError` 只在同步 callback 自己丟出時算超時。沒有執行器層的超時。`gateway_enabled` 或 `notifier_verified` 設成 true 會直接拒絕，但這不表示每一個可能的 live 旗標都已列舉。

`manage --live`、`status`、`open --live` 只寫在未啟用契約裡，指向 Mac 的 `~/quantboy-live/live_trader.py`。HALT、401、日虧 7%、累虧 30% 仍由那支 manage 負責。這包不執行。未驗 autoscan 不下單。paper 程式不改。60 秒 watch 與 2 小時老限價規則不改：老限價仍是叫醒原 AI，不撤單。

## 這包沒有的接點

- 沒有函式能喚回 Mac 上的原 prompt／執行器。沒注入 fallback 就只記 `original_ai_callback_unwired`，不印一個假的 keep 當作已經叫過。
- 沒有原 AI 的超時政策，也沒有持倉例外的處理入口。
- notifier 與 gateway 預設不呼叫。不能說平台 AI 已能執行，也不能說已經省下真喚醒。
