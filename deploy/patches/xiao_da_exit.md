# 手貼：小師弟市價、大師兄出場

不要改 Mac 上的 `docs/skill.md`。下面這段是建議以後貼進技能說明的文字，這次只放在這裡。

```
小師弟：流動性夠的幣都掃，不限 ETH。ETH 起跌要大於 100 點（遠大於 100 的更嚴數字未確認）。其他幣起跌大約 3% 以上。緩跌後急殺、爆量，彈回至少 40%。第二次下殺用市價，不先算死位置。
開單：python3 live_trader.py open --symbol XUSDT --side long --entry-type market --stop 前低下方 --target 試算2R --setup 小師弟
就算傳入 limit，程式也會改成市價。成交後立刻掛停損和止盈。數量用現有 1% 風控，以標記價和停損計算，成交後不改數量。止盈只用實際成交均價重算 2R。停損掛不上就立刻市價 reduceOnly 平倉。止盈收到 -2021 或標記價已越過止盈，走 target_passed_at_fill。成交後實際虛損超過 1% 怎麼處理未確認，不自動減碼。
瘸師弟仍是長黑低點下方 3%、4%、5% 三檔限價。大師兄仍是預掛限價。大師兄出場：2R 平 50%，其餘 3R 平，4R 前全出，2R 後停損不動。
```

## 價已過整倉止盈

部分成交觸發 `target_passed_at_fill` 的順序：先撤剩下的限價進場單，再市價 `reduceOnly` 平倉，最後清停損和止盈。市價平倉失敗就留下停損，不撤保護單。

`-4130` 只記補掛失敗，不平倉。標記價還沒到整倉止盈，也不平倉。

大師兄的 `target` 是 2R 平 50%，不是整倉。`target_passed_at_fill` 只拿 `target_3r`（3R 餘倉全出）比較。標記價過了 2R、還沒到 3R，就補掛 3R 止盈，不把整倉平掉。沒有 `target_3r` 時，不用 2R 去全平，也不把 2R 掛成 `closePosition` 止盈。

## 未確認

見 `src/prefilter.py` 的 `XIAO_UNCONFIRMED` 和 `DA_SHIXIONG_UNCONFIRMED`。不要把那些句子改成新的擋單數字。
