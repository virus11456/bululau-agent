"""可執行的 shadow runner。不讀金鑰、不 POST、不執行 live_trader --live。

正式喚醒未配置時維持 blocked，舊 AI 維持 keep。五欄測試 sender 不是這條通道。
"""
import json
import os
import subprocess
from pathlib import Path

from .ack_store import write_json_atomic
from .cycle import main as cycle_main
from .cycle import run_cycle
from .session import Store, refuse_foreign_state_dir

MARKER = "bululau-cycle-shadow\n"
REFUSED_BASES = ("live_trader.py", "sender_setup.py")


def _refused_command(argv):
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part.strip() for part in argv):
        return "malformed_command"
    for part in argv:
        base = os.path.basename(part)
        if part == "--live" or base in REFUSED_BASES or "sender_setup" in base:
            return "refused_unverified_live_command"
    return None


def _load_config(path):
    with open(path, encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict) or loaded.get("shadow") is not True:
        raise ValueError("shadow_required")
    for name in ("scanner", "manage", "status"):
        reason = _refused_command(loaded.get(name))
        if reason:
            raise ValueError(reason)
    notifier = loaded.get("notifier")
    if notifier is not None:
        reason = _refused_command(notifier)
        if reason:
            raise ValueError(reason)
    timeout = loaded.get("timeout_seconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise ValueError("malformed_config")
    return loaded


def _spawn(argv, timeout, payload=None):
    try:
        proc = subprocess.run(
            argv, input=payload, capture_output=True, text=True, timeout=timeout, shell=False,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "spawn_failed"
    if proc.returncode != 0:
        return proc.stdout, "exit_{0}".format(proc.returncode)
    return proc.stdout, None


def _object(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class CommandNotifier:
    """操作者注入的本機程式。這支 runner 自己不組 HTTP header，也不 POST。"""

    def __init__(self, argv, timeout):
        self.argv = argv
        self.timeout = timeout

    def notify(self, event):
        text, error = _spawn(self.argv, self.timeout, json.dumps(event, ensure_ascii=False))
        if error or _object(text) is None:
            return {"status": "blocked", "delivered": False, "http_sent": False, "processing_ack": False, "reason": "sender_failed"}
        return {"status": "blocked", "delivered": False, "http_sent": False, "processing_ack": False, "reason": "local_notifier_not_formal_channel"}


def _snapshot(status_text, manage_error):
    body = _object(status_text)
    positions = None if body is None else _count(body.get("positions"))
    orders = None if body is None else _count(body.get("orders"))
    if body is None or positions is None or orders is None or body.get("status") not in ("ok", "unknown") or "as_of" not in body:
        return {
            "status": "unknown", "as_of": None, "positions": None, "orders": None,
            "manage_error": True, "manage_complete": False, "limit_older_than_2h": False,
        }
    failed = bool(manage_error or body.get("manage_error"))
    return {
        "status": body.get("status"),
        "as_of": body.get("as_of"),
        "positions": positions,
        "orders": orders,
        "manage_error": failed,
        "manage_complete": not failed,
        "limit_older_than_2h": body.get("limit_older_than_2h") is True,
    }


def _prefilter(text, error):
    if error:
        return {"error": error, "candidates": []}
    body = _object(text)
    if body is None:
        return {"error": "malformed", "candidates": []}
    return body


def run_shadow(store, config, *, now, scan_id):
    timeout = config.get("timeout_seconds", 20)
    scan_text, scan_error = _spawn(config["scanner"], timeout)
    manage_text, manage_error = _spawn(config["manage"], timeout)
    status_text, status_error = _spawn(config["status"], timeout)
    del manage_text
    manage_exit = 0 if manage_error is None and status_error is None else 1
    snapshot = _snapshot(status_text, manage_error or status_error)
    if manage_error or status_error:
        snapshot["manage_error"] = True
        snapshot["manage_complete"] = False
    prefilter = _prefilter(scan_text, scan_error)
    notifier = CommandNotifier(config["notifier"], timeout) if config.get("notifier") else None
    decision = run_cycle(
        store, snapshot, prefilter, now=now, scan_id=scan_id, manage_exit=manage_exit, notifier=notifier,
    )
    decision["mode"] = "shadow"
    decision["ready"] = False
    decision["executed_live"] = False
    decision["routine_stopped"] = False
    decision["formal_channel"] = False
    decision["old_ai_poll"] = "keep"
    decision["command_errors"] = {"scanner": scan_error, "manage": manage_error, "status": status_error}
    write_json_atomic(str(store.root), str(Path(store.root) / "last_decision.json"), {
        key: decision.get(key) for key in (
            "mode", "ready", "executed_live", "routine_stopped", "formal_channel", "old_ai_poll",
            "ai_suggestion", "cycle_needs_ai", "reason", "protection_complete", "scan_blocked",
            "evaluation_id", "event_id", "command_errors",
        )
    })
    return decision


def install_shadow_schedule(dest, state_dir, repo, python):
    if _unsafe_schedule_dest(dest):
        raise ValueError("refused_schedule_dest")
    refuse_foreign_state_dir(state_dir)
    os.makedirs(dest, mode=0o700, exist_ok=True)
    marker = os.path.join(dest, "bululau-cycle-shadow.marker")
    if os.path.lexists(marker):
        with open(marker, encoding="utf-8") as handle:
            if handle.read() != MARKER:
                raise ValueError("refused_schedule_dest")
    else:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(MARKER)
    command = [
        python, "-m", "src.phase1.runner", "run", "--state-dir", state_dir,
        "--config", os.path.join(state_dir, "runner.json"), "--shadow",
    ]
    plist = os.path.join(dest, "bululau-cycle-shadow.plist")
    args = "".join("<string>{0}</string>".format(_xml(part)) for part in command)
    with open(plist, "w", encoding="utf-8") as handle:
        handle.write(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<plist version=\"1.0\"><dict>\n"
            "<key>Label</key><string>com.bululau.cycle-shadow</string>\n"
            "<key>Disabled</key><true/>\n"
            "<key>RunAtLoad</key><false/>\n"
            "<key>WorkingDirectory</key><string>{0}</string>\n"
            "<key>ProgramArguments</key><array>{1}</array>\n"
            "</dict></plist>\n".format(_xml(repo), args)
        )
    cron = os.path.join(dest, "bululau-cycle-shadow.cron")
    with open(cron, "w", encoding="utf-8") as handle:
        handle.write("# 預設不安裝、不取代舊 AI。\n# " + " ".join(command) + "\n")
    note = os.path.join(dest, "SHADOW.txt")
    with open(note, "w", encoding="utf-8") as handle:
        handle.write("這份排程是 Disabled。沒有 launchctl load，也不會停舊 routine。\n回退：python3 -m src.phase1.runner rollback-schedule --dest 這個目錄\n")
    return {"installed": False, "disabled": True, "dest": dest, "routine_stopped": False, "ready": False}


def rollback_shadow_schedule(dest):
    marker = os.path.join(dest, "bululau-cycle-shadow.marker")
    if not os.path.isfile(marker):
        raise ValueError("refused_schedule_dest")
    with open(marker, encoding="utf-8") as handle:
        if handle.read() != MARKER:
            raise ValueError("refused_schedule_dest")
    for name in (
        "bululau-cycle-shadow.marker",
        "bululau-cycle-shadow.plist",
        "bululau-cycle-shadow.cron",
        "SHADOW.txt",
    ):
        path = os.path.join(dest, name)
        if os.path.isfile(path) and not os.path.islink(path):
            os.remove(path)
    try:
        os.rmdir(dest)
    except OSError:
        pass
    return {"removed": True, "routine_stopped": False}


def _unsafe_schedule_dest(dest):
    real = os.path.realpath(dest)
    parts = set(Path(real).parts)
    if "LaunchAgents" in parts or "LaunchDaemons" in parts:
        return True
    if real == os.path.realpath("/etc") or real.startswith(os.path.realpath("/etc") + os.sep):
        return True
    return False


def _xml(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _emit(decision, code):
    shown = {
        key: decision.get(key) for key in (
            "mode", "ready", "executed_live", "routine_stopped", "formal_channel", "old_ai_poll",
            "ai_suggestion", "cycle_needs_ai", "reason", "protection_complete", "scan_blocked",
            "evaluation_id", "event_id", "registered", "command_errors",
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
    if argv[:1] != ["run"]:
        print("用法: python3 -m src.phase1.runner run --state-dir DIR --config FILE --now TIME --scan-id ID --shadow")
        return 2
    return _run_main(argv[1:])


def _run_main(argv):
    if "--shadow" not in argv or argv[::2] != ["--state-dir", "--config", "--now", "--scan-id", "--shadow"]:
        print("用法: python3 -m src.phase1.runner run --state-dir DIR --config FILE --now TIME --scan-id ID --shadow")
        return 2
    state_dir, config_path, now, scan_id = argv[1], argv[3], argv[5], argv[7]
    try:
        refuse_foreign_state_dir(state_dir)
        config = _load_config(config_path)
        store = Store(state_dir)
        decision = run_shadow(store, config, now=now, scan_id=scan_id)
    except ValueError as exc:
        return _emit({
            "mode": "shadow", "ready": False, "executed_live": False, "routine_stopped": False,
            "formal_channel": False, "old_ai_poll": "keep", "ai_suggestion": "keep",
            "cycle_needs_ai": True, "reason": str(exc), "protection_complete": False,
        }, 1)
    return _emit(decision, 0)


def _install_main(argv):
    if argv[::2] != ["--dest", "--state-dir", "--repo", "--python"]:
        print("用法: python3 -m src.phase1.runner install-schedule --dest DIR --state-dir DIR --repo DIR --python PYTHON")
        return 2
    try:
        result = install_shadow_schedule(argv[1], argv[3], argv[5], argv[7])
    except ValueError as exc:
        print(json.dumps({"ready": False, "reason": str(exc), "routine_stopped": False}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _rollback_main(argv):
    if argv[::2] != ["--dest"]:
        print("用法: python3 -m src.phase1.runner rollback-schedule --dest DIR")
        return 2
    try:
        result = rollback_shadow_schedule(argv[1])
    except ValueError as exc:
        print(json.dumps({"ready": False, "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
