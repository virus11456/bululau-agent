# Mac 掃描與 box 判斷

實盤與撈資料留在 Mac。box 只做開發、測試、協調和受控判斷。這份文件不表示已經接通，也不批准 POST。

## 已接上的程式路徑

本人 19:44 已核：現有新機會掃描是每 30 分鐘，routine 讀回 `*/30`，彈性 ±7。prompt 沒改，watch 仍是 60 秒。這包的預設與 Mac 下限現在也是 1800 秒，15 分鐘仍拒絕。報告用實際 `interval_seconds`，不再寫死 3600。這包還沒接到那條 routine，不提前切換，也不改 `com.quantboy.livewatch`。

每一輪只做兩件事：

- `src/prefilter.py --quiet --nolog`。掃描日誌若會落到 `~/quantboy-live`，就改寫到週期目錄的 `scanner-cache`。
- `python3 -m src.phase1.account_status`。這是帳戶、一般掛單、條件單三個只讀查詢。不開鎖、不寫 `state.json`、不呼叫 `day_state`。

不啟動 `manage --live`。60 秒的舊 watch、交易所停損和硬風控仍是原來的 `~/quantboy-live/live_trader.py`。

可信、新鮮、三查詢都成功，而且持倉與兩種掛單都是 0：不跑持倉保護 AI。這只省空判斷。有候選時新倉審核仍要跑，不會被空倉規則一起跳掉。查詢失敗、超時或未知維持 `cycle_needs_ai=true`，`old_ai_poll=keep`，不當成空倉。舊 `state.json` 快照不是這三個查詢。

有持倉或掛單時，這一輪仍會要 AI 裁量，但那仍是現有的 30 分鐘 routine，不是另一條已驗證的即時持倉事件。`position_ai_event_path` 是 `not_implemented`。

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

手動跑 `role=mac` 的一輪：只讀三查詢加公開掃描，不啟動 manage，不寫舊 state。零倉零單且新鮮、無候選時不叫保護 AI。不要為它再裝一條 cron。`install-schedule` 對 `role=mac` 會拒絕。現有 30 分鐘 routine 繼續負責原掃描；兩條一起跑就是雙排程。這包不提前切過去。

`python3 -m src.phase1.runner deploy-check --config runner.json` 會列出間隔、不開倉、不改 60 秒 watch、未知仍留原 AI。`switch_allowed` 仍是 false。現在不替換實盤。

## 必須留下的原 AI 與受監督判斷

有候選、有持倉、有掛單、查詢失敗或未知，都維持原 AI。`keep_original_ai` 與 `no_trade` 只記錄判斷，不會下單。真的開倉仍走 Mac 上原來的 `live_trader.py`，這包不呼叫它。

context 裡的持倉與掛單是筆數，沒有幣種、方向、數量、停損，也沒有 K 線。候選若來自掃描，會帶 symbol、setup、bar、ladder，以及掃描已經算出的 entry、stop、target、rr、plan。這些仍不是下單指令。審持倉要看 Mac 上原來的 status，不能只看這份 context。

回送繼續用 CopyToBox／CopyFromBox，再 `import-staged`。這要人在線批准。不是無人值守。

## 離線準備的回傳 adapter

`src/phase1/return_adapter.py` 是離線模組：沒有 CLI、沒有真 HTTP、沒有服務。測試用假 HTTP，以及先寫完再 `link` 的儲存。`mode` 不是 `offline` 就直接 `production_return_blocked`，不會因為旗標只是 false 就還能當正式通道。`VERCEL_BLOB_ATOMIC_CREATE_PROVEN` 仍是 false：官方 `allowOverwrite=false` 的並行建立還沒有證明，所以沒有 Blob 客戶端，也不用先 GET 再 PUT。

401／403 會在原目錄寫下 `stopped` 與 `alerted`。重啟後不再 GET、也不再告警。404 不是 processed。429、5xx、逾時和斷線在單事件 10 分鐘內按 5、10、20、40、60 秒加 jitter 退避。`Retry-After` 可以是秒數或 HTTP 日期。每次重試用推進後的時鐘檢查 15 分鐘，不沿用一開始的 now，也不改 `completed_at`。`applied` 要從原 outbox 讀到 acked、`processing_ack` 與 `process_count` 1 才算 duplicate。這不證明模型只跑了一次，`ai_execution_proven` 仍是 false。

Mac quiet 只接受非布林的整數 0、`status_trusted` 明確 true，以及帶 `call_ai`、`n_candidates`、候選清單和有限正價格 `btc.last` 的掃描。`btc` 裡其他數字也必須有限。NaN、正負無限、0、負數，以及 JSON 的 `NaN`／`Infinity` 都留原 AI，而且會叫 notifier。

可部署的入口在 `gateway/`。客戶端 URL 是 `/v1/...`，檔案在 `api/v1/...`，由 rewrite 接上。部署根、`npm ci`、`engines.node` `22.x`、`BLOB_STORE_ID` 與 OIDC 前提寫在 `gateway/DEPLOY.md`。 `functions.runtime` 不寫 `nodejs22.x`。原子建立是 Blob `put` 的 `allowOverwrite: false`（`x-allow-overwrite: 0`，服務端 `If-None-Match: *`）。沒有把 `token` 選項傳進去，並不證明當時用的是 OIDC。Python 的 `os.link` 只留給離線測試。還沒有建立 project 或 Blob。Hobby 用途與額度仍未知。

Mac 掃描之後、wake 之前仍走 quiet gate。有倉或有候選時，上下文必須帶白名單的 symbol、方向、數量、停損、掛單、對應 K 線與候選 plan；只有筆數不算足夠，不足就留原 AI。父層自己讀匯出的 context，把判斷寫進 `judgments-in/<event_id>.json`，把 trace 寫進同目錄的 `.trace.json`。這包不代寫。`keep_original_ai` 不是已完成的實盤裁量，現有介面也不能取代實盤裁量。gateway 客戶端預設 `enabled: false`，見 `gateway/client.example.json`。沒有 GM 給的 HTTPS 位址與有界授權就不會外發。不另裝排程，也不關現有 30 分鐘掃描或 60 秒 watch。

真的啟用仍要本人批准：一個隔離的 Vercel project、一個 private Blob、只做判斷回傳的 PUT／GET、box 發布與 Mac 讀取兩組 installation 憑證，以及費用上限。這不是現有連線附帶的權限。月費未知。POST 預算仍是 0。

GM 交過的非秘密 endpoint 是 `https://bululau-return-isolated-8qi96wkvs-virus11456s-projects.vercel.app`，路徑 `/v1/installations/<installation_id>/judgments/<event_id>`。PUT 給 publisher，GET 給 reader，標頭是 `Bearer key_id:secret`。`gateway/client.example.json` 把這個 URL 寫成預設關閉的 `base_url`。`installation_id` 與 `key_id` 留空，不猜。secret 不進 argv、不進 git、不讀角色 secret 檔。GM 說 b559ced 的隔離 deployment 是 READY，未認證 GET 為 401 且 `no-store`，OIDC 與 Blob 尚未證明。本包沒有對該 URL 發過 PUT、POST 或 GET，也不把 GM 的回報說成自己驗過。

`config/review_entry.example.json` 預設 `enabled`、`live_fetch`、`replaces_scan_entry`、`outbound` 都是 false。`review-round` 可以讀它，但 `runner run` 與 `com.quantboy.livewatch` 沒有改。15m×20 不夠原策略的完整 context。瘸師弟／小師弟要 24 根已收 15m（前 20 根量比較加訊號）、6 根已收 4H 與 6 根已收日 K（黑紅黑，不是均線）。布鹿流要 48 根已收 1H、50 根已收 4H（趨勢 30–50 的上緣，程式不判定成形）、6 根已收日 K。日均線參數仍是未定，`daily_ma_gate` 是 `not_evaluated`，不採用 30／45／60。有候選才另取 BTC 的 24 根已收 15m。未知 setup 或缺週期就留原 AI。空倉無候選不拉 K 線。未收盤的根會丟掉。

真取數要等 `live_fetch` 被明確打開，才會核對 `/Users/frank/quantboy-live/live_trader.py` 的 SHA256 `65f1d4526cd5d3b6c2115ca8eeebd102281b27a2fbd40d0ee07054ff4891459b`，然後只用該檔的 `req(method, path, params=None, signed=False, retries=5)`。帳戶是 `req("GET", "/fapi/v2/account", signed=True)`，一般單是 `/fapi/v1/openOrders`，條件單直接 `req("GET", "/fapi/v1/openAlgoOrders", signed=True)`，不用 `open_algo`。`req` 成功時 `json.loads` 可以是 None；None 或非容器留原 AI，不收成 `[]`。錯誤路徑會寫 backoff／warn 日誌，不能說完全沒有寫入。這檔沒有 klines 函式。K 線沿 `src.common.get("/fapi/v1/klines", ...)`，週期與根數按上面的 setup 表，只留已收盤。參數沒有在這輪打過真 API。SHA 不符就不執行檔案。repo 根的 `config/` 不是 `src/config.py`。載入或取數失敗留原 AI。這包仍沒有接到現有 routine。

## 下一步：30 分鐘 routine 等效接線（未啟用）

現有 routine 已是 `*/30`、彈性 ±7，prompt 與 60 秒 watch 都不要動。下一步不是把那條命令直接換成 `review-round`。離線順序在 [route_contract.md](route_contract.md)，可跑 `python3 -m src.phase1.route_contract`。原 prompt 與執行器未知，不能說已經等效。要等效，還得同時保住 manage、持倉保護、候選 AI，以及失敗時留原 AI。不另裝第二條排程，也不改 watch。啟用前仍要本人批准：打開 `live_fetch`（會讀已核平鋪檔、打三個只讀簽名 GET，並按 setup 拉公開 K 線）、接受 `req` 錯誤會寫日誌、gateway 的 installation／key 與有界試跑，以及日均線繼續未定、不能由程式宣佈通過。這份文件不執行上述任何一項。K 線必須排序、去重，並是指定週期的連續已收盤窗。publisher 用同一張 setup 表重驗週期、根數與完整 OHLCV。context 上限在 notifier 前用完整 delivery 的 UTF-8 位元組計算，省略不是無上限，超限留原 AI，不裁 K 線、不叫 notifier。

Ledger 只保證正常設定與行程崩潰：呼叫端不能改 ledger 路徑或 method→stage；空檔、壞檔、缺檔不會重開 `approved_at`；寫入用替換，中斷不會把檔案截成空的再當新期限。有磁碟管理權限的人若刪掉 ledger 再 `init-ledger`，次數會歸零，期限仍是授權裡的 `approved_at`。不宣稱擋得住刪檔。

## 還沒有的無人值守通道

公開的 artifact 自動傳送規格已向 GM 要，尚未拿到。沒有這份非秘密規格之前，不開服務、不複製 token、也不把 webhook 說成已接通。外部依賴就是這份規格，以及上面那張尚未批准的 Vercel 範圍。目前不能說已經省下喚醒。

## 回退

只刪版本化安裝目錄和自己的 `CYCLE_DIR`。不要刪 `~/quantboy-live`、`state.json`、`.lock`、`events.json`、HALT 旗標，也不要改 `com.quantboy.livewatch`。不要把掃描調回 15 分鐘。
