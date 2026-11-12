# AI 判斷紀錄（decision_log.py）

## 為什麼要這支

2026-11-11 稽核（[issue #13](https://github.com/virus11456/bululau-agent/issues/13)）：閘門印出 `WAKE` 之後，AI 否決候選的理由沒有寫進任何檔案，事後無法檢討判斷是不是過嚴。

`src/decision_log.py` 只做一件事：把 AI 對一個候選的判斷追加成一行 JSON。它不決定進不進場，不改門檻，也不下單。

閘門、硬條件篩選、預篩、下單、風控、60 秒 watch、現行 `*/15`（彈性 ±3）排程、實盤 routine 都維持原樣。`gate.py`、`hardfilter.py`、`prefilter.py` 不匯入這支程式，這支程式也不匯入它們。不讀金鑰、不讀 env 檔、不連網、不做簽章請求。

## 什麼時候呼叫

一個候選一行。同一輪有多個候選就各呼叫一次，`--round-time` 用同一個。沒有候選就不要呼叫。

這支程式要等呼叫端自己執行。Mac 的 `~/quantboy-gate/full_prompt.md` 第 5 行已經要求每輪判斷完候選就呼叫它，Mac 已在用。

## 欄位

每一行是一個 JSON 物件，檔案編碼 UTF-8。欄位順序如下：

| 欄位 | 內容 |
|---|---|
| `time` | 寫入這一刻的台北時間，例如 `2026-11-11T09:31:12+08:00`。這是留紀錄的時間。 |
| `symbol` | 幣種。1 到 32 碼大寫英數，例如 `ONEUSDT`。 |
| `pattern` | 型態名稱，1 到 40 字，例如 `瘸師弟`、`布鹿流`、`大師兄`、`小師弟`。對應候選裡的型態。 |
| `decision` | 只接受下面三個小寫值。 |
| `reason` | 理由原文。先去掉前後空白。超過 500 個 Unicode 字元（一個中文字算 1）只留前 500 個，不改寫、不加刪節號。 |
| `round_time` | 本輪 `gate_log.jsonl` 那一列的 `time`。沒給就是 `null`。有給會換算成台北時間、精確到秒再存。 |

`decision` 固定是這三個：

| 值 | 意思 |
|---|---|
| `reject` | 看過這個候選，決定不進場。`reason` 寫否決理由原文。 |
| `enter` | 決定進場。`reason` 寫進場理由原文。同一輪有進有否決時，進場也留一行，事後才看得到兩邊。 |
| `skip_other` | 沒有把這個候選當成進場或否決，例如已有倉、停開新倉、事件窗口、輸出看不懂。`reason` 寫略過原因原文。 |

寫出來的一行像這樣：

```json
{"time": "2026-11-11T09:31:12+08:00", "symbol": "ONEUSDT", "pattern": "瘸師弟", "decision": "reject", "reason": "只有1根長黑", "round_time": "2026-11-11T09:30:00+08:00"}
```

成功時 stdout 印 `已記錄 ONEUSDT reject`。這一行不是 `QUIET`，也不是 `WAKE`。錯誤印到 stderr，前綴是 `decision_log:`。

| 結束碼 | 意思 |
|---|---|
| 0 | 已追加一行 |
| 2 | 參數不合法。檔案不會被打開，已經有的內容維持原樣 |
| 1 | 路徑或寫入失敗。參數先檢查過；上一層目錄不在時不會自己建立 |

並發時用 `fcntl` 獨佔鎖，把整行寫完才放鎖，避免兩筆紀錄插成半行。只在 Linux 或 macOS 上跑。只用 Python 標準庫，相容 Python 3.9。Mac 上用 `/usr/bin/python3`。

預設路徑是 `~/quantboy-gate/decision_log.jsonl`，和 `gate_log.jsonl` 同一個目錄。檔案不存在時，第一次成功寫入會建立它。`~/quantboy-gate` 這個目錄要已經在（閘門部署時就有）；這支程式不建立那個目錄。

## 指令範例

在倉庫裡：

```bash
python3 src/decision_log.py \
  --symbol ONEUSDT \
  --pattern 瘸師弟 \
  --decision reject \
  --reason="只有1根長黑" \
  --round-time 2026-11-11T09:30:00+08:00
```

部署到 Mac 之後，同樣的參數，程式在 `~/quantboy-gate/decision_log.py`。要寫到別的檔，加 `--log`。

`--round-time` 可以不給，紀錄裡的 `round_time` 就是 `null`。格式要像 `2026-11-11T09:30:00+08:00`，必須帶時區。`Z` 或 `+00:00` 會先換成台北時間再存，這樣才能跟 `gate_log.jsonl` 的 `time` 用同一串字對上。

## 怎麼對回 gate_log.jsonl

`gate_log.jsonl` 每一列有 `time` 和 `decision`。`time` 是閘門那一輪開始的台北時間，精確到秒，例如 `2026-11-11T09:30:00+08:00`。WAKE 畫面上的「台北 2026-11-11 09:30」只到分鐘，秒不一定是 00，不要拿它拼 `--round-time`。

閘門先把這一輪寫進 `gate_log.jsonl`，再印 `WAKE`。看到 `WAKE` 時，檔尾那一列就是本輪。把它的 `time` 填進 `--round-time`。

這支工具不讀 `gate_log.jsonl`。`gate.py` 也不讀 `decision_log.jsonl`。對回是事後用字串比對：`decision_log` 的 `round_time` 等於 `gate_log` 的 `time`。

```bash
python3 -c '
import json, os
want = "2026-11-11T09:30:00+08:00"
gate = os.path.expanduser("~/quantboy-gate/gate_log.jsonl")
with open(gate, encoding="utf-8") as handle:
    for line in handle:
        row = json.loads(line)
        if row.get("time") == want:
            print("gate", row.get("decision"), row.get("reasons"))
dec = os.path.expanduser("~/quantboy-gate/decision_log.jsonl")
with open(dec, encoding="utf-8") as handle:
    for line in handle:
        row = json.loads(line)
        if row.get("round_time") == want:
            print(row["symbol"], row["pattern"], row["decision"], row["reason"])
'
```

同一輪多個候選的 `round_time` 相同，會一起印出來。

## 在 Mac 上安裝

本文件只說明做法。這次沒有把檔案複製到 Mac，也沒有改 routine、排程或 60 秒 watch。

只動 `~/quantboy-gate/decision_log.py` 這一個新檔。不要覆蓋 `gate.py`、`hardfilter.py`、`live_trader.py`，也不要動 `~/quantboy-live/`。

1. 備份（Mac）。第一次安裝沒有舊檔，略過備份是預期的：
   ```bash
   cd ~/quantboy-gate
   ts=$(date +%Y%m%d%H%M%S)
   if [ -f decision_log.py ]; then cp -p decision_log.py "decision_log.py.bak.$ts"; fi
   ```
2. 放檔：把合併後 main 的 `src/decision_log.py` 複製成 `~/quantboy-gate/decision_log.py`。避開 `*/15` 刻度前後 3 分鐘（routine 可能正在跑），先寫成 `decision_log.py.new` 再 `mv`。
3. 讀回驗證：
   ```bash
   shasum ~/quantboy-gate/decision_log.py
   # 應為 1d07bca6fbead4b0a4b62b11b4c2d1d7b332eb2d
   /usr/bin/python3 ~/quantboy-gate/decision_log.py --help
   ```
   `shasum` 是 SHA-1，和上面這串比對。`--help` 結束碼應為 0。
4. 試寫只准寫到 `/tmp`，不要寫進正式的 `decision_log.jsonl`，以免混進假理由：
   ```bash
   /usr/bin/python3 ~/quantboy-gate/decision_log.py \
     --symbol ONEUSDT \
     --pattern 瘸師弟 \
     --decision reject \
     --reason="部署試寫，不是實盤判斷" \
     --round-time "2026-11-11T09:30:00+08:00" \
     --log /tmp/decision_log_smoke.jsonl
   echo "exit $?"
   rm /tmp/decision_log_smoke.jsonl
   ```
   結束碼應為 0。這一步不打幣安、不讀金鑰、不跑閘門。

複製檔案本身不會開始記錄。提示詞第 5 行已接上，Mac 已在用。

要拿掉：把 `decision_log.py` 刪掉，或用備份蓋回去。`decision_log.jsonl` 留著也不影響下單，沒有任何交易程式讀它。

## 提示詞第 5 行已接上

Mac `~/quantboy-gate/full_prompt.md` 第 5 行已經是下面這句，Mac 已在用。倉庫遮罩備份 `backups/mac/quantboy-gate/full_prompt.md` 第 5 行是同一句。[agent_prompt.md](agent_prompt.md) 的正文仍是 11/07 快照，沒有改寫。

下面這句是已經寫進提示詞第 5 行的原文：

```text
每輪對每個候選判斷完之後，立刻執行 python3 ~/quantboy-gate/decision_log.py --symbol <幣種> --pattern <型態> --decision <reject、enter、skip_other 三選一> --reason="<理由原文，不要改寫>" --round-time <本輪 ~/quantboy-gate/gate_log.jsonl 裡 decision 為 WAKE 那一列的 time>；這個命令只追加紀錄，失敗就跳過，下單、出場、風控與本輪其他判斷都照原來的規則繼續。
```

理由開頭是「--」時，用空格寫法（`--reason "..."`）會被當成缺值，所以要用等號寫法（`--reason="..."`）。

狀態：已寫入 Mac 提示詞第 5 行，Mac 已在用。

## 測試

```bash
python3 -m unittest tests.test_decision_log
python3 -m unittest discover
```

測試不連網，也不讀金鑰。
