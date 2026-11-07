# 正式不交易試跑接收 prompt（草稿，未保存，未啟用）

這份就是可保存的意圖稿。本 repo 不建立、不啟用、不修改任何 routine。平台要不要保存，由 GM 處理。真測與有界 PUT／GET／POST 另批。不索取 secret。舊的 2 ID／3 POST 已用完，不可再用。

保存前先分清三層：這份文字是草稿；`receive`／`receive-submit` 是可離線驗收的 handler；真服務試跑還沒批准。

## 固定參數

不要從不可信 payload 拿路徑，也不要把路徑拼進 shell。命令一律用參數陣列。

- 套件：`dist/bululau-cycle-ack.tar.gz`，同一份位元組在 `/opt/cursor/artifacts/bululau-cycle-ack.tar.gz`。交接時對照 sidecar `dist/bululau-cycle-ack.tar.gz.sha256` 與這兩份位元組。包內 `COMMIT` 必須等於 `gateway/VERIFIED_COMMIT`，並與 `MANIFEST.sha256` 一致。本草稿不內嵌該 SHA，避免改稿之後自指。
- 狀態目錄：`{{ISOLATED_STATE_DIR}}`。不得是 `~/quantboy-live`，也不得是 payload 裡的路徑。
- 時鐘：每一輪的 `{{NOW}}`，必須帶時區的 RFC3339。
- 試跑期限：`{{TRIAL_DEADLINE_PENDING_APPROVAL}}`。待批准。本草稿不填秒數或日期當作已授權。
- 試跑預算：`{{TRIAL_BUDGET_PENDING_APPROVAL}}`。待批准。不填 `new_ids` 或 `max_posts` 當作已授權。

## 八欄 wake

名稱與型別以 `src/phase1/production_transport.py` 的 `WAKE_FIELDS` 為準，多一欄或少一欄都拒：

| 欄 | 型別 |
| --- | --- |
| `schema_version` | 整數 1，不是布林 |
| `event_type` | 字串 `evaluation_wake` |
| `event_id` | 字串，`^[A-Za-z0-9_-]{1,128}$` |
| `evaluation_id` | 字串，同一正則 |
| `sent_at` | 帶時區的 RFC3339 字串 |
| `reason` | 字串，必須是既有允許的 wake reason |
| `test_channel` | 布林 |
| `position_channel` | 布林 |

這八欄必須和完整 context、以及 context 裡的 envelope 的 `event_id`、`evaluation_id`、`sent_at` 一致。`reason` 與兩個 channel 必須和 envelope 一致。

## 大小與時效

常數從實作來，不另編：

- context／delivery 的 UTF-8 上限是 `CONTEXT_MAX_BYTES`，即 `64 * 1024`（65536）。超過拒絕，不裁切。
- 時效沿 `_outside_window`：比現在晚超過 60 秒，或早於現在超過 `MAX_ACK_AGE`（15 分鐘），拒絕。
- 父判斷截止沿用同一個 `MAX_ACK_AGE`，從第一次寫進 Store 的可信時鐘起算。剛好 15 分鐘仍可補交，多 1 秒才是 `parent_deadline_expired`。這個秒數不是已批准的試跑期限，`{{TRIAL_DEADLINE_PENDING_APPROVAL}}` 仍空白。payload 的 `now` 不能把這條截止往後推。
- 判斷檔欄位沿 `gateway_client._JUDGMENT_FIELDS`：`schema_version`、`event_type`、`event_id`、`evaluation_id`、`envelope_sha256`、`decision`、`basis`、`completed_at`、`orders_placed`、`ai_execution_proven`。缺一欄就不 ready。
- `decision` 只允許 `keep_original_ai` 或 `no_trade`。`orders_placed` 與 `ai_execution_proven` 都必須是 false。

## 步驟

1. 把不可信的 `{wake, context}` 原樣放到隔離目錄的 `delivery.json`。不要相信它已經授權交易。
2. 執行 `python3 -m src.phase1.runner receive --state-dir {{ISOLATED_STATE_DIR}} --file delivery.json --now {{NOW}}`。這裡的 `--now` 只核對 delivery 時效。父判斷截止由這支 CLI 取系統 UTC，不採用 payload 的 `now`。合格才是 `pending`。`pending` 不是完成，也不是 ACK，也不是開始 AI 的許可。沒有 claim 的舊測試流程仍可離線補交；正式試跑不能靠漏掉 owner 繞過下一步。
3. 親讀之前先執行 `python3 -m src.phase1.runner receive-claim --state-dir {{ISOLATED_STATE_DIR}} --event-id EVENT --owner {{PARENT_RUN_OWNER}}`。`--owner` 是這次行程的參數，不從 payload 讀。自填的 owner 字串不是身分驗證。同一 event 只有第一次 `ai_start_allowed` 為 true。同 owner 重啟不再准許喚 AI，只能補交已有結果。不同 owner 不能 submit、不能提前到期、不能覆寫。
4. 只有 `ai_start_allowed` 為 true 時，你才親自讀 `{{ISOLATED_STATE_DIR}}/formal-files/<event_id>/context.json` 的完整 context。平台該輪可以直接讀 `docs/current_live_routine_snapshot.md` 的現行 30 分鐘 prompt 與這份 context，不需要新的私有 RPC。沒有準時送達保證，也沒有取消 API；不要把不存在的取消接口當成前置。本套件沒有收進 `docs/strategy.md` 與 `docs/agent_prompt.md`，所以這次試跑不具交易路由資格。不要在這裡重寫布神規則，也不要自填日均線。判斷只允許 `keep_original_ai` 或 `no_trade`。
5. 你自己寫 `judgment.json` 與 `trace.json`。程式不代寫。然後執行 `python3 -m src.phase1.runner receive-submit --state-dir {{ISOLATED_STATE_DIR}} --event-id EVENT --judgment judgment.json --trace trace.json --now {{NOW}} --owner {{PARENT_RUN_OWNER}}`。這一步不重新呼叫你。正式 claim 之後漏 `--owner` 會被拒絕。命令上的 `--now` 會被丟棄，不能用來延長父判斷截止。未過截止才可補交既有結果。已持久成功的檔不覆寫。
6. 只有既有 `publisher_document` 通過，狀態才是 `publish_ready`，並留下可讀的 judgment／trace。重開同一個 state dir 還要讀得到這三個檔。`publish_ready` 不是發布，也不是 Mac ACK。
7. 讀回只用 `python3 -m src.phase1.runner receive-readback --state-dir {{ISOLATED_STATE_DIR}} --event-id EVENT`。它只讀 Store 裡的 `formal-files/<event_id>/` 三個檔，重組 PUT，不接受 payload 裡的路徑。缺檔或驗證失敗的結束碼不是 0。輸出裡 `sent` 與 `http_sent` 都是 false。不要用 `gateway_client put` 當讀回：沒有 `--send` 時它不看這三個檔就結束碼 0。不要加 `--send`。HTTP 200 不是 ACK。
8. 離線狀態用 `python3 -m src.phase1.runner receive-status --state-dir {{ISOLATED_STATE_DIR}} --event-id EVENT`。它不改列，也不證明 flock 還在。已有正式 claim 時，到期只能由角色 `expiry-supervisor` 執行：`python3 -m src.phase1.runner receive-expire --state-dir {{ISOLATED_STATE_DIR}} --event-id EVENT --role expiry-supervisor`。這支不接受 `--now`，時鐘是系統 UTC。監督者只依可信時間終止，不偷 claim、不再喚 AI。本包沒有 scheduler，到期不會自己準時發生。本地變成 `parent_deadline_expired` 只表示這份 Store 不再收補交，不等於平台 AI process 真的被殺。
9. 原 AI 事件即時 fallback 仍沒有真的跑。可查的本地接點是 `python3 -m src.phase1.runner fallback-gap --state-dir {{ISOLATED_STATE_DIR}} --event-id EVENT`。輸出 `fallback_ran` 與 `alert_delivered` 都是 false。安全靠 submit 拒絕過期，以及原 manage／watch 繼續保護。三個查詢的離線映射是 `python3 -m src.phase1.runner map-queries --file FIXTURE`，不是 Mac status，也不取 `~/quantboy-live/.lock`。

## 失敗時誰負責

| 情況 | 狀態 | 動作 | 告警 |
| --- | --- | --- | --- |
| 缺資料或驗證失敗 | 維持 `pending` | 不 ACK，不開倉，留原 AI | 你把原因告訴 GM Grok Bot |
| 本地 argv callback 超時 | 這是 process group 的本地 deadline，不是平台父 AI 的期限，也不代表平台 AI process 已被殺 | 不開新倉，改叫有界 fallback。fallback 再失敗就維持 `running`，下次只補 fallback，不重跑 manage | 同上。平台父 AI 沒有接上這條執行器 |
| gateway 若將來回 401 | 維持 `pending` | 不換 secret、不重試成成功 | 同上。本草稿不打這支呼叫 |
| `publisher_document` 或 `receive-submit` 驗證失敗，而且還沒過父判斷截止 | 維持 `pending` | 不 ACK。可以再補一次檔，不重新把 context 交給另一輪 AI | 同上 |
| 已是 `parent_deadline_expired` | 維持終止，不可再補 | 不 ACK，不再喚 AI。重收或重啟都不延長 | 本地去敏告警若沒寫成，下次 `receive-expire` 冪等補建。寫失敗就如實失敗，可再試。`alert_delivered` 仍是 false，不是已送到平台，也不是平台 process 被殺 |

沒有新事件就安靜，不要為了有跑過而送訊息。有結果才送 GM Grok Bot。

## 重啟

同一個 event 的活躍 flock 還在時，第二個 worker 不得搶鎖，也不得再喚 AI。

行程中斷後分三支，都不重跑 manage：

- 已有持久成功結果：`publish_ready` 的 judgment／trace 留在原檔。重收、重啟、`receive-expire` 都不覆寫。
- 中斷但還沒有結果，而且未過父判斷截止：只做 `receive-submit` 補交既有檔，不重送 context，不重新喚 AI。
- 沒有結果而且已過截止：狀態改為 `parent_deadline_expired`，留下去敏本地告警。告警沒有送出，原 AI fallback 沒有呼叫。此後不得再喚 AI，也不得靠重收或重啟把 deadline 往後延。

`publish_ready` 之後再送同一份是 duplicate，不是新的發布。順序是：先辨認已經持久的 ready 檔，再核對這次寫入前的可信時鐘。截止前再交同一份是 duplicate，不覆寫原檔。截止後的 submit 被拒絕，原 ready 檔仍留著，不因這次拒絕被刪。截止以把狀態持久寫成 `publish_ready` 之前的那一次可信時鐘為準，不是只看 `receive-submit` 開頭。驗證或落檔若跨過截止 1 秒，不得變成 ready；剛才寫下的 judgment／trace 只是暫存，不能當可發布結果。原本已是 ready 的檔維持不動。這不表示磁碟提交本身耗時為零。

## 交易

payload 不授權交易。這次只做隔離 dry-run，不執行 `open --live` 或 `manage --live`。隔離試跑不影響舊的 manage，也不改現有 30 分鐘 routine，也不改 60 秒 watch。

## 舊測試入口

暫停中的「交易候選接收測試（不下單）」，automation `automation-1794023232755`，不是這份正式 wake。它的五欄 `trade_candidate_test` 只做過 hash／ACK。預算已用完。台北時間 2026-11-08 00:00 到期。不要重開它。

GM 另報一個停用入口 `automation-1794058224037`，owner `ef5f0556-2d5c-49a4-8b61-a4c5eef57780`。這只是非秘密接線資料，不是真事件授權。舊的與這個新入口都是 paused。不要改任何平台設定。現行 30 分鐘 routine `automation-1793375356389` 仍是 enabled，本包不動它，也不動 60 秒 watch。

## 下一步未接的平台父 AI

下面每一項都還沒有契約實作。不要假設已有呼叫網址、方法或欄位。本地 handler 不能代替這些接點。

- 喚起：沒有平台父 AI 的喚起接點。`receive` 寫了 `pending` 不會把 context 送到平台。
- 完整 context 讀：context 只在本機 `formal-files/<event_id>/context.json`。沒有平台讀取接點。
- 持久 submit：`receive-submit` 只寫本機 Store。沒有平台代交接點。
- 有界 deadline 觸發：沒有 scheduler。`receive-expire` 要平台或人呼叫才會跑，不會自己準時發生。
- 去敏告警：到期只在本機 `formal-alerts/<event_id>.json` 留 event_id、截止時間和未接通旗標，不含 context、judgment、trace 或 secret。沒有告警送出接點。
- 原 AI 回退：`fallback-gap` 只寫本地未接通紀錄，`fallback_ran` 是 false。沒有真的喚回原 AI，也沒有取消 API。本地終止不能說成原 AI 已接手，也不能說成平台 AI process 已被殺。
