# 一輪閘門

`python3 -m unittest tests.test_cycle`。不 POST、不讀秘密、不改 `live_trader.py`。

`manage --live` 每輪都跑，不看候選。指令是 `src/phase1/manage_job.py` 的 `build_manage_argv()`。

免 AI 只是建議：manage 整輪完成、快照新鮮、0 倉、0 掛單、沒有新候選、而且 prefilter 沒有錯誤。空檔、缺欄、格式錯、timeout、429 都不能建議 skip。有倉、任何掛單、超過 2 小時的限價、過期、未知或查詢失敗，都留下原 AI。同一根 K 的指紋不能取消有倉時的下一輪複查，也不能把掃描錯誤蓋成空候選。

HTTP 2xx 不把 outbox 標成已處理。布林值不會讓它離開 pending。發送或 consumer 失敗時事件還在。掃描錯誤或 manage 沒跑完時 `protection_complete` 是 false。

可執行入口是 shadow runner，不讀金鑰、不 POST、不跑 `live_trader.py --live`，也不把五欄 sender 當正式通道。排程檔預設 Disabled，不安進 LaunchAgents。`ready` 維持 false。

```bash
python3 -m src.phase1.runner run --state-dir "$CYCLE_DIR" --config "$CYCLE_DIR/runner.json" --now TIME --scan-id ID --shadow
python3 -m src.phase1.runner install-schedule --dest "$CYCLE_DIR/schedule" --state-dir "$CYCLE_DIR" --repo "$PWD" --python python3
python3 -m src.phase1.runner rollback-schedule --dest "$CYCLE_DIR/schedule"
```

五欄 `trade_candidate_test` 仍只是測試入口，不是這條持倉喚醒通道。正式通道要另做契約。

只能在本機用真通道驗證的事：下一次經授權的送達、以及主人那台機器上的 manage／watch 排程沒被這支 PR 改過。

處理 ACK 寫回 `run_cycle` 用的同一個 `phase1_state.json`。鍵是 `cycle:<scan_id>`，`evaluation_id` 與 `event_id` 都必須是安全字元，而且和 outbox 項相同。`kind=accepted` 只代表收件。布林值 `processing_ack=True` 不會把 outbox 標成已處理。時間早於送出、晚於現在超過 60 秒，或早於現在超過 15 分鐘，都維持 pending。`old_ai_poll` 維持 keep，這個結果不會停舊 routine。

```bash
python3 -m src.phase1.cycle apply --state-dir "$CYCLE_DIR" --file processed-EVENT.json --expect EVENT_ID --sent-at SENT_AT --now NOW
```

`$CYCLE_DIR` 不能是實盤 `data/`，也不能是獨立 ACK 包的 `ack_state.json` 目錄。2026-11-07 13:08 台北那次 `t-4b2c8c37a3dd22be73af862aac4dcae5` 的 HTTP 200，和 13:09 的 accepted，只是隔離測試收件。該測試通道已暫停，這次不再 POST。

下一次真實驗證只做一件：consumer 另寫 `processed-<event_id>.json`，producer 讀到後 process_count 仍是 1，再投同一檔不會加一。在那之前不要停舊 AI。
