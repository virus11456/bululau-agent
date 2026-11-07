# 一輪閘門

`python3 -m unittest tests.test_cycle`。不 POST、不讀秘密、不改 `live_trader.py`。

`manage --live` 每輪都跑，不看候選。指令是 `src/phase1/manage_job.py` 的 `build_manage_argv()`。

免 AI 只有：manage 整輪完成、快照新鮮、0 倉、0 掛單、沒有新候選、沒有錯誤。有倉、任何掛單、超過 2 小時的限價、過期、未知或查詢失敗，都留下原 AI。同一根 K 的指紋不能取消有倉時的下一輪複查。

HTTP 2xx 不把 outbox 標成已處理。只有另外的 `ack_cycle(..., processing_ack=True)` 才離開 pending。發送或 consumer 失敗時事件還在。manage 沒跑完時 `protection_complete` 是 false。

五欄 `trade_candidate_test` 仍只是測試入口，不是這條持倉喚醒通道。正式通道要另做契約。

只能在本機用真通道驗證的事：真實 sender 送達、處理 ACK、故障後重送仍在、以及主人那台機器上的 manage／watch 排程沒被這支 PR 改過。本倉庫沒有這些證據。
