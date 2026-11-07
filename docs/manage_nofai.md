# manage 等效對照

不安裝、不改策略。只把已經由 `live_trader.py manage` 執行的固定風控從 AI 輪詢移開。型態、進出場、逾 2 小時撤不撤、結構轉弱和移動停損都不在這裡做決定。

| 原流程 | 新負責者與觸發 | 驗收證據 | 能否停舊 AI |
|---|---|---|---|
| 每 15 分鐘叫 AI 跑 `manage --live`：虧到 7% 停開新倉 | 同一支 `manage --live`，不含模型的指令觸發。不看候選 | `tests/test_manage_contract.py` `test_seven_percent_halts_new_and_still_scans_positions` | 只代表這一件。不能因此停整輪 |
| 虧到 30% 全平並寫 HALT | 必須帶 `--live` 的同一支 manage。不加 `--live` 不會留下旗標 | `test_thirty_percent_live_flattens_and_sets_halt_once`、`test_dry_run_thirty_percent_does_not_persist_halt` | 同上 |
| 有倉但沒有掛單，仍要巡保護 | `manage`。`watch` 在沒有待成交限價單時直接返回，不看帳戶 | `test_position_without_orders_is_scanned_by_manage_not_watch` | 同上。watch 不能當這件事的替代 |
| 缺停損：有紀錄就補，沒紀錄就市價平該倉 | 既有 manage | `test_seven_percent_halts_new_and_still_scans_positions`、`test_missing_stop_without_meta_market_closes` | 同上 |
| 限價未完成的部分成交，既有停損數量先不縮小；缺的止盈仍會補 | 既有 manage | `test_partial_fill_while_limit_open_does_not_shrink_protection` | 同上。這不是 1.5R 裁量 |
| 兩支程序同時跑 manage | 既有檔案鎖，後者直接退出 | `test_second_process_does_not_enter` | 同上 |
| 查單得到幣安錯誤 | 該幣跳過並寫 log，其他幣繼續 | `test_query_error_skips_that_symbol_and_continues` | 同上 |
| 查單網路中斷 | 中斷點之後的倉不會繼續。`manage_job` 只在結束碼非 0 時記一筆告警 | `test_disconnect_aborts_remaining_symbols`、`test_job_does_not_read_candidates_and_alerts_on_failure` | 不能把這當成逐倉補完。舊輪詢仍要留 |
| 新保護數量掛不上 | 不撤舊單 | `test_resize_failure_keeps_old_protection` | 同上 |
| 限價超過 2 小時，決定撤不撤 | 不撤單。只產生 `wake_original_ai`。結構轉弱與移動停損沒有非 AI 檢測器，明確留在原輪詢 | `test_old_limit_is_not_cancelled_by_manage`、`test_two_hour_limit_wakes_ai_without_cancelling` | 不能停。喚醒還沒有送到 AI |
| 型態與進出場 | 不移走。空掃描不叫 AI 只省掉沒有候選的那一輪 | `tests/test_token_switch.py` | 有新候選仍走原 AI |

固定指令仍是 `python3 src/live_trader.py manage --live`。`build_manage_argv()` 只組這一行，不讀候選。

## 因此現在不能停舊 AI 輪詢

- 逾 2 小時限價的撤單決定沒有送達路徑。Webhook 的 URL、key、認證都還沒配置。本機標記不算已經叫醒 AI。
- 持倉結構轉弱、移動停損沒有等效程序。沒有檢測器，也不硬編碼。
- 斷線後續倉不會在同一輪補完。

排程例子不要貼上。確認既有 `watch --live` 還在，才可另外加 manage。回退就是刪掉那一行，不動 watch，也不改策略檔。

```cron
# 尚未安裝
# */15 * * * * cd /絕對路徑/bululau-agent && /usr/bin/python3 src/live_trader.py manage --live >>/絕對路徑/manage.log 2>&1
```

2026-11-07 12:01 台北的 manage／status（0 倉、0 單、699.44、無 HALT、livewatch 結束碼 0、舊 AI routine 仍 enabled）是舊程序，不是這包已經上線。
