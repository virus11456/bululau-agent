# 省 token 閘門（gate.py）

## 為什麼要這支

現行 Grok routine 每 30 分鐘醒一次，一天 48 輪。每一輪都要讀完整份實盤 prompt，再跑 `manage --live`、`status`、prefilter，然後逐條審核。空倉又沒有候選時，這些工作的結論永遠是「沒事」，等於白花 token。

`src/gate.py` 把這些機械步驟改由程式來跑。空輪時 Grok 只要執行一個命令、看到第一行是 `QUIET`，就結束本輪。

這支程式不下單。它只跑原本每輪就會跑的同三支命令。下單、出場判斷與回報仍然由 Grok 照原本的 prompt 做。

## 硬條件篩選

prefilter 有候選時，閘門會再跑同目錄的 `hardfilter.py`，把確定不符合現行開單硬條件的候選拿掉。規則、原文出處、部署與回滾見 [hardfilter.md](hardfilter.md)。hardfilter 失敗、限流或輸出看不懂一律 WAKE（`hardfilter_failed`），候選全部保留。`--hardfilter off` 可停用。

## 什麼時候 QUIET，什麼時候 WAKE

以下全部成立才印 `QUIET`，結束碼 0：

- `manage --live` 結束碼是 0，輸出裡沒有成交、撤單、平倉、停開新倉、錯誤、孤兒條件單這類字。空倉時清掉殘留條件單會印「孤兒條件單」，這一輪要叫醒。
- `status` 結束碼是 0，而且讀得到新鮮的「時間 … 台北」、「持倉 0」「一般掛單 0、條件單 0」「全平旗標 False」。表頭的數字還要跟下面列出的明細行數對得上。出現「條件單讀取失敗」一律不可信（`live_trader.py status` 在條件單讀取失敗時會印這句並以非 0 結束，不會再印「條件單 0」）。
- prefilter 結束碼是 0，輸出的 JSON 寫明新鮮的 `time`、`call_ai: false`、`n_candidates: 0`，而且 `candidates` 是空清單；或 prefilter 有候選，但 hardfilter 成功跑完、全部擋下。張數和清單長度不一致（例如寫 0 但清單裡有東西）一律不可信。
- 上一輪狀態檔明確是 `flat`。如果上一輪有倉或有單、這輪突然全空，代表剛停損或剛出場，要叫醒一次讓 Grok 記錄、回報、記冷卻。狀態檔損壞、`last` 不是 `flat`／`busy`、或中間有一輪沒讀到帳本，也要叫醒。狀態檔根本不存在（第一次安裝）仍可 QUIET，這是下面的手動驗收與既有測試；若希望連「檔案被刪掉」也叫醒，需要另外確認，因為那會讓第一次安裝印 WAKE。

其他情況一律印 `WAKE <理由>`，結束碼 10，並附上三支命令的輸出。例如：有倉、有單、有候選、401、超時、檔案鎖一直被佔、輸出格式看不懂、讀數過期、閘門自己出錯。閘門中途出錯時，已經跑完的 manage／status／prefilter 輸出會留在 WAKE 裡，不會被清成空的。

新鮮度不是策略參數。status 的時間只到分鐘，而且印在 status 開始時；檢查發生在 status 與 prefilter 都結束之後。年齡上限是這兩支指令的逾時（90 秒與 240 秒）加上分鐘捨去 60 秒、再加 90 秒餘裕，一共 8 分鐘。時間超前超過 120 秒也當成不可信。90 秒餘裕和 120 秒超前要擁有者確認後才算數。

不確定的狀況永遠算 WAKE，不會被當成空倉。

## 斷線之後的補審

repo 沒有 Mac 上 `gate.py` 的備份。這段以本倉庫 `src/gate.py` 為準。`docs/hardfilter.md` 裡寫過一個部署前 sha1，那是較早的檔，不是這次的對照本。這支 `src/gate.py` 的 sha1 是 `5a3c5a8eab52e0bb92ff3cedc20a3a31523b80c3`。部署只覆蓋 `~/quantboy-gate/gate.py`。不要動 `live_trader.py`、prefilter、`full_prompt.md`、排程或 60 秒 watch。

`main()` 在 `manage`、`status` 失敗之後仍會呼叫公開 prefilter（只讀行情，不簽章）。11/12 那種幣安 401／-2015 不會把 prefilter 跳過。先前 `gate_log.jsonl` 只記 `n_candidates` 這個數字，不記是哪一個幣，下一輪也讀不回 WAKE 印出來的 JSON。所以斷線期間雖然有跑 prefilter，閘門自己沒有留下可補審的名單。

這次補上的只有名單，沒有改門檻、頻率、60 秒 watch、下單或 `live_trader`：

- 帳戶不可信（`status` 讀不到、過期，或這輪帳本是 `unknown`）時，照舊 WAKE，不補審。公開 prefilter 若有抓到候選，只把幣種、型態、訊號 K 的時間、看到的時間寫進 `gate_state.json` 的 `pending_catchup`。不寫階梯、停損、目標、進場價。
- 持倉／掛單恢復可信，而且這輪 prefilter 與 hardfilter 都跑完（hardfilter 沒失敗、行情時間仍新鮮、`--hardfilter` 不是 `off`）的第一輪，才拿名單去對這輪 hardfilter 留下的候選。幣種與型態都對上的，理由加 `catchup_review`，WAKE。印給 AI 的價格是這輪 prefilter 現算的，不是名單裡的舊價。對上之後名單清空。
- 對不上的（這輪 prefilter 沒再吐出、或 hardfilter 擋下）不補審，名單也在這一輪清空。prefilter 只看最近兩根 15m；訊號若已經不在這個窗口，這輪重跑就不會出現，本程式不另開窗口、也不用舊的掛單價再審一次。同一幣種若是新的一根訊號、但這輪 prefilter 與 hardfilter 仍留下，交出去的是這根新訊號的現算結果。要不要改成「必須同一根 K」請審查者定，這次不加這條。
- 這輪 prefilter 失敗、hardfilter 失敗、行情不新鮮，或 hardfilter 被關掉：不算那「第一輪」，名單留著，這輪不補審。

「仍合格」在這支程式裡就是：這輪 prefilter 吐出、而且 hardfilter 留在 `kept`。沒有另外判斷「窗口外但結構還在」。

「停開新倉」（當日虧損 7%）同一天只叫醒一次。「全平旗標」（30%）則每一輪都叫醒。

## 幣安 429／418 時才重試 prefilter

prefilter 結束碼 2 同時代表限流和其他行情失敗，**不能單靠結束碼重試**。閘門只在 stdout 或 stderr 認得出 429 或 418 時，才把整支 prefilter 再跑，最多再 2 次（一共最多 3 次）。其他失敗維持原來的 `prefilter_failed`，不重試。

認得出的寫法只有這三種：

- `rate_limited: 429` 或 `rate_limited: 418`。部署中的 public prefilter（`/Users/frank/quantboy-paper/discretionary/prefilter.py`，不在本倉庫、這次也不改）和本倉 `src/prefilter.py` 都是這句。11/10 21:01 的 `prefilter_log` 原文是 `rate_limited: 429 on /fapi/v1/klines`。
- `429 on /fapi/`、`418 on /fapi/`。這是上面那支在丟出例外時的訊息本身。
- `HTTP 429`、`HTTP Error 429`、`HTTP 418`、`HTTP Error 418`。這是 urllib 沒被接住、traceback 印在 stderr 時的寫法。

沒有這些字樣（例如只有 `HTTPError`、`timeout after 429ms`、401、逾時、結束碼 2 但輸出是空的）不重試。

沒有可用的 Retry-After 時，兩次重試分別等 5 秒、15 秒。stdout／stderr 若有 `Retry-After: <正數秒>` 就改等那個秒數。目前這支 prefilter 的錯誤 JSON **沒有**帶 Retry-After，所以實際多半是 5 秒和 15 秒。`Retry-After: 0` 或不是數字，也改走 5／15 秒。

三道上限。睡眠超過前兩道，或牆鐘預算不夠再打一發，就**不再等，也不再打下一發**，直接走原來的失敗路徑：

| 上限 | 秒 | 超過時 |
|---|---|---|
| 單次等待 | 45 | Retry-After 大於 45 就不等（`wait_cap`） |
| 累計睡眠 | 60 | 再等會讓睡眠合計超過 60 就不等（`wait_cap`） |
| 重試額外牆鐘 | 120 | 見下面。不夠就不等、不再打（`budget_exhausted`） |

重試的 120 秒從**第一次辨識到 429／418 的那一刻**起算，不含第一次呼叫本身。第一次呼叫的逾時維持 240 秒，就算它跑很久，也不扣這 120 秒。這樣才能同時保住「第一次逾時不變」和「重試多出來的時間 ≤ 120 秒」。若從函式一進來就起算，第一次呼叫超過 120 秒之後就永遠不會重試。

進入重試之後，每一次呼叫的逾時是 `min(240, 剩餘的 120 秒)`。決定要不要睡之前，剩餘必須大於這次等待；剩餘 ≤ 0，或剩餘蓋不住這次等待（下一發逾時會變成 0），就停，理由記 `budget_exhausted`。`gate_log.jsonl` 的 `prefilter_retry` 會有 `retries` 和 `stopped`。

重試後仍失敗：WAKE，理由仍是 `prefilter_failed`，並多一個 `prefilter_retries=N`（N 是實際等到的次數；一次都還沒等到就只有 `prefilter_failed`）。重試後成功：照原來的規則決定 QUIET 或 WAKE，不因為重試過就叫醒。

`gate_log.jsonl` 在有辨識到 429／418 的那一輪多一個 `prefilter_retry`：`attempts`（呼叫幾次）、`retries`（實際等了幾次）、`wait_s`、`waits_s`、`stopped`（`retries_exhausted`、`wait_cap`、`budget_exhausted`，成功時是 null）。

平常沒有 429／418 時，prefilter 仍只呼叫一次。不讀金鑰、不簽章。30 分鐘排程、60 秒 watch、新鮮度上限都沒有改。累計最多睡 60 秒；manage 在 prefilter 之前就結束，這段等待不佔 live_trader 的檔案鎖。

## 在 Mac 上安裝

```bash
mkdir -p ~/quantboy-gate
cp src/gate.py ~/quantboy-gate/gate.py
python3 ~/quantboy-gate/gate.py --help
```

條件單讀取失敗要變成非 0，Mac 上的 `live_trader.py` 也要換成倉庫這一版裡的 `status` 行為。只換 `gate.py`、不換 `live_trader.py` 的話，舊的 `status` 仍可能在讀取失敗時印「條件單 0」並以 0 結束，閘門會把它當成空倉。這份文件沒有把檔案拷到 Mac，也沒有改 30 分鐘排程或 60 秒 watch。

預設的路徑如下，不一樣時用參數改：

- `--root ~/quantboy-live`：`live_trader.py` 所在目錄
- `--prefilter prefilter.py`：可以寫相對 `--root` 的檔名，也可以寫絕對路徑。要指到現行 routine 用的那支 public prefilter
- `--state-dir ~/quantboy-gate`：閘門自己的 `gate_state.json` 與 `gate_log.jsonl`。不會寫進 live 的資料目錄

切換之前先手動驗一次，確認 Mac 上那份 `live_trader.py status` 的輸出格式讀得懂：

```bash
python3 ~/quantboy-gate/gate.py --no-manage
```

`--no-manage` 只跑 `status` 和 prefilter，不送任何委託。

- 帳戶空倉、沒有候選時，應該印 `QUIET`。
- 如果印出 `WAKE status_untrusted`，代表那份 `live_trader.py` 的輸出格式跟本倉庫的不一樣。這時候閘門每一輪都會叫醒 Grok，不會出事，只是省不到 token。把 `status` 的輸出貼給工程師調整即可。

## 新的 routine prompt

**排程不用改**，一樣每 30 分鐘、彈性 ±7 分鐘。60 秒 watch 也不用動。

把現行的完整 prompt（[agent_prompt.md](agent_prompt.md) 的提示詞正文）原封不動存成 Mac 上的 `~/quantboy-gate/full_prompt.md`。routine 的 prompt 改成下面這段：

```text
每輪第一步：在 Mac mini（{{MAC_MINI_MACHINE_ID}}）執行 python3 ~/quantboy-gate/gate.py
輸出第一行是 QUIET：本輪結束。不跑其他命令、不審核、不傳任何訊息。
輸出第一行是 WAKE：讀 ~/quantboy-gate/full_prompt.md，照裡面的規則做完這一輪。
閘門已經跑過 manage --live、status 和 prefilter，結果附在輸出裡，直接拿來用，不要再重跑 manage；要開倉時仍照規則先 dry-run 再 open --live，之後再跑 manage 確認保護單。
閘門出錯或輸出看不懂，一律當成 WAKE。
```

空輪時 Grok 只會讀到這五行，加上一行 `QUIET`，不會載入完整規則。

## 怎麼看省了多少

```bash
grep -c '"QUIET"' ~/quantboy-gate/gate_log.jsonl   # 沒叫 AI 的輪數
grep -c '"WAKE"'  ~/quantboy-gate/gate_log.jsonl   # 叫 AI 的輪數
```

## 要退回原本的做法

把 routine 的 prompt 換回 [agent_prompt.md](agent_prompt.md) 的原文就好。閘門不會改動 live 的檔案或排程，刪掉 `~/quantboy-gate` 也不影響實盤。

## 測試

```bash
python3 -m unittest tests.test_gate
python3 -m unittest discover
```

測試用的是假的 `live_trader` 和 `prefilter`，不連網、不需要金鑰。`tests/__init__.py` 讓 `discover` 能匯入這個目錄。
