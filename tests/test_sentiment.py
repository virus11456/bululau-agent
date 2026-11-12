"""sentiment 的公開情緒判斷。get 被換成假資料，不連網，不改門檻。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import sentiment  # noqa: E402


def fake_get(payloads):
    def get(path, **_params):
        if path not in payloads:
            return {"_err": "missing " + path}
        return payloads[path]
    return get


class SentimentTest(unittest.TestCase):
    def analyze(self, payloads):
        original = sentiment.get
        sentiment.get = fake_get(payloads)
        try:
            return sentiment.analyze("BTCUSDT")
        finally:
            sentiment.get = original

    def base(self, funding):
        return {
            "/fapi/v1/premiumIndex": {"lastFundingRate": funding},
            "/fapi/v1/fundingRate": [{"fundingRate": funding}],
            "/futures/data/globalLongShortAccountRatio": [
                {"longShortRatio": "1.0"},
                {"longShortRatio": "1.1"},
            ],
            "/futures/data/topLongShortPositionRatio": [
                {"longShortRatio": "1.0"},
                {"longShortRatio": "1.0"},
            ],
            "/futures/data/openInterestHist": [
                {"sumOpenInterest": "100"},
                {"sumOpenInterest": "100"},
            ],
            "/fapi/v1/ticker/24hr": {"priceChangePercent": "1"},
        }

    def test_funding_above_half_percent_blocks_long_and_does_not_set_size(self):
        out = self.analyze(self.base(0.0006))
        self.assertEqual(out["long"]["verdict"], "long_block")
        self.assertNotIn("portion", out)
        self.assertNotIn("size", out)

    def test_funding_between_caution_band_does_not_block(self):
        out = self.analyze(self.base(0.0004))
        self.assertEqual(out["long"]["verdict"], "long_caution")
        self.assertTrue(out["long"]["reasons"])

    def test_failed_read_is_unknown(self):
        out = self.analyze({"/fapi/v1/premiumIndex": {"_err": "down"}})
        self.assertEqual(out["long"]["verdict"], "unknown")
        self.assertEqual(out["short"]["verdict"], "unknown")

    def test_chg_does_not_divide_by_zero(self):
        self.assertIsNone(sentiment.chg(1, 0))
        self.assertEqual(sentiment.chg(110, 100), 10.0)
