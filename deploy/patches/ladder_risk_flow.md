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

完全沒成交：先撤進場單。任何一張撤單失敗（不是 -2011）就停，不撤停損，pending 留著。撤完要再查每一張 `executedQty`。確認全是 0、沒有持倉、也沒有還掛著的檔，才撤預掛停損。再查看到成交，或還有 NEW／PARTIALLY_FILLED，停損不動。有成交：只撤還沒成交的進場單，不停損、不止盈、也不平倉。

## O8：使用者資料流

新檔 `src/user_stream.py` 整份拷到 `~/quantboy-live/user_stream.py`。它用標準庫的 `socket` 和 `ssl` 做 websocket，不必 `pip install`。Python 3.9 就夠。

`cmd_watch` 一開頭加這段，不要直接 `import user_stream`。Mac 上若還沒拷 `user_stream.py`，60 秒 watch 必須照舊跑：

```python
def _user_stream_fresh():
    try:
        from user_stream import heartbeat_fresh
    except ImportError:
        return False
    try:
        return bool(heartbeat_fresh())
    except Exception:
        return False
```

然後 `if _user_stream_fresh(): return`。心跳檔是資料目錄的 `.ws_watch_heartbeat`。90 秒內有更新，60 秒的 `watch --live` 就不打 API。過期、或沒有那個模組，就照原來的輪詢。

資料流呼叫 `manage` 時要拿 `live_trader.Lock()` 這把非阻塞鎖（`manage_under_lock`）。拿不到就晚一秒再試，不能讓 `SystemExit` 把程序殺掉，也不能在沒鎖的時候讀寫 `state.json`。成交事件先合併約 2.5 秒，只跑一次 manage。

連上 websocket 之後要把 recv 逾時改成 180 秒。閒置逾時只送遮罩過的 ping，不重連、不重拿 listenKey。重連要指數退避。HTTP 429 照 Retry-After 等待。HTTP 418 停止，不要再打 listenKey。客戶端的 ping／pong 必須加遮罩。

launchd 用新的 `deploy/com.quantboy.userstream.plist`（Label 是 `com.quantboy.userstream`）。不要改既有的 `com.quantboy.livewatch`。plist 裡寫的是 `REPLACE_USER`，沒有寫死使用者名稱。這個 PR 還沒審過之前，不要 bootstrap。

安裝前先看實際使用者與家目錄，再替換路徑：

```bash
id -un
echo "$HOME"
ls "$HOME/quantboy-live"
```

確認 `user_stream.py` 會放在哪個目錄（通常是 `$HOME/quantboy-live`）。若家目錄是 `/Users/某個名字`，把 plist 裡的 `REPLACE_USER` 換成 `id -un` 的結果。若不是 `/Users/<名字>`，把程式路徑、WorkingDirectory、StandardOutPath、StandardErrorPath 四個都改成那個實際目錄。log 檔要在可寫的目錄。

```bash
USER_NAME=$(id -un)
sed "s/REPLACE_USER/${USER_NAME}/g" deploy/com.quantboy.userstream.plist > "$HOME/Library/LaunchAgents/com.quantboy.userstream.plist"
# 審過之後才載入：
# launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.quantboy.userstream.plist"
```

## O9

沒有改程式。排程建議寫在 PR 說明：gate 改到每小時的 :09、:24、:39、:54，避開整點後 0 到 7 分。
