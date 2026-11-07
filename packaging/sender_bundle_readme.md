# bululau-sender-test

只含測試 sender。沒有交易程式、沒有交易金鑰、沒有排程。正式 URL 與送達尚未驗證。設定不會自動發送。

解壓後目錄就是本套件。離線 smoke 不需要 clone 原倉庫。

```bash
mkdir -p "$HOME/bululau-sender-test"
tar -xzf bululau-sender-test.tar.gz -C "$HOME/bululau-sender-test" --strip-components=1
cd "$HOME/bululau-sender-test"
python3 sender_test.py
```

`sender_test.py` 結束碼 0 才算離線通過。它不讀你家裡的設定，也不外發。

安全配置（只寫檔）：

```bash
python3 sender_setup.py configure
```

輸入不顯示。不要把網址或 Authorization 放進指令參數或聊天。

人工單筆送測（另外啟動；此套件本身不會替你執行）：

```bash
python3 sender_setup.py send-test
```

回退只刪本套件與這兩個測試檔。目錄非空時 `rmdir` 會失敗，其他檔會留下。

```bash
rm -f -- "$HOME/.config/bululau-sender-test/sender.json" \
        "$HOME/.config/bululau-sender-test/trade_candidate_test.json"
rmdir -- "$HOME/.config/bululau-sender-test"
rm -rf -- "$HOME/bululau-sender-test"
```
