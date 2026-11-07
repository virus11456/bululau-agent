# 本機 sender 設定

還沒有任何 URL 或認證。這份程式不會替你安裝，也不會自己試連線。

設定檔目錄：`~/.config/bululau-agent/`（權限 0700）  
設定檔：`~/.config/bululau-agent/sender.json`（權限 0600）  
已保存的測試事件：`~/.config/bululau-agent/trade_candidate_test.json`（權限 0600）

只存兩欄：`webhook_url`、`authorization`。Authorization 的值原樣保存，程式不加 `Bearer`，也不猜別的格式。

在本機終端執行，輸入不會顯示。不要把網址或認證貼到聊天，也不要放進指令參數。

```bash
python3 -m src.phase1.sender_setup configure
```

這一步只寫檔，不發送。

另外再執行才會送出單筆測試。重跑仍用同一個 `event_id`。

```bash
python3 -m src.phase1.sender_setup send-test
```

測試本體只有五欄：`schema_version` 整數 1、`event_type` 為 `trade_candidate_test`、`event_id`、`test_only` 為 true、`sent_at` 為帶時區的 RFC3339。沒有其他欄位。這不是持倉事件通道。HTTP 成功之後狀態仍是 pending，不算處理完成。

非 HTTPS、或內容含換行，會拒絕。重新導向會停下，避免把認證帶到別的網址。失敗訊息不包含網址和認證。

回退：刪掉整個 `~/.config/bululau-agent/`。不要把這個目錄交進 git。
