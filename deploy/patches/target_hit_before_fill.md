# 手貼：未成交但價已到止盈

Mac 的 `~/quantboy-live/live_trader.py` 不要整份覆蓋，也不要改 `_keys()`。Python 3.9。

## 價先到止盈

`cmd_watch` 在 60 秒輪詢裡，某筆完全沒有成交（每張 `executedQty` 是 0、持倉是 0），而且標記價已到計畫止盈，就撤掉這筆全部限價單，以及它的停損和止盈條件單。log 的 `kind` 和 `event` 都是 `target_hit_before_fill`，然後把這個幣從 `pending` 拿掉。

做多是標記價 ≥ 止盈，做空是標記價 ≤ 止盈。價格用 `GET /fapi/v2/positionRisk` 的 `markPrice`，因為保護單本來就是 `MARK_PRICE`。同一筆回應裡也有 `positionAmt`，所以不必再查最新價。只有訂單已經顯示完全沒成交時才打這支；部分成交不打。

任一張限價單、positionRisk，或條件單讀取失敗，這一輪什麼都不撤。

部分成交仍走原來的補掛，以及超過 2 小時的 O12。這條新規則不管它們。

## 成交前不預掛止盈

`open` 的限價單和 `open --ladder` 成交前只預掛停損，不再預掛止盈。

停損繼續預掛：它是 `reduceOnly`，空倉時不會開出反向倉。限價若在急跌裡成交，停損已經在場上，不會有一段裸倉。止盈改到成交之後由 `manage` 補掛（`watch` 看到有成交且沒有止盈會叫 `manage`）。補掛失敗仍只記「補掛止盈失敗」，下一輪再試。`no_tp` 仍只給手動 `mark-reduced` 用，失敗不會自動寫上這個標記。

市價進場當時就有倉，停損和止盈仍一起掛。
