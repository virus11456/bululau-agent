# 正式接收 prompt 草稿（未啟用，未寫入平台）

這份是給 parent／GM 看的草稿。這個 repo 不建立、不啟用、不修改任何 routine。平台上要不要保存這段 prompt，由人決定。有界 PUT／GET／POST 仍待本人批准。不索取 secret，也不使用舊的 2 ID／3 POST。

三層不要混：

- 可驗收的 handler：`python3 -m src.phase1.runner receive --state-dir DIR --file delivery.json --now TIME` 寫入 `pending`。父層之後用 `python3 -m src.phase1.runner receive-submit --state-dir DIR --event-id EVENT --judgment judgment.json --trace trace.json --now TIME` 補交自己寫好的檔。這一步不重新呼叫父 AI。檔要通過既有 `publisher_document`，才變成 `publish_ready` 並留下可讀的 judgment／trace。缺欄或錯誤維持 pending，不 ACK。pending 不是完成。
- 平台人工保存的 prompt：這份草稿。程式沒有把它寫進 webhook。
- 真服務的有界試跑：尚未批准，這輪不做。執行器超時也沒有接上，不能把同步例外當成已經有超時器。

## 舊測試入口不是正式授權

GM 用 owner get 看到的舊 routine 是暫停的 webhook：owner 總經理boy `ef5f0556-2d5c-49a4-8b61-a4c5eef57780`，id `automation-1794023232755`，名稱「交易候選接收測試（不下單）」。CONTRACT 路徑 `/workspace/trading-webhook-e2e-test-20261107` 不在這台 Cloud 上，這裡不猜檔案內容。repo 裡已有的五欄契約是 `schema_version` 整數 1、`event_type=trade_candidate_test`、`event_id`、`test_only=true`、`sent_at`。它只做過 hash／ACK 測試，不是策略，也不能當成正式 wake。預算已用完。台北時間 2026-11-08 00:00 到期。這包不暫停、不重開那個 routine。

## 父 AI 要自己讀的規則

正式送達是 `{wake, context}`。八欄 wake 必須和完整 context 裡的 event、evaluation、digest 一致。schema 有限，UTF-8 超過 context 上限就拒，過時效拒，同一 event 不同摘要拒。父 AI 必須親自讀完整 context，再決定保護、新倉或 fallback。收到不可信 payload 不下交易。判斷和 trace 交回既有 gateway publisher；這輪真 socket 仍是 0。HTTP 200 不是 ACK。程式不假造父 AI 的 judgment。持倉保護和候選裁量仍要新鮮帳戶核對，以及原來的機械硬風控。dry-run 測試沒有交易。
