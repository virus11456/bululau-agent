# Mac 實盤檔唯讀快照

2026-11-13：`quantboy-paper/discretionary/prefilter.py` 與 `quantboy-gate/full_prompt.md` 這兩份快照已套上三項規則（瘸師弟參考點、布鹿流進場、七八波）。下面舊表的 sha1 是改之前的。`common.py` 沒有改。要部署的 auto-scan 檔在 `mac/quantboy-auto-scan/prefilter.py`，Mac 路徑是 `~/quantboy-auto-scan/prefilter.py`。

這是 2026-11-11 19:21（台北）從 Mac 抄下來的唯讀快照，給事後對照用。實盤仍讀 Mac 上的原路徑，路徑沒有改。執行、排程、閘門都不要改去讀 `backups/`。

這次放三個檔。`common.py` 於 19:22（台北）補進同一份快照。`prefilter.py` 與 `common.py` 之後沒有再改。

`full_prompt.md` 於 2026-11-11 19:34（台北）、使用者同意後，在 Mac 原檔檔尾加了一個換行與一句話，呼叫 `python3 ~/quantboy-gate/decision_log.py`。本倉庫只同步遮蔽版。Mac 舊版（sha1 `e9c573e06b7a097e71f5167bda8bb5040045c31d`，2223 bytes）已備份為 `~/quantboy-gate/full_prompt.md.bak.20261111193428`。

## 檔案與雜湊

| 檔案 | Mac 原路徑 | Mac 原檔 sha1 | Mac 原檔 sha256 | repo 路徑 | repo 檔 sha1 | repo 檔 sha256 |
|---|---|---|---|---|---|---|
| `prefilter.py` | `/Users/frank/quantboy-paper/discretionary/prefilter.py` | `c88941327c37af7e7e1593562458d0b16eea19fd` | `acd73578bbb9381040639ebaea910740c97113f13731d2e1c61b6aae1eab4794` | `backups/mac/quantboy-paper/discretionary/prefilter.py` | `7668d8f573713f32765166e6045e369c73705938` | `5708df9665c58a2b4b33225182421f4d07240a95084ee6dd0206e388e0b9a2ec` |
| `common.py` | `/Users/frank/quantboy-paper/discretionary/common.py` | `09463cbe52d279c02bbad04bd0650aee5a03bdf9` | `b5ea01792d02d0af0d87a4b912696d7015bc2dd3d833d79c74f6fc687be5697a` | `backups/mac/quantboy-paper/discretionary/common.py` | `09463cbe52d279c02bbad04bd0650aee5a03bdf9` | `b5ea01792d02d0af0d87a4b912696d7015bc2dd3d833d79c74f6fc687be5697a` |
| `full_prompt.md` | `~/quantboy-gate/full_prompt.md` | `0032025b93b9316559eb9c9bafcf18454d14a552` | `4eecf875f775cdc79b2bc0aa5885dbe189a0edf53e2c21cdffdae21c15b0cb06` | `backups/mac/quantboy-gate/full_prompt.md` | `b7771801d0c8bd1ea109d0b9e09a5859472fe8eb` | `e26bf9f53e4156a30804bd42aa4eca067744f38ca0efe11dbe0c15af92b6028e` |

`common.py` 仍與 Mac 原檔逐位元組相同，1944 bytes，沒有金鑰、帳戶資訊或機器 ID。`prefilter.py` 的 Mac 原檔是 12703 bytes；本 PR 改了瘸師弟長黑門檻之後，repo 檔不再等於 Mac 原檔。`full_prompt.md` 的 Mac 原檔是 2663 bytes；放進 repo 的是遮蔽版。遮蔽只動餘額與機器 ID。本 PR 再只改第 2 行的瘸師弟那一句，第 5 行 decision_log 留著。三份都查過，沒有 API 金鑰、token、密碼。

`deploy/pr15/` 只適用套用前 Mac `full_prompt.md` sha1 仍是 `0032025b93b9316559eb9c9bafcf18454d14a552` 的舊檔。現行不要再套。停損句用 `deploy/patches/stop_sentence.patch`，Mac 已於 2026-11-12 12:16（台北）套上，讀回 sha1 `a8c3693a93427d661c62b61c505cc40dc04da1d6`。`common.py` 沒有改。上表「Mac 原檔」是套 `deploy/pr15/` 之前的雜湊。

## full_prompt.md 遮蔽了哪兩處

閘門 WAKE 時讀的是 Mac 上未遮蔽的 `~/quantboy-gate/full_prompt.md`。repo 這份是遮蔽版：

1. 帳戶可用餘額改成「［已遮蔽：帳戶餘額］」。餘額是帳戶資料，本倉庫不存放。
2. Mac 的機器 ID 改成「［已遮蔽：機器 ID］」。機器識別不放進倉庫。

要對 Mac 上的 `full_prompt.md` 時，比的是上表「Mac 原檔」那一欄。要對倉庫裡這份遮蔽版有沒有被改過，比的是「repo 檔」那一欄。

## prefilter.py 與 common.py

Mac 這份 `prefilter.py` 第 8 行是 `from common import *`。`HERE` 與行情函式來自同一目錄的 `common.py`，已一併放在 `backups/mac/quantboy-paper/discretionary/common.py`。

倉庫裡的 `src/common.py`（sha1 `3bf4fa275d5fef3729d072f28d55dc84045c5060`）是另一份。Mac 這份只定義 `HERE`，沒有 `DATA_DIR`。與 `prefilter.py` 一樣，兩邊各自留在原處，不要互相蓋掉。快照仍不是執行來源。

## 與 src/prefilter.py 是兩個檔

倉庫裡的 `src/prefilter.py`（sha1 `f305b60b420788e8fb5cc4088a0f29e79ee81e99`）是另一份。它的紀錄路徑用 `DATA_DIR`。Mac 這份用 `HERE`，紀錄寫在腳本自己的目錄。兩邊各自留在原處，不要互相蓋掉。

## 之後在 Mac 上核對

在 Mac 上對原路徑算 sha1，跟本檔上表比：

```bash
shasum -a 1 /Users/frank/quantboy-paper/discretionary/prefilter.py
shasum -a 1 /Users/frank/quantboy-paper/discretionary/common.py
shasum -a 1 ~/quantboy-gate/full_prompt.md
```

套 `deploy/pr15/` 之前：`prefilter.py` 應等於 `c88941327c37af7e7e1593562458d0b16eea19fd`。`common.py` 應等於 `09463cbe52d279c02bbad04bd0650aee5a03bdf9`。`full_prompt.md` 應等於 `0032025b93b9316559eb9c9bafcf18454d14a552`（2663 bytes，含第 5 行 decision_log）。舊版 `~/quantboy-gate/full_prompt.md.bak.20261111193428` 的 sha1 應仍是 `e9c573e06b7a097e71f5167bda8bb5040045c31d`。

`deploy/pr15/full_prompt.md.patch` 沒有上下文，只換第 2 行的瘸師弟那一句，不含檔尾，所以第 5 行留在原處。它只適用上面那個 `0032025b…` 舊檔。現行檔已過這一版，改指 `deploy/patches/stop_sentence.patch`。

套 patch 之後：`prefilter.py` 應等於 `7668d8f573713f32765166e6045e369c73705938`（這份沒有遮蔽，和 repo 檔相同）。`full_prompt.md` 的 Mac 原檔因為第 1 行仍是未遮蔽的餘額與機器 ID，而且第 5 行 decision_log 還在，sha 不會等於 repo 遮蔽版。`common.py` 不變。

在本倉庫對現在這三個檔：

```bash
sha1sum backups/mac/quantboy-paper/discretionary/prefilter.py backups/mac/quantboy-paper/discretionary/common.py backups/mac/quantboy-gate/full_prompt.md
```

應分別等於 `7668d8f573713f32765166e6045e369c73705938`、`09463cbe52d279c02bbad04bd0650aee5a03bdf9` 與 `b7771801d0c8bd1ea109d0b9e09a5859472fe8eb`。

## 瘸師弟停損那一句（2026-11-12）

repo 遮罩檔只改第 2 行裡「合理好球帶內盲掛、結構停損1–2%（BTC/ETH至少3%）、1.5R全平，不平一半。」這一段。單根長黑、訊號量、RR1.5、風控、第 5 行 decision_log 沒有改。遮蔽沒有改。3136 bytes。

套到 Mac 未遮罩真檔時，第 1 行餘額與機器 ID 不在這個 patch 裡。第 2 行已是瞬間單根長黑（與本 repo 遮罩檔套用前相同），用 `deploy/patches/stop_sentence.patch`。

Mac 已套用 `deploy/patches/stop_sentence.patch`。讀回 `~/quantboy-gate/full_prompt.md` 的 sha1 是 `a8c3693a93427d661c62b61c505cc40dc04da1d6`，3121 bytes。第 1 行仍是未遮罩的餘額與機器 ID，所以不會等於 repo 遮罩檔。

## 測試與 import

`backups/` 不是執行來源，也不是 Python 套件（底下沒有 `__init__.py`）。現有程式只把 `src/` 或 `tests/` 放進 `sys.path`，不會 import 這裡。

`python3 -m unittest discover` 會收集 `tests/test_mac_prefilter_kuai.py`。那個測試把 `backups/mac/quantboy-paper/discretionary` 加進 `sys.path`，只呼叫瘸師弟的長黑判斷，並把 `common.get`、`common.klines` 換成會失敗的函式。`backups/` 仍然不是實盤執行來源。
