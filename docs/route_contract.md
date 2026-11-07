# 離線等效路由契約

這不是把現有 routine 換成 `review-round`。原 prompt 與執行器不在這包裡，不能說已經等效。可跑的 mock 是 `python3 -m src.phase1.route_contract`。它只印缺少的接點，不開網路、不下單、不改 30 分鐘 routine 或 60 秒 watch。

順序是：`manage` → 狀態確認 → 有倉或任一類單才走保護 AI → 有候選才走新倉 AI。任一步失敗或超時都 `old_ai_poll=keep`。quiet 只有一種：manage 成功、狀態可信且新鮮、0 倉、0 一般單、0 條件單、0 候選。

## 還沒接上的實際接點

- 原 prompt 不在這包，原執行器是哪一個行程還沒有核到。
- `review-round` 不跑 `manage`，也沒有接上保護 AI。
- 候選新倉 AI 仍用原來的 prompt。這包沒有那個執行器。
- 失敗與超時留原 AI 還沒有接到現有 30 分鐘 routine。
- `com.quantboy.livewatch` 仍是 60 秒，沒有切換。
- 日均線仍是未定，不稱策略成立。

`review_round_equivalent` 固定 false。
