"""路徑與金鑰。只讀環境變數與本機 env 檔，絕不把金鑰寫進日誌或例外訊息。"""
import os

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SRC_DIR)


def _parse_env_file(path):
    vals = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            vals[key.strip()] = value.strip().strip('"').strip("'")
    return vals


def _inject_file(path):
    path = os.path.expanduser(path)
    if not path or not os.path.isfile(path):
        return
    for key, value in _parse_env_file(path).items():
        # 空白不當設定。環境裡已經有內容的值優先，檔案只補缺的。
        if value and not os.environ.get(key):
            os.environ[key] = value


def load_local_env():
    """載入專案根目錄 .env，以及 BINANCE_ENV_FILE 指向的金鑰檔。不覆蓋既有環境變數。"""
    _inject_file(os.path.join(REPO_ROOT, ".env"))
    extra = os.environ.get("BINANCE_ENV_FILE")
    if extra:
        _inject_file(extra)


def data_dir():
    """執行時資料目錄。預設是專案下的 data/。可用 QUANTBOY_DATA_DIR 改掉。"""
    load_local_env()
    raw = os.environ.get("QUANTBOY_DATA_DIR", os.path.join(REPO_ROOT, "data"))
    path = os.path.abspath(os.path.expanduser(raw))
    os.makedirs(path, exist_ok=True)
    return path


def events_path():
    """重大事件表。預設 <資料目錄>/events.json，可用 QUANTBOY_EVENTS_FILE 改掉。"""
    load_local_env()
    raw = os.environ.get("QUANTBOY_EVENTS_FILE")
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    return os.path.join(data_dir(), "events.json")


def load_api_keys():
    """回傳 (api_key, api_secret)。找不到就停下來，訊息裡不含金鑰內容。"""
    load_local_env()
    key = os.environ.get("BINANCE_API_KEY", "").strip()
    secret = os.environ.get("BINANCE_API_SECRET", "").strip()
    if key and secret:
        return key, secret
    raise SystemExit(
        "找不到幣安金鑰。請在環境變數或專案根目錄的 .env（已列入 .gitignore）設定 "
        "BINANCE_API_KEY 與 BINANCE_API_SECRET，或用 BINANCE_ENV_FILE 指向本機金鑰檔。"
        "金鑰只開合約讀取與交易，不要開提現。格式見 config.example。"
    )
