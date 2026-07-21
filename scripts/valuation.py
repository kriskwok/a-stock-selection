"""Consensus EPS, forward valuation and cross-sectional growth scoring."""

from __future__ import annotations

import math
import statistics
from io import StringIO
from typing import Any

import pandas as pd

from data_sources import ResilientHttpClient, get_default_client


def _float(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def forward_pe(price: float | None, eps: float | None) -> float | None:
    if price is None or eps is None or price <= 0 or eps <= 0:
        return None
    return price / eps


def peg_ratio(pe: float | None, eps_growth_pct: float | None) -> float | None:
    if pe is None or eps_growth_pct is None or pe <= 0 or eps_growth_pct <= 0:
        return None
    return pe / eps_growth_pct


def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if isinstance(result.columns, pd.MultiIndex):
        result.columns = [" ".join(str(part) for part in col if str(part) != "nan") for col in result.columns]
    else:
        result.columns = [str(col) for col in result.columns]
    return result


def _pick_column(columns: list[str], *terms: str) -> str | None:
    for column in columns:
        if all(term in column for term in terms):
            return column
    return None


def fetch_ths_consensus(client: ResilientHttpClient, code: str) -> dict[str, Any]:
    text = client.get_text(
        f"https://basic.10jqka.com.cn/new/{code}/worth.html",
        encoding="gbk",
        headers={"Referer": "https://basic.10jqka.com.cn/"},
        source="同花顺一致预期",
    )
    frames = [_flatten_columns(frame) for frame in pd.read_html(StringIO(text))]
    target = None
    for frame in frames:
        joined = " ".join(frame.columns)
        if "均值" in joined and ("每股收益" in joined or "预测机构数" in joined):
            target = frame
            break
    if target is None or target.empty:
        raise ValueError("未找到一致预期 EPS 表格")
    columns = list(target.columns)
    mean_col = _pick_column(columns, "均值")
    count_col = _pick_column(columns, "预测机构数") or _pick_column(columns, "机构数")
    year_col = _pick_column(columns, "年度") or columns[0]
    records = []
    for _, row in target.iterrows():
        eps = _float(row.get(mean_col)) if mean_col else None
        if eps is None:
            continue
        records.append({
            "year": str(row.get(year_col) or ""),
            "eps": eps,
            "analyst_count": int(_float(row.get(count_col)) or 0) if count_col else 0,
        })
    if not records:
        raise ValueError("一致预期表没有可用 EPS")
    return {
        "eps_current": records[0]["eps"],
        "eps_next": records[1]["eps"] if len(records) > 1 else None,
        "analyst_count": records[0]["analyst_count"],
        "forecast_years": [row["year"] for row in records[:2]],
        "source": "同花顺一致预期",
    }


def fetch_eastmoney_consensus(client: ResilientHttpClient, code: str) -> dict[str, Any]:
    data = client.get_json(
        "https://reportapi.eastmoney.com/report/list",
        params={
            "industryCode": "*", "pageSize": "50", "industry": "*", "rating": "*",
            "ratingChange": "*", "beginTime": "2024-01-01", "endTime": "2030-01-01",
            "pageNo": "1", "qType": "0", "code": code,
        },
        headers={"Referer": "https://data.eastmoney.com/"},
        source="东方财富研报一致预期", fallback=True,
    )
    rows = data.get("data") or []
    current = [_float(row.get("predictThisYearEps")) for row in rows]
    following = [_float(row.get("predictNextYearEps")) for row in rows]
    current = [value for value in current if value is not None]
    following = [value for value in following if value is not None]
    if not current:
        raise ValueError("研报数据没有 EPS 预测")
    institutions = {str(row.get("orgSName") or "") for row in rows if row.get("orgSName")}
    return {
        "eps_current": statistics.median(current),
        "eps_next": statistics.median(following) if following else None,
        "analyst_count": len(institutions),
        "forecast_years": [],
        "source": "东方财富研报中位数",
    }


def fetch_consensus(client: ResilientHttpClient, code: str) -> dict[str, Any]:
    result, route = client.call_with_fallback(
        lambda: fetch_ths_consensus(client, code),
        lambda: fetch_eastmoney_consensus(client, code),
        "一致预期",
    )
    result["fallback"] = route == "fallback"
    return result


def _percentile(value: float | None, values: list[float], higher_better: bool = True) -> float | None:
    if value is None or not values:
        return None
    if len(values) == 1:
        return 50.0
    ordered = sorted(values)
    rank = sum(item < value for item in ordered) + 0.5 * sum(item == value for item in ordered)
    percentile = rank / len(ordered) * 100
    return percentile if higher_better else 100 - percentile


def score_valuations(records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    metric_rules = {
        "eps_growth_pct": True,
        "fundamental_growth_pct": True,
        "forward_pe": False,
        "peg": False,
    }
    pools = {
        key: [float(row[key]) for row in records.values() if row.get(key) is not None and float(row[key]) > 0]
        for key in metric_rules
    }
    weights = {"eps_growth_pct": 0.30, "fundamental_growth_pct": 0.40, "forward_pe": 0.15, "peg": 0.15}
    for row in records.values():
        components = []
        for metric, higher_better in metric_rules.items():
            value = row.get(metric)
            percentile = _percentile(value, pools[metric], higher_better)
            if percentile is not None:
                components.append((percentile, weights[metric]))
        if components:
            row["growth_valuation_score"] = round(
                sum(value * weight for value, weight in components) / sum(weight for _, weight in components), 1
            )
        else:
            row["growth_valuation_score"] = None
        row["valuation_coverage"] = round(len(components) / len(metric_rules), 2)
        row["coverage_label"] = "低覆盖" if int(row.get("analyst_count") or 0) < 3 else "正常覆盖"
    return records


def collect_valuations(
    candidates: list[dict[str, Any]],
    price_by_code: dict[str, float | None],
    growth_by_code: dict[str, dict[str, Any]],
    client: ResilientHttpClient | None = None,
) -> dict[str, dict[str, Any]]:
    client = client or get_default_client()
    records: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        code = str(candidate.get("股票代码") or candidate.get("code") or "")[:6]
        if not code:
            continue
        growth = growth_by_code.get(code, {})
        revenue = _float(growth.get("revenue_yoy"))
        profit = _float(growth.get("profit_yoy"))
        available_growth = [value for value in (revenue, profit) if value is not None]
        row: dict[str, Any] = {
            "code": code,
            "price": _float(price_by_code.get(code)),
            "revenue_yoy": revenue,
            "profit_yoy": profit,
            "fundamental_growth_pct": statistics.mean(available_growth) if available_growth else None,
            "errors": [],
        }
        try:
            row.update(fetch_consensus(client, code))
        except Exception as exc:
            row.update({"eps_current": None, "eps_next": None, "analyst_count": 0, "source": "未取得"})
            row["errors"].append(str(exc))
        eps_current, eps_next = row.get("eps_current"), row.get("eps_next")
        if eps_current is not None and eps_next is not None and eps_current > 0:
            row["eps_growth_pct"] = (eps_next / eps_current - 1) * 100
        else:
            row["eps_growth_pct"] = None
        row["forward_pe"] = forward_pe(row.get("price"), eps_current)
        row["peg"] = peg_ratio(row.get("forward_pe"), row.get("eps_growth_pct"))
        records[code] = row
    return score_valuations(records)
