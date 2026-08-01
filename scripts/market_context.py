"""Market regime, limit-up sentiment and cross-source theme resonance."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from data_sources import ResilientHttpClient, get_default_client
from market_utils import eastmoney_secid


ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"


def latest_weekday(value: datetime | None = None) -> datetime:
    value = value or datetime.now()
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _weighted_score(components: list[tuple[float | None, float]]) -> float | None:
    available = [(value, weight) for value, weight in components if value is not None]
    if not available:
        return None
    total_weight = sum(weight for _, weight in available)
    return round(sum(value * weight for value, weight in available) / total_weight, 1)


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def calculate_market_temperature(
    industries: list[dict[str, Any]] | None,
    limit_up: list[dict[str, Any]] | None,
    broken: list[dict[str, Any]] | None,
    limit_down: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    industries_available = industries is not None
    pools_available = all(pool is not None for pool in (limit_up, broken, limit_down))
    industries = industries or []
    limit_up = limit_up or []
    broken = broken or []
    limit_down = limit_down or []
    up_total = sum(float(row.get("up_count") or 0) for row in industries)
    down_total = sum(float(row.get("down_count") or 0) for row in industries)
    breadth = (up_total / (up_total + down_total) * 100) if industries_available and up_total + down_total else None

    zt_count, zb_count, dt_count = len(limit_up), len(broken), len(limit_down)
    limit_balance = (zt_count / (zt_count + dt_count) * 100) if pools_available and zt_count + dt_count else None
    seal_success = (zt_count / (zt_count + zb_count) * 100) if pools_available and zt_count + zb_count else None
    max_height = max((int(row.get("limit_days") or 0) for row in limit_up), default=None) if pools_available else None
    height_score = min(100.0, max_height / 5 * 100) if max_height else None
    temperature = _weighted_score(
        [(breadth, 0.30), (limit_balance, 0.30), (seal_success, 0.25), (height_score, 0.15)]
    )
    break_rate = round(zb_count / (zt_count + zb_count) * 100, 1) if pools_available and zt_count + zb_count else None
    return {
        "market_temperature": temperature,
        "industry_breadth_pct": round(breadth, 1) if breadth is not None else None,
        "limit_up_count": zt_count if pools_available else None,
        "broken_count": zb_count if pools_available else None,
        "limit_down_count": dt_count if pools_available else None,
        "break_rate_pct": break_rate,
        "max_limit_height": max_height,
        "coverage": sum(value is not None for value in (breadth, limit_balance, seal_success, height_score)) / 4,
    }


def calculate_theme_resonance(
    candidates: list[dict[str, Any]],
    ths_hot: list[dict[str, Any]] | None,
    em_hot: list[dict[str, Any]] | None,
    industries: list[dict[str, Any]] | None,
    concept_blocks: dict[str, list[str]] | None = None,
    board_funds: list[dict[str, Any]] | None = None,
    news_items: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    ths_codes = {str(row.get("code") or "")[-6:] for row in (ths_hot or [])}
    em_codes = {str(row.get("code") or "")[-6:] for row in (em_hot or [])}
    result: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        code = str(candidate.get("股票代码") or candidate.get("code") or "")[:6]
        theme = str(candidate.get("所属主题") or candidate.get("theme") or "")
        in_ths, in_em = code in ths_codes, code in em_codes
        concepts = concept_blocks.get(code, []) if concept_blocks is not None else []
        tags = [tag for tag in [theme, *concepts] if tag]
        matched_industries = [
            row for row in (industries or [])
            if any(tag in str(row.get("name") or "") or str(row.get("name") or "") in tag for tag in tags)
        ]
        matched_funds = [
            row for row in (board_funds or [])
            if any(tag in str(row.get("name") or "") or str(row.get("name") or "") in tag for tag in tags)
        ]
        industry_changes = [value for row in matched_industries if (value := _as_float(row.get("change_pct"))) is not None]
        industry_score = max(0.0, min(100.0, 50.0 + max(industry_changes) * 5)) if industry_changes else None
        fund_values = [value for row in matched_funds if (value := _as_float(row.get("main_net"))) is not None]
        fund_score = (75.0 if max(fund_values) > 0 else 25.0) if fund_values else None
        candidate_name = str(candidate.get("股票名称") or candidate.get("name") or "")
        related_news = []
        for news in news_items or []:
            text = f"{news.get('title', '')} {news.get('snippet', '')}"
            if (candidate_name and candidate_name in text) or any(tag and tag in text for tag in tags):
                related_news.append(text)
        news_score = None
        if related_news:
            news_text = " ".join(related_news)
            positive = sum(term in news_text for term in ("增长", "中标", "订单", "增持", "突破", "景气", "利好"))
            negative = sum(term in news_text for term in ("减持", "处罚", "下滑", "亏损", "风险", "调查", "退潮"))
            news_score = max(0.0, min(100.0, 50.0 + positive * 10 - negative * 15))
        theme_score = _weighted_score([(news_score, 0.35), (industry_score, 0.25), (fund_score, 0.40)])
        sources = [name for matched, name in ((in_ths, "同花顺热榜"), (in_em, "东方财富人气榜")) if matched]
        verified_sources = sum(value is not None for value in (news_score, industry_score, fund_score))
        result[code] = {
            "theme_resonance": theme_score,
            "market_theme_score": theme_score,
            "hot_sources": sources,
            "hot_source_count": len(sources),
            "industry_breadth_match": bool(matched_industries),
            "concept_tags": concepts,
            "concept_match": bool(concepts),
            "board_fund_match": bool(matched_funds),
            "news_sentiment_score": news_score,
            "board_trend_score": industry_score,
            "board_fund_score": fund_score,
            "coverage": round(verified_sources / 3, 2),
            "verification": "个股多维验证" if verified_sources >= 2 else ("单维验证" if verified_sources else "未验证"),
        }
    return result


def _industry_rows(client: ResilientHttpClient) -> list[dict[str, Any]]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f3", "fs": "m:90+t:2", "fields": "f3,f12,f14,f104,f105,f140,f136",
    }
    data = client.get_json(url, params=params, headers={"Referer": "https://quote.eastmoney.com/"})
    raw = (data.get("data") or {}).get("diff") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [
        {
            "name": row.get("f14", ""), "code": row.get("f12", ""),
            "change_pct": row.get("f3"), "up_count": row.get("f104"),
            "down_count": row.get("f105"), "leader": row.get("f140", ""),
            "leader_change": row.get("f136"),
        }
        for row in raw
    ]


def _board_fund_flow(client: ResilientHttpClient, board_type: str = "industry", period: str = "today") -> list[dict[str, Any]]:
    fs = {"industry": "m:90+t:2", "concept": "m:90+t:3", "region": "m:90+t:1"}[board_type]
    fields_by_period = {
        "today": ("f62", "f184", "f3"), "5d": ("f164", "f165", "f109"), "10d": ("f174", "f175", "f160"),
    }
    main, pct, change = fields_by_period[period]
    data = client.get_json(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={"pn": "1", "pz": "200", "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": main,
                "fs": fs, "fields": f"f12,f14,{main},{pct},{change}"},
        headers={"Referer": "https://quote.eastmoney.com/"}, source="东方财富板块资金流",
    )
    payload = data.get("data")
    if not isinstance(payload, dict) or "diff" not in payload:
        raise ValueError("板块资金流载荷缺少 diff")
    raw = payload.get("diff") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [{"name": row.get("f14") or "", "code": row.get("f12") or "", "main_net": row.get(main),
             "main_pct": row.get(pct), "change_pct": row.get(change), "period": period, "type": board_type}
            for row in raw]


def _concept_blocks(client: ResilientHttpClient, code: str) -> list[str]:
    data = client.get_json(
        "https://push2.eastmoney.com/api/qt/slist/get",
        params={"fltt": "2", "invt": "2", "secid": eastmoney_secid(code), "spt": "3", "pi": "0", "pz": "200", "po": "1",
                "fields": "f12,f14,f3,f128"},
        headers={"Referer": "https://quote.eastmoney.com/"}, source="东方财富个股板块",
    )
    raw = ((data.get("data") or {}).get("diff") or [])
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [str(row.get("f14") or "") for row in raw if row.get("f14")]


def _limit_pool(client: ResilientHttpClient, endpoint: str, sort: str, date: str) -> list[dict[str, Any]]:
    url = f"https://push2ex.eastmoney.com/{endpoint}"
    params = {
        "ut": ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0,
        "pagesize": 10000, "sort": sort, "date": date,
    }
    data = client.get_json(url, params=params, headers={"Referer": "https://quote.eastmoney.com/"})
    rows = (data.get("data") or {}).get("pool") or []
    return [
        {
            "code": row.get("c", ""), "name": row.get("n", ""),
            "pct": row.get("zdp"), "limit_days": row.get("lbc") or row.get("days") or 0,
            "industry": row.get("hybk", ""), "break_times": row.get("zbc") or row.get("oc") or 0,
        }
        for row in rows
    ]


def _ths_hot(client: ResilientHttpClient) -> list[dict[str, Any]]:
    url = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
    data = client.get_json(url, params={"stock_type": "a", "type": "hour", "list_type": "normal"})
    rows = (data.get("data") or {}).get("stock_list") or []
    return [
        {
            "rank": row.get("order"), "code": row.get("code", ""), "name": row.get("name", ""),
            "heat": row.get("rate"), "concepts": (row.get("tag") or {}).get("concept_tag") or [],
        }
        for row in rows
    ]


def _em_hot(client: ResilientHttpClient, top: int = 100) -> list[dict[str, Any]]:
    url = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
    body = {
        "appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "", "pageNo": 1, "pageSize": top,
    }
    data = client.post_json(url, json=body)
    return [
        {"rank": row.get("rk"), "code": str(row.get("sc") or "")[-6:]}
        for row in data.get("data") or []
    ]


def collect_market_context(
    candidates: list[dict[str, Any]],
    client: ResilientHttpClient | None = None,
    trade_date: str | None = None,
    news_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    client = client or get_default_client()
    trade_date = trade_date or latest_weekday().strftime("%Y%m%d")
    errors: list[str] = []

    def safe(label: str, fn):
        try:
            return fn()
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            return None

    industries = safe("行业广度", lambda: _industry_rows(client))
    limit_up = safe("涨停池", lambda: _limit_pool(client, "getTopicZTPool", "fbt:asc", trade_date))
    broken = safe("炸板池", lambda: _limit_pool(client, "getTopicZBPool", "fbt:asc", trade_date))
    limit_down = safe("跌停池", lambda: _limit_pool(client, "getTopicDTPool", "fund:asc", trade_date))
    ths_hot = safe("同花顺热榜", lambda: _ths_hot(client))
    em_hot = safe("东方财富人气榜", lambda: _em_hot(client))
    board_funds = safe("板块资金流", lambda: _board_fund_flow(client))
    concept_blocks: dict[str, list[str]] | None = {}
    for candidate in candidates:
        code = str(candidate.get("股票代码") or candidate.get("code") or "")[:6]
        if not code:
            continue
        tags = safe(f"{code}板块归属", lambda code=code: _concept_blocks(client, code))
        if tags is None:
            concept_blocks = None
            break
        concept_blocks[code] = tags
    sentiment = calculate_market_temperature(industries, limit_up, broken, limit_down)
    resonance = calculate_theme_resonance(candidates, ths_hot, em_hot, industries, concept_blocks, board_funds, news_items)
    return {
        "trade_date": trade_date,
        "sentiment": sentiment,
        "top_industries": (industries or [])[:15],
        "board_fund_flow": (board_funds or [])[:20],
        "concept_blocks": concept_blocks or {},
        "theme_resonance": resonance,
        "ths_hot_count": len(ths_hot) if ths_hot is not None else None,
        "em_hot_count": len(em_hot) if em_hot is not None else None,
        "errors": errors,
    }
