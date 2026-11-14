# 手貼：權重、status 快取、平倉分類

Mac 的 `~/quantboy-live/live_trader.py` 不從 `config.py` 讀金鑰，sha1 前綴曾是 `95522934`。不要整份覆蓋，也不要改 `_keys()`。下面的函式用檔裡既有的 `req`、`log`、`HOME`。Python 3.9。

這份還沒部署。O4（prefilter 增量 K 線快取）沒做：倉庫裡沒有一份「完整抓 K」的回放可以證明快取後的輸出逐位元組相同。

## O6：418 停止，429 仍重試

`req()` 遇到 HTTP 418：寫下共用檔、記 `backoff_418`、立刻拋出，不重試。429 或 `-1003` 仍用 `Retry-After`，沒有就 `min(60, 2 ** (attempt + 1))`，最多 5 次。

共用檔是 `os.path.join(HOME, ".ratelimit_until")`。Mac 上就是 `~/quantboy-live/.ratelimit_until`。內容是 `418 <epoch>` 或 `429 <epoch>`。檔案不在、空的、或格式不對就當作沒有退避。下一個請求若還在 418 的時間內，直接停；429 就先睡到那個時間。

## O7：權重

每次回應記下 `X-MBX-USED-WEIGHT-1M`，這個程序只留最大值。大於 1800 記 `weight_high`，不因此延後掃描。watch 心跳在看過權重時，行尾多 ` weight=N`。

## O2：`manage --with-status`

只加在 `manage`。用這一輪已經拿到的帳戶印出跟 `status` 一樣的欄位，不再呼叫第二次 `account()`。條件單要另外做一次 fail-closed 的全量讀取；讀失敗印「條件單讀取失敗」並以非 0 結束，不能印「條件單 0」。沒有這個旗標時，manage 不印 status、也不多讀。

某一幣的條件單或掛單讀失敗時，這一輪跳過該幣，記 log，不平倉。平倉後，以及同一幣沒有 pending 就再開倉之前，按幣清掉殘留的停損／止盈。

## O3：雙向與多資產只在 status 快取

`status` 和 `manage --with-status` 把 `positionSide/dual`、`multiAssetsMargin` 存到 `HOME/.account_mode_cache.json`，1 小時內重用。檔案壞掉或過期就重抓。`open` 下單前仍即時查雙向，不讀這份快取。

## O5：孤兒單不要每次都全市場掃

持倉、pending、meta 裡的幣，每次 manage 都按幣查 `openOrders` / `openAlgoOrders`。全市場的孤兒掃描改成第 1、5、9… 輪（`orphan_scan_n % 4 == 1`），以及這一輪剛剛記了 `position_closed` 的時候。`orphan_scan_n` 不是日欄，跨日不會被清掉。沒有計數的第一輪仍會全量掃，所以空帳戶上的孤兒單還是會被撤。

## 階梯 watch

第一檔已成交、其餘還掛著時，不要因為「有檔已經結束」就每分鐘叫 manage。比對 `last_done`：已結束的檔數變多才再叫。沒有停損、或成交量變了而且缺保護單，仍要叫。

## 減倉標記不要被階梯結束蓋掉

`mark-reduced` 若在階梯還在 pending 時做過，階梯全部成交轉成 meta 時要留下 `reduced`、成本價 `stop`、`no_tp`。程式仍不幫你改交易所上已經掛著的停損。

建議之後補進 SKILL（這份 PR 不改 `docs/skill.md`）的句子，放在減倉那一節：

> 手動減倉時，先在交易所把停損改到成本價（先掛新的、確認成功再撤舊的），然後才跑 `python3 live_trader.py mark-reduced --symbol 幣種 --live`。`mark-reduced` 只寫標記，不會移動已經掛著的停損單。布鹿流標記後程式不再補掛止盈。

## 平倉分類與數字

出場價用該幣的 tick 做 `Decimal` quantize。已實現損益印出來和寫進 log 的 `pnl` 是 4 位小數。跟原始值不同時，log 另加 `exit_price_raw`、`pnl_raw`。

價差帶從 0.3% 放寬到 1.5%（SHELLUSDT 止盈約 1%：出場 0.03908、目標 0.03869）。觸發後的實單若是 `MARKET`，再對 `GET /fapi/v1/allAlgoOrders`（只讀，用 `actualOrderId`）找原來的條件單。`TAKE_PROFIT`／`STOP`，包含 `closePosition` 的，仍記為止盈或停損。

## 手動全平

`flatten` 的理由是 `manual_flatten` 時，對當時有倉而且 meta 裡有的幣記 `position_closed`，`reason` 是 `manual`，並把該幣從 meta 拿掉。`--live` 才存檔。
