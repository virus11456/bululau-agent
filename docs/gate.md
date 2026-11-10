# 省 token 閘門（gate.py）

## 為什麼要這支

現行 Grok routine 每 30 分鐘醒一次，一天 48 輪。每一輪都要讀完整份實盤 prompt，再跑 `manage --live`、`status`、prefilter，然後逐條審核。空倉又沒有候選時，這些工作的結論永遠是「沒事」，等於白花 token。

`src/gate.py` 把這些機械步驟改由程式來跑。空輪時 Grok 只要執行一個命令、看到第一行是 `QUIET`，就結束本輪。

這支程式不下單。它只跑原本每輪就會跑的同三支命令。下單、出場判斷與回報仍然由 Grok 照原本的 prompt 做。

## 什麼時候 QUIET，什麼時候 WAKE

以下全部成立才印 `QUIET`，結束碼 0：

- `manage --live` 結束碼是 0，輸出裡沒有成交、撤單、平倉、停開新倉、錯誤這類字。
- `status` 結束碼是 0，而且讀得到「持倉 0」「一般掛單 0、條件單 0」「全平旗標 False」。表頭的數字還要跟下面列出的明細行數對得上。
- prefilter 結束碼是 0，輸出的 JSON 寫明 `call_ai: false`、`n_candidates: 0`。
- 上一輪也是空倉。如果上一輪有倉或有單、這輪突然全空，代表剛停損或剛出場，要叫醒一次讓 Grok 記錄、回報、記冷卻。

其他情況一律印 `WAKE <理由>`，結束碼 10，並附上三支命令的輸出。例如：有倉、有單、有候選、401、超時、檔案鎖一直被佔、輸出格式看不懂、閘門自己出錯。

不確定的狀況永遠算 WAKE，不會被當成空倉。

「停開新倉」（當日虧損 7%）同一天只叫醒一次。「全平旗標」（30%）則每一輪都叫醒。

## 在 Mac 上安裝

```bash
mkdir -p ~/quantboy-gate
cp src/gate.py ~/quantboy-gate/gate.py
python3 ~/quantboy-gate/gate.py --help
```

預設的路徑如下，不一樣時用參數改：

- `--root ~/quantboy-live`：`live_trader.py` 所在目錄
- `--prefilter prefilter.py`：可以寫相對 `--root` 的檔名，也可以寫絕對路徑。要指到現行 routine 用的那支 public prefilter
- `--state-dir ~/quantboy-gate`：閘門自己的 `gate_state.json` 與 `gate_log.jsonl`。不會寫進 live 的資料目錄

切換之前先手動驗一次，確認 Mac 上那份 `live_trader.py status` 的輸出格式讀得懂：

```bash
python3 ~/quantboy-gate/gate.py --no-manage
```

`--no-manage` 只跑 `status` 和 prefilter，不送任何委託。

- 帳戶空倉、沒有候選時，應該印 `QUIET`。
- 如果印出 `WAKE status_untrusted`，代表那份 `live_trader.py` 的輸出格式跟本倉庫的不一樣。這時候閘門每一輪都會叫醒 Grok，不會出事，只是省不到 token。把 `status` 的輸出貼給工程師調整即可。

## 新的 routine prompt

**排程不用改**，一樣每 30 分鐘、彈性 ±7 分鐘。60 秒 watch 也不用動。

把現行的完整 prompt（[agent_prompt.md](agent_prompt.md) 的提示詞正文）原封不動存成 Mac 上的 `~/quantboy-gate/full_prompt.md`。routine 的 prompt 改成下面這段：

```text
每輪第一步：在 Mac mini（{{MAC_MINI_MACHINE_ID}}）執行 python3 ~/quantboy-gate/gate.py
輸出第一行是 QUIET：本輪結束。不跑其他命令、不審核、不傳任何訊息。
輸出第一行是 WAKE：讀 ~/quantboy-gate/full_prompt.md，照裡面的規則做完這一輪。
閘門已經跑過 manage --live、status 和 prefilter，結果附在輸出裡，直接拿來用，不要再重跑 manage；要開倉時仍照規則先 dry-run 再 open --live，之後再跑 manage 確認保護單。
閘門出錯或輸出看不懂，一律當成 WAKE。
```

空輪時 Grok 只會讀到這五行，加上一行 `QUIET`，不會載入完整規則。

## 怎麼看省了多少

```bash
grep -c '"QUIET"' ~/quantboy-gate/gate_log.jsonl   # 沒叫 AI 的輪數
grep -c '"WAKE"'  ~/quantboy-gate/gate_log.jsonl   # 叫 AI 的輪數
```

## 要退回原本的做法

把 routine 的 prompt 換回 [agent_prompt.md](agent_prompt.md) 的原文就好。閘門不會改動 live 的檔案或排程，刪掉 `~/quantboy-gate` 也不影響實盤。

## 測試

```bash
python3 -m unittest tests.test_gate
```

測試用的是假的 `live_trader` 和 `prefilter`，不連網、不需要金鑰。
