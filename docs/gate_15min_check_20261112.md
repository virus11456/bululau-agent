# 閘門 15 分鐘排程核對（2026-11-12）

對照 [issue #11](https://github.com/virus11456/bululau-agent/issues/11)。資料是 Mac `~/quantboy-gate/gate_log.jsonl` 在 2026-11-12 12:17（台北）的完整副本，128 列，從 `2026-11-10T15:04:41+08:00` 到 `2026-11-12T12:17:08+08:00`。重算用的是 main `37914a1` 的 `src/gate.py`（sha1 `5a3c5a8eab52e0bb92ff3cedc20a3a31523b80c3`）。這份 log 沒有餘額。HTTP 狀態碼、prefilter 的 `time`、新鮮度檢查當下的時鐘，log 裡沒有，下面寫「未取得」，沒有用別的檔補。

結論：時間判斷與這支程式一致，沒有要修的 bug。15 分鐘與 ±3 分不進新鮮度公式。429／418 沒有因為改 15 分鐘而出現「重試仍失敗」。

## 排程實際長什麼樣

issue 寫 2026-11-11 19:09（台北）由每 30 分鐘改成 `*/15`、彈性 ±3，而且常晚開始。以該時刻切開：

| | 輪數 | QUIET | WAKE |
|---|---|---|---|
| 19:09 之前 | 57 | 44 | 13 |
| 19:09 起（含） | 71 | 31 | 40 |
| 合計 | 128 | 75 | 53 |

19:09 之後 70 個間隔：最短 0.15 分，最長 24.0 分。12–18 分有 36 個，9–21 分有 55 個，短於 9 分有 9 個，長於 21 分有 6 個。相對最近的 `*/15` 刻度，71 輪的偏移中位數是晚 171 秒；晚超過 3 分的 33 輪，早超過 3 分的 2 輪。短間隔裡有 03:30:40 與 03:30:49（0.15 分）、19:49 與 19:51（1.95 分）、12:14 與 12:17（2.80 分）。這是排程有沒有準時，不是閘門把上一輪的讀數拿來當這一輪。

## 時間判斷怎麼重算

`decide()` 不看距上一輪幾分鐘。QUIET 要的「上一輪也是空的」是狀態檔 `last == flat`（檔案根本不存在的第一次也可以），不是上一輪的 `decision` 必須是 QUIET。`previous_state_untrusted` 只在上一輪 `last` 不是 `flat`／`busy` 時出現。帳本讀不到或過期就存 `unknown`，下一輪因此不可信。

新鮮度是同一輪裡的事。`READING_MAX_AGE` 是 status 逾時 90 秒 + prefilter 逾時 240 秒 + 分鐘捨去 60 秒 + 餘裕 90 秒，一共 480 秒。時間超前超過 120 秒也不可信。15 分鐘、±3 分、晚開始都不在這個式子裡。status 的「時間」只到分鐘，印在 status 開始時；檢查在 status 與 prefilter 都結束之後。

用 log 裡的 `status` 往下推每一輪存進去的 `last`，再跟下一輪的理由比：

- `previous_state_untrusted` 該出現卻沒有、或不該出現卻有：0 列。
- QUIET 但帳本不是 0、有旗標、`n_candidates` 不是 0、或上一輪不是 `flat`：0 列。
- 理由裡沒有 `status_stale`、`prefilter_stale`、`book_just_went_flat`。
- 有 `status.as_of` 的 94 列，`time` 減 `as_of` 是 0–59 秒，都在同一分鐘，沒有超前。檢查當下的年齡與 prefilter 的 `time`：未取得。沒有一列在 prefilter 還沒跑完之前，`as_of` 就已經舊超過 8 分鐘。

有 8 列 QUIET 的上一輪是 WAKE（7 列 `candidates`、1 列 `prefilter_failed`）。那幾輪的帳本是 `flat`，所以下一輪可以 QUIET。這和「上一輪 decision 必須是 QUIET」不同，和程式一致。

11/12 帳戶失敗的銜接也對：

- 03:14:04 QUIET，帳本 flat。03:30:40 第一輪失敗只有 `manage_failed`、`status_untrusted`，沒有 `previous_state_untrusted`。
- 03:30:49 起，上一輪已存成 `unknown`，理由才多 `previous_state_untrusted`。一直到 11:36:19 都是這樣。
- 11:44:26 status 恢復（0 倉 0 單），理由是 `candidates`、`previous_state_untrusted`。12:00:29 只剩 `candidates`。12:17:08 QUIET。

`catchup` 欄位只在最後一列 12:17 出現，值是 null。03:30–11:36 那 34 輪沒有留下候選身份。這表示當時 Mac 上的 gate 還沒有 #22 的補審；不是 15 分鐘把時間算錯。main 上的 `src/gate.py` 現在會記身份，這份舊 log 重算不出當時有沒有寫進狀態檔。

重試的 120 秒牆鐘沒有加進 480 秒。#9 寫明新鮮度上限不改。這份 log 裡，有 status 又有重試的兩輪（11/11 14:06、16:06）都是 QUIET，沒有因此變成 `status_stale`。若以後一次重試把檢查拖過 8 分鐘，會多一個 `status_stale`，方向是 WAKE，不會把過期讀數 QUIET。這次不改這個餘裕。

## 429／418 與 401 分開

log 全文沒有 `429`、`418`、`401`、`-2015` 這幾個字。狀態碼能確定的，只有程式在辨識到 429 或 418 時才會寫的 `prefilter_retry`。欄位不記是 429 還是 418，所以拆不開，下面合在一起。

辨識到限流、而且有重試紀錄的 4 輪，全部重試後成功（`stopped` 是 null，理由沒有 `prefilter_failed`）。等待是 5 秒，或 5 秒加 15 秒，和沒有 Retry-After 時的退避相同。

| 時間（台北） | 決策 | attempts | retries | wait_s | 同時的其他理由 |
|---|---|---|---|---|---|
| 11/11 14:06:54 | QUIET | 2 | 1 | 5 | 無。重試成功不另叫醒 |
| 11/11 16:06:40 | QUIET | 3 | 2 | 20 | 無 |
| 11/12 09:02:51 | WAKE | 2 | 1 | 5 | 只有帳戶失敗那三個理由，prefilter 重試後 `n_candidates` 0 |
| 11/12 11:07:47 | WAKE | 3 | 2 | 20 | 同上 |

19:09 改 15 分鐘之前、重試欄位已經存在之後：2 輪，都成功。改了之後：2 輪，也都成功。沒有 `retries_exhausted`、`wait_cap`、`budget_exhausted`。

另外 2 輪只有 `prefilter_failed`，當時還沒有 `prefilter_retry` 欄位，這份檔看不出狀態碼，記未取得：

- 11/10 21:01:43
- 11/11 02:03:54

倉庫 CHANGELOG 把這兩輪記成公開 K 線 429，出處是 prefilter_log，不是這份 gate_log。

hardfilter：有這個物件的 25 列，`failed` 都是 false。其中 13 列有 `retries` 且是 0；較早 12 列沒有這個欄位，有沒有重試未取得。理由裡沒有 `hardfilter_retries`。

帳戶那一類和上面分開。`manage_failed` 與 `status_untrusted` 同時出現 34 輪，全部落在 11/12 03:30:40 到 11:36:19，`status` 都是 null，視窗外是 0 輪。其中 30 輪再加 `previous_state_untrusted`，3 輪再加上 `candidates`，第一輪 03:30:40 只有前兩個理由。這 34 輪沒有 `prefilter_failed`。其中 09:02 與 11:07 兩輪同時有上面的限流重試，而且重試成功。這份 log 沒有 401／-2015 原文，所以這 34 輪的 HTTP 狀態碼未取得；時間和回報的帳戶失敗窗口一致，結構也是 manage、status 一起失敗。

理由組合：

| 次數 | 理由 |
|---|---|
| 75 | （空，QUIET） |
| 30 | manage_failed、status_untrusted、previous_state_untrusted |
| 16 | candidates |
| 3 | manage_failed、status_untrusted、candidates、previous_state_untrusted |
| 2 | prefilter_failed |
| 1 | manage_failed、status_untrusted |
| 1 | candidates、previous_state_untrusted |

## 可貼到 issue #11

15 分鐘（`*/15`、彈性 ±3，log 裡常晚於刻度）沒有改到 gate 的時間判斷。用 2026-11-12 12:17（台北）的 `gate_log.jsonl` 128 列，對 main `37914a1` 的 `src/gate.py` 重算：`previous_state_untrusted` 該有就有、不該有就沒有，0 列不符；75 列 QUIET 的上一輪帳本都是 flat，沒有過期或超前理由。新鮮度上限仍是 8 分鐘，算的是同一輪 status／prefilter，不是兩輪間隔。有 status 的 94 列，`as_of` 與該輪開始差 0–59 秒。檢查當下的精確年齡未取得。

429 與 418 在 log 裡分不開（沒有狀態碼，只有重試欄位）：辨識到限流 4 輪，等待 5 秒或 5+15 秒，4 輪都重試成功，沒有再失敗。改 15 分鐘前 2 輪、改了之後 2 輪。hardfilter 有紀錄的重試是 0。另有 2 輪只有 `prefilter_failed`（11/10 21:01、11/11 02:03），這份檔的狀態碼未取得。

帳戶失敗是另外 34 輪（11/12 03:30:40–11:36:19）：`manage_failed` + `status_untrusted`，status 為 null。log 沒有 401／-2015 字樣，狀態碼未取得。沒有和 `prefilter_failed` 混在同一輪。第一輪沒有 `previous_state_untrusted`（上一輪 03:14 是 flat），下一輪起才有，11:44 恢復後再多叫醒一輪，12:17 回到 QUIET。與程式一致。

沒有要改的 bug，也沒改門檻、頻率或 watch。
