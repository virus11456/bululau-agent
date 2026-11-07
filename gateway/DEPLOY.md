# 判斷回傳 gateway 部署契約

這個目錄是唯一可部署根。不要把倉庫根、`src/`、`live_trader.py` 或 `prefilter.py` 設成 Vercel project root。這裡沒有建立 project、Blob、deployment，也沒有付費。

## 已驗 commit

`VERIFIED_COMMIT` 是已跑過 gateway 測試的程式 commit。部署 PR head，不要部署 `main`。PR head 可以只比該檔多戳記與 cycle ACK 套件；`git diff --stat $(cat gateway/VERIFIED_COMMIT) HEAD -- gateway` 除了 `VERIFIED_COMMIT` 本身不應再有程式差異。`create_git_project` 預設會從 production branch `main` 部署，不能拿它宣稱已部署這個 PR head。

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

入口只接受：

- `PUT /v1/installations/<installation_id>/judgments/<event_id>`
- `GET /v1/installations/<installation_id>/judgments/<event_id>`

路徑由路由參數決定。客戶端不能傳 pathname 或 callback URL。

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

Blob 連到這個 project 後，Function 使用平台注入的 OIDC。程式不傳 `token`。不要把 `BLOB_READ_WRITE_TOKEN` 或 `VERCEL_OIDC_TOKEN` 匯出、寫進文件或放到 Mac／box。

## 部署前仍要另批

一個隔離 project、一個 private Blob、上述環境變數，以及能指定這個 commit 的部署接口。Hobby 用途與剩餘額度仍未知，這份程式不是部署許可。父層部署時核對 target commit，不合併、不升級、不付費。
