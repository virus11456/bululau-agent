# 正式喚醒傳輸與消費

這不是五欄 `trade_candidate_test`，也不使用 `~/.config/bululau-sender-test/`。POST 預算不共用。本文件不執行發送。

實盤與撈資料都留在 Mac。box 只做開發、測試、協調和受控判斷。交易金鑰、`state.json` 與資料目錄不搬到 box。box 不跑 scanner，也不打幣安。GM 轉述 box 的公開 fapi time 是 HTTP 451；這裡沒有重測，也不繞過。Mac 到 box 的自動傳送還沒有接上。`verify-same-store` 的邏輯 ID 在複製目錄後仍會相同，不是原來的磁碟。ACK 要在原來的 Mac producer 目錄 apply，再從那個目錄讀回。

## 通知與上下文分開

八欄 wake 只通知接收端有一筆評估。它沒有快照、候選或時效。每一筆的上下文寫在 producer 狀態目錄的 `envelopes/<event_id>.json`。發布時先寫暫存檔並 fsync，再在同一把 `envelopes/publish.lock` 裡用 hard link 掛上最終檔名，並在這把鎖裡重讀 outbox、寫入 `envelope_sha256`。hard link 不會覆蓋已存在的檔。同事件不同內容只有先發布的那一份成功，另一份回 `immutable_conflict`。同內容再跑一次是冪等。不同事件都會留下。行程被殺掉時，這把鎖由作業系統釋放，不會死鎖。這不是斷電測試。

`CYCLE_DIR` 只是本機路徑字串。Mac 上的 producer 與 box 上的 consumer 不共用檔案系統，路徑相同也不代表同一個 store。store 身份是 `phase1_state.json` 裡的 `producer_store_id`。正式自動跨機傳送沒有實作。這包不另開網路服務，也不搬交易金鑰。

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

## 跨機接線（本包只做到本機檔，不自動傳送）

producer 在自己的狀態目錄發布 envelope。consumer 要看的是一份不含秘密的 `immutable_context` 檔，不是 producer 的路徑。

```bash
python3 -m src.phase1.production_transport export-context --state-dir PRODUCER_DIR --event-id EVENT --evaluation-id EVAL --out context.json
python3 -m src.phase1.production_transport verify-context --file context.json --now TIME
python3 -m src.phase1.production_transport verify-same-store --state-dir PRODUCER_DIR --file context.json
```

`export-context` 只在本機寫檔。檔裡有 event_id、evaluation_id、envelope_sha256、sent_at、producer_store_id 和 envelope 本體。`automatic_transport` 固定 false。`verify-context` 核對摘要、事件、評估與時效，並且 `same_store` 固定 false。`verify-same-store` 只回 `logical_id_match`。整份目錄複製後這個 ID 仍會相同，所以它不是原來那塊磁碟的證明。`physical_store_proven` 固定 false。真正的 ACK 必須在原來的 producer 目錄 apply，再從那個目錄讀回；複製出來的目錄 apply 不會改到原來的 outbox。這包不另做身份服務。

誰負責：

- producer：不可變 envelope、outbox 摘要、export 檔。不寫判斷。
- 搬運：父側自己把 export 檔送到 box，之後把判斷檔送回 producer。這包不做這段。
- consumer：做實際 AI 判斷，留下 trace 與理由，再寫判斷檔。basis 只會被 producer 拿來對 envelope，不能代替 trace。這包不代填 JSON，也不把 `ai_execution_proven` 設成 true。
- 回流：判斷檔必須放回當初 export 的那個 producer 目錄的 `judgments/<event_id>.json`，再在那個目錄 commit，然後 apply。另一台機器上的目錄不能 apply 這筆。

producer 的候選去重只表示沒有再排一次 wake。`ai_judged_once` 與 `ai_execution_proven` 都是 false。去重不是模型只判斷過一次。

## 消費

收到 wake 不能寫 processed。判斷檔是 `judgments/<event_id>.json`，不再使用單一的 `dry_run_judgment.json`。本程式只讀它，不建立它。一次 commit 只處理指定的 event_id。`basis` 必須和該事件 envelope 的 reason、as_of、positions、orders、freshness、候選數一致；這只是一致性，不是 AI 執行證明。

崩潰時 producer 的約定：

- consumer 還沒寫完判斷：producer 沒有判斷檔，commit 拒絕，outbox 仍是 pending。可以稍後再送同一份判斷。這不表示模型已經判斷過。
- 判斷已寫好但還沒 commit：沒有 `ack-inbox/processed-<event_id>.json`，outbox 仍是 pending。時效內可以 commit。過期就拒絕，不寫 processed。
- 已 commit 但還沒 apply：processed 檔留在 producer 的 ack-inbox，outbox 仍是 pending。apply 成功後再 apply 是 duplicate，`process_count` 停在 1。

`ai_execution_proven` 必須是 false。本地能寫狀態目錄的人仍可做出符合欄位的判斷檔；這是信任邊界。正式 consumer 由父側控制。父側若要可信完成收據，須另定簽章（至少覆蓋 event_id、evaluation_id、envelope_sha256、completed_at）。目前沒有這段簽章。

判斷檔：

```json
{"schema_version":1,"event_type":"dry_run_judgment","event_id":"c-…","evaluation_id":"s1","envelope_sha256":"<envelope 摘要>","decision":"keep_original_ai","basis":{"reason":"positions_open","as_of":"…","positions":1,"orders":0,"freshness_status":"fresh","candidate_count":1},"completed_at":"2026-11-07T04:22:00Z","orders_placed":false,"ai_execution_proven":false}
```

`decision` 只能是 `keep_original_ai` 或 `no_trade`。`basis` 必須和該事件 envelope 一致。`completed_at` 不得早於 `sent_at`，也不得比 `--now` 早超過 15 分鐘或晚超過 60 秒。錯摘要、舊時間、或聲稱 `ai_execution_proven=true` 都拒絕，且不寫 processed。

然後：

```bash
python3 -m src.phase1.production_transport commit-judgment --state-dir PRODUCER_DIR --event-id EVENT --evaluation-id EVAL --now NOW
python3 -m src.phase1.cycle apply --state-dir "$CYCLE_DIR" --file "$CYCLE_DIR/ack-inbox/processed-EVENT.json" --expect EVENT --sent-at SENT_AT --now NOW
```

`processed_at` 不得早於 `sent_at`，也不得比 apply 的 now 早超過 15 分鐘或晚超過 60 秒。對不上、判斷沒完成、或寫檔失敗，都不寫 processed，`old_ai_poll` 維持 keep。

## 這包能做的，以及要父側接上的

這包能做：本機 envelope 發布與衝突拒絕、本機 export/verify、判斷檔一致性與時效、commit、apply 去重、`check-root` 只讀核對。不能做：自動把檔案送到 box、在 consumer 上跑模型、代寫判斷、把 producer 去重當成 AI 已判斷、共用舊 root 的 state 或 `.lock`、讀寫 `.env`、啟動 `watch --live`。這些都不新增權限。

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

這只讀原始碼和檔名，不讀 `.env`、不讀 `state.json`、不建立 `data/`。父側 15:03 的唯讀 metadata：`/Users/frank/quantboy-live` 是平鋪的 `live_trader.py`，沒有 `src/` 和 `COMMIT`，SHA256 是 `65f1d4526cd5d3b6c2115ca8eeebd102281b27a2fbd40d0ee07054ff4891459b`，沒有 `status-json-2`。`/usr/bin/python3` 是 3.9.6。`com.quantboy.livewatch` 每 60 秒執行該檔的 `watch --live`，cwd 是舊 root，而且 plist 與 launchd 全域都沒有 `QUANTBOY_DATA_DIR`、`QUANTBOY_EVENTS_FILE`。舊 root 看得到 `state.json`、`.lock`、`events.json`，沒有 HALT 旗標。`.env` 和 state 內容沒有讀，程式如何決定資料路徑也還沒有證據。

父側之後給了平鋪程式的非秘密片段：`HOME = os.path.expanduser('~/quantboy-live')`，`STATE`、`EVENTS`、`HALT_FLAG`、`LOCK` 都是 `os.path.join(HOME, 固定檔名)`，沒有看到這些常數再被環境變數覆寫。`Lock` 是 `open(LOCK,'w')` 後 `LOCK_EX|LOCK_NB`，`BlockingIOError` 就結束，`with Lock()` 包住 status、open、manage、watch、flatten。這只證明路徑寫法與鎖的呼叫形狀。`state.json` 的欄位沒有在片段裡，所以 `manage_allowed` 與 `share_live_state` 仍是 false，新的 manage 不改去讀寫那個檔。這包的預設 `data/` 也不是那個 HOME。

```bash
python3 -m src.phase1.runner check-install --quantboy-root ~/quantboy-live
```

路徑不是 `~/quantboy-live` 就是 `unknown_path`，而且不 stat 那裡的 state。SHA、綁定、鎖形狀與 state 介面都要符合。介面符合時 `state_interface_match` 是 true：`load_state` 缺檔得到 `{}`，`save_state` 用固定的 `state.json.tmp` 再換上，`day_state` 在日期不同時整份換成新的 dict，`pending` 與 `meta` 不會留下。這是舊行為，新程式不執行它。只讀快照是 `python3 -m src.phase1.legacy_snapshot --state ~/quantboy-live/state.json --now TIME`。它不建 `data/`、不寫 state、不開鎖、不查帳戶，缺檔的 reason 是 `missing_state`，倉位是 null。即使 SHA 與介面都符合，`manage_command_authorized` 仍是 false，原因是 `existing_manage_not_spawned`：此套件不啟動舊的 `manage --live`。舊 watch 繼續跑既有程式。回退只刪版本化安裝目錄，不動 `~/quantboy-live`、state、鎖或 `com.quantboy.livewatch`。

版本化安裝只解到新目錄，不覆蓋 `/Users/frank/quantboy-live`，不複製 `.env`，不啟動 watch 或 manage，不雙寫：

```bash
INSTALL="$HOME/bululau-cycle-ack/COMMIT"
mkdir -p "$INSTALL"
tar -xzf bululau-cycle-ack.tar.gz -C "$INSTALL" --strip-components=1
python3 -m src.phase1.runner check-root --quantboy-root /Users/frank/quantboy-live
python3 -m src.phase1.runner check-root --quantboy-root "$INSTALL"
```

`CYCLE_DIR` 等父側選定安裝位置之後再核對。這輪不填真實路徑，也不假設它就是舊 root。

若要判斷舊程式的資料目錄能不能在以後對上，請父側補這些非秘密資料，不要貼 `.env` 或 state 內容：

- `live_trader.py`、`config.py`（若有）的 SHA256，以及設定 `HOME`、`STATE`、`LOCK`、`EVENTS` 的那幾行。值裡若有金鑰請塗掉。
- `state.json`、`.lock`、`events.json`、`HALT_REPORT_USER.flag`、`data/state.json`、`.env` 各自的存在、型別、大小、mtime、inode、是否為 symlink。`.env` 只報 mode，不報內容。
- `launchctl print` 的環境變數名稱，加上 `QUANTBOY_DATA_DIR` 與 `QUANTBOY_EVENTS_FILE` 的路徑值（若程序環境裡有）。不要帶其他秘密。
- 正在跑的 watch 行程打開的 `.lock` 與 `state.json` 路徑。

這台建置環境是 Python 3.12.3，沒有 3.9.6，所以沒有在 3.9.6 上跑測試。套件原始碼沒有 `match`，時間字串的 `Z` 會先換成 `+00:00` 再解析。Mac 上仍要以 `/usr/bin/python3` 重跑套件測試才算 3.9.6 通過。
