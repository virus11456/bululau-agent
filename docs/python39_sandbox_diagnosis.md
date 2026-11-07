# Python 3.9.6 sandbox 診斷

這份說明對的是 commit `b5c476dd81b06b5469526fb064eed7daaca94718` 的測試構造，以及下一包失敗時會多印出的欄位。它不解釋那次結束碼 1 的原因。Mac 上的 sandbox 沒有改，也沒有重跑。

## 已能從那次紀錄確定的事

三個 HTTPS 測試死在 `socket.listen` 的 `EPERM`。sandbox 允許 `localhost` 的 network，但這三筆是對 `127.0.0.1` listen。這不是缺 openssl，也不是 Python 3.9 語法不相容。

`check-root` 預期合成 `.env` 的 `env_file_present` 是 true，實際是 false。政策拒絕任何檔名是 `.env` 的 `file-read`。`os.path.isfile` 遇到 `OSError` 會回 false，所以紀錄裡沒有例外。下一包的報告多了 `env_file_stat_errno` 與 `env_file_stat_error`，仍不讀檔案內容。這還不是已證明的 syscall。

## 排程那筆的子程序構造

失敗點是 `tests/test_runner.py` 的 `test_generated_scan_id_and_runnable_disabled_schedule`。第一次斷言是 `now=None` 的 `_run`。那個子程序是：

```text
argv: <這次的 python> -m src.phase1.runner run --state-dir <暫存>/state --config <暫存>/state/runner.json
cwd: 解壓後的套件根目錄
沒有 --now、沒有 --scan-id、沒有 --shadow
```

`runner.json` 是測試自己寫的 `mode=production`，`quantboy_root` 指到同一個暫存目錄裡的假 `src/prefilter.py` 與 `src/live_trader.py`。外層 `sandbox-exec` 的命令不是這個子程序。

`run` 在沒有 `--now` 時自己產生 UTC 時間與 scan id。結束碼 1 且 stderr 是空的，形狀符合 `src/phase1/runner.py` 的 `_run_main` 抓到 `ValueError` 之後把 JSON 印到 stdout 再回 1。那份 JSON 的 `reason` 沒有留在這次紀錄裡，所以原因仍未知。未捕捉的例外會把 traceback 印到 stderr；這次 stderr 是空的，因此不是那種未捕捉例外。沒有寫入被拒的 traceback，不能把原因說成 sandbox 不讓寫檔。也不能說這是 Python 3.9 不相容。

## 同一政策下只重跑這一筆

把下面的路徑換成這次隔離目錄。不要放寬 sandbox，不要指向 `/Users/frank/quantboy-live`，不要讀 `.env`。

```bash
cd /Users/frank/bululau-runtime-check-COMMIT/bululau-cycle-ack
/usr/bin/sandbox-exec -f /Users/frank/bululau-runtime-check-COMMIT/sandbox.sb \
  /usr/bin/env -i \
  PATH=/usr/bin:/bin \
  HOME=/Users/frank/bululau-runtime-check-COMMIT/home \
  TMPDIR=/Users/frank/bululau-runtime-check-COMMIT/tmp \
  QUANTBOY_DATA_DIR=/Users/frank/bululau-runtime-check-COMMIT/data \
  QUANTBOY_EVENTS_FILE=/Users/frank/bululau-runtime-check-COMMIT/data/events.json \
  BINANCE_ENV_FILE=/Users/frank/bululau-runtime-check-COMMIT/env/absent \
  PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m unittest tests.test_runner.ProductionAdapterTest.test_generated_scan_id_and_runnable_disabled_schedule
```

失敗訊息是一個 JSON：`returncode`、`argv`、`cwd`、`reason`、`stdout_fields`、`stderr`、`env_names`。沒有環境變數的值。請把這整段貼回來。在那之前不要改測試期待，也不要為了讓它通過而放寬政策。
