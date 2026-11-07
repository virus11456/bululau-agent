"""篩選與紙上倉位共用的公開行情工具。只打幣安公開端點，不讀金鑰。"""
import json, time, urllib.request, urllib.error, os, datetime
from concurrent.futures import ThreadPoolExecutor
BASE = "https://fapi.binance.com"
TZ = datetime.timezone(datetime.timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))

def _data_dir():
    """repo 根有 config/ 目錄時，裸 import config 不是 src/config.py。"""
    try:
        from src.config import data_dir
    except ImportError:
        from config import data_dir
    return data_dir()

DATA_DIR = _data_dir()

class RateLimited(Exception):
    pass

def get(path, params=None, retries=2):
    q = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "paper-prefilter"}), timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                raise RateLimited(f"{e.code} on {path}")
            if i == retries: raise
        except Exception:
            if i == retries: raise
        time.sleep(0.5 * (i + 1))

def klines(sym, interval, limit=120, end_ms=None):
    p = {"symbol": sym, "interval": interval, "limit": limit}
    if end_ms: p["endTime"] = end_ms
    rows = get("/fapi/v1/klines", p)
    out = [dict(t=r[0], o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[7]), ct=r[6]) for r in rows]
    now = end_ms or int(time.time() * 1000)
    return [k for k in out if k["ct"] < now]  # closed bars only

def pmap(fn, items, workers=12):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))

def ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None

def ts(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d %H:%M")

def now_iso():
    return datetime.datetime.now(TZ).isoformat(timespec="seconds")

def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
