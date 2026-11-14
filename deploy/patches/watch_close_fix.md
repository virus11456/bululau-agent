# 手貼：跨日保留、平倉紀錄與 watch

Mac 的 `~/quantboy-live/live_trader.py` 目前 sha1 是 `95522934`（未遮罩那份的前 8 碼）。它不從 `config.py` 讀金鑰。下面只貼函式，不要覆蓋整檔，也不要改 `_keys()`。

## R1：跨日不要清掉 pending 和 meta

`day_state()` 在 00:00 後第一次執行會整份換掉 state.json。改成只重設日欄。找到：

```python
def day_state(acc):
    """每日 00:00 台北重算份額與日初淨值。"""
    s = load_state(); today = now_tpe().strftime("%Y-%m-%d")
    if s.get("day") != today:
        eq = equity_of(acc)
        s = dict(day=today, day_start_equity=eq, slice_margin=round(eq / CFG["slices"], 4),
                 halted_new=False, killed=False, created=now_tpe().isoformat(timespec="seconds"))
        save_state(s); log("day_reset", **s)
    return s
```

整段換成：

```python
# 跨日只換這幾欄。pending、meta、watch_heartbeat 等不能在 00:00 被清掉。
_DAY_FIELDS = ("day", "day_start_equity", "slice_margin", "halted_new", "killed", "created")

def day_state(acc):
    """每日 00:00 台北重算份額與日初淨值。只重設日欄，其餘原樣留著。"""
    s = load_state(); today = now_tpe().strftime("%Y-%m-%d")
    if s.get("day") != today:
        eq = equity_of(acc)
        fresh = dict(day=today, day_start_equity=eq, slice_margin=round(eq / CFG["slices"], 4),
                     halted_new=False, killed=False, created=now_tpe().isoformat(timespec="seconds"))
        kept = {k: v for k, v in s.items() if k not in _DAY_FIELDS}
        s = dict(kept); s.update(fresh)
        save_state(s); log("day_reset", **s)
    return s
```

這一段不讀金鑰。先貼這段就能在今晚 00:00 前保住 DODOX 這種跨日掛單。

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
        closed = lookup_close(sym, m)
        log("position_closed", symbol=sym, setup=m.get("setup"), stop=m.get("stop"), target=m.get("target"),
            exit_price=closed["exit_price"], exit_side=closed["exit_side"], qty=closed["qty"],
            reason=closed["reason"], pnl=pnl)
        px = "null" if closed["exit_price"] is None else closed["exit_price"]
        py = "null" if pnl is None else pnl
        print("{0} 已平倉 出場價 {1} 觸發={2} 已實現 {3}".format(sym, px, _exit_label(closed["reason"]), py))
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

平倉紀錄會呼叫 `lookup_close` 和 `_exit_label`。這兩個加上 `_price_gap`、`classify_exit`，貼在 `def cmd_manage` 前面。內容與倉庫 `src/live_trader.py` 裡同名函式相同，不讀金鑰。

## 持倉迴圈：減倉之後

找到持倉迴圈裡這段：

```python
        print(f"{sym} {amt} 停損 {'有' if has_sl else '缺'} 止盈 {'有' if has_tp else '缺'}")
        m = meta.get(sym)
        # 指定數量的保護單：若限價單已終結，數量需等於實際持倉
        if sym not in st.get("pending", {}):
            for po in protect_orders(sym, algo, oo):
                if po["cp"] or po["qty"] is None or Decimal(str(po["qty"])) == Decimal(str(abs(amt))): continue
```

在 `m = meta.get(sym)` 下一行加 `no_tp = bool(m and m.get("no_tp"))`。數量迴圈的第一行改成先跳過 no_tp 的止盈：

```python
            for po in protect_orders(sym, algo, oo):
                if no_tp and str(po["t"]).startswith("TAKE"):
                    continue
                if po["cp"] or po["qty"] is None or Decimal(str(po["qty"])) == Decimal(str(abs(amt))): continue
```

然後把

```python
        if not has_sl:
            if not m: log("error", msg="缺停損且無紀錄停損價，市價平倉", symbol=sym); market_close(sym, amt, live, "no_stop_no_meta"); continue
            try: place_protect(sym, close_side, "STOP_MARKET", m["stop"], live)
            except Exception as e:
                log("error", msg="補掛停損失敗，市價平倉", symbol=sym, err=str(e)); market_close(sym, amt, live, "restop_failed"); continue
        if not has_tp and m:
            try: place_protect(sym, close_side, "TAKE_PROFIT_MARKET", m["target"], live)
            except Exception as e: log("error", msg="補掛止盈失敗", symbol=sym, err=str(e))
```

換成

```python
        if no_tp:
            for po in protect_orders(sym, algo, oo):
                if not str(po["t"]).startswith("TAKE"):
                    continue
                print("no_tp_cancel {0}".format(sym))
                log("no_tp_cancel", symbol=sym, id=po.get("id"))
                try:
                    cancel_protect(sym, po, live)
                except Exception as e:
                    log("error", msg="撤止盈失敗", symbol=sym, err=str(e))
            has_tp = False
        if not has_sl:
            if not m: log("error", msg="缺停損且無紀錄停損價，市價平倉", symbol=sym); market_close(sym, amt, live, "no_stop_no_meta"); continue
            # reduced 之後 meta.stop 已是成本價，缺停損就補掛在這個價，不是原來的結構停損。
            try: place_protect(sym, close_side, "STOP_MARKET", m["stop"], live)
            except Exception as e:
                log("error", msg="補掛停損失敗，市價平倉", symbol=sym, err=str(e)); market_close(sym, amt, live, "restop_failed"); continue
        if not has_tp and m and not no_tp:
            try: place_protect(sym, close_side, "TAKE_PROFIT_MARKET", m["target"], live)
            except Exception as e: log("error", msg="補掛止盈失敗", symbol=sym, err=str(e))
```

沒有 `no_tp` 時，補掛停損和止盈的請求與改之前相同。

## mark-reduced

在 `def cmd_flatten` 前面貼上倉庫裡的 `def cmd_mark_reduced`。它只讀 `positionRisk` 的 `entryPrice`，寫 meta，不掛單不撤單。

`main()` 的子命令加：

```python
    mr = sub.add_parser("mark-reduced"); mr.add_argument("--symbol", required=True); mr.add_argument("--live", action="store_true")
```

分派字典加 `"mark-reduced": cmd_mark_reduced`。不要改 `open` 的風險參數。
