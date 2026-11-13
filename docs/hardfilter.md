# 硬條件篩選（hardfilter.py）

使用者 2026-11-10 23:44（台北）原話，經總經理boy轉達：「你依照原本的開單的條件跟頻率來撈單來做設定」。

`src/hardfilter.py` 接在 prefilter 之後，由 `gate.py` 呼叫。prefilter 給的是「近似」候選；這支只把**程式算得出來、而且確定不符合現行開單硬條件**的候選拿掉。剩下的照舊交給 AI。全部被拿掉、而且帳戶是新鮮可信的 0 倉 0 單，閘門才會 QUIET。

頻率現行是每 15 分鐘（`*/15`，彈性 ±3，2026-11-11 19:09 起）。11/10 寫這支時是每 30 分鐘（±7）。風控、60 秒 watch、下單流程不動。2026-11-11 只改 R2：瘸師弟長黑從「連續 2–3 根」改回布神原文的瞬間單根。爆量倍數、盲掛、停損、出場、RR 都不改。倉庫裡的 `src/prefilter.py` 沒有連續長黑邏輯，沒有改；Mac 實盤那份初篩另見 `backups/mac/quantboy-paper/discretionary/prefilter.py`。

## 原則

- 依據是 Mac 上的 `~/quantboy-gate/full_prompt.md`、本倉庫 `docs/strategy.md`，以及布神原文 `docs/sources/user_notes2.txt`、`docs/sources/four_strategies.md`。2026-11-11 起，瘸師弟長黑以這兩份原文為準，不再用 11-06 定稿的「連續 2–3 根」。
- **遇到不確定一律放行給 AI（fail-open to AI）。** 原文沒給數字、要看型態、欄位缺、K 線不夠、找不到訊號 K，都保留候選。訊號 K 找得到、但幅度或爆量確定不夠，就擋。
- 例外是布鹿流日 K（2026-11-13 13:58 核定的 ma7_25）。已收日 K 少於 33 根是確定不符合，擋，理由含「日K資料不足」，不留給 AI。日 K 讀取失敗（HTTP、fixture 沒有這段）仍保留該候選。
- 原文給的是區間時取較寬的一端，例如「小幣 4–5%」取 4%、「前 10–20 根」兩種都算、任一種達標就算有。
- 單一候選讀 K 線出錯（非限流）：保留該候選。
- 418／429 限流：同一輪最多再試 2 次（見下面「429／418 退避」）。仍失敗、或 Retry-After 超過上限：結束碼 2，gate 一律 WAKE（`hardfilter_failed`），全部候選原封不動交給 AI。
- 輸入看不懂、hardfilter 自己壞掉、逾時、輸出和候選對不上：不重試，gate 一樣 WAKE。
- 只讀幣安公開 `/fapi/v1/klines`，不簽章、不讀金鑰、不下單。只在有候選時才抓。瘸師弟抓 15m 一次；布鹿流抓 1H 一次、日 K 一次（`limit` 60，未收的那根丟掉）。前面的規則已經擋下就不抓後面的。429／418 的額外重試不算在這些次數裡面，而且整輪加起來最多再多 2 次，不是每個幣各 2 次。

## 規則與原文出處

| # | 適用 | 程式判斷 | full_prompt.md 原句 | strategy.md 原句 |
|---|---|---|---|---|
| R1 | 全部 | 幣種在黑名單（INIT、EWZ、CAP、TAG、APR、BREV、XAN、AIN、DOOD）就擋 | 「策略與操作以GitHub virus11456/bululau-agent main的2026-11-06定稿為準」 | 「黑名單維持不做：程式裡的 `INITUSDT`、`EWZUSDT`，以及 lessons 點名的 CAP、TAG、APR、BREV、XAN、AIN、DOOD、INIT。」 |
| R2 | 瘸師弟 | 訊號 K 本身是單根長黑，而且爆量達標，缺一不可。長黑＝收黑 ≥ BTCUSDT／ETHUSDT 2%、其餘 4%。爆量＝訊號量 ≥ 前 10 根或前 20 根均量的 3 倍，任一種即可。不看相鄰根數。理由代碼 `kuai_long_black` | 「瘸師弟15m瞬間單根較大跌勢的長黑（BTC/ETH約2–3%，程式取2%；小幣4–5%以上，程式取4%；長黑與爆量缺一不可）、訊號量至少前10–20根3倍」 | 「長黑是瞬間單根較大跌勢……程式取 2%。小幣 4–5% 以上，程式取 4%。」「爆量仍是訊號量至少前 10–20 根平均的 3 倍。」 |
| R3 | 布鹿流 | 停損先套 BTC/ETH 3%、小幣 5% 下限（和結構停損取較寬）。目標 = max(前高, 進場 + 1.5R)，前高不到 1.5R 時目標可以超過前高，不因此擋。進場價已經不低於前高（追價）就擋 | 「目標可以超過前高」「停損取前低下1–2%容錯與BTC/ETH3%（小幣5%）較寬者，RR不足1.5不進」「最低RR1.5」 | 「取寬之後風報比不到 1.5，就不做。」 |
| R4 | 布鹿流 | 從資料起點到前高（最近 48 根 1H 的最高點，和 prefilter 同一找法）之間，要有一根 1H 量 ≥ 前 10 根或前 20 根均量的 3 倍，沒有就擋 | 「布鹿流…爆量至少3倍」 | 「爆量至少是前量的 3 倍。…【布神原文】是前 10–20 根平均量的 3–10 倍。3 倍是最低，不是建議值。」 |
| R5 | 布鹿流 | 只用已收日 K（`close_time` < 決策時間）。少於 33 根就擋。否則收盤要同時高於 MA7 與 MA25，MA7 高於 MA25，而且這兩條都高於各自三根前的值；分形低點（左右各 2 根，嚴格小於）至少兩個，最近一個高於再前一個。不過就擋。理由代碼 `bulu_daily_trend` | 2026-11-13 13:58 核定把「做多日K只看均線向上發散、底底高」做成硬規則。均線參數用回放的 ma7_25，不改 full_prompt 原文 | 同左。1H 的 24+24 代理、RR、停損、目標都不改 |

「確認 K 已收」：prefilter 與 hardfilter 都只用 `close_time < 現在` 的已收 K。hardfilter 找不到訊號 K（例如還沒收）就保留給 AI。

R2 的原文是 `user_notes2.txt`：「長黑 = 瞬間單根較大跌勢，大幣（BTC、ETH）約 2–3%、小幣 4–5% 以上。」「爆量 + 長黑，缺一不可。」「爆量 = 平均量 N 倍（機器人用前 20 根平均判定，成交量是相對值不是絕對值）。」以及 `four_strategies.md`：「長黑為瞬間單根較大跌勢（大幣平均2%、小幣4-5%以上）」「爆量＋長黑，缺一不可」「爆量判定為平均量Ｎ倍」。PDF 的大幣是「平均 2%」，筆記是「約 2–3%」，程式取 2%。小幣取 4%。爆量倍數仍是 3 倍、前 10 或前 20 根，沒有改成筆記裡沒有寫死的 N，也沒有改成只看 20 根。門檻看幣種（BTCUSDT、ETHUSDT），不看 prefilter 的 `big`（24 小時成交額）。

R3 沒有再往前低下加 1–2% 容錯。加了 RR 只會更低，等於加嚴；不加只會讓 RR 偏高、不會誤擋。

`BULU_RR_CAP` 預設 `None`，目標不封頂。設成 `2.0` 才把目標收到進場加 2R（舊的 `min(rr, 2.0)`）。封頂後 RR 仍至少 1.5，不會因此多擋。深殺反彈沒有數字，這支不擋。

### 刻意不做、留給 AI 的

- 1H 的底底高、首波是否明確、七八波以上才考慮放棄、盤整（同一壓力測 3 次）：要看型態。波數不在這支裡擋。日 K 的底底高改由 R5 擋。
- 「妖幣急拉不追」「離好球帶太遠不盲掛」：2026-11-13 起不再當拒單理由，這支本來就沒有用這兩條擋。
- 日 K 均線以前列在 strategy.md 待確認，不能自動拒絕。2026-11-13 13:58 使用者改口，ma7_25 成為 R5，只套在布鹿流。
- 小師弟、資金費率法：今天沒有回放資料可驗證，不加篩，全部交給 AI。
- BTC guard、情緒濾網、事件窗口、連 3 敗、停損後等 3 根：prefilter 或 AI／live_trader 已處理，hardfilter 不重做。

### 布鹿流 RR 的目標價

2026-11-13 起，預設 `prior_high` 模式的目標是 `max(前高, 進場 + 1.5R)`。前高不到 1.5R 時目標放在 1.5R，可以超過前高（003：後波高點會突破前波）。進場價已經不低於前高則擋，那是追價。

`--rr-target plan_target` 仍用候選自己的 `target` 欄，不自動墊高。11/10 回放裡 MERL 的前高 RR 約 2.6、prefilter target 約 1.1，那個差異還在。

## gate.py 的改動

- 有候選時才跑 `hardfilter.py`，候選從 stdin 傳入，逾時 60 秒。
- 新鮮度檢查的時間點在 hardfilter 之前，`READING_MAX_AGE`（480 秒）與 `READING_MAX_FUTURE_SKEW`（120 秒）不變。hardfilter 只會減少候選，不會讓過期的帳本變成可信。
- 全部候選被擋：不加 `candidates` 理由；其他 QUIET 條件仍要全部成立。QUIET 行尾多印 `硬條件擋N`。
- 有留下的候選：WAKE，輸出多一段 `=== hardfilter …`，列出被擋的候選與理由。
- `gate_log.jsonl` 每列多一個 `hardfilter` 欄：`failed`、`before`（prefilter 原本幾個）、`blocked`（幣種、型態、規則、理由）。`n_candidates` 改為篩後數量。
- `--hardfilter PATH`：預設是 gate.py 同目錄的 `hardfilter.py`。`--hardfilter off` 回到只看 prefilter 的舊行為。
- routine 的命令與 `--prefilter` 路徑都不用改。
- hardfilter 若在 429／418 之後重試過，`gate_log.jsonl` 的 `hardfilter` 欄多記 `retries`、`wait_s`、`waits_s`、`stopped`。仍失敗時 WAKE 理由仍是 `hardfilter_failed`，並多一個 `hardfilter_retries=N`。

## 429／418 退避

公開 K 線收到 HTTP 429 或 418 才重試。其他狀態（例如 500、逾時、DNS）不重試：單一候選仍保留給 AI；429／418 則整輪失敗、結束碼 2。

同一輪 hardfilter（所有候選加起來，不是每個幣各算）最多再試 2 次。沒有正數的 `Retry-After` 就等 2 秒、再 6 秒；header 有正數就照它，`0` 或不是數字就改走 2／6 秒。

| 上限 | 秒 | 超過時 |
|---|---|---|
| 單次等待 | 8 | Retry-After 大於 8 就不等，結束碼 2 |
| 整輪累計睡眠 | 15 | 再等會超過 15 就不等，結束碼 2 |
| 整輪牆鐘 | 60 | 與 gate 給 hardfilter 的逾時相同。每次等待前檢查剩餘時間 |

這支一開始就記下 t0。每次要睡之前，剩餘時間（60 秒減掉已經過的）必須蓋過「這次等待 + 一次請求逾時 10 秒 + 餘裕 5 秒」。不夠就不等，結束碼 2，gate 走 `hardfilter_failed`（fail-open，候選全部留下）。沒有一個固定的「最壞約 45 秒」：請求本身會吃掉這 60 秒，能不能再等，只看當下還剩多少。正常沒有 429／418 時，請求次數不變。

失敗的那一行 JSON 會帶 `rate_limit_retries`、`rate_limit_wait_s`、`rate_limit_waits_s`、`rate_limit_stopped`（`retries_exhausted`、`wait_cap` 或 `budget_exhausted`）。重試後成功也帶前三個，`rate_limit_stopped` 是 null。

## 部署計畫（總經理boy 審過後才做）

只動 `~/quantboy-gate/`。不動 `/Users/frank/quantboy-paper/discretionary/prefilter.py`（`--prefilter` 路徑不變）、`~/quantboy-live/`、routine、排程、60 秒 watch。

1. 備份（Mac）：
   ```bash
   cd ~/quantboy-gate
   ts=$(date +%Y%m%d%H%M%S)
   cp -p gate.py gate.py.bak.$ts
   cp -p gate_state.json gate_state.json.bak.$ts
   cp -p gate_log.jsonl gate_log.jsonl.bak.$ts
   shasum gate.py   # db9821487e5cad2dc9ea9db92e2c4aa5fdbc33fa 是 2026-11-10 寫下這段時的舊檔。部署前與當時要部署的 main 上的 src/gate.py 比對，不要拿這個舊 sha 當現在的檢查。
   ```
2. 放檔：把合併後 main 的 `src/gate.py` 覆蓋 `~/quantboy-gate/gate.py`，新增 `src/hardfilter.py` 為 `~/quantboy-gate/hardfilter.py`。避開 `*/15` 刻度前後 3 分鐘（routine 可能正在跑），先寫成 `.new` 再 `mv`。
3. 讀回驗證：
   ```bash
   shasum ~/quantboy-gate/gate.py ~/quantboy-gate/hardfilter.py      # 和 main 上的檔案比對
   /usr/bin/python3 ~/quantboy-gate/hardfilter.py --help
   /usr/bin/python3 /Users/frank/quantboy-paper/discretionary/prefilter.py --quiet --nolog > /tmp/pf.json; echo "exit $?"
   /usr/bin/python3 ~/quantboy-gate/hardfilter.py < /tmp/pf.json; echo "exit $?"
   /usr/bin/python3 ~/quantboy-gate/gate.py --no-manage --state-dir /tmp/gate-verify \
       --prefilter /Users/frank/quantboy-paper/discretionary/prefilter.py
   ```
   - `prefilter --quiet --nolog` 不寫 prefilter_log。結束碼 0 或 10 是正常；2 是行情或限流，稍後再試。
   - hardfilter 應印 `{"ok": true, ...}`、結束碼 0。
   - `gate.py --no-manage` 不送委託；用 `/tmp/gate-verify` 當狀態目錄，不汙染正式的 gate_state／gate_log。第一行應是 `QUIET` 或 `WAKE …`。有候選時應看到 `=== hardfilter` 段落。
   - `gate.py --no-manage` 會跑 `live_trader.py status`（唯讀，但需要簽章讀帳戶），這一步要由操盤手或有權限的人執行，不是工程師。
4. 第一輪正式執行後，看 `~/quantboy-gate/gate_log.jsonl` 最後一列有 `hardfilter` 欄。

## 回滾

任一種都可以，不用改 routine：

- 最快：`cp -p ~/quantboy-gate/gate.py.bak.<ts> ~/quantboy-gate/gate.py`。舊 gate.py 不認得 hardfilter，`hardfilter.py` 留著也不會被呼叫。
- 或者只要停用篩選：把 `hardfilter.py` 改名（gate 找不到檔案會 WAKE `hardfilter_failed`，等於每輪有候選都叫 AI，比舊行為多一個理由字串而已）。
- gate_state.json 格式沒有改，不需要還原；備份只是保險。

## 測試與回放

```bash
python3 -m unittest discover                 # 全部，不連網
python3 tests/replay_hardfilter.py           # 11/10 回放表（預設 prior_high）
python3 tests/replay_hardfilter.py --rr-target plan_target
```

回放資料 `tests/fixtures/hardfilter_replay_20261110.json`：候選取自 Mac `prefilter_log.jsonl` 原始紀錄；AI 判定取自操盤手交易boy 各輪完成訊息原句；K 線是重建的（當時沒有保存），11/10 23:5x（台北）由 Mac 以幣安公開 K 線 API 依各輪 prefilter 時間（`endTime`）重抓，只留已收 K。

2026-11-11 瘸師弟單根長黑的新舊對照另見 `docs/kuai_single_black_replay_20261111.md` 與 `python3 tests/replay_kuai_single_black.py`（預設不連網）。
