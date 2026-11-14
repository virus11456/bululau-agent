# 手貼：小師弟 IOC、大師兄出場

Mac 實盤 routine 帶的是 `--prefilter /Users/frank/quantboy-paper/discretionary/prefilter.py`（2026-11-14 實查 sha1 `29d182f9`）。repo 對照檔是 `backups/mac/quantboy-paper/discretionary/prefilter.py`。不是 `src/prefilter.py`，也不是 `~/quantboy-auto-scan/prefilter.py`。auto-scan 那份實盤未使用。

部署時不要整份覆蓋。那份檔用 `from common import *`，log 寫在自己的目錄。做法：先把 Mac 原檔備份，再把 `src/prefilter.py` 裡的 `xiao_shidi`、`da_shixiong`、`filter_new_signals`、`record_signals`、`cap_near` 和檔頭那些常數抄進去，保留 Mac 檔自己的 import 和路徑。`src/live_trader.py` 的 `xiao_slippage_pct`、`--target-3r`、`scaled_2r`、兩張止盈、2 根 4H 撤單，要手貼進 Mac 的 `live_trader.py`，不要整份覆蓋，也不要動 `_keys()`。

`~/quantboy-gate/full_prompt.md` 裡小師弟那句要改成：

```
小師弟同短線結構停損，2R 一次全出，不平一半、不移保本。第二次下殺用限價 IOC，滑價容忍 0.3%，風險超過淨值 1% 就放棄。其他幣起跌至少 5%，急殺佔跌幅至少 60%。ETH 起跌大於 100 點。上線用正常倉位。
```

`docs/skill.md` 裡小師弟、大師兄的進出場段落已經直接改過。下面仍留 Mac 手貼用的對照文字。若 Mac 上的技能檔不是這份 repo 檔，把對照文字貼進對應段落，不要改風控 1%、瘸師弟、布鹿流。

## 預設、可調

- 小師弟停損在最低點下方再留 `XIAO_STOP_CUSHION = 0.005`（0.5%）。BTC、ETH 另外至少離進場 3%。這條 3% 是停損距離。BTC 歸在其他幣，起跌改成 5%，停損不跟著放寬。
- 大師兄 4H 長黑收盤在 MA30、MA45、MA60 下方，而且 MA30 < MA45 < MA60。使用者 2026-11-14 確認。布鹿流日 K 不用這組。
- 大師兄限價超過 `DA_LIMIT_EXPIRE_4H = 2` 根 4H 就撤剩餘進場單。

## 建議技能文字

`docs/skill.md` 已寫進同樣的進出場。下面這段留給 Mac 手貼，對照用。

```
小師弟：流動性夠的幣都掃，不限 ETH。ETH 起跌要大於 100 點（遠大於 100 的更嚴數字未確認）。其他幣（含 BTC）起跌跌幅至少 5%，急殺那一根佔整段跌幅至少 60%。緩跌後急殺、爆量，彈回至少 40%。第二次下殺用限價 IOC，不改掛市價。停損在最低點下方。上線用正常倉位，不先改 0.25 份，也不先只跑紙上。
開單：python3 live_trader.py open --symbol XUSDT --side long --stop 前低下方 --target 試算2R --setup 小師弟
程式會改成限價 IOC。做多上限是標記價 × 1.003，做空是標記價 × 0.997，再依 tick 往保守方向取整。這個 0.3% 是 `xiao_slippage_pct`，預設 0.003。數量用最壞價到停損來算，上限成交時風險是 1%。取整後若仍超過就放棄，記 xiao_risk_over_1pct。IOC 沒成交也放棄，記 xiao_ioc_unfilled，不重試。部分成交保留已成交數量，立刻掛停損和止盈。止盈只用實際成交均價重算 2R。停損掛不上就立刻市價 reduceOnly 平倉。止盈收到 -2021 或標記價已越過止盈，走 target_passed_at_fill。
大師兄：預掛限價，超過 2 根 4H 沒成交就撤。出場 2R 平一半、3R 平剩下、4R 前全出，2R 之後停損不動。平過一半記 scaled_2r，之後只掛 3R 的 closePosition。2R 還沒觸發時，半倉數量隨實際持倉變大。上線用正常倉位。
瘸師弟仍是長黑低點下方 3%、4%、5% 三檔限價。
```

## 價已過整倉止盈

部分成交觸發 `target_passed_at_fill` 的順序：先撤剩下的限價進場單，再市價 `reduceOnly` 平倉，最後清停損和止盈。市價平倉失敗就留下停損，不撤保護單。

`-4130` 只記補掛失敗，不平倉。標記價還沒到整倉止盈，也不平倉。

大師兄的 `target` 是 2R 平 50%，不是整倉。`target_passed_at_fill` 只拿 `target_3r`（3R 餘倉全出）比較。標記價過了 2R、還沒到 3R，就只平一半並把 `scaled_2r` 寫進 meta，之後只補 3R，不再掛 2R，也不再市價平半。沒有 `target_3r` 時，不用 2R 去全平，也不把 2R 掛成 `closePosition` 止盈。

## 未確認

見 `src/prefilter.py` 的 `XIAO_UNCONFIRMED` 和 `DA_SHIXIONG_UNCONFIRMED`。不要把那些句子改成新的擋單數字。
