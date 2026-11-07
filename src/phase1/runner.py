"""機械 runner。shadow 拒實盤；production 只跑固定的既有指令。

不讀金鑰、不 POST、不把五欄 sender 當正式通道。舊 AI 無條件保持。
"""
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ack_contract import cycle_key
from .ack_store import write_json_atomic
from .cycle import main as cycle_main
from .cycle import run_cycle
from .session import Store, refuse_foreign_state_dir

MARKER = "bululau-cycle-shadow\n"
HOURLY_SCAN_SECONDS = 3600
TPE = timezone(timedelta(hours=8))
WAKE_SCHEMA = 1
REFUSED_BASES = ("sender_setup.py",)
SHELL_CHARS = set("\n\r;&|$`<>")


def _plain(value):
    return isinstance(value, str) and bool(value) and not value.startswith("-") and not any(ch in value for ch in SHELL_CHARS)


def _refused_command(argv):
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part.strip() and not any(ch in part for ch in SHELL_CHARS) for part in argv):
        return "malformed_command"
    for part in argv:
        base = os.path.basename(part)
        if part == "--live" or base in ("live_trader.py",) or base in REFUSED_BASES or "sender_setup" in base:
            return "refused_unverified_live_command"
    return None


PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def mac_commands(python):
    """Mac 週期只跑公開掃描與只讀三查詢。不啟動 manage，舊 watch 維持原程式。"""
    if not _plain(python):
        raise ValueError("malformed_command")
    prefilter = PACKAGE_ROOT / "src" / "prefilter.py"
    if not prefilter.is_file() or prefilter.is_symlink():
        raise ValueError("quantboy_scripts_missing")
    return {
        "scanner": [python, str(prefilter), "--quiet", "--nolog"],
        "manage": None,
        "status": [python, "-m", "src.phase1.account_status"],
    }


def scanner_env(cycle_dir):
    """掃描日誌不得寫進舊的 ~/quantboy-live。"""
    from .legacy_root import legacy_home_path
    env = os.environ.copy()
    live = legacy_home_path()
    current = env.get("QUANTBOY_DATA_DIR")
    redirect = not current
    if current:
        real = os.path.realpath(os.path.expanduser(current))
        redirect = real == live or real.startswith(live + os.sep)
    if redirect:
        env["QUANTBOY_DATA_DIR"] = os.path.join(str(cycle_dir), "scanner-cache")
    return env


def production_argv(root, python):
    """只組既有三條指令。不接受任意 shell。"""
    if not _plain(python) or not _plain(root) or not os.path.isabs(root):
        raise ValueError("malformed_command")
    prefilter = os.path.join(root, "src", "prefilter.py")
    trader = os.path.join(root, "src", "live_trader.py")
    for path in (prefilter, trader):
        info = os.lstat(path) if os.path.lexists(path) else None
        if info is None or not info.st_mode or not os.path.isfile(path) or os.path.islink(path):
            raise ValueError("quantboy_scripts_missing")
    return {
        "scanner": [python, prefilter, "--quiet", "--nolog"],
        "manage": [python, trader, "manage", "--live"],
        "status": [python, trader, "status-json"],
    }


def _interval(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 86400:
        raise ValueError("malformed_config")
    return value


def _load_config(path):
    with open(path, encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError("malformed_config")
    mode = loaded.get("mode", "shadow")
    if mode not in ("shadow", "production"):
        raise ValueError("malformed_config")
    if mode == "production" and loaded.get("shadow") is True:
        raise ValueError("production_conflicts_with_shadow")
    if mode != "production" and loaded.get("shadow") is False:
        raise ValueError("shadow_required")
    timeout = loaded.get("timeout_seconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise ValueError("malformed_config")
    role = loaded.get("role", "package")
    interval = _interval(loaded.get("interval_seconds", HOURLY_SCAN_SECONDS))
    if role == "mac" and interval < HOURLY_SCAN_SECONDS:
        raise ValueError("scan_interval_too_fast")
    notifier = _notifier_config(loaded.get("notifier"))
    if role not in ("package", "mac"):
        raise ValueError("malformed_config")
    if role == "mac" and mode != "production":
        raise ValueError("mac_requires_production")
    if mode == "production":
        if any(name in loaded for name in ("scanner", "manage", "status")):
            raise ValueError("arbitrary_command")
        if role == "mac":
            root = loaded.get("quantboy_root")
            if root:
                from .legacy_root import check_legacy_install
                if not check_legacy_install(root)["path_is_legacy_home"]:
                    raise ValueError("unknown_path")
            commands = mac_commands(loaded.get("python") or "python3")
        else:
            from .legacy_root import check_legacy_install, production_block_reason
            blocked = production_block_reason(check_legacy_install(loaded.get("quantboy_root") or ""))
            if blocked:
                raise ValueError(blocked)
            commands = production_argv(loaded.get("quantboy_root"), loaded.get("python") or "python3")
    else:
        commands = {}
        for name in ("scanner", "manage", "status"):
            reason = _refused_command(loaded.get(name))
            if reason:
                raise ValueError(reason)
            commands[name] = loaded[name]
    return {
        "mode": mode,
        "commands": commands,
        "timeout_seconds": timeout,
        "interval_seconds": interval,
        "notifier": notifier,
        "quantboy_root": loaded.get("quantboy_root"),
        "role": role,
    }


def _notifier_config(value):
    if value is None:
        return None
    if isinstance(value, list):
        raise ValueError("notifier_unverified")
    if not isinstance(value, dict):
        raise ValueError("malformed_config")
    if value.get("verified") is not True:
        return {"verified": False, "argv": None}
    reason = _refused_command(value.get("argv"))
    if reason:
        raise ValueError(reason)
    return {"verified": True, "argv": value["argv"]}


def _spawn(argv, timeout, payload=None, env=None, cwd=None):
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=timeout, shell=False, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        return "", "", None, "timeout"
    except OSError:
        return "", "", None, "spawn_failed"
    return proc.stdout or "", proc.stderr or "", proc.returncode, None


def _object(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _scan_object(text):
    """掃描 JSON 拒絕 NaN 與 Infinity 字面值。溢位成無限的數字留給 quiet 價格契約擋下。"""
    if not isinstance(text, str) or not text.strip():
        return None

    def reject_non_finite(_constant):
        raise ValueError("non_finite")

    try:
        loaded = json.loads(text, parse_constant=reject_non_finite)
    except (json.JSONDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def adapt_prefilter(text, code, fault):
    """prefilter.py：0 無候選，10 是 call_ai，2 是行情失敗。10 不適用 manage/status。"""
    if fault:
        return {"error": fault, "candidates": []}
    body = _scan_object(text)
    if code == 2:
        if body is None:
            return {"error": "scan_failed", "candidates": []}
        cloned = dict(body)
        cloned["error"] = cloned.get("error") or "scan_failed"
        cloned["candidates"] = cloned.get("candidates") if isinstance(cloned.get("candidates"), list) else []
        return cloned
    if code == 10:
        if body is None or body.get("call_ai") is not True or not isinstance(body.get("candidates"), list):
            return {"error": "prefilter_exit_contract", "candidates": []}
        return body
    if code == 0:
        if body is None or body.get("call_ai") is not False or not isinstance(body.get("candidates"), list) or body.get("error"):
            return {"error": "prefilter_exit_contract", "candidates": []}
        return body
    return {"error": "unexpected_exit_{0}".format(code), "candidates": []}


def parse_status_text(text):
    """對應 live_trader.py cmd_status 的文字，不讀餘額。"""
    positions = as_of = None
    general = algo = None
    for raw in text.splitlines():
        line = raw.strip()
        stamp = re.fullmatch(r"時間 (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) 台北", line)
        if stamp:
            parsed = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=TPE)
            as_of = parsed.isoformat(timespec="minutes")
        held = re.fullmatch(r"持倉 (\d+)/(\d+)", line)
        if held:
            positions = int(held.group(1))
        booked = re.fullmatch(r"一般掛單 (\d+)\s+條件單 (\d+)", line)
        if booked:
            general, algo = int(booked.group(1)), int(booked.group(2))
    if positions is None or general is None or algo is None or as_of is None:
        return None
    return {"positions": positions, "orders": general + algo, "as_of": as_of}


def adapt_status_json(text, stderr, code, fault, manage_code, manage_fault):
    """production 只信 status-json。人類可讀 status 的零倉零單不能證明查詢成功。"""
    from .status_contract import parse_status_json
    parsed = None if fault else parse_status_json(text, code, stderr or "")
    manage_failed = manage_fault is not None or manage_code != 0
    if not parsed or not parsed["trusted"]:
        return {
            "status": "unknown", "as_of": None, "positions": None, "orders": None,
            "manage_error": True, "manage_complete": False, "limit_older_than_2h": False,
            "status_trusted": False,
            "queries_ok": False,
        }
    return {
        "status": "ok",
        "as_of": parsed["as_of"],
        "positions": parsed["positions"],
        "open_orders": parsed["open_orders"],
        "algo_orders": parsed["algo_orders"],
        "orders": parsed["orders"],
        "queries": parsed["queries"],
        "manage_error": manage_failed,
        "manage_complete": not manage_failed,
        "limit_older_than_2h": False,
        "status_trusted": True,
        "queries_ok": True,
    }


def _shadow_outputs(text, code, fault):
    if fault:
        return "", fault
    if code != 0:
        return text, "exit_{0}".format(code)
    return text, None


def wake_contract(event, sent_at):
    evaluation_id = event.get("evaluation_id")
    event_id = event.get("event_id")
    cycle_key(evaluation_id)
    cycle_key(event_id)
    return {
        "schema_version": WAKE_SCHEMA,
        "event_type": "evaluation_wake",
        "event_id": event_id,
        "evaluation_id": evaluation_id,
        "sent_at": sent_at,
        "reason": event.get("reason"),
        "test_channel": False,
        "position_channel": False,
    }


class CommandNotifier:
    """外部傳輸只證明 delivered。沒有 verified 就 blocked。不 POST。"""

    def __init__(self, spec, timeout, sent_at, publisher=None):
        self.spec = spec
        self.timeout = timeout
        self.sent_at = sent_at
        self.publisher = publisher

    def notify(self, event):
        blocked = {"status": "blocked", "delivered": False, "http_sent": False, "processing_ack": False, "reason": "notifier_unverified"}
        if not self.spec or self.spec.get("verified") is not True:
            return blocked
        if self.publisher:
            try:
                self.publisher(event)
            except ValueError:
                return {"status": "blocked", "delivered": False, "http_sent": False, "processing_ack": False, "reason": "sender_failed"}
        try:
            payload = wake_contract(event, self.sent_at)
        except ValueError:
            return {"status": "blocked", "delivered": False, "http_sent": False, "processing_ack": False, "reason": "rejected_id"}
        text, _stderr, code, fault = _spawn(self.spec["argv"], self.timeout, json.dumps(payload, ensure_ascii=False))
        body = _object(text)
        if fault or code != 0 or body is None or body.get("schema_version") != WAKE_SCHEMA or body.get("delivered") is not True:
            return {"status": "blocked", "delivered": False, "http_sent": False, "processing_ack": False, "reason": "sender_failed"}
        return {"status": "ok", "delivered": True, "http_sent": False, "processing_ack": False, "reason": "delivered"}


def _unknown_snapshot():
    return {
        "status": "unknown", "as_of": None, "positions": None, "orders": None,
        "manage_error": True, "manage_complete": False, "limit_older_than_2h": False,
    }


def run_configured(store, config, *, now, scan_id):
    timeout = config["timeout_seconds"]
    commands = config["commands"]
    if config["mode"] == "production":
        scan_env = scanner_env(store.root) if config.get("role") == "mac" else None
        scan_cwd = str(PACKAGE_ROOT) if config.get("role") == "mac" else None
        scan_text, _scan_err, scan_code, scan_fault = _spawn(commands["scanner"], timeout, env=scan_env, cwd=scan_cwd)
        if commands.get("manage") is None:
            manage_code, manage_fault = 0, None
            manage_ran = False
        else:
            _manage_text, _manage_err, manage_code, manage_fault = _spawn(commands["manage"], timeout)
            manage_ran = True
        status_text, status_err, status_code, status_fault = _spawn(commands["status"], timeout, cwd=scan_cwd)
        prefilter = adapt_prefilter(scan_text, scan_code, scan_fault)
        snapshot = adapt_status_json(status_text, status_err, status_code, status_fault, manage_code, manage_fault)
        manage_exit = 0 if manage_fault is None and manage_code == 0 else 1
        command_errors = {
            "scanner": None if scan_fault is None and scan_code in (0, 2, 10) else scan_fault or "unexpected_exit_{0}".format(scan_code),
            "manage": manage_fault or (None if manage_code == 0 else "exit_{0}".format(manage_code)),
            "status": status_fault or (None if status_code == 0 else "exit_{0}".format(status_code)),
        }
        executed = config.get("role") != "mac"
        mode = "production"
    else:
        manage_ran = True
        scan_text, _scan_err, scan_code, scan_fault = _spawn(commands["scanner"], timeout)
        manage_text, _manage_err, manage_code, manage_fault = _spawn(commands["manage"], timeout)
        status_text, _status_err, status_code, status_fault = _spawn(commands["status"], timeout)
        del manage_text
        scan_text, scan_error = _shadow_outputs(scan_text, scan_code, scan_fault)
        status_text, status_error = _shadow_outputs(status_text, status_code, status_fault)
        manage_error = manage_fault or (None if manage_code == 0 else "exit_{0}".format(manage_code))
        prefilter = {"error": scan_error, "candidates": []} if scan_error else (_object(scan_text) or {"error": "malformed", "candidates": []})
        body = _object(status_text)
        positions = None if body is None else _count(body.get("positions"))
        orders = None if body is None else _count(body.get("orders"))
        if status_error or body is None or positions is None or orders is None or body.get("status") not in ("ok", "unknown") or "as_of" not in body:
            snapshot = _unknown_snapshot()
        else:
            failed = bool(manage_error or body.get("manage_error"))
            snapshot = {
                "status": body.get("status"), "as_of": body.get("as_of"), "positions": positions, "orders": orders,
                "manage_error": failed, "manage_complete": not failed, "limit_older_than_2h": body.get("limit_older_than_2h") is True,
            }
        if manage_error or status_error:
            snapshot["manage_error"] = True
            snapshot["manage_complete"] = False
        manage_exit = 0 if manage_error is None and status_error is None else 1
        command_errors = {"scanner": scan_error, "manage": manage_error, "status": status_error}
        executed = False
        mode = "shadow"
    def _publisher(event):
        from .autoscan import freshness as freshness_of
        from .production_transport import bind_envelope, build_envelope
        fresh = freshness_of(snapshot.get("as_of"), now) if snapshot.get("as_of") else {"valid": False, "status": "missing", "age_seconds": None}
        envelope = build_envelope(
            snapshot, prefilter, fresh, event_id=event.get("event_id"), evaluation_id=event.get("evaluation_id"),
            sent_at=now, reason=event.get("reason"),
        )
        bind_envelope(store, envelope)

    notifier = CommandNotifier(config.get("notifier"), timeout, now, publisher=_publisher) if config.get("notifier") else None
    decision = run_cycle(
        store, snapshot, prefilter, now=now, scan_id=scan_id, manage_exit=manage_exit, notifier=notifier,
        mac_gate=config.get("role") == "mac",
    )
    decision["mode"] = mode
    decision["ready"] = False
    decision["switch_allowed"] = False
    decision["executed_live"] = executed
    decision["account_verified"] = False
    decision["routine_stopped"] = False
    decision["formal_channel"] = False
    decision["old_ai_poll"] = "keep"
    decision["delivered"] = bool(decision.get("sender", {}).get("delivered"))
    decision["processing_ack"] = False
    decision["limit_older_than_2h_implemented"] = False
    decision["command_errors"] = command_errors
    decision["manage_ran"] = manage_ran
    decision["watch_unchanged"] = config.get("role") == "mac"
    decision["live_connected"] = False
    decision["post_authorized"] = False
    decision["status_from_state_snapshot"] = False
    decision["scan_interval_seconds"] = config.get("interval_seconds")
    decision["opportunity_scan"] = "hourly"
    decision["position_ai_event_path"] = "not_implemented"
    decision["hard_watch_seconds"] = 60
    decision["hard_watch_modified"] = False
    decision["protection_ai_skipped"] = decision.get("reason") == "quiet_flat_book"
    decision["new_entry_ai"] = bool(decision.get("candidate_fingerprints"))
    decision["unknown_treated_as_flat"] = False
    if decision.get("event_id") and decision.get("evaluation_id"):
        from .autoscan import freshness as freshness_of
        from .production_transport import bind_envelope, build_envelope
        fresh = freshness_of(snapshot.get("as_of"), now) if snapshot.get("as_of") else {"valid": False, "status": "missing", "age_seconds": None}
        try:
            envelope = build_envelope(
                snapshot, prefilter, fresh, event_id=decision["event_id"], evaluation_id=decision["evaluation_id"],
                sent_at=now, reason=decision["reason"],
            )
            bind_envelope(store, envelope)
        except ValueError:
            pass
    write_json_atomic(str(store.root), str(Path(store.root) / "last_decision.json"), {
        key: decision.get(key) for key in (
            "mode", "ready", "switch_allowed", "executed_live", "account_verified", "routine_stopped",
            "formal_channel", "old_ai_poll", "ai_suggestion", "cycle_needs_ai", "reason",
            "protection_complete", "scan_blocked", "evaluation_id", "event_id", "delivered",
            "processing_ack", "command_errors", "manage_ran", "watch_unchanged",
            "live_connected", "post_authorized", "status_from_state_snapshot",
            "scan_interval_seconds", "opportunity_scan", "position_ai_event_path",
            "hard_watch_seconds", "hard_watch_modified", "protection_ai_skipped",
            "new_entry_ai", "unknown_treated_as_flat",
        )
    })
    return decision


def install_schedule(dest, state_dir, repo, python, config_path=None):
    if _unsafe_schedule_dest(dest):
        raise ValueError("refused_schedule_dest")
    refuse_foreign_state_dir(state_dir)
    interval = HOURLY_SCAN_SECONDS
    command = [python, "-m", "src.phase1.runner", "run", "--state-dir", state_dir, "--config", config_path or os.path.join(state_dir, "runner.json"), "--shadow"]
    if config_path:
        loaded = _load_config(config_path)
        if loaded.get("role") == "mac":
            raise ValueError("mac_schedule_not_installed")
        interval = loaded["interval_seconds"]
        if loaded["mode"] == "production":
            command = command[:-1]
    os.makedirs(dest, mode=0o700, exist_ok=True)
    marker = os.path.join(dest, "bululau-cycle-shadow.marker")
    if os.path.lexists(marker):
        with open(marker, encoding="utf-8") as handle:
            if handle.read() != MARKER:
                raise ValueError("refused_schedule_dest")
    else:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(MARKER)
    plist = os.path.join(dest, "bululau-cycle-shadow.plist")
    args = "".join("<string>{0}</string>".format(_xml(part)) for part in command)
    with open(plist, "w", encoding="utf-8") as handle:
        handle.write(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<plist version=\"1.0\"><dict>\n"
            "<key>Label</key><string>com.bululau.cycle-shadow</string>\n"
            "<key>Disabled</key><true/>\n"
            "<key>RunAtLoad</key><false/>\n"
            "<key>StartInterval</key><integer>{0}</integer>\n"
            "<key>WorkingDirectory</key><string>{1}</string>\n"
            "<key>ProgramArguments</key><array>{2}</array>\n"
            "</dict></plist>\n".format(interval, _xml(repo), args)
        )
    with open(os.path.join(dest, "bululau-cycle-shadow.cron"), "w", encoding="utf-8") as handle:
        handle.write("# Disabled。父側 load 之前不會跑，也不取代舊 AI。週期 {0} 秒。\n# ".format(interval) + " ".join(command) + "\n")
    with open(os.path.join(dest, "SHADOW.txt"), "w", encoding="utf-8") as handle:
        handle.write("plist 是 Disabled。沒有 launchctl load，也不會停舊 routine。\n回退：python3 -m src.phase1.runner rollback-schedule --dest 這個目錄\n")
    return {"installed": False, "disabled": True, "interval_seconds": interval, "runnable": True, "dest": dest, "routine_stopped": False, "switch_allowed": False}


def rollback_shadow_schedule(dest):
    marker = os.path.join(dest, "bululau-cycle-shadow.marker")
    if not os.path.isfile(marker):
        raise ValueError("refused_schedule_dest")
    with open(marker, encoding="utf-8") as handle:
        if handle.read() != MARKER:
            raise ValueError("refused_schedule_dest")
    for name in ("bululau-cycle-shadow.marker", "bululau-cycle-shadow.plist", "bululau-cycle-shadow.cron", "SHADOW.txt"):
        path = os.path.join(dest, name)
        if os.path.isfile(path) and not os.path.islink(path):
            os.remove(path)
    try:
        os.rmdir(dest)
    except OSError:
        pass
    return {"removed": True, "routine_stopped": False}


SWITCH_BLOCKERS = (
    "real_status_json_three_queries_on_account",
    "manage_live_observed_without_unexpected_orders",
    "dry_run_one_id_at_most_two_posts_delivered_not_processed",
    "judgment_digest_then_restart_duplicate",
    "watch_schedule_unchanged",
    "discretion_remains_original_ai",
    "parent_loaded_schedule",
    "parent_signoff",
)


KNOWN_LIVE_FLAT_SHA = "65f1d4526cd5d3b6c2115ca8eeebd102281b27a2fbd40d0ee07054ff4891459b"
LIVE_LAYOUT_FILES = (
    "state.json", ".lock", "events.json", "HALT_REPORT_USER.flag",
    "data/state.json", ".env", "config.py", "src/config.py", "COMMIT",
)


def _file_stat(path):
    """只 stat。不讀內容。被拒時保留 errno。"""
    try:
        info = os.lstat(path)
    except OSError as exc:
        return {"present": False, "errno": exc.errno, "error": exc.strerror}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return {"present": False, "errno": None, "error": "not_regular_file"}
    return {"present": True, "errno": None, "error": None, "mode": oct(info.st_mode & 0o777)}


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        chunk = handle.read(65536)
        while chunk:
            digest.update(chunk)
            chunk = handle.read(65536)
    return digest.hexdigest()


def inspect_quantboy_root(root):
    """只讀檔名與原始碼。不讀 .env、不讀 state.json、不執行查詢、不建立 data/。"""
    flat = os.path.join(root, "live_trader.py")
    packaged = os.path.join(root, "src", "live_trader.py")
    flat_ok = os.path.isfile(flat) and not os.path.islink(flat)
    src_ok = os.path.isfile(packaged) and not os.path.islink(packaged)
    if flat_ok and src_ok:
        layout = "mixed"
        trader = None
    elif flat_ok:
        layout = "flat"
        trader = flat
    elif src_ok:
        layout = "src"
        trader = packaged
    else:
        layout = None
        trader = None
    text = ""
    digest = None
    if trader:
        with open(trader, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        digest = _file_sha256(trader)
    revision = "status-json-2" if 'STATUS_JSON_REVISION = "status-json-2"' in text and "def cmd_status_json" in text else None
    present = {}
    for name in LIVE_LAYOUT_FILES:
        path = os.path.join(root, name)
        present[name] = os.path.lexists(path) and not os.path.isdir(path)
    env_stat = _file_stat(os.path.join(root, ".env"))
    foreign = any(os.path.lexists(os.path.join(root, name)) for name in ("state.json", ".lock", "HALT_REPORT_USER.flag"))
    from .legacy_root import check_legacy_install
    legacy = check_legacy_install(root)
    return {
        "quantboy_root": root,
        "layout": layout,
        "live_trader_sha256": digest,
        "known_live_flat_sha": digest == KNOWN_LIVE_FLAT_SHA,
        "prefilter_present": os.path.isfile(os.path.join(root, "src", "prefilter.py")) and not os.path.islink(os.path.join(root, "src", "prefilter.py")),
        "common_present": os.path.isfile(os.path.join(root, "src", "common.py")) and not os.path.islink(os.path.join(root, "src", "common.py")),
        "status_json_revision": revision,
        "root_supports_status_json": layout == "src" and revision == "status-json-2",
        "files_present": present,
        "env_file_present": env_stat["present"],
        "env_file_mode": env_stat.get("mode"),
        "env_file_stat_errno": env_stat.get("errno"),
        "env_file_stat_error": env_stat.get("error"),
        "env_file_copied": False,
        "data_dir_env_set_in_this_process": "QUANTBOY_DATA_DIR" in os.environ,
        "events_file_env_set_in_this_process": "QUANTBOY_EVENTS_FILE" in os.environ,
        "package_default_data_dir": "data",
        "package_default_matches_live_root": False,
        "same_lock_proven": bool(legacy["known_sha_match"] and legacy["bindings_match"] and legacy["lock_protocol_match"]),
        "lock_protocol_match": legacy["lock_protocol_match"],
        "bindings_match": legacy["bindings_match"],
        "state_schema_proven": False,
        "manage_allowed": False,
        "share_live_state": False,
        "cycle_dir_must_be_separate": True,
        "foreign_state_in_root": foreign,
        "deploy_complete": False,
        "spawned": False,
        "account_verified": False,
        "switch_allowed": False,
        "dual_write": False,
    }


def check_config(config_path, state_dir=None):
    """只讀。不建立狀態、不啟動程序、不 load 排程。"""
    loaded = _load_config(config_path)
    if state_dir and os.path.isdir(state_dir):
        refuse_foreign_state_dir(state_dir)
    return {
        "mode": loaded["mode"],
        "interval_seconds": loaded["interval_seconds"],
        "commands": {name: loaded["commands"][name] for name in ("scanner", "manage", "status")},
        "notifier_verified": bool(loaded["notifier"] and loaded["notifier"].get("verified") is True),
        "switch_allowed": False,
        "switch_blockers": list(SWITCH_BLOCKERS),
        "account_verified": False,
        "routine_stopped": False,
        "spawned": False,
        "quantboy_root": loaded.get("quantboy_root"),
        "role": loaded.get("role"),
        "scan_interval_seconds": loaded["interval_seconds"],
        "opportunity_scan": "hourly",
        "position_ai_event_path": "not_implemented",
        "hard_watch_seconds": 60,
        "hard_watch_modified": False,
        "live_connected": False,
        "post_authorized": False,
    }


def _unsafe_schedule_dest(dest):
    real = os.path.realpath(dest)
    parts = set(Path(real).parts)
    if "LaunchAgents" in parts or "LaunchDaemons" in parts:
        return True
    etc = os.path.realpath("/etc")
    if real == etc or real.startswith(etc + os.sep):
        return True
    return False


def _xml(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scan_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "c{0}{1}".format(stamp, secrets.token_hex(4))


def _emit(decision, code):
    shown = {
        key: decision.get(key) for key in (
            "mode", "ready", "switch_allowed", "executed_live", "account_verified", "routine_stopped",
            "formal_channel", "old_ai_poll", "ai_suggestion", "cycle_needs_ai", "reason",
            "protection_complete", "scan_blocked", "evaluation_id", "event_id", "delivered",
            "processing_ack", "registered", "command_errors", "spawned", "notifier_verified",
            "interval_seconds", "runnable", "manage_ran", "watch_unchanged",
            "live_connected", "post_authorized", "status_from_state_snapshot",
            "scan_interval_seconds", "opportunity_scan", "position_ai_event_path",
            "hard_watch_seconds", "hard_watch_modified", "protection_ai_skipped",
            "new_entry_ai", "unknown_treated_as_flat",
        )
    }
    print(json.dumps(shown, ensure_ascii=False))
    return code


def main(argv):
    if argv[:1] == ["apply"]:
        return cycle_main(argv)
    if argv[:1] == ["install-schedule"]:
        return _install_main(argv[1:])
    if argv[:1] == ["rollback-schedule"]:
        return _rollback_main(argv[1:])
    if argv[:1] == ["check"]:
        return _check_main(argv[1:])
    if argv[:1] == ["check-root"]:
        return _check_root_main(argv[1:])
    if argv[:1] == ["check-install"]:
        return _check_install_main(argv[1:])
    if argv[:1] == ["deploy-check"]:
        return _deploy_check_main(argv[1:])
    if argv[:1] == ["review-round"]:
        return _review_round_main(argv[1:])
    if argv[:1] != ["run"]:
        print("用法: python3 -m src.phase1.runner run --state-dir DIR --config FILE [--now TIME --scan-id ID] [--shadow]")
        return 2
    return _run_main(argv[1:])


def _parse(argv):
    found = {}
    index = 0
    while index < len(argv):
        key = argv[index]
        if key == "--shadow":
            found["shadow"] = True
            index += 1
            continue
        if key not in ("--state-dir", "--config", "--now", "--scan-id", "--dest", "--repo", "--python", "--quantboy-root", "--snapshot", "--prefilter", "--entry-config") or index + 1 >= len(argv):
            return None
        found[key[2:].replace("-", "_")] = argv[index + 1]
        index += 2
    return found


def _run_main(argv):
    found = _parse(argv)
    if not found or "state_dir" not in found or "config" not in found:
        print("用法: python3 -m src.phase1.runner run --state-dir DIR --config FILE [--now TIME --scan-id ID] [--shadow]")
        return 2
    now = found.get("now") or _utc_now()
    scan_id = found.get("scan_id") or _scan_id()
    try:
        cycle_key(scan_id)
        refuse_foreign_state_dir(found["state_dir"])
        config = _load_config(found["config"])
        if found.get("shadow") and config["mode"] == "production":
            raise ValueError("shadow_refuses_production")
        store = Store(found["state_dir"])
        decision = run_configured(store, config, now=now, scan_id=scan_id)
    except ValueError as exc:
        return _emit({
            "mode": "shadow" if found.get("shadow") else "unknown",
            "ready": False, "switch_allowed": False, "executed_live": False, "account_verified": False,
            "routine_stopped": False, "formal_channel": False, "old_ai_poll": "keep",
            "ai_suggestion": "keep", "cycle_needs_ai": True, "reason": str(exc), "protection_complete": False,
            "processing_ack": False,
        }, 1)
    return _emit(decision, 0)


def _install_main(argv):
    found = _parse(argv)
    if not found or any(key not in found for key in ("dest", "state_dir", "repo", "python")):
        print("用法: python3 -m src.phase1.runner install-schedule --dest DIR --state-dir DIR --repo DIR --python PYTHON [--config FILE]")
        return 2
    try:
        result = install_schedule(found["dest"], found["state_dir"], found["repo"], found["python"], found.get("config"))
    except ValueError as exc:
        print(json.dumps({"switch_allowed": False, "reason": str(exc), "routine_stopped": False}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _rollback_main(argv):
    found = _parse(argv)
    if not found or "dest" not in found or len(found) != 1:
        print("用法: python3 -m src.phase1.runner rollback-schedule --dest DIR")
        return 2
    try:
        result = rollback_shadow_schedule(found["dest"])
    except ValueError as exc:
        print(json.dumps({"reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def deploy_report(config_path):
    """不啟動程序。說明這份設定會不會變成第二條排程或自行開倉。"""
    loaded = _load_config(config_path)
    return {
        "role": loaded.get("role"),
        "scan_interval_seconds": loaded["interval_seconds"],
        "reverts_to_15m": False,
        "install_schedule_for_mac": False,
        "hard_watch_seconds": 60,
        "hard_watch_modified": False,
        "this_package_spawns_manage": loaded["commands"].get("manage") is not None,
        "old_ai_poll": "keep",
        "unknown_keeps_original_ai": True,
        "context_positions": "count_only",
        "context_has_klines": False,
        "context_review": "whitelist_or_keep_original_ai",
        "counts_are_sufficient": False,
        "trading_discretion_complete": False,
        "judgment_opens_position": False,
        "allowed_decisions": ["keep_original_ai", "no_trade"],
        "unattended_transport": False,
        "switch_allowed": False,
        "live_connected": False,
        "post_authorized": False,
    }


def _now_ms(text):
    from .timeparse import parse_instant
    moment = parse_instant(text)
    if moment.tzinfo is None:
        raise ValueError("clock_invalid")
    return int(moment.timestamp() * 1000)


def _review_round_main(argv):
    """預設不取數、不外發、不改現有 routine。時鐘來自 --now，quiet 在 notifier 之前。"""
    from .market_adapter import disabled_entry, load_entry_config, run_review_entry
    from .session import Store
    from .token_switch import UnconfiguredWebhook
    found = _parse(argv)
    if not found or "state_dir" not in found or "now" not in found or "scan_id" not in found or "snapshot" not in found:
        print(json.dumps({
            "external_send": False, "routine_stopped": False, "wakes_saved": False,
            "reason": "review_round_disabled", "live_fetch": False,
        }, ensure_ascii=False))
        return 2
    try:
        now_ms = _now_ms(found["now"])
    except (TypeError, ValueError):
        print(json.dumps({
            "quiet": False, "external_send": False, "routine_stopped": False, "wakes_saved": False,
            "reason": "clock_invalid", "live_fetch": False, "now_ms": None,
        }, ensure_ascii=False))
        return 1
    with open(found["snapshot"], encoding="utf-8") as handle:
        snapshot = json.load(handle)
    prefilter_path = found.get("prefilter")
    if prefilter_path:
        with open(prefilter_path, encoding="utf-8") as handle:
            prefilter = json.load(handle)
    else:
        prefilter = {}
    entry_path = found.get("entry_config")
    if entry_path:
        with open(entry_path, encoding="utf-8") as handle:
            entry = load_entry_config(json.load(handle))
    else:
        entry = disabled_entry()
    store = Store(found["state_dir"])
    result = run_review_entry(
        store, snapshot, prefilter, now=found["now"], scan_id=found["scan_id"], now_ms=now_ms,
        entry=entry, notifier=UnconfiguredWebhook(),
    )
    print(json.dumps({key: result.get(key) for key in (
        "quiet", "wake_sent", "external_send", "routine_stopped", "watch_modified", "wakes_saved",
        "old_ai_poll", "context_sufficient", "discretion_complete", "fetch_review_called", "reason",
        "live_fetch", "now_ms", "entry_enabled", "routine_replaced", "replaces_scan_entry",
    )}, ensure_ascii=False))
    return 0


def _deploy_check_main(argv):
    found = _parse(argv)
    if not found or "config" not in found:
        print("用法: python3 -m src.phase1.runner deploy-check --config FILE")
        return 2
    try:
        report = deploy_report(found["config"])
    except ValueError as exc:
        print(json.dumps({"switch_allowed": False, "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _check_install_main(argv):
    found = _parse(argv)
    if not found or "quantboy_root" not in found:
        print("用法: python3 -m src.phase1.runner check-install --quantboy-root DIR")
        return 2
    from .legacy_root import check_legacy_install
    report = check_legacy_install(found["quantboy_root"])
    print(json.dumps(report, ensure_ascii=False))
    return 1


def _check_root_main(argv):
    found = _parse(argv)
    if not found or "quantboy_root" not in found:
        print("用法: python3 -m src.phase1.runner check-root --quantboy-root DIR")
        return 2
    report = inspect_quantboy_root(found["quantboy_root"])
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["root_supports_status_json"] else 1


def _check_main(argv):
    found = _parse(argv)
    if not found or "config" not in found:
        print("用法: python3 -m src.phase1.runner check --config FILE [--state-dir DIR]")
        return 2
    try:
        result = check_config(found["config"], found.get("state_dir"))
    except ValueError as exc:
        print(json.dumps({"switch_allowed": False, "account_verified": False, "spawned": False, "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
