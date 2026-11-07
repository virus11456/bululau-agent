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

## 受監督回流

官方已核到的送法是外層 `{wake, context}`。`wake` 維持既有八欄，不加第九欄。八欄的 `test_channel` 與 `position_channel` 必須和 envelope 裡的布林值相同；缺值不改成 false。`context` 是完整 export。全文太大就拒絕，不截斷。`c-15a60b2092e54c0a92e7fc28423b8559` 這筆舊合成事件維持 pending，不改它的時間。沒有找到官方的同步判斷回應、結果下載或 Mac 自動 pull，所以這裡不猜。CopyToBox / CopyFromBox 是逐檔搬運，要 Mac 在線並經本機批准，因此這條是受監督，不是無人值守。

這輪 POST 預算是 0。`dispatch-hold` 不送。`send-delivery` 只有 `post_approved: true` 才會 POST，Mac 週期不會自己叫它，這包也不建立正式 webhook。

```bash
python3 -m src.phase1.production_transport dispatch-hold --state-dir PRODUCER --event-id EVENT --evaluation-id EVAL --out to-box.json
python3 -m src.phase1.box_receiver box-accept --inbox BOX --file to-box.json --now TIME
python3 -m src.phase1.box_receiver box-show --inbox BOX --event-id EVENT
python3 -m src.phase1.production_transport import-staged --state-dir PRODUCER --file judgment.json --now TIME
python3 -m src.phase1.production_transport commit-judgment --state-dir PRODUCER --event-id EVENT --evaluation-id EVAL --now TIME
python3 -m src.phase1.cycle apply --state-dir PRODUCER --file PRODUCER/ack-inbox/processed-EVENT.json --expect EVENT --sent-at SENT --now TIME
```

父層 routine 要做的判斷，契約在 `docs/production_transport.md` 的判斷檔。模型讀 `box-show` 印出的 envelope，自己寫 `judgment.json`，欄位是 `schema_version`、`event_type=dry_run_judgment`、`event_id`、`evaluation_id`、`envelope_sha256`、`decision`（`keep_original_ai` 或 `no_trade`）、`basis`、`completed_at`、`orders_placed=false`、`ai_execution_proven=false`。`basis` 必須和該 envelope 的 reason、as_of、positions、orders、freshness、候選數一致。這包不代寫這份檔。

`import-staged` 核對事件、摘要、時效，和已有判斷衝突就拒絕。然後才在原來的 producer 目錄 commit 與 apply，並從同一目錄讀回。再 apply 是 duplicate。重啟後 dispatch 紀錄還在；已送達的同一摘要不會再 POST。

無人值守要另有已批准的公開 artifact 傳送。本輪沒有這個批准。

Mac 的 `account_status` 三查詢已在 15:38 用既有憑證通過，state 與鎖沒有被改。這項不必再測，除非以後改了 `account_status` 的查詢行為。

## 已驗收，不必再測

- 15:38：Mac 既有憑證 `account_status` 結束碼 0，三查詢 true，0 倉、0 一般單、0 條件單。`state.json` 與 `.lock` 的 inode、大小、mtime、ctime 都沒變。沒有 POST、沒有 manage。
- 15:48 至 15:50：新的隔離 synthetic producer 經 CopyToBox、`box-accept`／`box-show`、真判斷 `keep_original_ai`、CopyFromBox、原目錄 `import-staged`／`commit` 時仍 pending，`apply` 後才 acked，`processing_ack` 與 `process_count` 為 1。新進程再 apply 是 duplicate，count 仍為 1。這不是無人值守，也不是真交易。
- `c-15a60b2092e54c0a92e7fc28423b8559` 沒有 commit，維持 pending。不要改它的時間。`test_channel=true` 的事件不是真帳戶。

判斷與 ACK 的時效以程式為準：`src/phase1/ack_contract.py` 的 `MAX_ACK_AGE` 是 15 分鐘。`_outside_window` 還允許比現在晚 60 秒。剛好 15 分鐘仍可收，多 1 秒才過期。不是幾分鐘。

## 可安全做的 quiet 掃描

手動跑 `role=mac` 的一輪：只讀三查詢加公開掃描，不啟動 manage，不寫舊 state。零倉零單且新鮮、無候選時不叫保護 AI。不要為它再裝一條 cron。`install-schedule` 對 `role=mac` 會拒絕。現有每小時 routine 繼續負責原掃描；兩條一起跑就是雙排程。

`python3 -m src.phase1.runner deploy-check --config runner.json` 會列出間隔、不開倉、不改 60 秒 watch、未知仍留原 AI。`switch_allowed` 仍是 false。現在不替換實盤。

## 必須留下的原 AI 與受監督判斷

有候選、有持倉、有掛單、查詢失敗或未知，都維持原 AI。`keep_original_ai` 與 `no_trade` 只記錄判斷，不會下單。真的開倉仍走 Mac 上原來的 `live_trader.py`，這包不呼叫它。

context 裡的持倉與掛單是筆數，沒有幣種、方向、數量、停損，也沒有 K 線。候選若來自掃描，會帶 symbol、setup、bar、ladder，以及掃描已經算出的 entry、stop、target、rr、plan。這些仍不是下單指令。審持倉要看 Mac 上原來的 status，不能只看這份 context。

回送繼續用 CopyToBox／CopyFromBox，再 `import-staged`。這要人在線批准。不是無人值守。

## 離線準備的回傳 adapter

`src/phase1/return_adapter.py` 是離線模組：沒有 CLI、沒有真 HTTP、沒有服務。測試用假 HTTP，以及先寫完再 `link` 的儲存。`mode` 不是 `offline` 就直接 `production_return_blocked`，不會因為旗標只是 false 就還能當正式通道。`VERCEL_BLOB_ATOMIC_CREATE_PROVEN` 仍是 false：官方 `allowOverwrite=false` 的並行建立還沒有證明，所以沒有 Blob 客戶端，也不用先 GET 再 PUT。

401／403 會在原目錄寫下 `stopped` 與 `alerted`。重啟後不再 GET、也不再告警。404 不是 processed。429、5xx、逾時和斷線在單事件 10 分鐘內按 5、10、20、40、60 秒加 jitter 退避。`Retry-After` 可以是秒數或 HTTP 日期。每次重試用推進後的時鐘檢查 15 分鐘，不沿用一開始的 now，也不改 `completed_at`。`applied` 要從原 outbox 讀到 acked、`processing_ack` 與 `process_count` 1 才算 duplicate。這不證明模型只跑了一次，`ai_execution_proven` 仍是 false。

Mac quiet 只接受非布林的整數 0、`status_trusted` 明確 true，以及帶 `call_ai`、`n_candidates`、候選清單和 `btc.last` 的掃描。`False`、`status_trusted` 缺值、空的 prefilter 都留原 AI，而且會叫 notifier。

真的啟用仍要本人批准：一個隔離的 Vercel project、一個 private Blob、只做判斷回傳的 PUT／GET、box 發布與 Mac 讀取兩組 installation 憑證，以及費用上限。這不是現有連線附帶的權限。月費未知。POST 預算仍是 0。

## 還沒有的無人值守通道

公開的 artifact 自動傳送規格已向 GM 要，尚未拿到。沒有這份非秘密規格之前，不開服務、不複製 token、也不把 webhook 說成已接通。外部依賴就是這份規格，以及上面那張尚未批准的 Vercel 範圍。目前不能說已經省下喚醒。

## 回退

只刪版本化安裝目錄和自己的 `CYCLE_DIR`。不要刪 `~/quantboy-live`、`state.json`、`.lock`、`events.json`、HALT 旗標，也不要改 `com.quantboy.livewatch`。不要把掃描調回 15 分鐘。
