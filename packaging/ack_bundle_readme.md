# bululau-ack-contract

只含處理 ACK。不 POST、不讀 sender 設定、不含交易程式。`accepted` 不是 `processed`。

操作者自己指定狀態目錄，程式不猜路徑：

```bash
mkdir -p "$HOME/bululau-ack-contract"
tar -xzf bululau-ack-contract.tar.gz -C "$HOME/bululau-ack-contract" --strip-components=1
cd "$HOME/bululau-ack-contract"
python3 -m unittest discover -s . -p 'test_*.py'
```

discover 的測試數必須大於 0。這是離線驗收，不是正式送達。

正式契約目錄由操作者設定，例如：

```bash
export BULULAU_ACK_DIR="$HOME/.config/bululau-ack-contract"
mkdir -p "$BULULAU_ACK_DIR"
chmod 700 "$BULULAU_ACK_DIR"
python3 ack_contract.py apply --state-dir "$BULULAU_ACK_DIR" --file processed-EVENT.json --expect EVENT_ID --sent-at SENT_AT --now NOW
```

consumer 只能在測試處理完成後寫 `kind=processed`。不能把 `kind=accepted` 拿來當 processed。殘留的 `ack_state.json.tmp` 不會被刪，也不會擋住下一次保存。

回退只刪這個解壓目錄，以及操作者自己建的 `$BULULAU_ACK_DIR` 裡這套狀態檔。目錄非空就不要強制刪：

```bash
rm -f -- "$BULULAU_ACK_DIR/ack_state.json"
rmdir -- "$BULULAU_ACK_DIR"
rm -rf -- "$HOME/bululau-ack-contract"
```
