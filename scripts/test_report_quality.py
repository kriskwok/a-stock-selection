import unittest
from unittest.mock import patch

import a_stock_selection as workflow
from data_quality import compose_rankings
from market_context import calculate_market_temperature, calculate_theme_resonance


class NewsAndHotspotTests(unittest.TestCase):
    def test_irrelevant_ai_news_and_invalid_link_are_filtered(self):
        items = [
            {"title": "申真谞战胜围棋AI", "snippet": "围棋人机大战", "date": "2026-07-21 14:00", "source": "财联社", "link": "null"},
            {"title": "半导体板块走强", "snippet": "A股半导体公司涨停", "date": "2026-07-21 14:01", "source": "财联社", "link": "null"},
        ]
        result = workflow.filter_fresh_news(items, today=workflow.datetime(2026, 7, 21, 15, 0))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["link"], "")

    def test_cards_and_hotspot_table_share_quality_score(self):
        items = [
            {"title": "半导体产业链走强", "snippet": "A股半导体板块涨停", "date": "2026-07-21 14:00", "source": "财联社"},
        ]
        rows = workflow.interpret_hotspots(items)
        self.assertGreater(rows[0]["热点质量分"], 0)
        self.assertEqual(rows[0]["验证状态"], "单源热度")
        self.assertIn("独立来源", workflow.hotspot_interpretation_markdown(rows))


class DegradationAndRankingTests(unittest.TestCase):
    def test_failed_market_sources_are_unverified_not_zero(self):
        sentiment = calculate_market_temperature(None, None, None, None)
        self.assertIsNone(sentiment["limit_up_count"])
        self.assertIsNone(sentiment["market_temperature"])
        resonance = calculate_theme_resonance(
            [{"股票代码": "000001.SZ", "所属主题": "银行"}], None, None, None
        )
        self.assertIsNone(resonance["000001"]["theme_resonance"])

    def test_missing_score_components_are_not_default_fifty(self):
        result = compose_rankings(
            [{"股票代码": "000001.SZ", "股票名称": "甲", "技术结构分": 80}],
            {"sentiment": {}, "theme_resonance": {}},
            {"000001": {"growth_valuation_score": None, "valuation_coverage": 0}},
            {"000001": {"risk_penalty": 0, "coverage": 0}},
            formal=False,
        )
        self.assertEqual(result[0]["综合研究分"], 80.0)
        self.assertEqual(result[0]["评分状态"], "暂定综合研究分")

    def test_diversify_candidates_caps_single_theme(self):
        rows = ([{"股票代码": f"6000{i:02}.SH", "所属主题": "半导体", "_score": 9} for i in range(6)] +
                [{"股票代码": "300001.SZ", "所属主题": "创新药", "_score": 8},
                 {"股票代码": "300002.SZ", "所属主题": "机器人", "_score": 7}])
        selected = workflow.diversify_candidates(rows)
        first_five = selected[:5]
        self.assertLessEqual(sum(row["所属主题"] == "半导体" for row in first_five), 4)


class ReportPresentationTests(unittest.TestCase):
    def test_pe_and_trend_presentation(self):
        self.assertEqual(workflow.format_pe(-10), "N/M")
        self.assertIn("极高", workflow.format_pe(1000))
        response = {"result": [{"query": "最近5日涨跌幅", "content": "| 股票代码 | 股票名称 | 5日涨跌幅(%) |\n| --- | --- | --- |\n| 000001.SZ | 甲 | 未验证 |", "source": "备源", "status": "success"}]}
        self.assertEqual(workflow.display_fin_sections(response), [])


class IndependentTrendSourceTests(unittest.TestCase):
    def test_ths_jsonp_daily_history_parses_close_high_volume(self):
        text = 'quotebridge_v6_line_hs_000001_01_last({"data":"20260102,10,11,9,10.5,100,1050;20260105,10.5,12,10,11,120,1320"})'
        rows = workflow._parse_ths_history_payload(text)
        self.assertEqual(rows[0]["close"], 10.5)
        self.assertEqual(rows[1]["high"], 12.0)
        self.assertEqual(rows[1]["amount"], 1320.0)

    def test_independent_ths_history_precedes_eastmoney(self):
        history = [
            {"date": f"2026-01-{index:02d}", "close": float(index), "high": float(index + 1), "volume": 100, "amount": 1000}
            for index in range(1, 80)
        ]
        with patch.object(workflow, "_ths_history", return_value=history), patch.object(
            workflow, "_eastmoney_history", side_effect=AssertionError("must not call Eastmoney")
        ):
            result = workflow._trend_metrics("000001", {"price": 79.0})
        self.assertEqual(result["history_source"], "同花顺K线独立备源")
        self.assertIsNotNone(result["ma60"])

    def test_candidate_universe_does_not_require_eastmoney_board(self):
        quotes = {
            "300308": {"mcap_yi": 500, "amount_wan": 60000},
            "300502": {"mcap_yi": 500, "amount_wan": 60000},
            "300394": {"mcap_yi": 500, "amount_wan": 60000},
            "601138": {"mcap_yi": 500, "amount_wan": 60000},
            "000977": {"mcap_yi": 500, "amount_wan": 60000},
            "000938": {"mcap_yi": 500, "amount_wan": 60000},
            "002463": {"mcap_yi": 500, "amount_wan": 60000},
            "002281": {"mcap_yi": 500, "amount_wan": 60000},
            "688256": {"mcap_yi": 500, "amount_wan": 60000},
            "002371": {"mcap_yi": 500, "amount_wan": 60000},
        }
        with patch.object(workflow, "_latest_ths_hot_rows", return_value=("2026-07-21", [])), patch.object(
            workflow, "_tencent_quotes", return_value=quotes
        ), patch.object(workflow, "_eastmoney_board_catalog", side_effect=AssertionError("must not call Eastmoney")):
            response = workflow.free_candidates(["AI算力", "半导体"])
        self.assertTrue(response["result"])
        self.assertIn("内置主题池", response["result"][0]["source"])


if __name__ == "__main__":
    unittest.main()
