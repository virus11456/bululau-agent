# 正式喚醒傳輸與消費

這不是五欄 `trade_candidate_test`，也不使用 `~/.config/bululau-sender-test/`。POST 預算不共用。本文件不執行發送。

## 通知與上下文分開

八欄 wake 只通知接收端有一筆評估。它沒有快照、候選或時效。每一筆的上下文寫在 `$CYCLE_DIR/envelopes/<event_id>.json`。寫入先落在同目錄的暫存檔並 fsync，再換上最終檔名。換檔前中斷不會把半成品當成可信 envelope，也不會蓋掉已有的有效摘要。這是行程中斷測試，不是斷電測試。新的 scan 不會改掉舊 pending 的檔。

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
python3 -m src.phase1.production_transport send-wake --config-dir DIR --state-dir "$CYCLE_DIR" --event-id EVENT --evaluation-id EVAL --now TIME
```

runner 的 notifier 把八欄 JSON 從 stdin 交給：

```bash
python3 -m src.phase1.production_transport send-wake-stdin --config-dir DIR --state-dir "$CYCLE_DIR" --now TIME
```

stdin 必須和該事件 envelope 導出的八欄一致。設定錯誤、解析錯誤與傳送失敗只回固定 reason，不印 URL query 或 Authorization。`delivered=true` 之後 outbox 仍是 pending。

`send-wake` 只 POST 這八欄：

```json
{"schema_version":1,"event_type":"evaluation_wake","event_id":"c-…","evaluation_id":"s1","sent_at":"2026-11-07T04:20:00Z","reason":"positions_open","test_channel":false,"position_channel":false}
```

HTTP 2xx 的結果是 `delivered=true`、`processing_ack=false`、`pending=true`。重送沿用同一個 `event_id`。

## 讀 envelope

```bash
python3 -m src.phase1.production_transport read-envelope --state-dir "$CYCLE_DIR" --event-id EVENT --evaluation-id EVAL
```

只讀 `$CYCLE_DIR/envelopes/<event_id>.json`，並核對該 outbox 項的 `envelope_sha256`。目錄裡若有實盤 `state.json` 或測試用 `ack_state.json`，拒絕。`limit_older_than_2h_implemented` 固定 false：有掛單已經會留 AI，這不是已實作的老單訊號。

## 消費

收到 wake 不能寫 processed。布林 `judgment_completed=true` 也不能證明模型真的跑過；本程式不核對父側的 consumer 身分。父側若要可信完成收據，須另定簽章（至少覆蓋 event_id、evaluation_id、envelope_sha256、completed_at）。在那之前 `ai_execution_proven` 必須是 false，processed 只表示這份可稽核判斷被接受。

每筆判斷各寫 `$CYCLE_DIR/judgments/<event_id>.json`。一次 commit 只處理指定的 event_id，不會清掉另一筆 pending，也不會拿 A 的摘要去配 B。本地能寫狀態目錄的人可以做出符合欄位的判斷檔；這是信任邊界。`ai_execution_proven` 必須維持 false，本程式不另造執行證明。正式 consumer 由父側控制。

判斷檔：

```json
{"schema_version":1,"event_type":"dry_run_judgment","event_id":"c-…","evaluation_id":"s1","envelope_sha256":"<envelope 摘要>","decision":"keep_original_ai","basis":{"reason":"positions_open","as_of":"…","positions":1,"orders":0,"freshness_status":"fresh","candidate_count":1},"completed_at":"2026-11-07T04:22:00Z","orders_placed":false,"ai_execution_proven":false}
```

`decision` 只能是 `keep_original_ai` 或 `no_trade`。`basis` 必須和該事件 envelope 一致。`completed_at` 不得早於 `sent_at`，也不得比 `--now` 早超過 15 分鐘或晚超過 60 秒。錯摘要、舊時間、或聲稱 `ai_execution_proven=true` 都拒絕，且不寫 processed。

然後：

```bash
python3 -m src.phase1.production_transport commit-judgment --state-dir "$CYCLE_DIR" --now NOW
python3 -m src.phase1.cycle apply --state-dir "$CYCLE_DIR" --file "$CYCLE_DIR/ack-inbox/processed-EVENT.json" --expect EVENT --sent-at SENT_AT --now NOW
```

`processed_at` 不得早於 `sent_at`，也不得比 apply 的 now 早超過 15 分鐘或晚超過 60 秒。對不上、判斷沒完成、或寫檔失敗，都不寫 processed，`old_ai_poll` 維持 keep。

## 有界 dry-run 驗收

這份計畫給父側，不在這裡執行，也不新建 routine。

- 與已用完的 2 個 ID、3 次測試 POST 分開。這次建議只新增 1 個 event_id，最多 2 次 POST（1 次送出加 1 次同 ID 重送）。第 3 次就停。正式範圍由 GM 向本人確認後才做。
- 只做 AI 判斷，不開倉、不平倉、不撤單。`orders_placed` 必須維持 false。
- 接收端先收八欄，再讀同一狀態目錄的 envelope，判斷完成後才 `commit-judgment`。重開 consumer 再 apply 同一檔必須是 duplicate，`process_count` 停在 1。
- 停止條件：任何下單、任何秘密進日誌、判斷完成前就寫 processed、envelope 不是那個固定檔、把 HTTP 2xx 當成 processed、或超過 2 次 POST。
- 持倉、逾 2 小時限價、結構轉弱、移動停損仍走原 manage／watch／AI。`switch_allowed` 與 `account_verified` 不能由設定檔改成 true。
- 正式環境都驗完之後，最小可切換條件仍是下面八項同時成立，缺一項就維持 keep：真帳戶 `status-json` 三查詢成功、一次被授權的 `manage --live` 沒有意外下單、上述 1 ID / 最多 2 POST 都是 delivered 且不是 processed、判斷摘要對上且重啟 apply 為 duplicate、`watch` 排程沒被這包改掉、裁量仍留原 AI、父側自己 load 了排程、父側明確簽核。這份程式現在不把這八項標成已完成。

## status-json

`live_trader.py status` 的文字裡，`open_algo()` 失敗會變成空清單並且整支程式仍可能結束碼 0。`multiAssetsMargin` 失敗也會被接住後結束碼 0。所以「時間 + 持倉 0 + 掛單 0」不能證明查詢成功；後面再出現「查詢失敗 HTTP 429」仍必須拒絕。

production 改走只讀的 `status-json`。三個查詢的原始回傳都要先通過型別檢查：帳戶是 dict 且 `positions` 是 list，兩種掛單都必須是 list。空 list 才表示真的沒有單。`null`、`false`、`0`、空字串、dict 或錯的元素都不能變成空單。三個查詢都成功、`ok` 是 true、結束碼是 0，而且輸出不得含餘額或失敗文字，才可信。任一失敗結束碼 2，且不印零倉冒充成功。時鐘新鮮不能代替這個契約。

```bash
python3 -m src.phase1.runner check-root --quantboy-root /既有/quantboy
```

這只讀原始碼，不執行查詢。舊目錄沒有 `STATUS_JSON_REVISION = "status-json-2"` 就不是已支援。把 root 指到一個目錄不會變成部署完成。套件內含 `src/prefilter.py`、`src/common.py` 與 `src/live_trader.py`，仍不含金鑰。
