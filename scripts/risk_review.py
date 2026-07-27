"""Event and capital-structure risk review for technical candidates."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from data_sources import ResilientHttpClient, get_default_client
from market_utils import eastmoney_secid, market_prefix


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
RISK_ANNOUNCEMENT_TERMS = (
    "减持", "立案", "处罚", "警示函", "重大亏损", "预亏", "退市风险", "终止上市",
    "债务逾期", "违规担保", "诉讼", "冻结", "质押违约",
)
_CNINFO_ORGIDS: dict[str, str] = {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _datacenter(
    client: ResilientHttpClient,
    report_name: str,
    filter_str: str,
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict[str, Any]]:
    params = {
        "reportName": report_name, "columns": "ALL", "filter": filter_str,
        "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    data = client.get_json(DATACENTER_URL, params=params)
    return ((data.get("result") or {}).get("data") or [])


def fetch_lockups(client: ResilientHttpClient, code: str, trade_date: str) -> list[dict[str, Any]]:
    end = (datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=90)).strftime("%Y-%m-%d")
    rows = _datacenter(
        client, "RPT_LIFT_STAGE",
        f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{trade_date}\')(FREE_DATE<=\'{end}\')',
        20, "FREE_DATE", "1",
    )
    result = []
    for row in rows:
        ratio = _float(row.get("FREE_RATIO"))
        ratio_pct = ratio * 100 if 0 < ratio <= 1 else ratio
        result.append({
            "date": str(row.get("FREE_DATE") or "")[:10],
            "type": row.get("FREE_SHARES_TYPE") or "",
            "shares": row.get("FREE_SHARES") or 0,
            "able_shares": row.get("ABLE_FREE_SHARES") or 0,
            "ratio_pct": round(ratio_pct, 2),
        })
    return result


def _load_cninfo_orgids(client: ResilientHttpClient) -> None:
    if _CNINFO_ORGIDS:
        return
    data = client.get_json("https://www.cninfo.com.cn/new/data/szse_stock.json", source="巨潮资讯")
    for row in data.get("stockList") or []:
        if row.get("code") and row.get("orgId"):
            _CNINFO_ORGIDS[str(row["code"])] = str(row["orgId"])


def _cninfo_announcements(
    client: ResilientHttpClient, code: str, start: str, end: str,
) -> list[dict[str, Any]]:
    try:
        _load_cninfo_orgids(client)
    except Exception:
        pass
    org_id = _CNINFO_ORGIDS.get(code)
    if not org_id:
        org_id = f"gssh0{code}" if code.startswith("6") else f"gssz0{code}"
    payload = {
        "stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": "30", "pageNum": "1",
        "column": "", "category": "", "plate": "", "seDate": f"{start}~{end}",
        "searchkey": "", "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    data = client.post_json(
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        data=payload,
        headers={"Referer": "https://www.cninfo.com.cn/new/disclosure", "Origin": "https://www.cninfo.com.cn"},
        source="巨潮资讯",
    )
    return [
        {
            "title": row.get("announcementTitle") or "",
            "date": datetime.fromtimestamp(row["announcementTime"] / 1000).strftime("%Y-%m-%d")
            if isinstance(row.get("announcementTime"), (int, float)) else str(row.get("announcementTime") or "")[:10],
            "source": "巨潮资讯",
        }
        for row in data.get("announcements") or []
    ]


def _szse_announcements(
    client: ResilientHttpClient, code: str, start: str, end: str,
) -> list[dict[str, Any]]:
    data = client.post_json(
        "https://www.szse.cn/api/disc/announcement/annList",
        json={"channelCode": ["listedNotice_disc"], "pageSize": 30, "pageNum": 1, "stock": [code]},
        headers={"Referer": "https://www.szse.cn/disclosure/listed/notice/index.html"},
        source="深交所公告备源", fallback=True,
    )
    return [
        {"title": row.get("title") or "", "date": str(row.get("publishTime") or "")[:10], "source": "深交所"}
        for row in data.get("data") or []
        if start <= str(row.get("publishTime") or "")[:10] <= end
    ]


def _sh_announcements(client: ResilientHttpClient, code: str, start: str, end: str) -> list[dict[str, Any]]:
    """Independent Shanghai announcement fallback when CNINFO is unavailable."""
    data = client.get_json(
        "https://np-anotice-stock.eastmoney.com/api/security/ann",
        params={"sr": "-1", "page_size": 30, "page_index": 1, "ann_type": "A", "client_source": "web",
                "stock_list": code, "f_node": "0", "s_node": "0"},
        source="东财沪市公告备源", fallback=True,
    )
    return [
        {"title": row.get("title") or "", "date": str(row.get("notice_date") or "")[:10], "source": "东方财富沪市公告"}
        for row in (data.get("data") or {}).get("list") or []
        if start <= str(row.get("notice_date") or "")[:10] <= end
    ]


def fetch_announcements(client: ResilientHttpClient, code: str, trade_date: str) -> list[dict[str, Any]]:
    end = trade_date
    start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    if code.startswith(("0", "3")):
        rows, _ = client.call_with_fallback(
            lambda: _cninfo_announcements(client, code, start, end),
            lambda: _szse_announcements(client, code, start, end),
            "公告",
        )
        return rows
    rows, _ = client.call_with_fallback(
        lambda: _cninfo_announcements(client, code, start, end),
        lambda: _sh_announcements(client, code, start, end),
        "公告",
    )
    return rows


def fetch_fund_flow(client: ResilientHttpClient, code: str) -> list[dict[str, Any]]:
    data = client.get_json(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        params={
            "secid": eastmoney_secid(code), "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57", "lmt": "20",
        },
        headers={"Referer": "https://quote.eastmoney.com/"},
    )
    payload = data.get("data")
    if not isinstance(payload, dict) or "klines" not in payload:
        raise ValueError("东财资金流载荷缺少 klines")
    result = []
    for raw in payload.get("klines") or []:
        parts = raw.split(",")
        if len(parts) >= 6:
            result.append({"date": parts[0], "main_net": _float(parts[1]), "super_net": _float(parts[5])})
    return result


def _sina_fund_flow(client: ResilientHttpClient, code: str, days: int = 20) -> list[dict[str, Any]]:
    prefix = market_prefix(code)
    text = client.get_text(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs",
        params={"page": "1", "num": str(days), "sort": "opendate", "asc": "0", "daima": f"{prefix}{code}"},
        headers={"Referer": "https://finance.sina.com.cn/"}, source="新浪资金流备源", fallback=True,
    )
    start, end = text.find("["), text.rfind("]")
    rows = json.loads(text[start:end + 1]) if start >= 0 and end >= start else []
    return [
        {"date": row.get("opendate") or "", "main_net": _float(row.get("netamount")), "super_net": None,
         "source": "新浪日度资金流"}
        for row in rows
    ]


def fetch_fund_flow_with_fallback(client: ResilientHttpClient, code: str) -> list[dict[str, Any]]:
    rows, _ = client.call_with_fallback(
        lambda: fetch_fund_flow(client, code), lambda: _sina_fund_flow(client, code), "个股资金流"
    )
    return rows


def fetch_margin(client: ResilientHttpClient, code: str) -> list[dict[str, Any]]:
    rows = _datacenter(client, "RPTA_WEB_RZRQ_GGMX", f'(SCODE="{code}")', 10, "DATE", "-1")
    return [
        {"date": str(row.get("DATE") or "")[:10], "balance": _float(row.get("RZYE")), "buy": _float(row.get("RZMRE"))}
        for row in rows
    ]


def fetch_dragon_tiger(client: ResilientHttpClient, code: str, trade_date: str) -> dict[str, Any]:
    start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    records = _datacenter(
        client, "RPT_DAILYBILLBOARD_DETAILSNEW",
        f'(TRADE_DATE>=\'{start}\')(TRADE_DATE<=\'{trade_date}\')(SECURITY_CODE="{code}")',
        30, "TRADE_DATE", "-1",
    )
    if not records:
        return {"records": [], "institution_net_wan": 0.0}
    latest = str(records[0].get("TRADE_DATE") or "")[:10]
    buy_rows = _datacenter(
        client, "RPT_BILLBOARD_DAILYDETAILSBUY",
        f'(TRADE_DATE=\'{latest}\')(SECURITY_CODE="{code}")', 20, "BUY", "-1",
    )
    sell_rows = _datacenter(
        client, "RPT_BILLBOARD_DAILYDETAILSSELL",
        f'(TRADE_DATE=\'{latest}\')(SECURITY_CODE="{code}")', 20, "SELL", "-1",
    )
    institution_buy = sum(_float(row.get("BUY")) for row in buy_rows if str(row.get("OPERATEDEPT_CODE") or "") == "0")
    institution_sell = sum(_float(row.get("SELL")) for row in sell_rows if str(row.get("OPERATEDEPT_CODE") or "") == "0")
    return {
        "records": [
            {
                "date": str(row.get("TRADE_DATE") or "")[:10],
                "reason": row.get("EXPLANATION") or "",
                "net_buy_wan": round(_float(row.get("BILLBOARD_NET_AMT")) / 10000, 1),
            }
            for row in records
        ],
        "institution_net_wan": round((institution_buy - institution_sell) / 10000, 1),
    }


def _exchange_dragon_tiger(client: ResilientHttpClient, code: str, trade_date: str) -> dict[str, Any]:
    """Official-exchange fallback; institution seats are deliberately left unknown."""
    if code.startswith(("0", "3")):
        data = client.get_json(
            "https://www.szse.cn/api/report/ShowReport/data",
            params={"SHOWTYPE": "JSON", "CATALOGID": "1842_xxpl", "TABKEY": "tab1", "txtStart": trade_date,
                    "txtEnd": trade_date, "random": "0.9"},
            headers={"Referer": "https://www.szse.cn/disclosure/supervision/dealinfo/index.html"},
            source="深交所龙虎榜备源", fallback=True,
        )
        raw = data[0].get("data", []) if isinstance(data, list) and data else []
        records = [{"date": trade_date, "reason": row.get("plyy") or "", "net_buy_wan": None,
                    "source": "深交所官方"} for row in raw if str(row.get("zqdm") or "") == code]
    else:
        text = client.get_text(
            "https://query.sse.com.cn/infodisplay/showTradePublicFile.do",
            params={"jsonCallBack": "cb", "isPagination": "false", "dateTx": trade_date},
            headers={"Referer": "https://www.sse.com.cn/disclosure/diclosure/public/"},
            source="上交所龙虎榜备源", fallback=True,
        )
        # SSE returns a JSONP publication; preserve its verified availability but
        # never infer institution flow from unstructured bulletin text.
        records = [{"date": trade_date, "reason": "上交所龙虎榜公告（待人工复核）", "net_buy_wan": None,
                    "source": "上交所官方"}] if code in text else []
    return {"records": records, "institution_net_wan": None, "institution_verified": False}


def fetch_dragon_tiger_with_fallback(client: ResilientHttpClient, code: str, trade_date: str) -> dict[str, Any]:
    result, _ = client.call_with_fallback(
        lambda: fetch_dragon_tiger(client, code, trade_date),
        lambda: _exchange_dragon_tiger(client, code, trade_date), "龙虎榜",
    )
    return result


def calculate_risk_penalty(review: dict[str, Any]) -> dict[str, Any]:
    flags: list[str] = []
    verified = set(review.get("verified") or [])
    lockups = review.get("lockups") or [] if "lockups" in verified else []
    max_unlock = max((_float(row.get("ratio_pct")) for row in lockups), default=0.0)
    if max_unlock >= 5:
        unlock_penalty = 8
        flags.append(f"未来90日解禁比例最高{max_unlock:g}%")
    elif max_unlock >= 2:
        unlock_penalty = 5
        flags.append(f"未来90日存在{max_unlock:g}%解禁")
    elif max_unlock >= 0.5:
        unlock_penalty = 2
        flags.append("未来90日存在小规模解禁")
    else:
        unlock_penalty = 0

    risky_announcements = []
    for row in review.get("announcements") or []:
        title = str(row.get("title") or "")
        if "减持" in title and any(term in title for term in ("实施完毕", "完成")):
            continue
        if any(term in title for term in RISK_ANNOUNCEMENT_TERMS) and "announcements" in verified:
            risky_announcements.append({"title": title, "date": row.get("date", ""), "source": row.get("source", "")})
    announcement_penalty = min(6, len(risky_announcements) * 2)
    if risky_announcements:
        flags.append("近期公告含风险关键词")

    flow = review.get("fund_flow") or [] if "fund_flow" in verified else []
    total_flow = sum(_float(row.get("main_net")) for row in flow[-20:])
    negative_days = sum(_float(row.get("main_net")) < 0 for row in flow[-20:])
    flow_penalty = (1 if total_flow < 0 else 0) + (2 if negative_days >= 12 else 0)
    if flow_penalty:
        flags.append("近20日资金流偏弱")

    margin = review.get("margin") or [] if "margin" in verified else []
    margin_growth = None
    margin_penalty = 0
    if len(margin) >= 5 and _float(margin[-1].get("balance")) > 0:
        margin_growth = (_float(margin[0].get("balance")) / _float(margin[-1].get("balance")) - 1) * 100
        if margin_growth > 20:
            margin_penalty = 3
            flags.append("融资余额短期快速增加")

    institution_value = (review.get("dragon_tiger") or {}).get("institution_net_wan") if "dragon_tiger" in verified else None
    institution_net = _float(institution_value) if institution_value is not None else None
    institution_penalty = 3 if institution_net is not None and institution_net < 0 else 0
    if institution_penalty:
        flags.append("最近龙虎榜机构席位净卖出")

    capital_penalty = min(6, flow_penalty + margin_penalty + institution_penalty)
    total = min(20, unlock_penalty + announcement_penalty + capital_penalty)
    return {
        "risk_penalty": total,
        "unlock_penalty": unlock_penalty,
        "announcement_penalty": announcement_penalty,
        "capital_penalty": capital_penalty,
        "risk_flags": flags,
        "risky_announcements": risky_announcements[:5],
        "max_unlock_ratio_pct": max_unlock if "lockups" in verified else None,
        "fund_flow_20d": total_flow if flow else None,
        "margin_growth_pct": round(margin_growth, 1) if margin_growth is not None else None,
        "institution_net_wan": institution_net if (review.get("dragon_tiger") or {}).get("records") else None,
    }


def review_candidate(
    code: str,
    trade_date: str,
    client: ResilientHttpClient | None = None,
) -> dict[str, Any]:
    client = client or get_default_client()
    review: dict[str, Any] = {"code": code, "trade_date": trade_date, "errors": {}, "verified": []}

    def collect(name: str, fn, empty):
        try:
            value = fn()
            review["verified"].append(name)
            return value
        except Exception as exc:
            review["errors"][name] = str(exc)
            return empty

    review["lockups"] = collect("lockups", lambda: fetch_lockups(client, code, trade_date), [])
    review["announcements"] = collect("announcements", lambda: fetch_announcements(client, code, trade_date), [])
    review["fund_flow"] = collect("fund_flow", lambda: fetch_fund_flow_with_fallback(client, code), [])
    review["margin"] = collect("margin", lambda: fetch_margin(client, code), [])
    review["dragon_tiger"] = collect(
        "dragon_tiger", lambda: fetch_dragon_tiger_with_fallback(client, code, trade_date), {"records": [], "institution_net_wan": None}
    )
    review.update(calculate_risk_penalty(review))
    review["coverage"] = round(len(review["verified"]) / 5, 2)
    return review


def collect_risk_reviews(
    candidates: list[dict[str, Any]],
    trade_date: str,
    client: ResilientHttpClient | None = None,
) -> dict[str, dict[str, Any]]:
    client = client or get_default_client()
    result = {}
    for candidate in candidates:
        code = str(candidate.get("股票代码") or candidate.get("code") or "")[:6]
        if code:
            result[code] = review_candidate(code, trade_date, client)
    return result
