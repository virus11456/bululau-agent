# 手貼：小師弟市價、大師兄出場

Mac 實盤篩網讀的是 `~/quantboy-paper/discretionary/prefilter.py`，repo 裡的對照檔是 `backups/mac/quantboy-paper/discretionary/prefilter.py`。不是 `src/prefilter.py`。

部署時不要整份覆蓋。那份檔用 `from common import *`，log 寫在自己的目錄。做法：先把 Mac 原檔備份，再把 `src/prefilter.py` 裡的 `xiao_shidi`、`da_shixiong`、`filter_new_signals`、`cap_near` 和檔頭那些常數抄進去，保留 Mac 檔自己的 import 和路徑。`src/live_trader.py` 的 `--target-3r`、兩張止盈、2 根 4H 撤單，要手貼進 Mac 的 `live_trader.py`，不要整份覆蓋，也不要動 `_keys()`。

`~/quantboy-gate/full_prompt.md` 裡小師弟那句要改成：

```
小師弟同短線結構停損，2R 一次全出，不平一半、不移保本。第二次下殺用限價 IOC，滑價上限是單筆淨值 1%。
```

不要改 `docs/skill.md`。

## 待使用者確認的預設

- 小師弟停損在最低點下方再留 `XIAO_STOP_CUSHION = 0.005`（0.5%）。BTC、ETH 另外至少離進場 3%。
- 大師兄向下發散用收盤 < MA7 < MA25。週期 7 和 25 待確認。
- 大師兄限價超過 `DA_LIMIT_EXPIRE_4H = 2` 根 4H 就撤剩餘進場單。

## 建議技能文字

不要改 `docs/skill.md`。下面這段是建議以後貼進技能說明的文字，這次只放在這裡。

```
小師弟：流動性夠的幣都掃，不限 ETH。ETH 起跌要大於 100 點（遠大於 100 的更嚴數字未確認）。其他幣起跌大約 3% 以上。緩跌後急殺、爆量，彈回至少 40%。第二次下殺用限價 IOC，不改掛市價。
開單：python3 live_trader.py open --symbol XUSDT --side long --stop 前低下方 --target 試算2R --setup 小師弟
程式會改成限價 IOC。價格設在以該價成交時虧損仍不超過淨值 1% 的最遠一檔，再依 tick 往保守方向取整。下單前預估就超過 1% 就放棄，記 xiao_risk_over_1pct。IOC 沒成交也放棄，記 xiao_ioc_unfilled，不重試。部分成交保留已成交數量，立刻掛停損和止盈。數量用現有 1% 風控。止盈只用實際成交均價重算 2R。停損掛不上就立刻市價 reduceOnly 平倉。止盈收到 -2021 或標記價已越過止盈，走 target_passed_at_fill。
瘸師弟仍是長黑低點下方 3%、4%、5% 三檔限價。大師兄仍是預掛限價。大師兄出場：2R 平 50%，其餘 3R 平，4R 前全出，2R 後停損不動。
```

## 價已過整倉止盈

部分成交觸發 `target_passed_at_fill` 的順序：先撤剩下的限價進場單，再市價 `reduceOnly` 平倉，最後清停損和止盈。市價平倉失敗就留下停損，不撤保護單。

`-4130` 只記補掛失敗，不平倉。標記價還沒到整倉止盈，也不平倉。

大師兄的 `target` 是 2R 平 50%，不是整倉。`target_passed_at_fill` 只拿 `target_3r`（3R 餘倉全出）比較。標記價過了 2R、還沒到 3R，就補掛 3R 止盈，不把整倉平掉。沒有 `target_3r` 時，不用 2R 去全平，也不把 2R 掛成 `closePosition` 止盈。

## 未確認

見 `src/prefilter.py` 的 `XIAO_UNCONFIRMED` 和 `DA_SHIXIONG_UNCONFIRMED`。不要把那些句子改成新的擋單數字。
