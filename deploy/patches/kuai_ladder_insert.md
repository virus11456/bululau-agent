# 手貼：瘸師弟三檔限價

Mac 的 `~/quantboy-live/live_trader.py` 與本倉庫 `src/live_trader.py` 不是同一份。Mac 那份不從 `config.py` 讀金鑰。不要整份覆蓋。

本倉庫這次的新函式集中在 `src/live_trader.py` 的 `# --- kuai ladder (paste start) ---` 到 `# --- kuai ladder (paste end) ---`。這一段不讀金鑰、不呼叫 `load_api_keys`、不 import `config`。貼進 Mac 時，`account`、`req`、`sym_info`、`day_state`、`log`、`place_protect` 仍用 Mac 檔裡既有的函式。真正送單會簽章，那是 Mac 檔自己的金鑰路徑，不是這一段新邏輯。

## 1. 貼上新函式

在 Mac 檔 `# ---------- 指令 ----------`（或 `def cmd_status`）之前，整段貼上本倉庫那兩個標記之間的內容，含標記。不要貼 `_keys`，也不要加 `from config import ...`。

## 2. `cmd_manage` 的 pending 迴圈

找到：

```python
for sym, pd in list(st.get("pending", {}).items()):
    if not pd.get("orderId"): continue
```

在 `if not pd.get("orderId")` 之前加上下面這段。沒有 `ladder` 鍵的舊單仍走原本的 `orderId` 分支。

```python
        if pd.get("ladder"):
            done = manage_ladder_pending(sym, pd, held_amt, live)
            if done:
                if done == "meta":
                    st.setdefault("meta", {})[sym] = {k: pd[k] for k in ("stop", "target", "setup")}
                st["pending"].pop(sym, None)
                log("ladder_done", symbol=sym, pos=held_amt.get(sym, 0.0))
                if live:
                    save_state(st)
            continue
```

## 3. `cmd_watch`

找到 `for sym, pd in pend.items():` 裡原本的 `if not pd.get("orderId"): continue`。在它之前加上本倉庫 `cmd_watch` 裡 `if pd.get("ladder"):` 那一整段，到 `continue` 為止。60 秒排程本身不用改。沒有階梯的單，watch 的條件與以前相同。

## 4. 命令列

`open` 的參數裡加一行：

```python
o.add_argument("--ladder", help="三檔限價，由淺到深，例如 97,96,95。權重 2:3:5。不給就維持單張")
```

`main()` 在呼叫指令的地方改成：有 `--ladder` 就呼叫 `open_ladder(a)`，否則仍呼叫原來的 `cmd_open`。不要改 `cmd_open` 的函式本體。

```python
if a.cmd == "open" and getattr(a, "ladder", None):
    open_ladder(a)
else:
    # 原本的 status / open / manage / watch / flatten 分派
```

## 5. 依賴金鑰的部分

- `parse_ladder`、`weighted_entry`、`split_ladder_qty`、`order_size`、`ladder_target`、`ladder_rr`、`limit_order`、`check_ladder_levels`：純計算。
- `open_ladder` 會呼叫 `account()`、`req()`、`sym_info()`。這三個在 Mac 檔裡才讀金鑰。dry-run 仍會打 `/order/test`（簽章），與現在的單張 dry-run 一樣。
- `manage_ladder_pending`、`resize_protects`、`cancel_entry_order`：沿用既有的 `req`。

貼完用倉庫的測試觀念自己對一下：不帶 `--ladder` 的單張仍是一張限價；`--ladder 97,96,95 --target 1.5R` 在 dry-run 印出三張，而且不送出真正的 `/fapi/v1/order`。
