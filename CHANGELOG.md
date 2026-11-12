# 變更紀錄

## 2026-11-12（瘸師弟停損句不再被讀成 1–2% 上限）

只改 `backups/mac/quantboy-gate/full_prompt.md` 第 2 行裡瘸師弟的「合理好球帶內盲掛、結構停損1–2%（BTC/ETH至少3%）」那一段。改成：低掛是前低下方的掛單深度（布神例：前低 100 掛 97、96、95），不是停損上限；停損自設、嚴格執行、不可擴大；沒有 1–2% 上限；結構太近才往外推。1.5R 全平、單根長黑、訊號量、其他門檻、第 5 行 decision_log 沒有改。

可套到 Mac 未遮罩檔的 diff 在 `deploy/patches/stop_sentence.patch`（第 2 行已是瞬間單根長黑時）與 `deploy/patches/stop_sentence.mac-0032025b.patch`（Mac 原檔 sha1 仍是 `0032025b93b9316559eb9c9bafcf18454d14a552`、第 2 行仍是連 2–3 長黑時）。沒有改程式，沒有部署。

## 2026-11-12（帳戶斷線恢復後補審仍合格的候選）

11/12 03:30 起幣安 401／-2015 時，gate 因 `manage_failed`／`status_untrusted` WAKE。公開 prefilter 在 `manage`、`status` 失敗後仍會跑，但 `gate_log` 只記候選張數，下一輪對不到是哪一個幣，也沒有舊掛單價可以沿用。

- `src/gate.py`：帳戶不可信時，把 prefilter 抓到的幣種、型態、訊號 K 記進 `gate_state.json`，不記價格。持倉／掛單恢復可信、且這輪 prefilter 與 hardfilter 都跑完的第一輪，只把重跑後仍留在 kept 的候選交給 AI（`catchup_review`）。價格用這輪現算的結果。狀態仍不可信、或重跑後不在 kept，不補審。不改門檻、頻率、60 秒 watch、下單、`live_trader`。
- repo 沒有 Mac `gate.py` 備份，這次以 `src/gate.py` 為準。
- 新增 `tests/test_gate.py` 的補審測試。不連網、不簽章。

## 2026-11-11（瘸師弟長黑改回布神原文：單根）

使用者 19:19（台北）原話：「我們要模糊的正確 不要精準的錯誤」。19:23 原話：「prompt 和 quantboy-paper 的初篩都一起改成單根長黑，驗證審過再部署」「布神原版筆記怎麼寫就怎麼改」。

- `src/hardfilter.py` R2：瘸師弟不再要求 15m 連續 2–3 根長黑。訊號 K 本身是單根長黑即可。BTCUSDT、ETHUSDT 門檻 2%，其餘小幣 4%。長黑與爆量仍須同時成立；爆量仍是前 10 或前 20 根均量的 3 倍，倍數不改。理由代碼由 `kuai_consecutive_blacks` 改為 `kuai_long_black`。
- Mac 實盤快照 `backups/mac/quantboy-gate/full_prompt.md`：只改瘸師弟那一句，改成瞬間單根較大跌勢，並寫出 BTC／ETH 約 2–3%（程式取 2%）、小幣 4–5% 以上（程式取 4%）、長黑與爆量缺一不可。同一句裡的訊號量倍數、盲掛、停損、1.5R 全平沒有改。遮蔽的餘額與機器 ID 沒有動。
- Mac 實盤快照 `backups/mac/quantboy-paper/discretionary/prefilter.py`：瘸師弟長黑門檻從「24 小時成交額達標的大幣 2%、否則 4%」改成「BTCUSDT／ETHUSDT 2%、其餘 4%」。原本就是最近兩根各自判斷，不是連續根數。爆量仍是前 20 根均量的 3 倍。階梯、停損、其他型態沒有改。
- 倉庫 `src/prefilter.py` 沒有連續長黑邏輯，沒有改。`src/gate.py`、重試、盲掛、停損、出場、RR、風控、頻率、watch、下單流程沒有改。`docs/sources/` 沒有改。
- 可套到 Mac 原檔的 diff 在 `deploy/pr15/`。`full_prompt.md.patch` 沒有上下文，只換第 2 行，不含檔尾。Mac 原檔套用前 sha1 應為 `0032025b93b9316559eb9c9bafcf18454d14a552`。repo 遮罩檔以含第 5 行 decision_log 的版本為底，只改第 2 行。還沒部署。

## 2026-11-11（同步 full_prompt.md 遮罩備份）

2026-11-11 19:34（台北），使用者同意後，Mac 上的 `~/quantboy-gate/full_prompt.md` 在檔尾加了一個換行與一句話，呼叫 `decision_log.py`（Refs #13）。

- 用遮蔽版逐位元組取代 `backups/mac/quantboy-gate/full_prompt.md`。相對上一份遮罩備份，差別只有檔尾這一個換行與這一句。帳戶餘額與機器 ID 的遮蔽沒有改。
- Mac 新原檔 sha1 `0032025b93b9316559eb9c9bafcf18454d14a552`、sha256 `4eecf875f775cdc79b2bc0aa5885dbe189a0edf53e2c21cdffdae21c15b0cb06`、2663 bytes。repo 遮罩檔 sha1 `d35dc44e84fe555c468bd2066060168d4afebb82`、sha256 `784f5296ecafe01a0d3721b157d004cb89fcd32a33ae2e7c3ce0cba57c567dd9`、2678 bytes。
- Mac 舊版（sha1 `e9c573e06b7a097e71f5167bda8bb5040045c31d`）已備份為 `~/quantboy-gate/full_prompt.md.bak.20261111193428`。
- 更新 `backups/mac/README.md` 的雜湊與大小。沒有改其他檔案，沒有改程式，沒有部署到 Mac。

## 2026-11-11（AI 判斷紀錄）

2026-11-11 稽核（issue #13）：閘門 WAKE 之後，AI 否決候選的理由沒有寫進任何檔案。

- 新增 `src/decision_log.py`：獨立小工具。AI 每輪判斷完一個候選後自行呼叫，把幣種、型態、決策（`reject`／`enter`／`skip_other`）、理由原文追加成一行 jsonl。預設路徑 `~/quantboy-gate/decision_log.jsonl`，與 `gate_log.jsonl` 同目錄。`reason` 超過 500 字只留前 500 字。輸入不合法時結束碼非 0，錯誤印到 stderr。只用標準庫，Python 3.9 相容。`gate.py` 不匯入它。
- 新增 `tests/test_decision_log.py`、`docs/decision_log.md`。文件說明怎麼用 `round_time` 對回 `gate_log.jsonl` 的 `time`。
- 沒有改 `src/gate.py`、`src/hardfilter.py`、`src/prefilter.py` 的判斷、門檻、重試、逾時、結束碼。沒有改下單、風控、watch、排程、routine。沒有改 `docs/strategy.md`、`docs/agent_prompt.md`。沒有呼叫幣安，沒有讀金鑰。沒有部署到 Mac，也沒有改 Mac 上的 `full_prompt.md`。要開始記錄，還得使用者另外同意在那份提示詞加一句（草稿見 `docs/decision_log.md`）。

## 2026-11-11（Mac 實盤檔唯讀快照）

- 新增 `backups/mac/`：Mac 上實盤仍在讀的 `prefilter.py`、同目錄的 `common.py`，以及遮蔽帳戶餘額與機器 ID 後的 `full_prompt.md`。只做對照，不改程式，也不改 Mac 路徑。見 `backups/mac/README.md`。

## 2026-11-11（429／418 有上限的退避重試）

11/11 02:03（台北）一輪幣安公開 K 線回 429，prefilter 失敗，閘門以 `prefilter_failed` WAKE。

- `src/gate.py`：只在 stdout／stderr 辨識得出 429 或 418 時，才把 prefilter 最多再跑 2 次。沒有 Retry-After 就等 5 秒、15 秒；有就照它。單次超過 45 秒，或累計睡眠會超過 60 秒，就不再等、也不再打。重試的額外牆鐘從第一次辨識到 429／418 起算，最多 120 秒（不含第一次呼叫；第一次逾時仍是 240 秒）。進入重試後每次逾時是 `min(240, 剩餘預算)`；剩餘不夠再打一發就停，記 `budget_exhausted`。仍失敗維持 WAKE（`prefilter_failed`，並註記 `prefilter_retries=N`）。`gate_log.jsonl` 記錄呼叫次數、重試次數、等待秒數與停止原因。
- `src/hardfilter.py`：自己的公開 K 線遇到 429／418，整輪最多再試 2 次。沒有正數 Retry-After 就等 2 秒、6 秒。單次上限 8 秒、累計睡眠 15 秒。每次等待前看離 gate 的 60 秒逾時還剩多少，必須蓋過這次等待加一次 10 秒請求再加 5 秒餘裕，否則不等，結束碼 2。仍失敗維持 WAKE。
- 辨識依據是錯誤文字，不是結束碼 2。結束碼 2 也用於其他行情失敗，那些不重試。部署中的 prefilter 不在本倉庫，沒有改它；它目前的錯誤句是 `rate_limited: 429 on /fapi/v1/klines`（11/10 21:01 的 prefilter_log 同一句），而且沒有帶出 Retry-After。
- 沒有改 R1–R4、策略門檻、頻率、風控、60 秒 watch、下單流程。平常沒有 429／418 時不多打請求。不簽章、不讀金鑰。沒有部署到 Mac。

## 2026-11-10（閘門加硬條件篩選）

使用者 11/10 23:44（台北）原話（經總經理boy轉達）：「你依照原本的開單的條件跟頻率來撈單來做設定」。

- 新增 `src/hardfilter.py`：prefilter 有候選時由 gate.py 呼叫，只擋程式能確定不符合的硬條件：黑名單、瘸師弟只有 1 根長黑、布鹿流停損套 3%／5% 下限後 RR < 1.5、布鹿流第一波沒有 3 倍量。不確定、缺資料一律留給 AI；限流、讀取失敗、輸出看不懂一律 WAKE。
- `src/gate.py`：接上 hardfilter（`--hardfilter`，`off` 可停用），gate_log 多記被擋的候選與理由。新鮮度門檻不變。
- 新增 `tests/test_hardfilter.py`、`tests/replay_hardfilter.py`、`tests/fixtures/hardfilter_replay_20261110.json`（11/10 實際候選回放，K 線為事後以公開 API 重建）、`docs/hardfilter.md`（規則出處、部署、回滾）。
- 沒有改 prefilter、live_trader、routine prompt、30 分鐘排程、60 秒 watch、風控或策略門檻。沒有部署到 Mac。

## 2026-11-10（閘門失敗改為叫醒）

收緊 `src/gate.py` 與 `live_trader.py status`：只有新鮮、可信、上一輪也明確是空倉，而且一般單與條件單都是 0，才 QUIET。讀取失敗、過期、前後矛盾、狀態檔損壞都改為 WAKE。

- `status` 讀條件單失敗時印「條件單讀取失敗」並以非 0 結束，不再把失敗印成「條件單 0」。manage、watch、flatten 讀條件單失敗的舊行為沒有改。
- 空倉清掉殘留條件單時，manage 原本印的「孤兒條件單」現在會讓閘門 WAKE。
- 閘門中途出錯時，已經拿到的 manage／status／prefilter 輸出會留在 WAKE 裡。
- 狀態檔損壞、`last` 不可信，或中間有一輪沒讀到帳本，下一輪空倉會 WAKE。狀態檔根本不存在（第一次跑）仍可 QUIET。
- status／prefilter 的時間過期或超前就 WAKE。8 分鐘年齡上限與 120 秒超前餘裕要擁有者確認，不是策略參數。
- `tests/__init__.py` 讓 `python3 -m unittest discover` 能找到測試。
- 沒有改 routine prompt、風控公式、60 秒 watch、30 分鐘排程，沒有部署到 Mac，測試沒有連網也沒有讀金鑰。

## 2026-11-10（省 token 閘門）

新增 `src/gate.py`、`tests/test_gate.py`、`docs/gate.md`。依使用者要求另起精簡版，不依賴 PR #3。

- 每輪由程式跑 `manage --live`、`status`、`prefilter --quiet`。全部明確是空的（0 倉、0 一般單、0 條件單、0 候選、沒有旗標、沒有錯誤，上一輪也是空的）才印 `QUIET`，其他一律印 `WAKE` 並附上輸出。
- 不下單，不讀金鑰，不改 `live_trader.py` 與 live 資料目錄。策略條文與風控都沒有改。
- 還沒部署到 Mac，也還沒改 routine prompt。部署步驟見 `docs/gate.md`。

## 2026-11-10

依使用者「全部上傳」，只更新文件，把 main 上已過時的「每 15 分鐘／實盤停止、只做紙上」改成已核對的現行操作。沒有改 `src/` 下單程式，沒有部署，沒有合併 [PR #3](https://github.com/virus11456/bululau-agent/pull/3)，也沒有改 Mac 或現行 routine。

相對 11-06 文件，這次寫入的是操作現況，不是改策略條文：

- 現行新機會掃描是每 30 分鐘（11/7 19:44 讀回 `*/30`、彈性 ±7 分鐘）。之前 11-06 寫每 15 分鐘，11/7 一度改為每小時（該次平台讀回原文本輪未另重讀）。60 秒 watch、硬風控與 11-06 型態條文未改。掃描與持倉 AI 在同一輪，故持倉檢查也隨輪每 30 分鐘。
- 11/7 00:47 使用者要求接手實盤。11/8 06:02 因幣安 401 暫停排程，最後一次成功讀取是 05:01（當時 0 倉 0 單）。11/10 10:03 連線確認後已恢復同一條 30 分鐘 routine。幣安資料與實盤執行留在 Mac。
- 11/7 使用者要求空倉（新鮮可信空倉且兩類掛單為 0）時跳過持倉／風控 AI；有倉、有單、失敗或未知不當空倉。已核 prompt（11/7 21:29）未寫入這句。操盤手 11/8 日報稱操作上已依此做。程式化閘門只在未合併的 PR #3，不是現行實盤。
- 現行 saved prompt 已放入 `docs/agent_prompt.md` 與 `docs/sources/routine_live_prompt_2026-11-07.md`（機器代號改為佔位符）。11-06 紙上候選提示詞改存 `docs/sources/paper_candidate_prompt_2026-11-06.md`。
- 現行 prompt 寫小師弟 1.5–2R 出場，與定稿「出場 R 待確認」並存，不把任一邊改掉。

## 2026-11-06

使用者於 22:23（台北時間）確認策略定稿。這一版只更新文件，沒有改下單程式，沒有部署，也沒有讀取帳戶。實盤維持停止，紙上為現階段。重啟實盤還要等固定監控部署、測試完成，以及使用者確認資金可用。

相對 11-03 文件，定稿寫明或收緊的規則：

- 固定程式每 15 分鐘掃全市場。沒有候選不呼叫 AI。AI 只判斷型態、份數、進場、停損、停利。監控程式負責複核、風控與停損停利監視。這段尚未實作。
- 瘸師弟維持 1.5R 全部平倉。不退回 2R。
- 15 分鐘結構停損維持至少 1–2%，BTC、ETH 至少 3%。不退回小幣 10% 下限，也不改成布神「小幣低於 5% 是送錢」的那一條。
- 布鹿流大約 3 波以上不做。爆量下限是前量的 3 倍。停損改為前低下方 1–2% 容錯，與 BTC／ETH 3%、小幣 5% 取較寬者；取寬後風報比不到 1.5 就不做。
- 1.5R 平一半只適用布鹿流。大師兄與小師弟不套用。這兩種的出場 R 改列待確認，不寫成新的預設數字。
- 新增硬條件：日 K 須在均線上（參數待確認，不採用 30／45／60）、風報比 1.5、連 3 敗當天停手、4H／日 K「黑 → 紅吞 → 黑吞」不進、趨勢至少 30–50 根 K。
- 布神沒給的數字集中在 `docs/strategy.md` 的待確認區，包含均線參數、V 轉收不回時的做法、大師兄與小師弟的出場 R、連敗後縮小金額的比例。

11-03 的安全紅線保留：只做多、BTC 關鍵價、事件前後 30 分鐘、7%／30%、單筆 1%、停損先掛交易所、最多 5 倉。

## 2026-11-03

這一版把實盤程式收成可設定路徑的專案，並以當天的實盤例行流程當作最新規則。下面是相對「2026-11-03 較早的操盤規則稿」與 10 月底 lessons 的規則變更。出處都是使用者定案；與布神原文的對照寫在 [docs/strategy.md](docs/strategy.md)。

- 瘸師弟改為 1.5R 全部停利，不移停損、不平一半。先前稿子寫交易所掛 2R 全平。
- 15 分鐘短線（瘸師弟、小師弟）拿掉小幣停損 10% 下限，改為結構停損，離進場至少 1%、建議 1–2%。BTC、ETH 的 15 分鐘單仍至少 3%。大師兄維持 BTC／ETH 至少 3%、小幣至少 10%。
- 布鹿流已拉大約三波以上不做。先前寫七到八波、或七到十波以上才放棄。
- 爆量下限定為訊號 K 的量 ≥ 前 10–20 根的 3 倍。布鹿流「第一波」本身仍是量增 2–3 倍，與這條爆量門檻分開。
- 不追價寫進每輪：離開好球帶不進；先深殺、再彈回原來的好球帶，也不進。布鹿流回檔後離前高很近仍可進，過不了前高再出。
- 資金費率 0.03% 到 0.05%、持倉量暴增這類 caution 只註明，不再降份。大於 0.05% 仍然不做多。
- 亞洲盤不降信心、不降份。
- 布鹿流出場不用均線死叉。

同日一併定下來、先前文件已有的紅線維持不變：BTC 帶量破關鍵價後兩根 15m 不開多、重大事件前後 30 分鐘不開新倉、當日虧 7% 停開新倉、30% 全平、單筆虧損上限為淨值 1%、停損先掛在交易所、最多 5 個持倉、不做空。

程式這一版只改了資料目錄、事件檔與金鑰的讀取位置，開倉數量公式、7%／30% 風控與保護單流程維持原樣。`status` 另外印出掛單時間與單號，方便判斷 2 小時撤單。沒有新增減倉或單獨撤單指令。
