import os
import unittest

from data_sources import ResilientHttpClient
from market_context import collect_market_context


@unittest.skipUnless(os.environ.get("RUN_LIVE_SMOKE") == "1", "set RUN_LIVE_SMOKE=1 to call public sources")
class LiveSourceSmokeTests(unittest.TestCase):
    def test_small_market_context_request(self):
        client = ResilientHttpClient(retries=0, eastmoney_min_interval=0.2)
        result = collect_market_context(
            [{"股票代码": "600519.SH", "股票名称": "贵州茅台", "所属主题": "白酒"}], client=client
        )
        self.assertIn("sentiment", result)
        self.assertIn("600519", result["theme_resonance"])
        self.assertTrue(client.trace())
        self.assertTrue(any(event["status"] == "success" for event in client.trace()))
        self.assertIsNotNone(result["sentiment"].get("market_temperature"))


if __name__ == "__main__":
    unittest.main()
