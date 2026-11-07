# 省 token 的最小切換

目標是同一套每 15 分鐘掃描，沒有新候選就不叫 AI。這份元件不下單、不讀交易金鑰、不取代 `watch`／`manage`。

## 現在能獨立跑的

固定程式吃現有 `prefilter.py` 印出的 JSON，寫進自己的 outbox。同一根 K、同一個型態、同一組梯子只會喚醒一次。空結果結束碼 0。有新候選結束碼 10。掃描失敗結束碼 2，而且不會把失敗寫成「市場沒有候選」。

```bash
python3 src/prefilter.py --quiet --nolog > /tmp/prefilter.json
python3 -m src.phase1.token_switch --input /tmp/prefilter.json --state-dir /獨立目錄/token-switch
```

`--state-dir` 不要指到實盤的 `data/`。結束碼 0：這輪不要啟動模型。結束碼 10：只把這次的新候選交給 AI。結束碼 2：這輪不要當成空市場。

篩選結果多了 `bar_open_ms`，只為了指紋能對到同一根 K。進場條件沒有改。

## 這輪 AI 還在做、這個閘門沒接走的事

現有每 15 分鐘提示詞不只有找候選。下面這些仍要有人做，否則會出現保護缺口：

| 仍由原流程負責 | 為什麼不能先拿掉 |
|---|---|
| `live_trader.py manage --live` | 7%／30% 與補保護單。每分鐘的 `watch` 只在有待成交限價單時才會進 manage，沒有掛單時不會巡全倉 |
| `live_trader.py watch --live` | 限價成交後補停損。這個閘門不看帳戶 |
| `status` 與超過 2 小時的限價單 | 程式仍不能只撤一張單，要人看 |
| 有候選之後的型態、份數、進出場 | 閘門只決定要不要叫 AI，不代替判斷 |
| 1.5R 減倉、移停損、對使用者回報 | 現有執行器不會自動做 |

## 接替前要先滿足的條件

1. 原有的 `watch --live` 排程繼續留著。這個倉庫沒有查使用者機器上的排程，不能把它當成已經在跑。
2. `manage --live` 改由不含模型的固定指令繼續跑，頻率至少和原來的 15 分鐘一樣。這個閘門不會執行它，因為那要交易金鑰，而且可能送單。
3. 即便帳上看起來是空的，也不能單靠候選指紋結束碼停掉舊 AI。免 AI 只是建議：新鮮快照、0 倉、0 掛單、0 候選、沒有錯誤，四個同時成立時 `ai_suggestion=skip`，`old_ai_poll` 仍是 keep。有倉、任何掛單、狀態過期或 manage 出錯，每一輪仍要原 AI。outbox 鍵是 `cycle:<scan_id>`，`scan_id` 本身是安全字元，和 K 線指紋分開。
4. 舊輪詢只有在真實 sender 送達、處理 ack、以及故障恢復都通過之後才可以切。現在 URL、key、認證 header 都沒有，結果是 `old_ai_poll=keep`、sender `blocked`。沒有通過不能當成已經切換。

## Webhook

文件只說明 routine 可以用 webhook 觸發，URL 和 sender key 要由使用者從面板複製。這裡沒有那兩個值，也沒有正式文件寫認證 header 的名稱。所以程式不送 HTTP，也不把通知失敗改成更頻繁的 AI 輪詢。新候選會留在 outbox，狀態是 pending，不代表已經送到模型。

把 URL 和 key 填上，在 header 寫法被正式文件寫明之前，仍然不算接通。
