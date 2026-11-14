# 手貼：平倉紀錄與 watch 不再空轉

只改 Mac `~/quantboy-live/live_trader.py` 的 `cmd_manage` 與 `cmd_watch`。不要整份覆蓋，也不要改它的 `_keys()`、`open_algo` 說明或 `cmd_status` 格式。這兩段不讀 `config.py`。`req` 仍用 Mac 檔自己的金鑰。

假設階梯的 `cmd_watch` 分支已經是上一份手貼（`kuai_ladder_insert.md`）的樣子。下面的舊碼是那一版。

## cmd_manage

在算完 `held_amt` 之後、pending 迴圈之前加一行：

```python
    meta_at_start = set(st.get("meta") or {})
```

這一輪才從 pending 寫進 meta 的先留著，避免帳戶快照還沒看到成交就被當成已平倉。

然後在「已無持倉的幣種：清掉孤兒保護單」那段，把

```python
    # 已無持倉的幣種：清掉孤兒保護單
    held = {p["symbol"] for p in positions(acc)}
    for o in algo:
```

換成

```python
    # 已無持倉的幣種：先把 meta 裡已平的記下來，再清孤兒保護單（清理本身不改）。
    held = {p["symbol"] for p in positions(acc)}
    closed_note = False
    for sym in list(st.get("meta") or {}):
        if sym not in meta_at_start or sym in held or sym in st.get("pending", {}):
            continue
        m = st["meta"].get(sym) or {}
        pnl = None
        try:
            start_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
            rows = req("GET", "/fapi/v1/income",
                       {"symbol": sym, "incomeType": "REALIZED_PNL", "startTime": start_ms, "limit": 1000},
                       signed=True)
            if isinstance(rows, list):
                pnl = sum(float(r.get("income") or 0) for r in rows)
        except Exception:
            pnl = None
        log("position_closed", symbol=sym, setup=m.get("setup"), stop=m.get("stop"), target=m.get("target"), pnl=pnl)
        print("{0} 已平倉（交易所停損/止盈或手動），清除 meta".format(sym))
        st["meta"].pop(sym, None)
        closed_note = True
    if closed_note and live:
        save_state(st)
    for o in algo:
```

後面兩個 `for o in algo` / `for o in oo` 的孤兒單清理不要動。`time` 這個模組 Mac 檔若已經 `import time` 就不用再加。

## cmd_watch

把函式開頭到階梯分支的 `continue`（不含後面的單張 `if not pd.get("orderId")`）從

```python
    st = load_state(); pend = st.get("pending", {})
    if not pend: return
    need = False
    for sym, pd in pend.items():
        if pd.get("ladder"):
            types = {po["t"] for po in protect_orders(sym)}
            has_sl = any(str(t).startswith("STOP") for t in types)
            has_tp = any(str(t).startswith("TAKE") for t in types)
            for item in pd.get("orders") or []:
                if not item.get("orderId"):
                    continue
                try:
                    o = req("GET", "/fapi/v1/order", {"symbol": sym, "orderId": item["orderId"]}, signed=True, retries=2)
                except BinanceError as e:
                    log("warn", msg="watch 查單失敗", symbol=sym, err=str(e)); continue
                executed = float(o.get("executedQty") or 0)
                resting = o["status"] in ("NEW", "PARTIALLY_FILLED")
                if (not resting) or executed > 0 or not has_sl or not has_tp:
                    log("watch_trigger", symbol=sym, status=o["status"], executed=o.get("executedQty"), ladder=True)
                    need = True
                    break
            continue
```

換成

```python
    st = load_state(); pend = st.get("pending", {})
    if not pend:
        now = now_tpe()
        last = st.get("watch_heartbeat")
        due = True
        if last:
            try:
                prev = datetime.fromisoformat(last)
                if prev.tzinfo is None:
                    prev = prev.replace(tzinfo=TPE)
                due = (now - prev).total_seconds() >= 3600
            except (TypeError, ValueError):
                due = True
        if due:
            print("[watch] 無待成交限價單 {0}".format(now.strftime("%H:%M")))
            st["watch_heartbeat"] = now.isoformat(timespec="seconds")
            save_state(st)
        return
    need = False
    for sym, pd in pend.items():
        if pd.get("ladder"):
            types = {po["t"] for po in protect_orders(sym)}
            has_sl = any(str(t).startswith("STOP") for t in types)
            has_tp = any(str(t).startswith("TAKE") for t in types)
            total_exec = 0.0
            any_done = False
            for item in pd.get("orders") or []:
                if not item.get("orderId"):
                    continue
                try:
                    o = req("GET", "/fapi/v1/order", {"symbol": sym, "orderId": item["orderId"]}, signed=True, retries=2)
                except BinanceError as e:
                    log("warn", msg="watch 查單失敗", symbol=sym, err=str(e)); continue
                total_exec += float(o.get("executedQty") or 0)
                if o["status"] not in ("NEW", "PARTIALLY_FILLED"):
                    any_done = True
            prev = pd.get("last_exec")
            try:
                prev_f = float(prev) if prev is not None else None
            except (TypeError, ValueError):
                prev_f = None
            changed = prev_f is None or abs(total_exec - prev_f) > 1e-9
            pd["last_exec"] = total_exec
            save_state(st)
            # 完全沒成交時，只缺預掛止盈不叫 manage。有成交才看缺單或數量變化；沒有停損仍要叫。
            if any_done or (total_exec > 0 and ((not has_sl or not has_tp) or changed)) or (not has_sl):
                log("watch_trigger", symbol=sym, executed=total_exec, ladder=True, has_sl=has_sl, has_tp=has_tp)
                need = True
            continue
```

`if not pd.get("orderId"): continue` 之後的單張邏輯不要改。Mac 檔需要已有 `datetime` 與 `TPE`（`now_tpe` 用的那一個）。
