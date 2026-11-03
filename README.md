# 布神式幣安 USDT-M 合約實盤交易機器人

這是一個做多的幣安 USDT-M 永續合約執行器，搭配四種圖形型態使用：布鹿流、大師兄、小師弟、瘸師弟。程式負責風控閘門、算數量、下單與把停損掛上交易所；型態判斷與減倉節奏由人，或由每 15 分鐘跑一次的 AI 代理，依照 [docs/strategy.md](docs/strategy.md) 決定。

最新規則以 2026-11-03 的實盤例行流程為準，全文在 [docs/sources/routine_live_prompt_2026-11-03.md](docs/sources/routine_live_prompt_2026-11-03.md)。操作步驟在 [docs/operation.md](docs/operation.md)。給代理貼上的提示詞在 [docs/agent_prompt.md](docs/agent_prompt.md)。

合約可以虧掉全部保證金。這不是投資建議。先用模擬輸出（不加 `--live`）看過計畫，再用你虧得起的資金。

## 四種型態

| 型態 | 在做什麼 | 常用週期 | 出場節奏（現行） |
|---|---|---|---|
| 布鹿流 | 順勢。等明確第一波之後的回檔，底底高才進 | 1H 以上 | 先掛 2R 保護；過不了前高出場，突破則續抱 |
| 大師兄 | 大週期偏逆勢。爆量長黑之後等反彈，二次測前低、賭不破底 | 1H／4H／8H | 目標約 2–3R；到 +1.5R 先減半並把剩餘停損移到成本 |
| 小師弟 | 跟大師兄同一套想法，週期短。目前以 ETH 最有規律 | 15m | 1.5–2R 的短打 |
| 瘸師弟 | 短線逆勢。爆量加長黑之後，在前低下方掛低接針 | 15m | 盤整段固定 1.5R，到了全部停利，不移停損 |

四種都只做多。完整進場、停損、份數與出處見 [docs/strategy.md](docs/strategy.md)。

## 環境需求

- Python 3.9 或以上（建議 3.11）
- Linux 或 macOS。實盤用 `fcntl` 檔案鎖，Windows 不能直接跑 `live_trader.py`
- 不需要安裝第三方套件，見 `requirements.txt`
- 幣安 USDT-M 合約帳戶，持倉模式要是**單向**，不要開雙向

## 設定幣安 API

1. 在幣安建立 API 金鑰。只勾選讀取，以及 USDT-M 合約交易。不要開提現，不要開現貨以外你用不到的權限。能綁 IP 就綁這台機器。
2. 金鑰只放本機，兩種方式擇一：
   - 環境變數 `BINANCE_API_KEY`、`BINANCE_API_SECRET`
   - 本機檔案。把 `config.example` 複製成專案根目錄的 `.env`，或用 `BINANCE_ENV_FILE` 指向別的路徑
3. `.env` 已列入 `.gitignore`。不要提交、不要貼到聊天、不要寫進提示詞。

```bash
cp config.example .env
chmod 600 .env
# 編輯 .env，填入兩把金鑰

mkdir -p data
cp config/events.example.json data/events.json
# 之後自行增刪重大事件。沒有這個檔，程式就不會擋事件前後 30 分鐘
```

資料目錄預設是專案下的 `data/`（日誌、`state.json`、事件、鎖、停機旗標）。要用別的位置就設 `QUANTBOY_DATA_DIR`，排程請寫絕對路徑。

## 目錄結構

```text
config.example                  金鑰與路徑的範本，可提交
config/events.example.json      重大事件範例
requirements.txt                無第三方依賴的說明
src/live_trader.py              實盤：status / open / manage / watch / flatten
src/prefilter.py                公開行情機械篩選，不下單
src/sentiment.py                資金費率、持倉量、多空比，不下單
src/position_manager.py         紙上倉位檢查。實盤出場不要用它的參數
src/common.py                   公開 K 線工具
src/config.py                   資料目錄與金鑰讀取
docs/strategy.md                策略與規則，每條有出處
docs/operation.md               每一輪怎麼操作、出錯怎麼處理
docs/agent_prompt.md            每 15 分鐘貼給 AI 代理的提示詞
docs/sources/                   規則來源。私人連結已移除
data/                           執行時才產生，不進 git
```

## 每一輪在做什麼

每 15 分鐘一輪。細節與回報格式在 [docs/operation.md](docs/operation.md)。

1. `manage --live`：檢查當日虧損。滿 7% 今天不再開新倉；滿 30% 全部平倉並寫下停機旗標。缺停損就補上，補不上就市價平掉該倉。
2. `status`：看持倉與掛單。限價單掛超過 2 小時還沒成交，回報使用者，由使用者撤。程式沒有「只撤一張單」的指令，不要用 `flatten` 代替。
3. `prefilter.py` 撈候選，再用策略規則複查。篩選結果只是提示，不合格或碰到紅線就不做。
4. 要進場時先不加 `--live` 跑 `open`，核對停損距離、數量、預估虧損，再加 `--live`。下完再跑一次 `manage --live`，確認交易所上有保護單。
5. 有開倉、成交、撤單、減倉、移停損、平倉、出錯、風控才回報。沒有這些事就保持安靜。

出場的「到 1.5R 平一半、停損移到成本、布鹿流往上移停損」程式不會自動做，也沒有對應子命令。代理算出價格與數量後停下來回報，等使用者處理。原因與狀態檔的跨日行為見 [docs/operation.md](docs/operation.md)。

## 指令範例

在專案根目錄執行。下面的價格只是格式示範，不是進場建議。

```bash
# 不需金鑰
python3 src/sentiment.py BTCUSDT ETHUSDT
python3 src/prefilter.py --quiet

# 需金鑰。status 會讀真實帳戶，跨日還會重寫 data/state.json
python3 src/live_trader.py status
python3 src/live_trader.py manage

# 先看計畫（仍會呼叫帳戶與 /order/test，不會送出委託）
python3 src/live_trader.py open \
  --symbol ETHUSDT --side BUY --entry-type limit --price 3500 \
  --stop 3395 --target 3710 --setup 布鹿流 --portion 1 --leverage 3

# 確認後才送出，並再檢查保護單
python3 src/live_trader.py open \
  --symbol ETHUSDT --side BUY --entry-type limit --price 3500 \
  --stop 3395 --target 3710 --setup 布鹿流 --portion 1 --leverage 3 --live
python3 src/live_trader.py manage --live
```

常用參數：

- `--side BUY`。策略只做多。程式留有 `SELL`，實盤不要用。
- `--portion` 只接受 `1`、`0.5`、`0.25`（全份、半份、妖幣四分之一份）。
- `--leverage` 最高 5，預設 5。保證金模式會設成逐倉。
- `--entry-type limit` 時一定要給 `--price`。
- `watch --live` 給每分鐘排程用。沒有待成交限價單時不會打幣安；有成交卻缺保護單時，會跑一次 manage。

紙上倉位檔不是實盤狀態。`position_manager.py` 讀的是 `data/positions.json`，裡面的停利與時間停損是 2026-10 的紙上參數，不能拿來管真實持倉。

## 搭配 AI 代理，每 15 分鐘跑一輪

判斷層與機械層分開：

- **每 15 分鐘**：排程把 [docs/agent_prompt.md](docs/agent_prompt.md) 的提示詞正文交給 AI 代理（Grok Bot、Cursor 雲端代理，或其他能下終端機指令的代理）。代理負責跑上面的一輪、對照策略、決定下不下。工作目錄設成專案根目錄。提示詞裡的 `{{PROJECT_DIR}}`、`{{DATA_DIR}}`、`{{PYTHON}}`、`{{MACHINE_ID}}` 換成這台機器的值。金鑰只留在這台機器的 `.env`。
- **每分鐘**（建議）：只跑保護單監視，避免限價單成交後有一段時間沒有停損。

```cron
* * * * * cd /絕對路徑/bululau-agent && QUANTBOY_DATA_DIR=/絕對路徑/bululau-agent/data /usr/bin/python3 src/live_trader.py watch --live >>/絕對路徑/bululau-agent/data/watch.log 2>&1
```

同一時間只會有一個 `live_trader.py` 拿到檔案鎖。若看到「另一個 live_trader 正在執行」，等一下再試，不要改跑 `flatten`。

代理這一輪若沒有開倉、成交、撤單、減倉、移停損、平倉、出錯或風控，不要發「無事」訊息。

## 部位怎麼算出來

單筆風險預算是淨值的 1%。數量用停損距離反推，同時不超過「這一份保證金 × 槓桿」。停損較近時，通常是保證金上限先卡住，實際預估虧損會小於 1%。`--portion` 縮的是保證金上限。停損很遠、風險預算先卡住時，半份與全份的數量可能一樣。對帳方式與數字例子在 [docs/strategy.md](docs/strategy.md) 的部位一節。開倉前看 `open` 印出的 `est_loss` 與 `risk_cap`。

## 風險聲明

- 這是實盤下單程式。`--live` 會送出真實委託，虧損由使用者自行承擔。
- 停損單可能滑價，流動性差的合約可能超出預估的 1%。爆倉與穿倉都有可能。
- 程式有固定上限（最多 5 個持倉、單幣一倉、槓桿 5 倍、當日 7%／30%），擋不住判斷錯誤。
- 30% 全平之後，`data/HALT_REPORT_USER.flag` 會留下。在使用者明確同意之前，不要刪旗標、不要再加 `--live`。
- 金鑰外洩等於別人可以替你交易。金鑰檔權限設成只有自己能讀，定期更換，並且永遠不要開提現權限。
