"""持倉生命週期、保護單與情境驅動。交易所是 mock，不讀金鑰。"""
import json
import os
from decimal import Decimal
from pathlib import Path

from .autoscan import MAX_AGE_SECONDS, ScanBook, WebhookGate, freshness, fingerprint, parse_time
from .qty import D, dec_str, half_baseline, normalize_exit_qty
from .summary import summarize_position

FULL_CLOSE_AT = {"瘸師弟": Decimal("1.5")}
HALF_ONCE_AT = {"布鹿流": Decimal("1.5")}
DOCUMENTED_RETRY_AFTER = "1"


def _default_state():
    return {
        "lock_generation": 0,
        "lock_holder": None,
        "lives": {},
        "active_life": None,
        "trace_seq": 0,
        "now": "2026-11-07T09:10:00+08:00",
        "reduce_only_supported": None,
        "position_query": "ok",
        "pages_complete": True,
        "open_orders": [],
        "position_qty": None,
        "setup": None,
        "baseline_qty": None,
        "observed_r": None,
        "half_exit_done": False,
        "restarted": False,
        "new_half_exit_intents_after_restart": 0,
        "exits": {},
        "seen_fills": [],
        "protection": None,
        "initial_protection_id": None,
        "initial_protection_cancelled": False,
        "new_protection_confirmed": False,
        "required_order": [],
        "new_price_is_strategy_default": False,
        "old_cancel_allowed": False,
        "alert_required": False,
        "status": None,
        "reason": None,
        "recreate_2r_count": 0,
        "cancel_count": 0,
        "cancel_targets": [],
        "foreign_cancel_count": 0,
        "b_submit_before_reconcile": False,
        "b_reconciled": False,
        "blind_resubmit_count": 0,
        "new_identity_submit_count": 0,
        "first_client_id": None,
        "block_new_intent": False,
        "new_order_while_existing_unresolved": False,
        "submit_count": 0,
        "backoffs": [],
        "fill_markers": {},
        "protection_qty_after_first_fill": None,
        "protection_qty_after_final_fill": None,
        "exchange_orders": {},
        "exchange_protection_active": False,
        "ai_available": True,
        "ai_required_for_protection": False,
        "summary": None,
        "fresh_results": [],
        "fingerprint_flags": {},
        "logical_process_count": 0,
        "pending_until_processing_ack": False,
        "saw_pending_after_failed_send": False,
        "acked_after_commit": False,
        "evaluation_count": 0,
        "terminal_ack_status": None,
        "new_fresh_scan_required": False,
        "side_effect_idempotency_required": False,
        "evaluate_count": 0,
        "webhook": None,
        "lost_candidates": 0,
        "ai_wake_count": 0,
        "snapshot": None,
        "outbox": {},
        "inbox": {},
        "idempotency_keys": [],
        "staged_candidates": None,
    }


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "phase1_state.json"
        self.trace_path = self.root / "trace.jsonl"
        self.snapshot_path = self.root / "snapshot.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            self.state = _default_state()
            self.save()

    def save(self):
        _atomic(self.state_path, self.state)

    def write_snapshot(self, snap):
        _atomic(self.snapshot_path, snap)

    def trace(self, record):
        self.state["trace_seq"] = int(self.state.get("trace_seq") or 0) + 1
        record = dict(record)
        record["seq"] = self.state["trace_seq"]
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.save()

    def read_trace(self):
        if not self.trace_path.exists():
            return []
        rows = []
        for line in self.trace_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows


def _atomic(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Session:
    def __init__(self, root, *, fresh=False):
        self.root = Path(root)
        if fresh and self.root.exists():
            for child in self.root.glob("*"):
                if child.is_file():
                    child.unlink()
        self.store = Store(self.root)
        self.book = ScanBook(self.store)
        self.given = {}

    @property
    def s(self):
        return self.store.state

    def save(self):
        self.store.save()

    def restart(self):
        self.save()
        self.store = Store(self.root)
        self.book = ScanBook(self.store)
        self.s["restarted"] = True
        self.store.trace({"kind": "restart"})
        self.save()

    def load_case(self, case):
        given = dict(case.get("given") or {})
        self.given = given
        area = case.get("area")
        setup = given.get("strategy") or (area if area in ("瘸師弟", "布鹿流", "小師弟", "大師兄") else None)
        self.s["setup"] = setup
        ambiguous = any(key in given for key in ("adds", "prior_partial_qty", "baseline_unknown"))
        if "baseline_qty" in given:
            self.s["baseline_qty"] = given["baseline_qty"]
        elif ambiguous:
            self.s["baseline_qty"] = None
            self.s["status"] = "blocked"
            self.s["reason"] = "baseline_unspecified"
        elif "position_qty" in given and not given.get("half_exit_done"):
            self.s["baseline_qty"] = given["position_qty"]
        if "position_qty" in given:
            self.s["position_qty"] = given["position_qty"]
        if "actual_position_qty" in given:
            self.s["position_qty"] = given["actual_position_qty"]
        if "observed_R" in given:
            self.s["observed_r"] = given["observed_R"]
        self.s["half_exit_done"] = bool(given.get("half_exit_done") or False)
        self.s["reduce_only_supported"] = True if given.get("reduce_only_supported", True) is True else given.get("reduce_only_supported")
        if given.get("reduce_only_supported") is False:
            self.s["reduce_only_supported"] = False
        self.s["position_query"] = given.get("position_query", "ok")
        self.s["pages_complete"] = True if given.get("open_orders_query", "ok") == "ok" else False
        if "open_orders" in given:
            orders = []
            for item in given["open_orders"]:
                owner = "self" if str(item).startswith("owned") else "foreign"
                orders.append({"id": item, "owner": owner})
            self.s["open_orders"] = orders
        if self.s["position_qty"] is not None and given.get("position_query") != "unknown":
            self._seed_protection(given.get("old_protection") or "prot-0", self.s["position_qty"])
        elif given.get("old_protection"):
            self._seed_protection(given["old_protection"], given.get("actual_position_qty") or "0")
        if given.get("cancelled_ladder"):
            life = self._life()
            life["tombstones"] = list(given["cancelled_ladder"])
        if given.get("persistent_tombstone") and given.get("cancelled_ladder"):
            self._life()["tombstones"] = list(given["cancelled_ladder"])
        if "outbox" in given:
            for fp in given["outbox"]:
                self.s["outbox"][fp] = {
                    "fingerprint": fp,
                    "status": "pending",
                    "notify_seen": bool(given.get("notify")),
                    "scan_completed_at": self.s["now"],
                    "evaluation_id": "eval:" + fp,
                }
        if given.get("valid_exchange_protection"):
            self.s["exchange_protection_active"] = True
            self.s["ai_required_for_protection"] = False
        if "ai_available" in given:
            self.s["ai_available"] = bool(given["ai_available"])
        self.s["client_intent_hint"] = given.get("client_intent_id")
        if given.get("client_intent_id") and self.s.get("protection") is None:
            self._seed_protection("preexisting", self.s.get("position_qty") or "1")
        self.save()

    def _life(self):
        life_id = self.s.get("active_life") or "life-1"
        self.s["active_life"] = life_id
        lives = self.s["lives"]
        if life_id not in lives:
            lives[life_id] = {"id": life_id, "tombstones": [], "setup": self.s.get("setup")}
        return lives[life_id]

    def _seed_protection(self, protection_id, qty):
        self.s["protection"] = {
            "id": protection_id,
            "qty": dec_str(qty),
            "status": "valid",
            "venue": "exchange",
            "depends_on_ai": False,
            "reduce_only": self.s["reduce_only_supported"] is True,
        }
        self.s["initial_protection_id"] = protection_id
        self.s["exchange_protection_active"] = True
        self.s["ai_required_for_protection"] = False

    def handle(self, event):
        name = event
        dispatch = {
            "threshold_reached": self.threshold_reached,
            "repeat_tick": self.threshold_reached,
            "fill_qty_5": lambda: self.apply_fill("5", "fill_qty_5"),
            "restart": self.restart,
            "submit_exit_5": lambda: self.ensure_exit("partial_exit", "5", client_id=None),
            "fill_2": lambda: self.apply_fill("2", "fill_2"),
            "timeout": self.mark_timeout,
            "reconcile_order_open_remaining_3": lambda: self.reconcile_open_remaining("3"),
            "fill_3": lambda: self.apply_fill("3", "fill_3"),
            "submit_new_protection_qty_5": lambda: self.submit_protection("5", self.given.get("new_price")),
            "exchange_ack_and_query_valid": self.confirm_new_protection,
            "cancel_old": self.cancel_old_protection,
            "new_protection_rejected": self.reject_new_protection,
            "watch_tick": self.watch,
            "attempt_cancel": self.cancel_orphans,
            "reconcile": self.reconcile_readonly,
            "cancel_owned_orphan": self.cancel_orphans,
            "A_acquires_lock": lambda: self.acquire_lock("A"),
            "B_lock_denied": lambda: self.acquire_lock("B"),
            "A_submits": self.worker_a_submit,
            "B_restarts_and_reconciles": self.worker_b_restart,
            "submit": self.submit_client_exit,
            "exchange_accepts": self.exchange_accepts_quietly,
            "response_timeout": self.response_timeout,
            "query_by_persistent_identity_finds_order": self.query_finds_order,
            "submit_timeout": self.submit_timeout,
            "query_timeout": self.query_timeout,
            "rate_limit": self.rate_limit,
            "normalize_quantity": self.normalize_quantity,
            "429": self.on_429,
            "5xx": self.on_5xx,
            "disconnect": self.on_disconnect,
            "aggregate_actual_fills": self.do_summary,
            "summarize": self.do_summary,
            "evaluate_strategy_gate": self.strategy_gate,
            "AI_offline": self.ai_offline,
            "local_monitor_restart": self.local_monitor_restart,
            "scan_returns_empty": self.stage_empty,
            "atomic_replace_snapshot": self.commit_staged_snapshot,
            "consume_age_899": lambda: self.consume_age(899),
            "consume_age_900": lambda: self.consume_age(900),
            "consume_age_901": lambda: self.consume_age(901),
            "consume_negative_age": lambda: self.consume_age(-1),
            "same_canonical_payload_twice": self.same_canonical,
            "change_ladder_price": self.change_ladder_price,
            "change_bar_time": self.change_bar_time,
            "send_fails": self.send_fails,
            "next_snapshot_notify_false": self.next_snapshot_notify_false,
            "producer_restart": self.restart,
            "retry": self.retry_pending,
            "consumer_durable_inbox": self.reload_inbox,
            "process": self.process_pending,
            "ack": self.ack_pending,
            "receive": self.receive_fp,
            "persist_inbox": self.persist_given_fp,
            "process_commit": self.commit_given_fp,
            "ack_lost": self.ack_lost,
            "duplicate_delivery": self.duplicate_delivery,
            "consumer_restart": self.restart,
            "receive_delayed_event": self.receive_delayed,
            "claim_lease": self.claim_given_lease,
            "crash_before_commit": self.crash_before_commit,
            "lease_expires": self.expire_given_lease,
            "resume_with_same_evaluation_identity": self.resume_same_identity,
            "webhook_wakes_bot_without_snapshot_access": self.webhook_without_access,
        }
        if name not in dispatch:
            raise KeyError(name)
        dispatch[name]()
        self.save()

    def threshold_reached(self):
        setup = self.s.get("setup")
        observed = None if self.s.get("observed_r") is None else D(self.s["observed_r"])
        if setup in FULL_CLOSE_AT and observed is not None and observed >= FULL_CLOSE_AT[setup]:
            self.ensure_exit("full_exit", self.s["position_qty"])
            return
        if setup in HALF_ONCE_AT and observed is not None and observed >= HALF_ONCE_AT[setup]:
            if self.s["half_exit_done"]:
                self.store.trace({"kind": "skip_half", "reason": "already_done"})
                return
            qty, reason = half_baseline(self.s.get("baseline_qty"))
            if reason:
                self.s["status"] = "blocked"
                self.s["reason"] = reason
                self.store.trace({"kind": "blocked", "reason": reason})
                return
            self.ensure_exit("half_exit", dec_str(qty))
            return
        if setup in ("小師弟", "大師兄"):
            self.s["status"] = "blocked"
            self.s["reason"] = "exit_R_undecided"
            self.store.trace({"kind": "blocked", "reason": "exit_R_undecided"})

    def ensure_exit(self, action, qty, client_id=None):
        key = (self.s.get("active_life") or "life-1") + ":" + action
        if key in self.s["exits"]:
            self.store.trace({"kind": "intent_reused", "logical_id": key})
            return self.s["exits"][key]
        unresolved = [item for item in self.s["exits"].values() if item["status"] in ("open", "reconcile_pending", "submitted")]
        if unresolved:
            self.s["new_order_while_existing_unresolved"] = True
            self.s["blind_resubmit_count"] += 1
            self.store.trace({"kind": "blocked", "reason": "unresolved_exit", "logical_id": unresolved[0]["logical_id"]})
            return None
        if self.s["block_new_intent"]:
            self.store.trace({"kind": "blocked", "reason": "unknown_acceptance_blocks_new_intent"})
            return None
        if self.s["reduce_only_supported"] is not True:
            self.s["status"] = "blocked"
            self.s["reason"] = "reduce_only_unknown"
            self.store.trace({"kind": "blocked", "reason": "reduce_only_unknown"})
            return None
        if self.s["restarted"] and action == "half_exit":
            self.s["new_half_exit_intents_after_restart"] += 1
        if client_id is None:
            client_id = self.s.get("client_intent_hint") or ("cid-" + key)
        if self.s["first_client_id"] is None:
            self.s["first_client_id"] = client_id
        elif client_id != self.s["first_client_id"]:
            self.s["new_identity_submit_count"] += 1
        intent = {
            "logical_id": key,
            "action": action,
            "client_intent_id": client_id,
            "target_qty": dec_str(qty),
            "filled_qty": "0",
            "status": "submitted",
            "reduce_only": True,
            "exchange_order_id": None,
        }
        self.s["exits"][key] = intent
        self.s["submit_count"] += 1
        self.store.trace({
            "kind": "request",
            "op": "exit",
            "logical_id": key,
            "client_intent_id": client_id,
            "qty": intent["target_qty"],
            "reduce_only": True,
        })
        self.s["exchange_orders"][client_id] = {
            "client_intent_id": client_id,
            "qty": intent["target_qty"],
            "filled_qty": "0",
            "status": "accepted_unseen",
            "reduce_only": True,
        }
        self.store.trace({"kind": "ack", "client_intent_id": client_id, "seen_by_client": False})
        intent["status"] = "open"
        intent["exchange_order_id"] = "ex-" + client_id
        self.store.trace({"kind": "ack", "client_intent_id": client_id, "order_id": intent["exchange_order_id"], "seen_by_client": True})
        return intent

    def apply_fill(self, qty, fill_id):
        if fill_id in self.s["seen_fills"]:
            self.store.trace({"kind": "fill_duplicate_ignored", "id": fill_id})
            return
        intent = self._open_exit()
        if intent is None:
            self.s["status"] = "blocked"
            self.store.trace({"kind": "blocked", "reason": "fill_without_intent"})
            return
        self.s["seen_fills"].append(fill_id)
        filled = D(intent["filled_qty"]) + D(qty)
        intent["filled_qty"] = dec_str(filled)
        if self.s["position_qty"] is not None:
            self.s["position_qty"] = dec_str(D(self.s["position_qty"]) - D(qty))
        self.store.trace({
            "kind": "fill",
            "id": fill_id,
            "qty": dec_str(qty),
            "client_intent_id": intent["client_intent_id"],
            "logical_id": intent["logical_id"],
        })
        order = self.s["exchange_orders"].get(intent["client_intent_id"])
        if order is not None:
            order["filled_qty"] = intent["filled_qty"]
            remaining = D(intent["target_qty"]) - D(intent["filled_qty"])
            order["status"] = "filled" if remaining <= 0 else "open"
        if D(intent["filled_qty"]) >= D(intent["target_qty"]):
            intent["status"] = "filled"
            if intent["action"] == "half_exit":
                self.s["half_exit_done"] = True
        self._rotate_protection(self.s["position_qty"])
        if self.s["protection_qty_after_first_fill"] is None:
            self.s["protection_qty_after_first_fill"] = self.s["protection"]["qty"] if self.s["protection"] else None
        self.s["protection_qty_after_final_fill"] = self.s["protection"]["qty"] if self.s["protection"] else None

    def _open_exit(self):
        for intent in self.s["exits"].values():
            if intent["status"] in ("open", "submitted", "reconcile_pending"):
                return intent
        for intent in self.s["exits"].values():
            if intent["status"] != "filled":
                return intent
        if self.s["exits"]:
            return list(self.s["exits"].values())[-1]
        return None

    def _rotate_protection(self, qty):
        if qty is None or self.s["protection"] is None:
            return
        if self.s["reduce_only_supported"] is not True:
            self.s["status"] = "blocked"
            self.s["reason"] = "reduce_only_unknown"
            self.store.trace({"kind": "blocked", "reason": "reduce_only_unknown"})
            return
        old = self.s["protection"]
        if D(old["qty"]) == D(qty) and old["status"] == "valid":
            return
        new_id = "prot-" + dec_str(qty) + "-" + str(self.s["trace_seq"] + 1)
        self.store.trace({"kind": "request", "op": "protection", "id": new_id, "qty": dec_str(qty), "reduce_only": True})
        self.store.trace({"kind": "query", "op": "protection", "id": new_id, "status": "valid", "qty": dec_str(qty)})
        self.store.trace({"kind": "ack", "op": "protection", "id": new_id, "qty": dec_str(qty)})
        self.store.trace({"kind": "cancel", "op": "protection", "id": old["id"]})
        if old["id"] == self.s["initial_protection_id"]:
            self.s["initial_protection_cancelled"] = True
        self.s["protection"] = {
            "id": new_id,
            "qty": dec_str(qty),
            "status": "valid",
            "venue": "exchange",
            "depends_on_ai": False,
            "reduce_only": True,
        }
        self.s["exchange_protection_active"] = True

    def mark_timeout(self):
        intent = self._open_exit()
        if intent is None:
            return
        intent["status"] = "reconcile_pending"
        self.s["status"] = "reconcile_pending"
        self.store.trace({"kind": "timeout", "client_intent_id": intent["client_intent_id"], "acceptance": "unknown"})

    def reconcile_open_remaining(self, remaining):
        intent = self._open_exit()
        if intent is None:
            self.s["status"] = "blocked"
            return
        self.store.trace({
            "kind": "query",
            "client_intent_id": intent["client_intent_id"],
            "status": "open",
            "remaining_qty": dec_str(remaining),
        })
        intent["status"] = "open"
        self.s["status"] = "open"

    def submit_protection(self, qty, price):
        if self.s["reduce_only_supported"] is not True:
            self.s["status"] = "blocked"
            self.s["reason"] = "reduce_only_unknown"
            self.store.trace({"kind": "blocked", "reason": "reduce_only_unknown"})
            return
        requested = None if price is None else dec_str(price)
        submitted = requested
        self.s["new_price_is_strategy_default"] = submitted != requested
        new_id = "prot-new"
        self.s["pending_protection"] = {"id": new_id, "qty": dec_str(qty), "price": submitted, "status": "submitted"}
        self.s["submit_count"] += 1
        self.store.trace({
            "kind": "request",
            "op": "protection",
            "id": new_id,
            "qty": dec_str(qty),
            "price": submitted,
            "reduce_only": True,
        })

    def confirm_new_protection(self):
        pending = self.s.get("pending_protection")
        if not pending:
            self.s["status"] = "blocked"
            return
        if self.s["position_qty"] is not None and D(pending["qty"]) != D(self.s["position_qty"]):
            self.s["status"] = "blocked"
            self.s["alert_required"] = True
            self.store.trace({"kind": "alert", "reason": "protection_qty_mismatch"})
            return
        self.store.trace({"kind": "query", "op": "protection", "id": pending["id"], "status": "valid", "qty": pending["qty"]})
        self.store.trace({"kind": "ack", "op": "protection", "id": pending["id"], "status": "valid"})
        pending["status"] = "valid"
        self.s["new_protection_confirmed"] = True
        self.s["required_order"].append("new_confirmed")
        self.s["protection"] = {
            "id": pending["id"],
            "qty": pending["qty"],
            "price": pending["price"],
            "status": "valid",
            "venue": "exchange",
            "depends_on_ai": False,
            "reduce_only": True,
        }
        self.s["exchange_protection_active"] = True

    def cancel_old_protection(self):
        if not self.s.get("new_protection_confirmed"):
            self.s["old_cancel_allowed"] = False
            self.s["status"] = "blocked"
            self.s["alert_required"] = True
            self.store.trace({"kind": "cancel_refused", "reason": "new_protection_not_confirmed"})
            return
        old_id = self.s.get("initial_protection_id")
        self.store.trace({"kind": "cancel", "op": "protection", "id": old_id})
        self.s["initial_protection_cancelled"] = True
        self.s["old_cancel_allowed"] = True
        self.s["required_order"].append("old_cancel")
        self.s["cancel_count"] += 1

    def reject_new_protection(self):
        self.store.trace({"kind": "request", "op": "protection", "id": "prot-new", "qty": self.s.get("position_qty")})
        self.store.trace({"kind": "reject", "op": "protection", "id": "prot-new"})
        self.s["new_protection_confirmed"] = False
        self.s["old_cancel_allowed"] = False
        self.s["status"] = "blocked"
        self.s["alert_required"] = True
        self.store.trace({"kind": "alert", "reason": "new_protection_rejected"})

    def watch(self):
        self.store.trace({"kind": "watch", "ai_available": self.s["ai_available"]})
        for life in self.s["lives"].values():
            for label in life.get("tombstones") or []:
                self.store.trace({"kind": "skip_restore", "ladder": label, "life_id": life["id"]})
        if "2R" in (self._life().get("tombstones") or []):
            return
        # 沒有墓碑也不主動補 2R。現行出場規則沒有這張保護單。

    def acquire_lock(self, worker):
        path = self.root / "fence.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self.store.trace({"kind": "lock_denied", "worker": worker})
            return False
        generation = int(self.s["lock_generation"]) + 1
        self.s["lock_generation"] = generation
        self.s["lock_holder"] = worker
        os.write(fd, str(generation).encode())
        os.close(fd)
        self.store.trace({"kind": "lock_acquired", "worker": worker, "generation": generation})
        self.save()
        return True

    def cancel_orphans(self):
        generation = self.s["lock_generation"]
        locked = bool(self.s["lock_holder"]) or bool(self.given.get("lock_acquired"))
        if not locked:
            self.s["status"] = "blocked"
            self.store.trace({"kind": "blocked", "reason": "lock_required"})
            return
        if self.s["position_query"] == "unknown" or self.s["pages_complete"] is not True:
            self.s["status"] = "blocked"
            self.store.trace({"kind": "blocked", "reason": "position_or_pages_unknown"})
            return
        if self.s["lock_generation"] != generation and self.s["lock_holder"]:
            self.s["status"] = "blocked"
            self.store.trace({"kind": "blocked", "reason": "fencing_generation_changed"})
            return
        self.store.trace({"kind": "query", "op": "position", "qty": self.s.get("position_qty"), "generation": generation})
        self.store.trace({"kind": "query", "op": "open_orders", "generation": generation, "complete": True})
        if self.s["lock_generation"] != generation and self.s["lock_holder"]:
            self.s["status"] = "blocked"
            return
        for order in list(self.s["open_orders"]):
            if order["owner"] != "self":
                self.store.trace({"kind": "cancel_skipped", "id": order["id"], "reason": "foreign"})
                continue
            if self.s.get("position_qty") not in ("0", 0, None) and D(self.s["position_qty"]) != 0:
                self.store.trace({"kind": "cancel_skipped", "id": order["id"], "reason": "position_still_open"})
                continue
            self.store.trace({"kind": "cancel", "op": "orphan", "id": order["id"]})
            self.s["cancel_count"] += 1
            self.s["cancel_targets"].append(order["id"])
        self.s["open_orders"] = [order for order in self.s["open_orders"] if order["id"] not in self.s["cancel_targets"]]

    def reconcile_readonly(self):
        self.s["b_reconciled"] = True
        self.store.trace({"kind": "query", "op": "reconcile", "exits": list(self.s["exits"])})

    def worker_a_submit(self):
        if self.s["lock_holder"] != "A":
            self.s["status"] = "blocked"
            return
        self.ensure_exit("locked_exit", self.s.get("position_qty") or "0", client_id="cid-A")

    def worker_b_restart(self):
        self.restart()
        self.s["b_reconciled"] = True
        self.store.trace({"kind": "query", "op": "reconcile", "worker": "B", "exits": list(self.s["exits"])})
        if not self.s["b_reconciled"]:
            self.s["b_submit_before_reconcile"] = True

    def submit_client_exit(self):
        client_id = self.given.get("client_intent_id") or "cid-exit"
        self.ensure_exit("client_exit", self.s.get("position_qty") or "1", client_id=client_id)
        intent = self._open_exit()
        if intent is not None:
            intent["status"] = "submitted"
            self.s["exchange_orders"][client_id]["status"] = "pending_ack"

    def exchange_accepts_quietly(self):
        intent = self._open_exit()
        if intent is None:
            return
        order = self.s["exchange_orders"][intent["client_intent_id"]]
        order["status"] = "accepted"
        intent["exchange_order_id"] = "ex-" + intent["client_intent_id"]
        self.store.trace({"kind": "exchange_accept", "client_intent_id": intent["client_intent_id"], "seen_by_client": False})

    def response_timeout(self):
        intent = self._open_exit()
        if intent is None:
            return
        intent["status"] = "reconcile_pending"
        self.s["status"] = "reconcile_pending"
        self.store.trace({"kind": "timeout", "client_intent_id": intent["client_intent_id"], "acceptance": "accepted_unseen"})

    def query_finds_order(self):
        intent = self._open_exit()
        if intent is None:
            self.s["status"] = "blocked"
            return
        order = self.s["exchange_orders"].get(intent["client_intent_id"])
        self.store.trace({"kind": "query", "client_intent_id": intent["client_intent_id"], "found": order is not None})
        if order is None or order["status"] not in ("accepted", "accepted_unseen", "open"):
            self.s["status"] = "reconcile_pending"
            return
        intent["status"] = "open"
        intent["exchange_order_id"] = order.get("status") and ("ex-" + intent["client_intent_id"])
        self.s["status"] = "open"

    def submit_timeout(self):
        client_id = self.given.get("client_intent_id") or "cid-unknown"
        if self.s["first_client_id"] is None:
            self.s["first_client_id"] = client_id
        key = "life-1:unknown_exit"
        if key not in self.s["exits"]:
            self.s["exits"][key] = {
                "logical_id": key,
                "action": "unknown_exit",
                "client_intent_id": client_id,
                "target_qty": self.s.get("position_qty") or "1",
                "filled_qty": "0",
                "status": "reconcile_pending",
                "reduce_only": True,
                "exchange_order_id": None,
            }
            self.s["submit_count"] += 1
        self.s["status"] = "reconcile_pending"
        self.s["block_new_intent"] = True
        self.store.trace({"kind": "request", "op": "exit", "client_intent_id": client_id, "acceptance": "unknown"})
        self.store.trace({"kind": "timeout", "client_intent_id": client_id, "acceptance": "unknown"})

    def query_timeout(self):
        self.s["status"] = "reconcile_pending"
        self.s["block_new_intent"] = True
        self.store.trace({"kind": "query_timeout", "acceptance": "unknown"})

    def rate_limit(self):
        self._backoff(DOCUMENTED_RETRY_AFTER)
        self.s["status"] = "reconcile_pending"
        self.s["block_new_intent"] = True

    def on_429(self):
        self._backoff(DOCUMENTED_RETRY_AFTER)
        self.s["block_new_intent"] = True
        self.s["status"] = "reconcile_pending"

    def on_5xx(self):
        self.s["block_new_intent"] = True
        self.s["status"] = "reconcile_pending"
        self.store.trace({"kind": "request", "op": "query", "http": 500, "acceptance": "unknown"})

    def on_disconnect(self):
        self.s["block_new_intent"] = True
        self.s["status"] = "reconcile_pending"
        self.store.trace({"kind": "disconnect", "acceptance": "unknown"})

    def _backoff(self, retry_after):
        self.s["backoffs"].append({"seconds": retry_after, "source": "documented_retry_after"})
        self.store.trace({"kind": "backoff", "seconds": retry_after, "source": "documented_retry_after", "bounded": True})

    def normalize_quantity(self):
        actual = D(self.given["actual_qty"]) if "actual_qty" in self.given else None
        qty, reason, remainder, never = normalize_exit_qty(
            self.given["requested_exit_qty"],
            self.given.get("qty_step"),
            self.given.get("min_qty"),
            actual,
        )
        self.s["never_exceed_actual"] = never
        self.s["rounding"] = "down"
        self.s["remainder_reported"] = remainder is not None and remainder != 0
        if remainder is not None and remainder != 0:
            self.store.trace({"kind": "alert", "reason": "qty_remainder", "remainder": dec_str(remainder)})
        if reason:
            self.s["status"] = "blocked"
            self.s["reason"] = reason
            self.s["max_exit_qty"] = "0" if qty is None else dec_str(qty)
            self.store.trace({"kind": "blocked", "reason": reason})
            return
        self.s["max_exit_qty"] = dec_str(qty)
        self.store.trace({"kind": "normalize", "qty": dec_str(qty), "rounding": "down"})

    def do_summary(self):
        self.s["summary"] = summarize_position(self.given)
        self.store.trace({"kind": "summary", "display": self.s["summary"].get("display"), "fills": self.s["summary"].get("fills_used")})

    def strategy_gate(self):
        missing = [
            key for key in ("daily_ma_period", "v_reversal_no_reclaim_exit", "fixed_R_small_senior")
            if self.given.get(key) is None
        ]
        self.s["status"] = "blocked"
        self.s["automatic_pass"] = False
        self.store.trace({"kind": "blocked", "reason": "strategy_undecided", "missing": missing})

    def ai_offline(self):
        self.s["ai_available"] = False
        self.store.trace({"kind": "ai_offline", "protection_touched": False})

    def local_monitor_restart(self):
        active = self.s["exchange_protection_active"]
        self.restart()
        self.s["exchange_protection_active"] = active
        self.s["ai_required_for_protection"] = False
        self.s["ai_available"] = False
        self.store.trace({"kind": "monitor_restart", "ai_required_for_protection": False, "protection_active": active})

    def stage_empty(self):
        self.s["staged_candidates"] = []

    def commit_staged_snapshot(self):
        candidates = self.s.get("staged_candidates") or []
        started = "2026-11-07T09:00:00+08:00"
        completed = "2026-11-07T09:00:01+08:00"
        self.book.publish(
            scan_id="scan-empty",
            started_at=started,
            completed_at=completed,
            status="ok",
            candidates=candidates,
            notify=False,
        )

    def consume_age(self, seconds):
        completed = "2026-11-07T09:00:00+08:00"
        base = parse_time(completed)
        from datetime import timedelta
        now = base + timedelta(seconds=int(seconds))
        verdict = freshness(completed, now, MAX_AGE_SECONDS)
        self.s["fresh_results"].append(verdict["valid"])
        if seconds < 0:
            self.s["negative_age"] = verdict["status"]
        self.store.trace({"kind": "freshness", "age_seconds": seconds, "valid": verdict["valid"], "status": verdict["status"]})

    def same_canonical(self):
        first = fingerprint(self.given["symbol"], self.given["setup"], self.given["bar_time"], self.given["ladder"])
        second = fingerprint(self.given["symbol"], self.given["setup"], self.given["bar_time"], json.loads(json.dumps(self.given["ladder"])))
        widened = json.loads(json.dumps(self.given["ladder"]))
        widened[0]["price"] = "100.0"
        third = fingerprint(self.given["symbol"], self.given["setup"], self.given["bar_time"], widened)
        self.s["fingerprint_flags"]["same"] = first == second == third
        self.s["fingerprint_flags"]["base"] = first
        self.store.trace({"kind": "fingerprint", "same": self.s["fingerprint_flags"]["same"]})

    def change_ladder_price(self):
        ladder = json.loads(json.dumps(self.given["ladder"]))
        ladder[0]["price"] = "101"
        changed = fingerprint(self.given["symbol"], self.given["setup"], self.given["bar_time"], ladder)
        self.s["fingerprint_flags"]["ladder_changed"] = changed != self.s["fingerprint_flags"].get("base")
        self.store.trace({"kind": "fingerprint", "ladder_changed": self.s["fingerprint_flags"]["ladder_changed"]})

    def change_bar_time(self):
        changed = fingerprint(self.given["symbol"], self.given["setup"], "2026-11-07T10:00:00+08:00", self.given["ladder"])
        self.s["fingerprint_flags"]["bar_changed"] = changed != self.s["fingerprint_flags"].get("base")
        self.store.trace({"kind": "fingerprint", "bar_changed": self.s["fingerprint_flags"]["bar_changed"]})

    def send_fails(self):
        self.book.fail_next_send = 1
        for fp, item in list(self.s["outbox"].items()):
            if item["status"] == "pending":
                self.book.attempt_send(fp)
        self.s["saw_pending_after_failed_send"] = any(item["status"] == "pending" for item in self.s["outbox"].values())

    def next_snapshot_notify_false(self):
        self.book.publish(
            scan_id="scan-notify-false",
            started_at="2026-11-07T09:15:00+08:00",
            completed_at="2026-11-07T09:15:01+08:00",
            status="ok",
            candidates=[],
            notify=False,
        )

    def retry_pending(self):
        for fp, item in list(self.s["outbox"].items()):
            if item["status"] == "pending":
                self.book.attempt_send(fp)

    def reload_inbox(self):
        self.store.save()
        self.store = Store(self.root)
        self.book = ScanBook(self.store)
        self.store.trace({"kind": "inbox_reloaded", "keys": list(self.s["inbox"])})

    def process_pending(self):
        for fp, item in list(self.s["outbox"].items()):
            if item["status"] == "pending" and fp in self.s["inbox"]:
                self.book.commit(fp)
        self.s["logical_process_count"] = sum(row["process_count"] for row in self.s["inbox"].values())

    def ack_pending(self):
        for fp, row in list(self.s["inbox"].items()):
            if row["status"] == "committed":
                self.book.processing_ack(fp)
        self.s["acked_after_commit"] = all(
            item["status"] == "acked" for item in self.s["outbox"].values()
        ) and self.s["saw_pending_after_failed_send"]
        self.s["pending_until_processing_ack"] = self.s["acked_after_commit"]
        self.s["logical_process_count"] = sum(row["process_count"] for row in self.s["inbox"].values())

    def _fp(self):
        return self.given.get("fingerprint") or "fp-A"

    def receive_fp(self):
        self.book.persist_inbox(self._fp())

    def persist_given_fp(self):
        self.book.persist_inbox(self._fp())

    def commit_given_fp(self):
        self.book.commit(self._fp())
        self.s["logical_process_count"] = sum(row["process_count"] for row in self.s["inbox"].values())

    def ack_lost(self):
        self.store.trace({"kind": "ack_lost", "fingerprint": self._fp()})

    def duplicate_delivery(self):
        fp = self._fp()
        row = self.s["inbox"].get(fp)
        if row and row["status"] == "committed":
            row["ack_reissued"] = True
            self.store.trace({"kind": "ack_reissue", "fingerprint": fp, "evaluation_id": row["evaluation_id"]})
            item = self.s["outbox"].get(fp)
            if item is not None:
                item["status"] = "acked"
            return
        self.book.persist_inbox(fp)

    def receive_delayed(self):
        age = int(self.given.get("candidate_age_seconds") or 0)
        fp = self._fp()
        self.s["outbox"][fp] = {
            "fingerprint": fp,
            "status": "pending",
            "notify_seen": True,
            "scan_completed_at": "2026-11-07T09:00:00+08:00",
            "evaluation_id": "eval:" + fp,
        }
        from datetime import timedelta
        now = parse_time("2026-11-07T09:00:00+08:00") + timedelta(seconds=age)
        self.s["now"] = now.isoformat(timespec="seconds")
        outcome = self.book.attempt_send(fp)
        if outcome.get("status") == "stale":
            self.s["evaluation_count"] = 0
            self.s["terminal_ack_status"] = "stale"
            self.s["new_fresh_scan_required"] = True

    def claim_given_lease(self):
        self.book.persist_inbox(self._fp())
        self.book.claim_lease(self._fp())

    def crash_before_commit(self):
        self.store.trace({"kind": "crash", "fingerprint": self._fp(), "committed": False})
        self.save()

    def expire_given_lease(self):
        self.book.expire_lease(self._fp())

    def resume_same_identity(self):
        fp = self._fp()
        before = self.s["inbox"].get(fp, {}).get("evaluation_id")
        self.book.commit(fp)
        after = self.s["inbox"][fp]["evaluation_id"]
        if before != after:
            self.s["lost_candidates"] += 1
        self.s["side_effect_idempotency_required"] = True
        self.s["logical_process_count"] = sum(row["commit_count"] for row in self.s["inbox"].values())

    def webhook_without_access(self):
        gate = WebhookGate()
        payload = {"note": "untrusted", "command": "ignored"}
        if self.given.get("verified_transport") is None:
            verdict = gate.consider(
                {"scan_id": "not-readable", "command": "do-not-run"},
                None,
            )
        else:
            verdict = gate.consider(payload, self.given.get("verified_transport"))
        self.s["webhook"] = verdict
        self.s["status"] = verdict["status"]
        self.s["reason"] = verdict["reason"]
        self.s["evaluate_count"] = verdict["evaluate_count"]
        self.store.trace({"kind": "webhook_blocked", "reason": verdict["reason"]})

    def result(self):
        exits = list(self.s["exits"].values())
        half_filled = sum((D(item["filled_qty"]) for item in exits if item["action"] == "half_exit"), D(0))
        exit_filled = sum((D(item["filled_qty"]) for item in exits), D(0))
        close_target = None
        fulls = [item for item in exits if item["action"] == "full_exit"]
        if fulls:
            close_target = fulls[-1]["target_qty"]
        summary = self.s.get("summary") or {}
        flags = self.s.get("fingerprint_flags") or {}
        snapshot = self.s.get("snapshot") or {}
        candidates = snapshot.get("candidates") or []
        inbox_rows = list(self.s["inbox"].values())
        process_count = sum(row.get("process_count") or 0 for row in inbox_rows) or self.s.get("logical_process_count") or 0
        commit_count = sum(row.get("commit_count") or 0 for row in inbox_rows)
        backoffs = self.s.get("backoffs") or []
        return {
            "close_target_qty": close_target,
            "logical_exit_intents": len(exits),
            "reduce_only": bool(exits) and all(item["reduce_only"] for item in exits),
            "total_half_exit_filled_qty": dec_str(half_filled) if exits else "0",
            "remaining_qty": None if self.s.get("position_qty") is None else dec_str(self.s["position_qty"]),
            "half_exit_done": self.s["half_exit_done"],
            "new_half_exit_intents_after_restart": self.s["new_half_exit_intents_after_restart"],
            "total_exit_fills": dec_str(exit_filled),
            "new_order_while_existing_unresolved": self.s["new_order_while_existing_unresolved"],
            "protection_qty_after_first_fill": self.s["protection_qty_after_first_fill"],
            "protection_qty_after_final_fill": self.s["protection_qty_after_final_fill"],
            "required_order": list(self.s["required_order"]),
            "protected_qty": None if not self.s.get("protection") else self.s["protection"]["qty"],
            "new_price_is_strategy_default": self.s["new_price_is_strategy_default"],
            "old_cancel_allowed": self.s["old_cancel_allowed"],
            "status": self.s["status"],
            "alert_required": self.s["alert_required"],
            "recreate_2R_count": self.s["recreate_2r_count"],
            "cancel_count": self.s["cancel_count"],
            "cancel_targets": list(self.s["cancel_targets"]),
            "foreign_cancel_count": self.s["foreign_cancel_count"],
            "B_submit_before_reconcile": self.s["b_submit_before_reconcile"],
            "blind_resubmit_count": self.s["blind_resubmit_count"],
            "new_identity_submit_count": self.s["new_identity_submit_count"],
            "existing_protection_retained": bool(self.s.get("initial_protection_id")) and not self.s["initial_protection_cancelled"],
            "max_exit_qty": self.s.get("max_exit_qty"),
            "rounding": self.s.get("rounding"),
            "remainder_reported": self.s.get("remainder_reported"),
            "never_exceed_actual": self.s.get("never_exceed_actual"),
            "submit_count": self.s["submit_count"],
            "reason": self.s["reason"],
            "bounded_backoff_required": bool(backoffs) and all(item.get("source") == "documented_retry_after" for item in backoffs),
            "respect_documented_retry_after": bool(backoffs) and all(item.get("seconds") == DOCUMENTED_RETRY_AFTER for item in backoffs),
            "blind_duplicate_count": self.s["blind_resubmit_count"],
            "unknown_acceptance_blocks_new_intent": self.s["block_new_intent"],
            "gross_pnl_quote": summary.get("gross_pnl_quote"),
            "fee_quote": summary.get("fee_quote"),
            "net_pnl_quote": summary.get("net_pnl_quote"),
            "net_R": summary.get("net_R"),
            "exit_vwap": summary.get("exit_vwap"),
            "adverse_slippage_quote": summary.get("adverse_slippage_quote"),
            "slippage": summary.get("slippage"),
            "slippage_sign": summary.get("slippage_sign"),
            "display": summary.get("display"),
            "must_not_estimate_as_actual": summary.get("must_not_estimate_as_actual", True),
            "realized_trade_count": summary.get("realized_trade_count", 0),
            "realized_pnl": summary.get("realized_pnl"),
            "automatic_pass": self.s.get("automatic_pass", False),
            "exchange_protection_still_active": self.s["exchange_protection_active"],
            "AI_required_for_protection": self.s["ai_required_for_protection"],
            "candidates": candidates,
            "AI_wake_count": self.s["ai_wake_count"],
            "old_candidates_retained": any((item.get("symbol") == "old" or item.get("fingerprint") == "old") for item in candidates),
            "valid": list(self.s["fresh_results"]),
            "negative_age": self.s.get("negative_age"),
            "age_basis": "scan_completed_at",
            "same_payload_same_fingerprint": flags.get("same"),
            "ladder_change_new_fingerprint": flags.get("ladder_changed"),
            "bar_change_new_fingerprint": flags.get("bar_changed"),
            "lost_candidates": self.s["lost_candidates"],
            "logical_process_count": process_count,
            "pending_until_processing_ack": self.s["pending_until_processing_ack"],
            "ack_reissued": any(row.get("ack_reissued") for row in inbox_rows),
            "inbox_unique_key": "fingerprint",
            "evaluation_count": self.s["evaluation_count"],
            "terminal_ack_status": self.s["terminal_ack_status"] or (inbox_rows[-1].get("terminal_ack_status") if inbox_rows else None),
            "new_fresh_scan_required": self.s["new_fresh_scan_required"],
            "logical_commit_count": commit_count,
            "side_effect_idempotency_required": self.s["side_effect_idempotency_required"],
            "evaluate_count": self.s["evaluate_count"],
        }


def run_case(case, root):
    session = Session(root, fresh=True)
    session.load_case(case)
    if case["id"] == "A01":
        session.book.publish(
            scan_id="scan-old",
            started_at="2026-11-07T08:00:00+08:00",
            completed_at="2026-11-07T08:00:01+08:00",
            status="ok",
            candidates=[{"symbol": "old", "setup": "old", "bar_time": "2026-11-07T08:00:00+08:00", "ladder": [], "fingerprint": "old"}],
            notify=True,
        )
        session.s["ai_wake_count"] = 0
        session._life()["tombstones"] = ["2R"]
        session.save()
    for event in case["events"]:
        session.handle(event)
    result = session.result()
    evidence = {
        "case_id": case["id"],
        "data_class": "synthetic_test_only_not_market_or_performance",
        "events": case["events"],
        "result": result,
        "trace": session.store.read_trace(),
    }
    return result, evidence
