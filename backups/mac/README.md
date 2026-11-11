# Mac 實盤檔唯讀快照

這是 2026-11-11 19:21（台北）從 Mac 抄下來的唯讀快照，給事後對照用。實盤仍讀 Mac 上的原路徑，路徑沒有改。執行、排程、閘門都不要改去讀 `backups/`。

這次放三個檔。`common.py` 於 19:22（台北）補進同一份快照。沒有改 Mac 上的任何檔案。

## 檔案與雜湊

| 檔案 | Mac 原路徑 | Mac 原檔 sha1 | Mac 原檔 sha256 | repo 路徑 | repo 檔 sha1 | repo 檔 sha256 |
|---|---|---|---|---|---|---|
| `prefilter.py` | `/Users/frank/quantboy-paper/discretionary/prefilter.py` | `c88941327c37af7e7e1593562458d0b16eea19fd` | `acd73578bbb9381040639ebaea910740c97113f13731d2e1c61b6aae1eab4794` | `backups/mac/quantboy-paper/discretionary/prefilter.py` | `c88941327c37af7e7e1593562458d0b16eea19fd` | `acd73578bbb9381040639ebaea910740c97113f13731d2e1c61b6aae1eab4794` |
| `common.py` | `/Users/frank/quantboy-paper/discretionary/common.py` | `09463cbe52d279c02bbad04bd0650aee5a03bdf9` | `b5ea01792d02d0af0d87a4b912696d7015bc2dd3d833d79c74f6fc687be5697a` | `backups/mac/quantboy-paper/discretionary/common.py` | `09463cbe52d279c02bbad04bd0650aee5a03bdf9` | `b5ea01792d02d0af0d87a4b912696d7015bc2dd3d833d79c74f6fc687be5697a` |
| `full_prompt.md` | `~/quantboy-gate/full_prompt.md` | `e9c573e06b7a097e71f5167bda8bb5040045c31d` | `db9366fd031789d95191090b2504f494469c03da29c92def64f6e46959080e61` | `backups/mac/quantboy-gate/full_prompt.md` | `47074b5292626917177733e13942003c167518a3` | `8f47a892a916f6acfacf105f00c884ff99f673b1169ca7d88359b662737f7521` |

`prefilter.py` 原檔與 repo 檔逐位元組相同，12703 bytes。`common.py` 也是逐位元組相同，1944 bytes，沒有金鑰、帳戶資訊或機器 ID，不需遮蔽。`full_prompt.md` 的 Mac 原檔是 2223 bytes；放進 repo 的是遮蔽版，2238 bytes，所以原檔雜湊與 repo 檔雜湊不同。遮蔽版相對原檔只改了下面兩處，其餘一字不改。三份都查過，沒有 API 金鑰、token、密碼。

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

`prefilter.py` 應等於 `c88941327c37af7e7e1593562458d0b16eea19fd`。`common.py` 應等於 `09463cbe52d279c02bbad04bd0650aee5a03bdf9`。`full_prompt.md` 應等於遮蔽前的 `e9c573e06b7a097e71f5167bda8bb5040045c31d`。sha256 同樣用 `shasum -a 256 <路徑>`，對上表的 sha256。

在本倉庫對快照本身：

```bash
sha1sum backups/mac/quantboy-paper/discretionary/prefilter.py backups/mac/quantboy-paper/discretionary/common.py backups/mac/quantboy-gate/full_prompt.md
```

應分別等於 `c88941327c37af7e7e1593562458d0b16eea19fd`、`09463cbe52d279c02bbad04bd0650aee5a03bdf9` 與 `47074b5292626917177733e13942003c167518a3`。

## 測試與 import

`backups/` 不是執行來源，也不是 Python 套件（底下沒有 `__init__.py`）。現有程式只把 `src/` 或 `tests/` 放進 `sys.path`，不會 import 這裡。

現有測試用 `python3 -m unittest discover`，預設只收集檔名符合 `test*.py` 的模組。`backups/` 裡沒有這種檔，收集時不會載入 `prefilter.py` 或 `common.py`。pytest 預設只收集 `test_*.py` 與 `*_test.py`，同樣不會把這兩個檔當成測試匯入。
