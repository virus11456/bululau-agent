"""ISO 時間。Python 3.9 的 fromisoformat 不接受結尾 Z，3.11 以後才接受。"""
from datetime import datetime


def _fromisoformat(text):
    return datetime.fromisoformat(text)


def parse_instant(value):
    """結尾 Z 先改成 +00:00。沒有時區的字串維持 naive，由呼叫端決定。"""
    if isinstance(value, datetime):
        return value
    text = value.strip() if isinstance(value, str) else str(value).strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    return _fromisoformat(text)
