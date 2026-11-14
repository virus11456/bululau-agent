# 手貼：階梯停損數量、2 小時撤單、使用者資料流

這份是第二個 PR，跟跨日那個 PR 分開。Mac 的 `~/quantboy-live/live_trader.py` 不從 `config.py` 讀金鑰。不要整份覆蓋。下面的新函式呼叫的 `req`、`_keys`、`place_protect` 都用 Mac 檔裡既有的。

## R2：部分成交時停損維持計畫總量

`manage_ladder_pending` 裡，部分成交原本呼叫 `resize_protects(..., 實際持倉)`，停損會被縮小。改成呼叫 `resize_ladder_partial`。

停損用 `reduceOnly` 加上計畫總量，不用 `closePosition`。幣安規定 `closePosition=true` 不能同時帶 `quantity`，所以沒有辦法一邊標整筆數量、一邊用 closePosition。`reduceOnly` 只減倉，數量大於現在的持倉也不會開出空單。止盈仍改成實際持倉，觸發價不重算。

把倉庫 `src/live_trader.py` 的 `resize_ladder_partial` 貼到 `resize_protects` 前面，並把部分成交那段的 `resize_protects(...)` 換成：

```python
        planned = Decimal(str(pd["qty"])) if pd.get("qty") else sum(
            (Decimal(str(o.get("origQty") or "0")) for o in rows), Decimal(0))
        try:
            resize_ladder_partial(sym, close_side, Decimal(str(abs(amt))), planned, live)
        except Exception as e:
            log("error", msg="調整階梯保護單數量失敗", symbol=sym, err=str(e))
```

## O12：超過 2 小時的未成交檔

貼上 `LADDER_EXPIRE_MS`、`ladder_anchor_ms`、`expire_unfilled_ladder`。在 `manage_ladder_pending` 查完每一檔之後呼叫 `expire_unfilled_ladder`。`open_ladder` 寫入 pending 時加 `placed_at`。

`cmd_watch` 的階梯分支在決定要不要叫 manage 時，若掛出已超過 2 小時且還有 NEW 或 PARTIALLY_FILLED，也要叫 manage。單張限價的那個 `if not pd.get("orderId")` 之後不要改，單張仍不自動撤。

完全沒成交：撤進場單，並撤預掛停損，log 是 `ladder_expire`。有成交：只撤還沒成交的進場單，不停損、不止盈、也不平倉。

## O8：使用者資料流

新檔 `src/user_stream.py` 整份拷到 `~/quantboy-live/user_stream.py`。它用標準庫的 `socket` 和 `ssl` 做 websocket，不必 `pip install`。Python 3.9 就夠。

`cmd_watch` 一開頭加：

```python
    from user_stream import heartbeat_fresh
    if heartbeat_fresh():
        return
```

心跳檔是資料目錄的 `.ws_watch_heartbeat`。90 秒內有更新，60 秒的 `watch --live` 就不打 API。過期就照原來的輪詢。

launchd 用新的 `deploy/com.quantboy.userstream.plist`（Label 是 `com.quantboy.userstream`）。不要改既有的 `com.quantboy.livewatch`。

```bash
cp deploy/com.quantboy.userstream.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quantboy.userstream.plist
```

程式路徑若不是 `/Users/frank/quantboy-live/user_stream.py`，先改 plist 再載入。

## O9

沒有改程式。排程建議寫在 PR 說明：gate 改到每小時的 :09、:24、:39、:54，避開整點後 0 到 7 分。
