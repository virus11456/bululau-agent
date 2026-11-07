# Mac 掃描與 box 判斷

實盤與撈資料留在 Mac。box 只做開發、測試、協調和受控判斷。這份文件不表示已經接通，也不批准 POST。

## 已接上的程式路徑

`runner.json` 設 `mode=production`、`role=mac`。沒寫 `interval_seconds` 就是 3600 秒。小於 3600 直接拒絕，部署不會把掃描調回 15 分鐘。這個角色不另裝排程，也不改 `com.quantboy.livewatch`。

每一輪只做兩件事：

- `src/prefilter.py --quiet --nolog`。掃描日誌若會落到 `~/quantboy-live`，就改寫到週期目錄的 `scanner-cache`。
- `python3 -m src.phase1.account_status`。這是帳戶、一般掛單、條件單三個只讀查詢。不開鎖、不寫 `state.json`、不呼叫 `day_state`。

不啟動 `manage --live`。60 秒的舊 watch、交易所停損和硬風控仍是原來的 `~/quantboy-live/live_trader.py`。

可信、新鮮、三查詢都成功，而且持倉與兩種掛單都是 0：不跑持倉保護 AI。這只省空判斷。有候選時新倉審核仍要跑，不會被空倉規則一起跳掉。查詢失敗、超時或未知維持 `cycle_needs_ai=true`，`old_ai_poll=keep`，不當成空倉。舊 `state.json` 快照不是這三個查詢。

有持倉或掛單時，這一輪仍會要 AI 裁量，但那是同一條每小時週期，不是另一條已驗證的即時持倉事件。`position_ai_event_path` 是 `not_implemented`。

## 還沒有接通的回流

`prepare-handoff` 只在 Mac 寫出上下文檔和清單。`post_authorized` 是 false。程式不代寫 `judgments/<event_id>.json`。判斷要由 box 上的模型寫，再送回原來的 Mac producer 目錄，然後 `commit-judgment` 與 `apply`，並從同一目錄讀回。複製目錄的邏輯 ID 不算。自動跨機傳送沒有實作。

正式 POST 仍未授權。已用完的是 2 個 ID、3 次 POST。新的 1 個 ID、最多 2 次 POST 只是提案。

## 替換前要先有的驗收

原掃描已經是每小時。替換後頻率不回到 15 分鐘。下面沒有完成之前，不替換現行程式，也不把 `switch_allowed` 改成 true。

1. Mac 用既有金鑰跑一次 `account_status`，結束碼 0，三個查詢都是 true，輸出沒有餘額，`~/quantboy-live` 的 state 與鎖檔時間沒有被這次改寫。
2. 同一輪 `role=mac` 在真的零倉零單、無候選時 `protection_ai_skipped=true`，有候選時 `new_entry_ai=true`。
3. `com.quantboy.livewatch` 仍是每 60 秒的舊指令。
4. 回退只刪版本化安裝目錄與自己的 `CYCLE_DIR`，不動舊 root、state、鎖和 watch。
5. box 模型寫回的判斷在原 Mac 目錄 apply 後讀回；這一步目前沒有做過。

這台環境沒有 Mac、沒有交易金鑰、沒有正式接收端，所以上面第 1 與第 5 項不能在這裡宣稱完成。
