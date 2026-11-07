"""舊平鋪 live_trader 的路徑與鎖。只讀原始碼與檔案型別，不讀 state、不讀 .env、不 flock。

父側片段：HOME 固定 os.path.expanduser('~/quantboy-live')，沒有看到這些常數被環境變數覆寫。
鎖是 open(LOCK,'w') 後 LOCK_EX|LOCK_NB，包住 status/open/manage/watch/flatten。
state.json 的結構沒有在這份證據裡，所以 manage 仍不准沿用那個檔。
"""
import hashlib
import os
import stat

KNOWN_LIVE_FLAT_SHA = "65f1d4526cd5d3b6c2115ca8eeebd102281b27a2fbd40d0ee07054ff4891459b"
LEGACY_HOME_EXPR = "os.path.expanduser('~/quantboy-live')"
BINDINGS = {
    "HOME": LEGACY_HOME_EXPR,
    "STATE": "os.path.join(HOME,'state.json')",
    "EVENTS": "os.path.join(HOME,'events.json')",
    "HALT_FLAG": "os.path.join(HOME,'HALT_REPORT_USER.flag')",
    "LOCK": "os.path.join(HOME,'.lock')",
}
DISPATCH = ("status", "open", "manage", "watch", "flatten")


def legacy_home_path():
    return os.path.realpath(os.path.expanduser("~/quantboy-live"))


def _strip_comment(line):
    out = []
    quote = None
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "#":
            break
        else:
            out.append(char)
    return "".join(out).strip()


def _compact(text):
    return text.replace('"', "'").replace(" ", "").replace("\t", "")


def parse_bindings(text):
    found = {}
    duplicated = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if "=" not in line:
            continue
        name, _, expr = line.partition("=")
        name = name.strip()
        if name not in BINDINGS:
            continue
        if name in found:
            duplicated.append(name)
        found[name] = _compact(expr.strip())
    return found, duplicated


def bindings_match(text):
    found, duplicated = parse_bindings(text)
    if duplicated or set(found) != set(BINDINGS):
        return False
    return all(found[name] == _compact(BINDINGS[name]) for name in BINDINGS)


def lock_protocol_match(text):
    compact = _compact(text)
    required = (
        "classLock",
        "self.f=open(LOCK,'w')",
        "fcntl.flock(self.f,fcntl.LOCK_EX|fcntl.LOCK_NB)",
        "exceptBlockingIOError",
        "另一個live_trader正在執行",
        "def__exit__",
        "fcntl.flock(self.f,fcntl.LOCK_UN)",
        "self.f.close()",
        "withLock():",
    )
    if any(part not in compact for part in required):
        return False
    tail = compact[compact.find("withLock():"):compact.find("withLock():") + 900]
    if "BinanceError" not in tail:
        return False
    return all(name in tail for name in DISPATCH)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        chunk = handle.read(65536)
        while chunk:
            digest.update(chunk)
            chunk = handle.read(65536)
    return digest.hexdigest()


def _kind(path):
    """不讀內容。讀取被拒時只留下 errno，不當成檔案不存在的結論。"""
    try:
        info = os.lstat(path)
    except OSError as exc:
        return {"kind": "stat_error", "errno": exc.errno, "error": exc.strerror}
    if stat.S_ISLNK(info.st_mode):
        return {"kind": "symlink", "errno": None, "error": None}
    if stat.S_ISREG(info.st_mode):
        return {"kind": "file", "errno": None, "error": None}
    if stat.S_ISDIR(info.st_mode):
        return {"kind": "dir", "errno": None, "error": None}
    return {"kind": "other", "errno": None, "error": None}


def check_legacy_install(root, home=None):
    expected = os.path.realpath(home) if home else legacy_home_path()
    root_text = os.path.abspath(root or "")
    exists = os.path.isdir(root_text) and not os.path.islink(root_text)
    root_real = os.path.realpath(root_text) if exists else root_text
    path_ok = exists and root_real == expected
    trader = os.path.join(root_text, "live_trader.py")
    text = ""
    digest = None
    if os.path.isfile(trader) and not os.path.islink(trader):
        with open(trader, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        digest = _sha256(trader)
    matched_bindings = bool(text) and bindings_match(text)
    matched_lock = bool(text) and lock_protocol_match(text)
    sha_ok = digest == KNOWN_LIVE_FLAT_SHA
    accepted = path_ok and matched_bindings and matched_lock and sha_ok
    if not path_ok:
        reason = "unknown_path"
    elif not sha_ok:
        reason = "sha_mismatch"
    elif not matched_bindings:
        reason = "bindings_mismatch"
    elif not matched_lock:
        reason = "lock_protocol_mismatch"
    else:
        reason = "state_schema_unproven"
    files = {}
    for name in ("state.json", "events.json", ".lock", "HALT_REPORT_USER.flag"):
        files[name] = _kind(os.path.join(root_text, name)) if path_ok else {"kind": "not_checked", "errno": None, "error": None}
    return {
        "quantboy_root": root_text,
        "legacy_home": expected,
        "path_is_legacy_home": path_ok,
        "layout": "flat" if text else None,
        "bindings_match": matched_bindings,
        "lock_protocol_match": matched_lock,
        "known_sha_match": sha_ok,
        "legacy_source_accepted": accepted,
        "state_schema_proven": False,
        "share_live_state": False,
        "manage_allowed": False,
        "package_data_shared": False,
        "watch_modified": False,
        "spawned": False,
        "flock_taken": False,
        "state_bytes_read": False,
        "files": files,
        "reason": reason,
    }


def production_block_reason(report):
    """平鋪舊 root 或未知平鋪路徑不啟動 manage。src 套件目錄維持原生產測試路徑。"""
    if not report.get("path_is_legacy_home") and report.get("layout") != "flat":
        return None
    if report.get("legacy_source_accepted"):
        return "state_schema_unproven"
    return report.get("reason") or "unknown_path"
