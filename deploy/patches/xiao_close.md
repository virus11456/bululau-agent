# 小師弟 15 分 K 收盤觸發

15 分 K 一收盤就只跑小師弟篩網。有訊號就走 Mac 上現有的 `live_trader.py` IOC 開倉。不叫 AI。

這份是設計和部署步驟。不要在這份 PR 裡 bootstrap，也不要改已經在跑的 plist。

## 為什麼不叫 AI

小師弟的進場、停損、2R 止盈都由 `xiao_shidi` 算出來。數量、1% 風險、5 倉、日損 7%、事件窗、IOC 0.3% 都在 `cmd_open` 裡。中間沒有需要模型補的判斷，所以這條路徑不讀 prompt、不跑 `gate.py`。

## 架構

`src/xiao_close.py` 是觸發器。Mac 上之後放在 `~/quantboy-live/xiao_close.py`，向兩支現有程式借功能，不複製規則：

- `--prefilter` 預設 `/Users/frank/quantboy-paper/discretionary/prefilter.py`。這是實盤 routine 在讀的那支，不是 `~/quantboy-auto-scan/prefilter.py`。
- `--trader` 預設 `~/quantboy-live/live_trader.py`。只呼叫它的 `cmd_open`。不呼叫 `main()`，避免同一支程式再搶一次鎖。
- 篩網只拉 15 分 K，只呼叫 `xiao_shidi` 和 `btc_guard`。不跑瘸師弟、布鹿流、大師兄、資金費率。
- 下單參數固定是做多、`setup=小師弟`、`portion=1`、`entry_type=limit`。`resolve_entry_type` 會把它改成 IOC。觸發器不自己算數量，也不會改成市價。
- 載入的 `live_trader` 必須把小師弟解析成 `ioc`，而且 `xiao_slippage_pct` 仍是 `0.003`。其他型態仍是呼叫端給的方式。不符合就記 `trader_not_ioc`，不寫去重、不下單。

`deploy/com.quantboy.xiao-close.plist` 是範本。Label 是 `com.quantboy.xiao-close`。裡面是 `--dry-run`。沒有 `KeepAlive`，失敗不會自動再跑。

## 時序

launchd 的 `StartCalendarInterval` 只能指定分，不能指定秒。所以排在每小時的 :00、:15、:30、:45，由程式自己等。

1. :00:00.000 這根 15 分 K 收盤，launchd 啟動程式。
2. 程式睡到收盤後 `--grace` 秒（預設 5），再向幣安要已收盤的 K 線。`klines` 本來就會丟掉還沒收的那根。
3. 用 BTC 的最後一根開盤時間對齊剛收盤的那 15 分鐘。對不齊就記 `bar_not_ready`，不寫去重、不下單。
4. 只對這一根跑 `xiao_shidi`。BTC 防護擋下就記 `btc_guard`，同樣不寫去重。
5. 沒有小師弟訊號就記 `no_signal` 結束。這一步不拿鎖。

有訊號而且是 `--live` 時才進入下面的臨界區。掃描本身不拿 `~/quantboy-live/.lock`，避免整輪行情把 60 秒 watch 擋住。

log 每一輪一行 JSON：觸發時間、K 線收盤時間、延遲秒數、篩網結果、每個訊號的下單結果或放棄原因。`--dry-run` 只印在 stdout，不寫這個檔。

## 和 15 分鐘 routine 怎麼錯開

routine 真正下單、以及 `manage` / `status` / `watch`，都進 `live_trader.main` 的 `Lock`，鎖檔是 `~/quantboy-live/.lock`。觸發器用同一檔做 `flock`。拿到之前最多等 `--lock-wait` 秒（預設 25）。等到了才去重、才呼叫 `cmd_open`。等不到就記 `lock_busy`，不寫 `signal_seen`，讓正拿著鎖的那一邊自己做完。

`cmd_open` 在同一個程序裡呼叫，鎖還在我們手上。如果改成子程序去跑 `live_trader.py open`，子程序會再拿一次這把鎖，自己把自己擋住。

routine 的 prefilter 在交給 AI 之前就會把新訊號寫進 `signal_seen.jsonl`。編號是 `xiao|幣|最低點時間`。觸發器呼叫同一支 `filter_new_signals`，`--seen` 指到同一個檔。誰先寫進去，另一邊就不會再下這一筆。寫入時用旁邊的 `signal_seen.jsonl.lock`，兩支程序不會一起讀到「還沒有」再各寫一次。

因此：

- 觸發器先寫、先下：routine 後面那輪 prefilter 看不到這筆，不會再 WAKE、不會再下。
- routine 的 prefilter 先寫：觸發器看成重複，不下。這一根仍由 routine 的 AI 決定，只是沒有搶到收盤那幾秒。
- 兩邊不會同時送出開倉單。

`--asof` 和 `--nolog` 仍不寫去重檔，也不建鎖。

Mac 上那支 prefilter 沒有 `DATA_DIR`，去重檔的預設目錄是它自己的 `HERE`，也就是 `/Users/frank/quantboy-paper/discretionary/signal_seen.jsonl`。plist 把 `--seen` 寫死在這條路徑，避免和 `~/quantboy-live` 分成兩個檔。

沒有 `low_t` 的舊版 `xiao_shidi` 產不出穩定編號。觸發器會記 `no_signal_id` 並拒絕下單，避免和 routine 各下一筆。

## 失敗

交易所錯誤（`BinanceError`）和網路錯誤（`URLError`、逾時、`OSError`）記下來，這一輪結束。不再呼叫 `cmd_open`，也不把 `entry_type` 改成市價。

`live_trader.req` 裡原本的 429／5xx 重試沒有改。那是同一張單的 HTTP 重試，不是失敗後改掛市價。

風控拒絕（日損、5 倉、事件窗、停損方向）是 `cmd_open` 自己的 `SystemExit`。觸發器記 `risk_blocked` 後結束，不換一條路徑再送。

IOC 沒成交、或取整後風險仍超過 1%，沿用 `cmd_open` 現在的放棄，不改掛市價。

## 價已經超過止盈

不要做 `kuai_target_already_hit`。使用者決定：現價已經超過止盈時，照現在一樣先掛再撤。

未成交的限價仍由 watch／manage 的 `cancel_unfilled_if_target_hit` 處理。小師弟 IOC 若成交當下標記價已經越過止盈，仍走現有的 `target_passed_at_fill`（先掛停損，再平倉）。這次不加「下單前發現止盈已過就直接不送」的捷徑。

## 沒有改的東西

- `src/gate.py`、`src/hardfilter.py`、`docs/skill.md`
- 現有的 plist（含 `deploy/com.quantboy.userstream.plist`）
- watch、`xiao_slippage_pct`、IOC 上限 0.3%
- `live_trader` 的風控式子。觸發器只是呼叫 `cmd_open`

## Mac 部署步驟

這份 PR 只寫步驟。不要在合併前做，也不要在這次把 plist 載入 launchd。

先決條件：Mac 的 `/Users/frank/quantboy-paper/discretionary/prefilter.py` 已經有這支 `xiao_shidi`（回傳 `low_t`）、`signal_id`、`default_seen_path`、帶檔案鎖的 `filter_new_signals`。`~/quantboy-live/live_trader.py` 已經有小師弟 IOC，而且 `xiao_slippage_pct` 是 `0.003`。兩支都不要整份覆蓋。prefilter 保留它自己的 `from common import *` 和 `HERE`。live_trader 不要動 `_keys()`。

1. 避開交易輪次。不要在台北時間 :00、:15、:30、:45 前後 3 分鐘動手，routine 可能在跑。建議放在 :07 到 :12。
2. 先備份，檔名帶時間，留下 `.bak`：

```bash
stamp=$(date +%Y%m%d%H%M%S)
cp -p /Users/frank/quantboy-paper/discretionary/prefilter.py \
  /Users/frank/quantboy-paper/discretionary/prefilter.py.bak.$stamp
cp -p ~/quantboy-live/live_trader.py ~/quantboy-live/live_trader.py.bak.$stamp
shasum -a 1 /Users/frank/quantboy-paper/discretionary/prefilter.py ~/quantboy-live/live_trader.py
```

若 `~/quantboy-live/xiao_close.py` 已經在，也先 `cp -p` 成 `.bak.$stamp`。這次不要覆蓋 `live_trader.py`，除非只手貼上面那幾個還沒上去的函式。

3. 只新增觸發器，不覆蓋 watch、gate、既有 plist：

```bash
cp -p src/xiao_close.py ~/quantboy-live/xiao_close.py
```

4. 讀回 sha1，要和倉庫裡這支檔相同：

```bash
shasum -a 1 ~/quantboy-live/xiao_close.py
shasum -a 1 src/xiao_close.py
```

prefilter 若有手貼，再讀一次 `/Users/frank/quantboy-paper/discretionary/prefilter.py`，和步驟 2 的 `.bak` 對過，確認只動了小師弟去重那幾個函式。

5. 手動 dry-run。這一步不寫 `signal_seen`、不寫 `state.json`、不寫 `live_log.jsonl`、不建鎖：

```bash
python3 ~/quantboy-live/xiao_close.py --dry-run --grace 0 \
  --prefilter /Users/frank/quantboy-paper/discretionary/prefilter.py \
  --trader ~/quantboy-live/live_trader.py \
  --seen /Users/frank/quantboy-paper/discretionary/signal_seen.jsonl
```

stdout 應有一行 JSON，裡面有 `trigger_tpe`、`bar_close_tpe`、`delay_s`、`screen`、`result`。

6. plist 只當範本留在倉庫的 `deploy/com.quantboy.xiao-close.plist`。要裝的時候把 `REPLACE_USER` 換成 `frank`，使 prefilter 路徑正好是 `/Users/frank/quantboy-paper/discretionary/prefilter.py`。範本裡是 `--dry-run`。不要 `launchctl bootstrap`，也不要改 `com.quantboy.livewatch` 或其他已經載入的 plist。

使用者要改成實盤時，才在已經裝好的那份把 `--dry-run` 換成 `--live`，再 bootstrap。沒有 `KeepAlive`：失敗記在 log 裡，不會自動再下一筆。這份 PR 不做這一步。
