"""Python 3.9 的 fromisoformat 拒絕結尾 Z。這支測試把那個拒絕裝回去，避免只在 3.12 上通過。"""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import src.phase1.timeparse as timeparse
from src.phase1.ack_contract import _parse_time
from src.phase1.autoscan import freshness, parse_time
from src.phase1.runner import _utc_now


def _reject_z(value):
    if isinstance(value, str) and value[-1:] in ("Z", "z"):
        raise ValueError("Invalid isoformat string: '%s'" % value)
    return datetime.fromisoformat(value)


class TimeParseTest(unittest.TestCase):
    def test_trailing_z_survives_a_python39_fromisoformat(self):
        with patch.object(timeparse, "_fromisoformat", _reject_z):
            stamp = "2026-11-07T07:21:55Z"
            parsed = timeparse.parse_instant(stamp)
            self.assertEqual(parsed, datetime(2026, 11, 7, 7, 21, 55, tzinfo=timezone.utc))
            self.assertEqual(parse_time(stamp), parsed)
            self.assertEqual(_parse_time(stamp), parsed)
            fresh = freshness("2026-11-07T15:21:55+08:00", stamp)
            self.assertEqual(fresh["status"], "fresh")
            generated = _utc_now()
            self.assertTrue(generated.endswith("Z"))
            parse_time(generated)
            _parse_time(generated)


if __name__ == "__main__":
    unittest.main()
