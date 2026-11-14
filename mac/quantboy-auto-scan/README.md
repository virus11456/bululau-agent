# quantboy-auto-scan

這是 Mac mini 上 `~/quantboy-auto-scan/prefilter.py` 的 repo 副本，已套 2026-11-13 的三項規則。

這份不是實盤初篩。routine 實際帶的是 `--prefilter /Users/frank/quantboy-paper/discretionary/prefilter.py`（2026-11-14 實查 sha1 `29d182f9`）。`~/quantboy-auto-scan/prefilter.py` 實盤未使用，不要覆蓋它來當現行篩網。同目錄的 `common.py` 與 Mac 現檔相同（sha1 `09463cbe52d279c02bbad04bd0650aee5a03bdf9`），不用覆蓋。

要改的是 `backups/mac/quantboy-paper/discretionary/prefilter.py` 對應的 Mac 原檔。只蓋 auto-scan 不會進到 routine。
