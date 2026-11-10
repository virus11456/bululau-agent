# 硬條件篩選（hardfilter.py）

使用者 2026-11-10 23:44（台北）原話，經總經理boy轉達：「你依照原本的開單的條件跟頻率來撈單來做設定」。

`src/hardfilter.py` 接在 prefilter 之後，由 `gate.py` 呼叫。prefilter 給的是「近似」候選；這支只把**程式算得出來、而且確定不符合現行開單硬條件**的候選拿掉。剩下的照舊交給 AI。全部被拿掉、而且帳戶是新鮮可信的 0 倉 0 單，閘門才會 QUIET。

不改策略門檻，不放寬也不加嚴。頻率仍每 30 分鐘（±7）。風控、60 秒 watch、下單流程、prefilter 都不動。

## 原則

- 依據只有兩份：Mac 上的 `~/quantboy-gate/full_prompt.md`（現行實盤 prompt），以及本倉庫 `docs/strategy.md`（11-06 定稿）。
- **遇到不確定一律放行給 AI（fail-open to AI）。** 原文沒給數字、要看型態、欄位缺、K 線不夠、找不到訊號 K、重算和 prefilter 不一致，都保留候選。
- 原文給的是區間時取較寬的一端，例如「小幣 4–5%」取 4%、「前 10–20 根」兩種都算、任一種達標就算有。
- 單一候選讀 K 線出錯（非限流）：保留該候選。
- 418／429 限流：同一輪最多再試 2 次（見下面「429／418 退避」）。仍失敗、或 Retry-After 超過上限：結束碼 2，gate 一律 WAKE（`hardfilter_failed`），全部候選原封不動交給 AI。
- 輸入看不懂、hardfilter 自己壞掉、逾時、輸出和候選對不上：不重試，gate 一樣 WAKE。
- 只讀幣安公開 `/fapi/v1/klines`，不簽章、不讀金鑰、不下單。只在有候選時才抓，每個候選最多 2 次請求（15m 與 1H 各一次）。429／418 的額外重試不算在這 2 次裡面，而且整輪加起來最多再多 2 次，不是每個幣各 2 次。

## 規則與原文出處

| # | 適用 | 程式判斷 | full_prompt.md 原句 | strategy.md 原句 |
|---|---|---|---|---|
| R1 | 全部 | 幣種在黑名單（INIT、EWZ、CAP、TAG、APR、BREV、XAN、AIN、DOOD）就擋 | 「策略與操作以GitHub virus11456/bululau-agent main的2026-11-06定稿為準」 | 「黑名單維持不做：程式裡的 `INITUSDT`、`EWZUSDT`，以及 lessons 點名的 CAP、TAG、APR、BREV、XAN、AIN、DOOD、INIT。」 |
| R2 | 瘸師弟 | 訊號 K 和相鄰已收 15m K 要連成至少 2 根長黑；長黑＝收黑幅度 ≥ 小幣 4%／大幣 2%。只有 1 根就擋。相鄰那根不另要求 3 倍量 | 「瘸師弟15m連2–3長黑、訊號量至少前10–20根3倍」 | 「篩網：15m 連續 2–3 根長黑，而且量至少是前量的 3 倍。」「大幣長黑大約 2%，小幣 4–5% 以上。」 |
| R3 | 布鹿流 | 停損先套 BTC/ETH 3%、小幣 5% 下限（和結構停損取較寬），再以前高為目標算 RR，< 1.5 就擋 | 「停損取前低下1–2%容錯與BTC/ETH3%（小幣5%）較寬者，RR不足1.5不進」「最低RR1.5」 | 「取寬之後風報比不到 1.5，就不做。」「禁止把停損收窄來湊。」 |
| R4 | 布鹿流 | 從資料起點到前高（最近 48 根 1H 的最高點，和 prefilter 同一找法）之間，要有一根 1H 量 ≥ 前 10 根或前 20 根均量的 3 倍，沒有就擋 | 「布鹿流…爆量至少3倍」 | 「爆量至少是前量的 3 倍。…【布神原文】是前 10–20 根平均量的 3–10 倍。3 倍是最低，不是建議值。」 |

「確認 K 已收」：prefilter 與 hardfilter 都只用 `close_time < 現在` 的已收 K。hardfilter 找不到訊號 K（例如還沒收）就保留給 AI。

R3 沒有再往前低下加 1–2% 容錯。加了 RR 只會更低，等於加嚴；不加只會讓 RR 偏高、不會誤擋。

### 刻意不做、留給 AI 的

- 底底高、首波是否明確、約 3 波以上、盤整（同一壓力測 3 次）、離好球帶、深殺反彈：要看型態或原文沒有數字。
- 妖幣「1 小時急拉超過 10% 不追」：原文沒有說急拉之後多久內算追。今天的妖幣急拉候選全部已被 R2 擋下。
- 「日 K 在均線上」：strategy.md 待確認第 1 條，「確認前，這一條不能自動通過，也不能自動拒絕」。
- 小師弟、資金費率法：今天沒有回放資料可驗證，不加篩，全部交給 AI。
- BTC guard、情緒濾網、事件窗口、連 3 敗、停損後等 3 根：prefilter 或 AI／live_trader 已處理，hardfilter 不重做。

### 待確認：布鹿流 RR 的目標價

R3 預設以 prefilter 的 `prior_high`（前高）當目標，也就是 prefilter 自己算 `rr` 用的同一個價。原文「回檔離前高很近仍然可以進。過不了前高再出場，突破則續抱」。

AI 在 20:30 與 21:29 兩輪用的是 prefilter 的 `target` 欄。那個價是用窄停損算出的 2R 截斷值（`target = c + min(rr, 2) * risk`），而 strategy.md 說 prefilter 與 position_manager 的停利參數「都不是 11-06 的核准參數」。兩種算法結果不同（MERL 以前高算約 2.6，以 target 算約 1.1），所以預設採較寬的前高，交給 AI。

若使用者確認 RR 應以 prefilter target 計，改用 `--rr-target plan_target`（或把 `BULU_RR_TARGET` 改成 `plan_target`），回放顯示會和 AI 原判定 18/18 一致。

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
| 整輪累計等待 | 15 | 再等會超過 15 就不等，結束碼 2 |

數字比閘門小，是因為 gate 只給 hardfilter 60 秒，而每次請求逾時 10 秒。最壞是 3 次請求都打滿 10 秒，再加上 15 秒等待，約 45 秒，留下約 15 秒，避免被閘門的逾時殺掉。正常沒有 429／418 時，請求次數不變。

失敗的那一行 JSON 會帶 `rate_limit_retries`、`rate_limit_wait_s`、`rate_limit_waits_s`、`rate_limit_stopped`（`retries_exhausted` 或 `wait_cap`）。重試後成功也帶前三個，`rate_limit_stopped` 是 null。

## 部署計畫（總經理boy 審過後才做）

只動 `~/quantboy-gate/`。不動 `/Users/frank/quantboy-paper/discretionary/prefilter.py`（`--prefilter` 路徑不變）、`~/quantboy-live/`、routine、排程、60 秒 watch。

1. 備份（Mac）：
   ```bash
   cd ~/quantboy-gate
   ts=$(date +%Y%m%d%H%M%S)
   cp -p gate.py gate.py.bak.$ts
   cp -p gate_state.json gate_state.json.bak.$ts
   cp -p gate_log.jsonl gate_log.jsonl.bak.$ts
   shasum gate.py   # 部署前應為 db9821487e5cad2dc9ea9db92e2c4aa5fdbc33fa
   ```
2. 放檔：把合併後 main 的 `src/gate.py` 覆蓋 `~/quantboy-gate/gate.py`，新增 `src/hardfilter.py` 為 `~/quantboy-gate/hardfilter.py`。避開整點／半點 ±7 分鐘（routine 可能正在跑），先寫成 `.new` 再 `mv`。
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
