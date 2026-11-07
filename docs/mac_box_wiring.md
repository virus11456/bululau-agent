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

官方已核到的送法是外層 `{wake, context}`。`wake` 維持既有八欄，不加第九欄。`context` 是完整 export。全文太大就拒絕，不截斷。沒有找到官方的同步判斷回應、結果下載或 Mac 自動 pull，所以這裡不猜。CopyToBox / CopyFromBox 是逐檔搬運，要 Mac 在線並經本機批准，因此這條是受監督，不是無人值守。

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

## 替換前要先有的驗收

原掃描已經是每小時。替換後頻率不回到 15 分鐘。下面沒有完成之前，不替換現行程式，也不把 `switch_allowed` 改成 true。

1. Mac 用既有金鑰跑一次 `account_status`，結束碼 0，三個查詢都是 true，輸出沒有餘額，`~/quantboy-live` 的 state 與鎖檔時間沒有被這次改寫。
2. 同一輪 `role=mac` 在真的零倉零單、無候選時 `protection_ai_skipped=true`，有候選時 `new_entry_ai=true`。
3. `com.quantboy.livewatch` 仍是每 60 秒的舊指令。
4. 回退只刪版本化安裝目錄與自己的 `CYCLE_DIR`，不動舊 root、state、鎖和 watch。
5. 父層用模型寫出的 `judgment.json` 經 CopyFromBox 回到 Mac，`import-staged` 通過後在原目錄 apply 並讀回。離線迴路已測；真模型這一步還沒有做。

第 1 項已在 15:38 完成。第 5 項的真模型判斷與無人值守傳送都還沒有。
