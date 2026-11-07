# 正式喚醒傳輸與消費

這不是五欄 `trade_candidate_test`，也不使用 `~/.config/bululau-sender-test/`。POST 預算不共用。本文件不執行發送。

## 通知與上下文分開

八欄 wake 只通知接收端有一筆評估。它沒有快照、候選或時效。AI 判斷必須再讀同一個狀態目錄裡的固定檔 `evaluation_envelope.json`。不能另給 payload 路徑，也不能讀金鑰或餘額。

`event_id` 在 `wake_gate._enqueue_cycle` 產生，形狀 `c-` 加 32 個十六進位，寫進 `phase1_state.json` 的 outbox 後不覆寫。`evaluation_id` 就是通過 `^[A-Za-z0-9_-]{1,128}$` 的 `scan_id`。outbox 鍵是 `cycle:<evaluation_id>`。`sent_at` 是 runner 產生的 UTC 時間。

允許的 `reason` 只有：

- `snapshot_missing`
- `snapshot_stale`
- `snapshot_blocked_clock_skew`
- `snapshot_unknown`
- `manage_or_scan_error`
- `positions_open`
- `orders_open`
- `candidates_present`
- `not_quiet`

## 傳輸

設定目錄由操作者指定，檔名固定 `transport.json`，只有 `webhook_url` 與 `authorization`。URL 必須是 HTTPS。Authorization 原樣使用，程式不加 `Bearer`。不跟隨重新導向。

```bash
python3 -m src.phase1.production_transport configure
python3 -m src.phase1.production_transport send-wake --config-dir DIR --state-dir "$CYCLE_DIR"
```

`send-wake` 只 POST 這八欄：

```json
{"schema_version":1,"event_type":"evaluation_wake","event_id":"c-…","evaluation_id":"s1","sent_at":"2026-11-07T04:20:00Z","reason":"positions_open","test_channel":false,"position_channel":false}
```

HTTP 2xx 的結果是 `delivered=true`、`processing_ack=false`、`pending=true`。重送沿用同一個 `event_id`。

## 讀 envelope

```bash
python3 -m src.phase1.production_transport read-envelope --state-dir "$CYCLE_DIR" --event-id EVENT --evaluation-id EVAL
```

只讀 `$CYCLE_DIR/evaluation_envelope.json`，並核對 outbox 上的 `envelope_sha256`。目錄裡若有實盤 `state.json` 或測試用 `ack_state.json`，拒絕。`limit_older_than_2h_implemented` 固定 false：有掛單已經會留 AI，這不是已實作的老單訊號。

## 消費

收到 wake 不能寫 processed。接收端把 dry-run 判斷寫進 `$CYCLE_DIR/dry_run_judgment.json`，而且 `judgment_completed` 必須是 true、`orders_placed` 必須是 false、`decision` 只能是 `keep_original_ai` 或 `no_trade`。然後：

```bash
python3 -m src.phase1.production_transport commit-judgment --state-dir "$CYCLE_DIR"
python3 -m src.phase1.cycle apply --state-dir "$CYCLE_DIR" --file "$CYCLE_DIR/ack-inbox/processed-EVENT.json" --expect EVENT --sent-at SENT_AT --now NOW
```

`processed_at` 不得早於 `sent_at`，也不得比 apply 的 now 早超過 15 分鐘或晚超過 60 秒。對不上、判斷沒完成、或寫檔失敗，都不寫 processed，`old_ai_poll` 維持 keep。

## 有界 dry-run 驗收

這份計畫給父側，不在這裡執行，也不新建 routine。

- 與已用完的 3 次測試 POST 分開計。上限是 1 次新的 wake POST，加 1 次同一 `event_id` 的重送。第 3 次 POST 就停。
- 只做 AI 判斷，不開倉、不平倉、不撤單。`orders_placed` 必須維持 false。
- 接收端先收八欄，再讀同一狀態目錄的 envelope，判斷完成後才 `commit-judgment`。重開 consumer 再 apply 同一檔必須是 duplicate，`process_count` 停在 1。
- 停止條件：任何下單、任何秘密進日誌、判斷完成前就寫 processed、envelope 不是那個固定檔、把 HTTP 2xx 當成 processed、或超過 2 次 POST。
- 持倉、逾 2 小時限價、結構轉弱、移動停損仍走原 manage／watch／AI。`switch_allowed` 與 `account_verified` 不能由設定檔改成 true。

## status-json

`live_trader.py status` 的文字裡，`open_algo()` 失敗會變成空清單並且整支程式仍可能結束碼 0。`multiAssetsMargin` 失敗也會被接住後結束碼 0。所以「時間 + 持倉 0 + 掛單 0」不能證明查詢成功；後面再出現「查詢失敗 HTTP 429」仍必須拒絕。

production 改走只讀的 `status-json`。三個查詢 `account`、`open_orders`、`open_algo_orders` 都必須是 true，`ok` 必須是 true，結束碼必須是 0，而且輸出不得含餘額或失敗文字。任一失敗結束碼 2，且不印零倉冒充成功。時鐘新鮮不能代替這個契約。
