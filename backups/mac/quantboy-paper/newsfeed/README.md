# 每日新聞／總經日程腳本（newsfeed）備份

這是 Mac mini 上 `~/quantboy-paper/newsfeed/newsfeed.py` 的對照備份。備份，非部署來源；不要拿它覆蓋 Mac 原檔，排程也不要改去讀 `backups/`。

- 取得時間：2026-11-27（台北）。Mac 原檔於 2026-11-27 07:38（台北）部署 v2.3，舊版備份在 Mac `newsfeed.py.bak.20261127_0738`（sha1 `fc9b679ec8c01e8d10ea07bbece68a2dabc6ca54`）。
- Mac 原檔 sha1：`ee4dcbc8fda67de9662925adc1cbb375fdd7c8e3`（sha256 `5a5fd33b537e331bd7437da2fc297f5a9baa6795e29c8c569b73b381095f5af7`，383 行）。
- repo 檔 sha1：`4c0da927b41a3eb6ae76535cf7c62897093e00aa`（sha256 `e254202c70f8051131d0882f4f5fcb984d5d6d4838a5504d82c83dd93da181a8`）。與 Mac 原檔只差第 8 行 User-Agent 裡的聯絡信箱，改成「［已遮蔽：聯絡信箱］」。
- 只用 Python 標準庫（Mac `/usr/bin/python3` 3.9.6），沒有其他原始檔依賴。檔內沒有金鑰、token、密碼，也不讀任何 `.env`。只打公開網址；幣安只用公開 K 線與 ticker。

## 排程與輸出

- LaunchAgent `com.quantboy.newsfeed`：每天 06:30（台北）執行 `/usr/bin/python3 -u /Users/frank/quantboy-paper/newsfeed/newsfeed.py`，stdout/stderr 寫到同目錄 `stdout.log`。plist 不在本倉庫，也沒有改。
- 輸出在 `out/`（不放進倉庫）：`latest.md`、`latest.json`（schema `quantboy.newsfeed/3`）、`YYYY-MM-DD.md/json`；有旗標時寫 `FLAG.json`，沒有旗標時把舊的改名 `FLAG.cleared_*.json`。ETF 每日持倉快照寫在 `out/etf_holdings/YYYY-MM-DD.json`，用來前後日相減。
- 新聞搜集boy 讀這些輸出。腳本不發通知、不打 webhook。

## 來源

- 新聞 RSS：CoinDesk、Cointelegraph、Decrypt、Bitcoin Magazine、The Block、Fed press。
- 價位：Binance 公開 API（BTCUSDT、ETHUSDT、XAUTUSDT）。
- 日曆（未來 14 天，只收美國關鍵數據：非農、CPI、PPI、JOLTS、GDP、PCE，加 FOMC、初領失業金、ISM 日程；不收 Census、非美國）：BLS ICS、BEA ICS、Fed FOMC 行事曆頁、DOL 初領失業金公布規則與改期表（oui.doleta.gov）。ForexFactory／faireconomy 已移除，不帶預期值。
- 官方前值與近 8 天已公布實際值：FRED（St. Louis Fed）收錄的 BLS／BEA／DOL 數列，依資料期對齊，對不到就標「未取得」。
- ETF 流量（一律標「推算值」，非全市場總量）：iShares 官網（IBIT、ETHA）、Bitwise 官網（BITB、ETHW）、21Shares 官網（ARKB，股數＝AUM÷NAV）。iShares 資料日 D 的股數反映前一交易日申贖，流量記在前一資料日、用該日 NAV（已用 Farside 11/20–11/24 IBIT 數字驗證）；Bitwise／21Shares 的交易日對齊尚待驗證。

## 修不了的項目與原因（2026-11-27 查證）

- GBTC、BTC（Mini）、ETHE、ETH（Mini）：etfs.grayscale.com 回 HTTP 429，標頭 `x-vercel-mitigated: challenge`，是 Vercel Security Checkpoint 機器人挑戰頁，連 robots.txt 也被擋。腳本有 429 退避重試，但遇挑戰頁即停，不繞過。
- FBTC、FETH：fidelity.com 回 HTTP 400（內容為 Service Unavailable 頁），digital.fidelity.com 為前端渲染，沒有免登入的公開檔。
- ARK 持倉 CSV：assets.ark-funds.com 的 robots.txt 是 `User-agent: *` / `Disallow: /`，不抓；ark-funds.com 本站 403 Cloudflare。
- ISM PMI 數值：ISM 日程與報告頁導向登入頁（含 reCAPTCHA），不繞過；日程依慣例推算並標「時間為慣例未確認」。ISM 新聞稿轉載於 PR Newswire，但其使用條款禁止 robot／spider／自動擷取，未接入。
- 代幣解鎖：Tokenomist 條款禁止爬取；CryptoRank 條款頁 403 Cloudflare；DefiLlama emissions API 402 付費；VYLOS 無公開 API，資料轉自 DefiLlama，未接入（使用者另行寫信申請授權中）。
- CPI 年增：FRED 缺 2025-10（停擺未公布），不硬配，標「未取得」。
- GDP：FRED 只存最新估計值，初值／修正值分不出版本。
- 市場共識預期值：沒有合法免費的官方來源，不列。

## 在 Mac 上核對

```bash
shasum -a 1 /Users/frank/quantboy-paper/newsfeed/newsfeed.py   # 應為 ee4dcbc8fda67de9662925adc1cbb375fdd7c8e3
```
