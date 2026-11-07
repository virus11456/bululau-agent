# 判斷回傳 gateway 部署契約

這個目錄是唯一可部署根。不要把倉庫根、`src/`、`live_trader.py` 或 `prefilter.py` 設成 Vercel project root。這裡沒有建立 project、Blob、deployment，也沒有付費。

## 已驗 commit

套件裡的 `COMMIT` 與打包後的 `gateway/VERIFIED_COMMIT` 是同一個 git HEAD，也就是跑過 gateway 測試的那一版。git 工作樹裡的 `VERIFIED_COMMIT` 只說明這條規則，不另記一個較舊的程式 commit。branch head 可以只比套件 COMMIT 多 `dist/bululau-cycle-ack.tar.gz` 與 `.sha256`。不要把空的戳記 commit 說成與已測程式同一版。部署套件 COMMIT，不要部署 `main`。`create_git_project` 預設會從 production branch `main` 部署，不能拿它宣稱已部署這個 head。

## root / build

| 項目 | 值 |
| --- | --- |
| rootDirectory | `gateway` |
| framework | `null`（Other，不是 Next.js） |
| installCommand | `npm ci` |
| buildCommand | 不設定。沒有靜態輸出目錄 |
| output | `api/v1/installations/[installationId]/judgments/[eventId].js` 這一個 Node Function |
| runtime | `nodejs22.x` |
| node engines | `22.x` |
| maxDuration | 15 秒 |

客戶端使用的 URL 是：

- `PUT /v1/installations/<installation_id>/judgments/<event_id>`
- `GET /v1/installations/<installation_id>/judgments/<event_id>`

檔案在 `api/v1/...`，所以 Vercel 的實體路徑是 `/api/v1/...`。`vercel.json` 的 rewrite 把公開的 `/v1/...` 轉到那個 Function。兩種路徑都解析成同一組 installation 與 event。客戶端不能傳 pathname 或 callback URL。

## 原子建立

使用 `@vercel/blob` 的一次 `put(pathname, body, { access: "private", allowOverwrite: false, addRandomSuffix: false })`。SDK 送出 `x-allow-overwrite: 0`。官方客戶端說明：不允許覆寫時，api-blob 會對 S3 送 `If-None-Match: *`（vercel/storage commit `50270f88df7d39fc78e14a8d9e67d4eb023d2334`）。AWS conditional writes 把這個條件定義成原子的「不存在才建立」，已存在則 412。文件：<https://vercel.com/docs/vercel-blob/using-blob-sdk> 與 <https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html>。

這不是先 GET 再 PUT，也不是 Python 的 `os.link`。衝突後才 `get(pathname, { access: "private", useCache: false })`，用來分辨同摘要重試與 409，不當作互斥。

## 環境變數名稱

只在 Vercel project 設定名稱，不把值寫進 git，也不複製到 Mac 或 box：

- `GATEWAY_INSTALLATION_ID`
- `GATEWAY_PUBLISHER_KEY_ID`
- `GATEWAY_PUBLISHER_SECRET_SHA256`
- `GATEWAY_READER_KEY_ID`
- `GATEWAY_READER_SECRET_SHA256`

Blob 認證前提是 Function 執行環境同時有 `BLOB_STORE_ID` 與 `VERCEL_OIDC_TOKEN`。程式把這兩個值傳給 SDK，而且在缺任一個時直接拒絕，不呼叫 Blob。`@vercel/blob` 在沒有 OIDC 時會改讀 `BLOB_READ_WRITE_TOKEN`；這個 gateway 不走那條 fallback。沒有把 `token` 選項傳進 `put`/`get`，並不能單獨證明當時用的是 OIDC。`BLOB_READ_WRITE_TOKEN` 與 `VERCEL_OIDC_TOKEN` 都不得寫進 git，也不得配置到 Mac 或 box。

`npm audit` 在覆寫前有 4 個 high，其中 zod `GHSA-mrhx-c7xc-m3w5` 經 `@vercel/cli-config` 與 `@vercel/oidc` 進入 `@vercel/blob`。`package.json` 的 overrides 把這三個釘在 `@vercel/oidc@4.0.7`、`@vercel/cli-config@0.3.7`、`zod@4.6.7`。覆寫後 `npm audit` 是 0 vulnerabilities。沒有用 `--audit-level` 或忽略設定把警告藏起來。

## 部署前仍要另批

一個隔離 project、一個 private Blob、上述環境變數，以及能指定這個 commit 的部署接口。Hobby 用途與剩餘額度仍未知，這份程式不是部署許可。父層部署時核對 target commit，不合併、不升級、不付費。
