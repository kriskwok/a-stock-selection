import unittest
from datetime import datetime
from unittest.mock import patch

import a_stock_selection as workflow
from market_utils import eastmoney_secid, is_listed_trade_day, is_xshg_trade_day, market_prefix, parse_sina_trade_calendar


class MarketRouteTests(unittest.TestCase):
    def test_prefix_handles_explicit_indexes_etfs_and_beijing(self):
        self.assertEqual(market_prefix("sh000001"), "sh")
        self.assertEqual(market_prefix("000001.SH"), "sh")
        self.assertEqual(market_prefix("000001"), "sz")
        self.assertEqual(market_prefix("000300"), "sh")
        self.assertEqual(market_prefix("510300"), "sh")
        self.assertEqual(market_prefix("920002"), "bj")
        self.assertEqual(eastmoney_secid("600519"), "1.600519")
        self.assertEqual(eastmoney_secid("920002"), "0.920002")

    def test_exchange_calendar_requires_verified_date(self):
        calendar = parse_sina_trade_calendar('var datelist="20260724,20260727";')
        self.assertTrue(is_listed_trade_day(datetime(2026, 7, 27, 9, 30), calendar))
        self.assertFalse(is_listed_trade_day(datetime(2026, 7, 26, 9, 30), calendar))

    def test_xshg_calendar_and_workflow_skip_weekend_without_network(self):
        self.assertTrue(is_xshg_trade_day(datetime(2026, 7, 27, 9, 30)))
        self.assertFalse(is_xshg_trade_day(datetime(2026, 7, 26, 9, 30)))
        with patch.object(workflow, "get_default_client", side_effect=AssertionError("no network")):
            self.assertIsNone(workflow.verified_a_share_trade_date(datetime(2026, 7, 26, 9, 30)))

    def test_trade_check_uses_verified_exchange_calendar(self):
        self.assertEqual(workflow.verified_a_share_trade_date(datetime(2026, 7, 27, 9, 30)).date().isoformat(), "2026-07-27")


if __name__ == "__main__":
    unittest.main()
