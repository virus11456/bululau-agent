# 布神式幣安 USDT-M 合約策略

這是布神四種型態（布鹿流、大師兄、小師弟、瘸師弟）的策略與執行器骨架，只做多。策略條文以 2026-11-06 22:23（台北時間）的定稿為準，寫在 [docs/strategy.md](docs/strategy.md)。現行實盤怎麼跑，寫在 [docs/operation.md](docs/operation.md)。

**策略定稿仍是 11-06 那份；實盤已在跑，但不是本倉庫自動下單。** 2026-11-07 00:47（台北）使用者要求接手實盤後，由 Mac mini 上的原執行器與「布神式實盤交易」routine 執行，並保留 60 秒機械 watch。掃描在 2026-11-11 19:09 起是每 15 分鐘（`*/15`，彈性 ±3）；11/7 19:44 到那時是每 30 分鐘。11/8 06:02 曾因幣安 401 暫停，11/10 10:03 已恢復。本倉庫不存放帳戶餘額。11-06 文件裡那套「固定程式掃完全市場再交給監控程式」尚未全部作為現行實盤路徑部署。空倉省 token 用的是 main 上的 `src/gate.py`，Mac 已在跑。[PR #3](https://github.com/virus11456/bululau-agent/pull/3) 於 2026-11-12 12:21（台北）關閉、未合併，不是現行實盤。

合約可以虧掉全部保證金。這不是投資建議。

## 四種型態

| 型態 | 在做什麼 | 常用週期 | 出場（現行） |
|---|---|---|---|
| 布鹿流 | 順勢。第一波之後的回檔，底底高。七八波以上才考慮放棄 | 1H 以上 | 1.5R 平一半，剩餘移到成本，再依結構移動 |
| 大師兄 | 大週期偏逆勢。長黑後等反彈，二次測前低 | 4H／8H 為主 | 本次不改原文。出場 R 見策略文件的待確認，不平一半 |
| 小師弟 | 急殺後反彈到跌幅的 40–50% 才設計 | 15m | 1.5R 平一半不適用。出場 R 待確認 |
| 瘸師弟 | 爆量長黑之後往下低掛。15m 瞬間單根長黑（BTC／ETH 2%、小幣 4%） | 15m | 1.5R 全部平倉，不移停損。不是 2R |

15 分鐘的瘸師弟與小師弟用結構停損，至少 1–2%；BTC、ETH 至少 3%。不套小幣 10% 下限。布鹿流的停損是前低下方 1–2% 容錯，再與 BTC／ETH 3%、小幣 5% 取較寬者。完整條文與出處在策略文件。

## 現行掃描與尚未部署的目標分工

現行實盤是每 15 分鐘（`*/15`，彈性 ±3）在 Mac 先跑 `gate.py`。`QUIET` 結束本輪；`WAKE` 再用閘門附上的 `manage --live`、`status`、public prefilter 做定稿審核。60 秒 watch 另外負責保護單。實際使用的提示詞在 [docs/agent_prompt.md](docs/agent_prompt.md)。

11-06 定稿另外寫過一套目標，還沒有寫成新的掃描程式，也還沒接到現行 routine：

- 固定程式掃全市場。沒有候選就不呼叫 AI，只留一行 log。11-06 寫每 15 分鐘；11/7 曾改成每 30 分鐘；2026-11-11 19:09 起現行又是每 15 分鐘（`*/15`，彈性 ±3），由 gate 先跑，不是那套固定掃描已經全部上線。
- AI 只判斷候選的型態、份數、進場、停損、停利。
- 監控程式複核條件、執行風控，並監視停損與停利。複核失敗就丟棄。
- 通過的單先記在紙上。現有的 `position_manager.py` 不是這套監控。

日 K 均線用哪一條、大師兄與小師弟的出場 R、V 轉收不回時怎麼處理、連敗後縮小多少，都在策略文件的待確認區。這些沒有預設數字。

## 省 token 閘門

`src/gate.py` 讓 Grok 每輪先跑一個命令。新鮮、可信的空倉、0 單、0 候選時印 `QUIET`，Grok 直接結束本輪，不載入完整規則。其他情況印 `WAKE` 並附上 manage／status／prefilter 的結果，再照原本的 prompt 做。安裝見 [docs/gate.md](docs/gate.md)。這支程式不下單，已在 main，Mac 的 `gate_log.jsonl` 自 11/10 起有列。倉庫裡的 `live_trader.py status` 在條件單讀取失敗時會非 0 結束；Mac 上那份有沒有換成這一版，這次未取得。

## 環境需求

- Python 3.9 或以上（建議 3.11）
- Linux 或 macOS。現有執行器用 `fcntl` 檔案鎖
- 沒有第三方套件，見 `requirements.txt`
- 若將來重啟實盤，幣安 USDT-M 必須是單向持倉。那一步現在還沒到

## 設定

金鑰只放本機，不進 git。只開讀取與 USDT-M 合約交易，不要開提現。能綁 IP 就綁實際執行實盤的那台 Mac。金鑰不要放到 box 或本倉庫。

```bash
cp config.example .env
chmod 600 .env
# 編輯 .env。沒有要讀取帳戶時，可以先留空

mkdir -p data
cp config/events.example.json data/events.json
```

資料目錄預設是專案下的 `data/`。若在某台機器啟用執行器，請把 `QUANTBOY_DATA_DIR` 設成絕對路徑。現行實盤排程在 Mac 上，不由本倉庫直接啟停。

## 目錄

```text
config.example                  金鑰與路徑範本
config/events.example.json      重大事件範例
src/live_trader.py              舊執行器。交易計算這次沒改。不要加 --live
src/prefilter.py                舊篩選，不是 11-06 的全市場掃描
src/sentiment.py                情緒讀數，不下單
src/position_manager.py         2026-10 紙上參數，不要拿來做出場
src/config.py                   資料目錄與金鑰讀取
src/gate.py                     省 token 閘門，空輪不叫 AI
docs/gate.md                    閘門安裝、判斷條件與新的 routine prompt
docs/strategy.md                2026-11-06 定稿，並附 11-07 起的操作變更
docs/operation.md               現行實盤排程、啟停與尚未部署的目標架構
docs/agent_prompt.md            現行實盤 routine 已核提示詞（機器代號已改佔位符）
docs/sources/                   規則來源與 prompt 快照。私人連結已移除
```

## 現有指令

下面的價格只是格式，不是進場建議。不要加上 `--live`。

```bash
python3 src/sentiment.py BTCUSDT
python3 src/prefilter.py --quiet
python3 src/live_trader.py --help
```

`prefilter.py` 結束碼 10 只代表舊篩選有輸出，不等於定稿合格，不應因此呼叫 AI 去下單。結束碼 0 是沒有舊候選。結束碼 2 是行情失敗。

`live_trader.py` 仍然認得 `status`、`open`、`manage`、`watch`、`flatten`。加上 `--live` 會對交易所送出委託。現行實盤只在使用者的 Mac mini 上由操盤手依規則加 `--live`。不要從其他環境對該帳戶排程或下單。本倉庫這次沒有改這些程式。

## 安全紅線

只做多。BTC 帶量破關鍵價後兩根 15m 不開多。重大事件前後 30 分鐘不開新倉。當日虧 7% 停開新倉，30% 全停並回報。單筆預估虧損不超過淨值的 1%，停損先掛在交易所。最多 5 個持倉，單幣一個倉。連 3 敗當天停手。風報比不到 1.5 不進。金鑰不進倉庫、不開提現。

## 風險聲明

- 實盤委託的虧損由使用者承擔。現行實盤已在 Mac 上執行，不是本倉庫自動下單。
- 停損單可能滑價。流動性差的合約可能超出預估虧損。
- 30% 旗標若已存在於某台機器的資料目錄，沒有使用者明確同意，不要刪除。
- 這份文件沒有讀到帳戶，任何人都不該從本倉庫推論資金是否可用。
