# bululau-cycle-ack

把 `run_cycle` 登記的 outbox 和 processed 回條放在同一個 `phase1_state.json`。不 POST，不讀 sender 設定，不碰實盤 `data/`，也不碰獨立 ACK 目錄裡的 `ack_state.json`。

```bash
mkdir -p "$HOME/bululau-cycle-ack"
tar -xzf bululau-cycle-ack.tar.gz -C "$HOME/bululau-cycle-ack" --strip-components=1
cd "$HOME/bululau-cycle-ack"
python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

discover 的測試數必須大於 0。`ai_suggestion=skip` 只出現在真的沒有倉、沒有單、沒有新事件且掃描沒有錯誤的時候。timeout、429、空檔、缺欄、格式錯都不能 skip。`old_ai_poll` 仍是 keep，`ready` 是 false，不會停舊 routine。

shadow runner 不讀金鑰，不 POST，不執行 `live_trader.py` 或 `--live`。正式 notifier 沒配置就是 blocked。

```bash
python3 -m src.phase1.runner run --state-dir "$CYCLE_DIR" --config "$CYCLE_DIR/runner.json" --now TIME --scan-id ID --shadow
python3 -m src.phase1.runner install-schedule --dest "$CYCLE_DIR/schedule" --state-dir "$CYCLE_DIR" --repo "$PWD" --python python3
python3 -m src.phase1.runner rollback-schedule --dest "$CYCLE_DIR/schedule"
```

`install-schedule` 只在指定目錄寫 Disabled 的 plist，不會 load，也不會刪舊 AI 排程。

正式狀態目錄由操作者自建，不要指到實盤資料目錄或 GM 的 ACK 目錄：

```bash
export CYCLE_DIR="$HOME/.config/bululau-cycle-ack"
mkdir -p "$CYCLE_DIR"
chmod 700 "$CYCLE_DIR"
python3 -m src.phase1.cycle apply --state-dir "$CYCLE_DIR" --file processed-EVENT.json --expect EVENT_ID --sent-at SENT_AT --now NOW
```

舊鍵 `cycle:cycle:<id>` 不會在讀取時被清掉。要複製到新鍵：

```bash
python3 -m src.phase1.cycle migrate --state-dir "$CYCLE_DIR"
python3 -m src.phase1.cycle rollback-migration --state-dir "$CYCLE_DIR"
```

回退只刪這個解壓目錄，以及操作者自己的 `$CYCLE_DIR`。目錄裡還有別的檔就不要 `rmdir`。不要刪 `ack_state.json` 那個目錄。

```bash
rm -f -- "$CYCLE_DIR/phase1_state.json" "$CYCLE_DIR/phase1_state.lock" "$CYCLE_DIR/trace.jsonl" "$CYCLE_DIR/snapshot.json"
rmdir -- "$CYCLE_DIR"
rm -rf -- "$HOME/bululau-cycle-ack"
```
