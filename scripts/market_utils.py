"""A-share code routing and exchange-calendar helpers shared by data adapters."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


SH_INDEX_CODES = {"000010", "000016", "000300", "000688", "000852", "000905"}


def normalize_code(value: Any) -> str:
    """Return a six-digit security code from common A-share ticker spellings."""
    text = str(value or "").strip().lower()
    match = re.search(r"(\d{6})", text)
    return match.group(1) if match else ""


def market_prefix(value: Any) -> str:
    """Route an A-share code to Tencent/Sina's ``sh``/``sz``/``bj`` prefix.

    An explicit prefix wins, which keeps ``sh000001`` (Shanghai Composite) distinct
    from ``sz000001`` (Ping An Bank).
    """
    raw = str(value or "").strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        return raw[:2]
    if raw.endswith((".sh", ".sz", ".bj")):
        return raw[-2:]
    code = normalize_code(raw)
    if code.startswith("92") or code.startswith(("4", "8")):
        return "bj"
    if code in SH_INDEX_CODES or code.startswith(("5", "6", "9")):
        return "sh"
    return "sz"


def eastmoney_secid(value: Any) -> str:
    """Build Eastmoney's market.code identifier for stocks, ETFs and indices."""
    code = normalize_code(value)
    prefix = market_prefix(value)
    return f"{1 if prefix == 'sh' else 0 if prefix == 'sz' else 0}.{code}"


def parse_sina_trade_calendar(text: str) -> set[str]:
    """Parse Sina's public Shanghai trading-day list into ISO date strings."""
    return {
        f"{item[:4]}-{item[4:6]}-{item[6:8]}"
        for item in re.findall(r"\b(\d{8})\b", text or "")
        if item.startswith(("19", "20"))
    }


def is_listed_trade_day(value: date | datetime, calendar: set[str]) -> bool:
    """Return true only when a date is present in the verified exchange calendar."""
    day = value.date() if isinstance(value, datetime) else value
    return day.isoformat() in calendar


def is_xshg_trade_day(value: date | datetime) -> bool:
    """Check the Shanghai Stock Exchange calendar bundled by exchange_calendars."""
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError("缺少 exchange_calendars，无法核验 A 股交易日") from exc
    day = value.date() if isinstance(value, datetime) else value
    return bool(xcals.get_calendar("XSHG").is_session(day.isoformat()))
