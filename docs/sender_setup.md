# 僅測試 sender

正式設定與送達都還沒驗證。這輪沒有 URL、沒有認證、沒有 POST。

專用目錄只有這兩個檔，回退不要刪整個 `.config`：

- `~/.config/bululau-sender-test/sender.json`（0600）
- `~/.config/bululau-sender-test/trade_candidate_test.json`（0600）

`configure` 只在本機終端用不回顯輸入寫入 HTTPS URL 與 Authorization 原值，不發送。`send-test` 要另外執行。只允許五欄測試事件。HTTP 成功仍是 pending。不跟隨重新導向。非 HTTPS、網址內含帳密、或含換行都會拒絕。

可搬套件的安裝、離線 smoke、配置、送測與回退命令在套件內的 `README.md`。
