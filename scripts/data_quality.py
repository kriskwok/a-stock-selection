"""Candidate-level confidence, composite ranking and source coverage summaries."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def confidence_for_candidate(
    technical_complete: bool,
    market: dict[str, Any] | None,
    valuation: dict[str, Any] | None,
    risk: dict[str, Any] | None,
) -> dict[str, Any]:
    market = market or {}
    valuation = valuation or {}
    risk = risk or {}
    technical_coverage = 1.0 if technical_complete else 0.0
    market_coverage = float(market.get("coverage") or 0.0)
    valuation_coverage = float(valuation.get("valuation_coverage") or 0.0)
    if valuation_coverage and int(valuation.get("analyst_count") or 0) < 3:
        valuation_coverage *= 0.5
    risk_coverage = float(risk.get("coverage") or 0.0)
    score = round(
        technical_coverage * 40
        + market_coverage * 15
        + valuation_coverage * 20
        + risk_coverage * 25,
        1,
    )
    if score >= 80:
        grade = "高"
    elif score >= 60:
        grade = "中"
    else:
        grade = "低"
    missing = []
    if not technical_complete:
        missing.append("技术核心字段")
    if not market_coverage:
        missing.append("市场题材")
    if valuation_coverage < 0.5:
        missing.append("成长估值")
    if risk_coverage < 0.8:
        missing.append("事件风险复核")
    return {"confidence_score": score, "confidence_grade": grade, "missing_dimensions": missing}


def compose_rankings(
    technical_candidates: list[dict[str, Any]],
    market_context: dict[str, Any],
    valuations: dict[str, dict[str, Any]],
    risks: dict[str, dict[str, Any]],
    formal: bool,
) -> list[dict[str, Any]]:
    resonance = market_context.get("theme_resonance") or {}
    combined = []
    for technical_rank, candidate in enumerate(technical_candidates, 1):
        code = str(candidate.get("股票代码") or "")[:6]
        market = resonance.get(code, {})
        market_score = market.get("market_theme_score")
        valuation = valuations.get(code, {})
        valuation_score = valuation.get("growth_valuation_score")
        risk = risks.get(code, {})
        risk_penalty = float(risk.get("risk_penalty") or 0.0)
        technical_score = float(candidate.get("技术结构分") or candidate.get("综合研究分") or 0.0)
        # Missing research dimensions must not become arbitrary neutral scores.
        # Re-normalise only verified positive-score components and expose the result as provisional.
        components = [(technical_score, 0.65)]
        if market_score is not None:
            components.append((float(market_score), 0.15))
        if valuation_score is not None:
            components.append((float(valuation_score), 0.20))
        component_weight = sum(weight for _, weight in components)
        composite = max(0.0, min(100.0, sum(value * weight for value, weight in components) / component_weight - risk_penalty))
        confidence = confidence_for_candidate(formal, market, valuation, risk)
        item = dict(candidate)
        item.update(
            {
                "技术排序": technical_rank,
                "技术结构分": round(technical_score, 1),
                "市场题材分": round(float(market_score), 1) if market_score is not None else None,
                "成长估值分": round(float(valuation_score), 1) if valuation_score is not None else None,
                "风险扣分": round(risk_penalty, 1),
                "综合研究分": round(composite, 1),
                "风险标签": "；".join(risk.get("risk_flags") or []) or ("暂无显著信号" if risk.get("coverage") else "未验证"),
                "估值覆盖": valuation.get("coverage_label") or "未取得",
                "评分状态": "正式综合研究分" if formal and component_weight == 1.0 else "暂定综合研究分",
                **confidence,
            }
        )
        combined.append(item)
    combined.sort(key=lambda row: (row["综合研究分"], row["技术结构分"]), reverse=True)
    for index, row in enumerate(combined, 1):
        row["综合排名"] = index
        row["排名"] = index
    return combined


def source_matrix(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"source": "", "requests": 0, "success": 0, "failed": 0, "fallback": 0, "latest_at": "", "last_error": ""}
    )
    for event in events:
        source = str(event.get("source") or "unknown")
        row = grouped[source]
        row["source"] = source
        row["requests"] += 1
        row["latest_at"] = max(str(row.get("latest_at") or ""), str(event.get("timestamp") or ""))
        status = str(event.get("status") or "")
        if status in {"success", "fallback_success"}:
            row["success"] += 1
        else:
            row["failed"] += 1
        if event.get("fallback") or status == "fallback_success":
            row["fallback"] += 1
        if event.get("error"):
            row["last_error"] = str(event["error"])
    return sorted(grouped.values(), key=lambda row: row["source"])
