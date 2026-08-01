import unittest
from unittest.mock import patch

import a_stock_selection as workflow
from data_quality import compose_rankings, confidence_for_candidate
from market_context import calculate_market_temperature, calculate_theme_resonance
from risk_review import calculate_risk_penalty
from valuation import forward_pe, peg_ratio, score_valuations


class ThresholdTests(unittest.TestCase):
    def test_requested_thresholds(self):
        self.assertEqual(workflow.MIN_MARKET_CAP_YI, 100.0)
        self.assertEqual(workflow.MIN_DAILY_AMOUNT_WAN, 50000.0)

    def test_candidate_gate_preserves_discovery_order(self):
        rows = [
            {"股票代码": "000001.SZ", "股票名称": "甲"},
            {"股票代码": "000002.SZ", "股票名称": "乙"},
            {"股票代码": "000003.SZ", "股票名称": "丙"},
            {"股票代码": "600001.SH", "股票名称": "ST丁"},
        ]
        quotes = {
            "000001": {"mcap_yi": 399, "amount_wan": 60000},
            "000002": {"mcap_yi": 401, "amount_wan": 50000},
            "000003": {"mcap_yi": 900, "amount_wan": 49999},
            "600001": {"mcap_yi": 101, "amount_wan": 50001},
        }
        with patch.object(workflow, "_tencent_quotes", return_value=quotes):
            selected = workflow.enforce_candidate_constraints(rows)
        self.assertEqual([row["股票代码"] for row in selected], ["000001.SZ", "000002.SZ", "600001.SH"])
        self.assertNotIn("市值偏好", selected[0])

    def test_market_cap_and_amount_do_not_change_technical_score(self):
        base = {"short_momentum": 3, "momentum_10d": 6, "medium_momentum": 8, "long_momentum": 10,
                "close": 11, "ma5": 10.8, "ma10": 10.5, "ma20": 10, "volume_ratio_5d": 1.3}
        small = dict(base, market_cap_yi=101, amount_wan=50000)
        large = dict(base, market_cap_yi=5000, amount_wan=900000)
        self.assertEqual(workflow.calculate_technical_score(small), workflow.calculate_technical_score(large))

    def test_five_and_ten_day_trends_outweigh_longer_trends(self):
        short_strong = {"short_momentum": 4, "momentum_10d": 8, "medium_momentum": -2, "long_momentum": -3,
                        "close": 11, "ma5": 10.8, "ma10": 10.5, "ma20": 10, "volume_ratio_5d": 1.3}
        long_strong = {"short_momentum": -2, "momentum_10d": -3, "medium_momentum": 12, "long_momentum": 20,
                       "close": 9.5, "ma5": 9.8, "ma10": 10, "ma20": 9, "volume_ratio_5d": 1.0}
        self.assertGreater(workflow.calculate_technical_score(short_strong), workflow.calculate_technical_score(long_strong))


class MarketScoringTests(unittest.TestCase):
    def test_market_temperature(self):
        industries = [{"up_count": 70, "down_count": 30}]
        limit_up = [{"limit_days": 2}] * 20
        broken = [{}] * 5
        limit_down = [{}] * 5
        result = calculate_market_temperature(industries, limit_up, broken, limit_down)
        self.assertGreater(result["market_temperature"], 60)
        self.assertEqual(result["break_rate_pct"], 20.0)

    def test_hot_list_membership_is_evidence_not_score(self):
        candidates = [{"股票代码": "000001.SZ", "所属主题": "银行"}]
        result = calculate_theme_resonance(
            candidates, [{"code": "000001"}], [{"code": "000001"}], [{"name": "银行"}]
        )
        self.assertIsNone(result["000001"]["theme_resonance"])
        self.assertEqual(result["000001"]["hot_source_count"], 2)

    def test_candidate_specific_news_board_and_funds_can_score(self):
        result = calculate_theme_resonance(
            [{"股票代码": "000001.SZ", "股票名称": "甲", "所属主题": "银行"}],
            None,
            None,
            [{"name": "银行", "change_pct": 2}],
            {"000001": ["银行"]},
            [{"name": "银行", "main_net": 100}],
            [{"title": "银行板块景气增长", "snippet": "甲获得订单"}],
        )["000001"]
        self.assertIsNotNone(result["market_theme_score"])
        self.assertIsNotNone(result["news_sentiment_score"])
        self.assertIsNotNone(result["board_fund_score"])


class ValuationTests(unittest.TestCase):
    def test_forward_pe_and_peg_boundaries(self):
        self.assertEqual(forward_pe(20, 2), 10)
        self.assertIsNone(forward_pe(20, -1))
        self.assertEqual(peg_ratio(30, 30), 1)
        self.assertIsNone(peg_ratio(30, 0))

    def test_cross_sectional_score_and_low_coverage(self):
        records = {
            "a": {"eps_growth_pct": 40, "fundamental_growth_pct": 30, "forward_pe": 20, "peg": 0.5, "analyst_count": 5},
            "b": {"eps_growth_pct": 10, "fundamental_growth_pct": 5, "forward_pe": 60, "peg": 4, "analyst_count": 1},
        }
        scored = score_valuations(records)
        self.assertGreater(scored["a"]["growth_valuation_score"], scored["b"]["growth_valuation_score"])
        self.assertEqual(scored["b"]["coverage_label"], "低覆盖")


class RiskAndCompositeTests(unittest.TestCase):
    def test_risk_penalty_is_capped(self):
        review = {
            "verified": ["lockups", "announcements", "fund_flow", "margin", "dragon_tiger"],
            "lockups": [{"ratio_pct": 12}],
            "announcements": [{"title": "立案处罚及重大亏损"}] * 5,
            "fund_flow": [{"main_net": -1}] * 20,
            "margin": [{"balance": 130}] + [{"balance": 100}] * 4,
            "dragon_tiger": {"records": [{}], "institution_net_wan": -1000},
        }
        result = calculate_risk_penalty(review)
        self.assertEqual(result["risk_penalty"], 20)
        self.assertEqual(result["unlock_penalty"], 8)

    def test_unverified_dimensions_are_not_zero_or_penalised(self):
        review = {
            "verified": ["announcements"],
            "announcements": [{"title": "关于董事股份减持计划实施完毕的公告", "date": "2026-01-01", "source": "巨潮"}],
        }
        result = calculate_risk_penalty(review)
        self.assertEqual(result["risk_penalty"], 0)
        self.assertIsNone(result["max_unlock_ratio_pct"])

    def test_double_score_reorders_and_preserves_technical_rank(self):
        technical = [
            {"股票代码": "000001.SZ", "股票名称": "甲", "技术结构分": 90},
            {"股票代码": "000002.SZ", "股票名称": "乙", "技术结构分": 85},
        ]
        market = {"sentiment": {"market_temperature": 60}, "theme_resonance": {
            "000001": {"market_theme_score": 50}, "000002": {"market_theme_score": 90},
        }}
        valuations = {
            "000001": {"growth_valuation_score": 40, "valuation_coverage": 1, "analyst_count": 5},
            "000002": {"growth_valuation_score": 95, "valuation_coverage": 1, "analyst_count": 5},
        }
        risks = {
            "000001": {"risk_penalty": 20, "coverage": 1, "risk_flags": ["风险"]},
            "000002": {"risk_penalty": 0, "coverage": 1, "risk_flags": []},
        }
        result = compose_rankings(technical, market, valuations, risks, formal=True)
        self.assertEqual(result[0]["股票代码"], "000002.SZ")
        self.assertEqual(result[0]["技术排序"], 2)
        self.assertEqual(result[0]["综合排名"], 1)

    def test_missing_data_lowers_confidence_not_score_as_risk(self):
        confidence = confidence_for_candidate(True, {}, {}, {})
        self.assertEqual(confidence["confidence_score"], 40.0)
        self.assertEqual(confidence["confidence_grade"], "低")

    def test_low_analyst_coverage_reduces_confidence(self):
        low = confidence_for_candidate(
            True, {"market_theme_score": 70}, {"valuation_coverage": 1, "analyst_count": 1}, {"coverage": 1}
        )
        normal = confidence_for_candidate(
            True, {"market_theme_score": 70}, {"valuation_coverage": 1, "analyst_count": 5}, {"coverage": 1}
        )
        self.assertLess(low["confidence_score"], normal["confidence_score"])


if __name__ == "__main__":
    unittest.main()
