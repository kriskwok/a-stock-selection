import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from data_quality import compose_rankings, source_matrix
from data_sources import SourceEvent, get_default_client
from market_utils import eastmoney_secid, is_xshg_trade_day, market_prefix, normalize_code
from market_context import collect_market_context
from risk_review import collect_risk_reviews
from valuation import collect_valuations


ROOT = Path.cwd()
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
DEFAULT_OUTPUT_DIR = Path(
    os.environ.get(
        "A_STOCK_SELECTION_OUTPUT_DIR",
        str(Path.home() / "Documents" / "001" / "自动选股"),
    )
)
NEWS_LOOKBACK_DAYS = 3
FREE_HTTP_TIMEOUT = 15
FREE_HTTP_RETRIES = 2
FREE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
EXPECTED_STOCK_COUNT = 11
MAX_CANDIDATES_FOR_TRENDS = 50
MIN_MARKET_CAP_YI = 100.0
MIN_DAILY_AMOUNT_WAN = 50000.0
MIN_VOLUME_RATIO_5D = 1.15
TREND_COMPLETENESS_MIN_RATIO = 0.6
HOT_STOCK_LIMIT = 10
BOARD_MOVER_LIMIT = 5
BOARD_MEMBER_LIMIT = 3
BLOCKED_NEWS_DOMAINS = (
    "guba.eastmoney.com",
    "xyhndec.cn",
)
HOT_TOPIC_MARKERS = (
    "A股",
    "上市公司",
    "板块",
    "概念",
    "产业链",
    "赛道",
    "涨停",
    "跌停",
    "主力资金",
    "政策",
    "订单",
    "业绩",
)
POSITIVE_HOTSPOT_TERMS = (
    "政策",
    "订单",
    "业绩",
    "景气",
    "资本开支",
    "Capex",
    "需求",
    "涨停",
    "突破",
    "国产替代",
    "产业链",
    "加速",
    "落地",
)
NEGATIVE_HOTSPOT_TERMS = (
    "退潮",
    "回调",
    "高位",
    "资金流出",
    "减持",
    "估值过高",
    "跌幅居前",
    "谨慎",
    "调整",
    "分化",
    "套现",
    "欺诈",
)
def load_keys():
    """Compatibility shim: the free-source workflow does not require API keys."""
    return None, None


def configure_output_dir(output_dir):
    global ROOT, DATA_DIR, REPORT_DIR
    ROOT = Path(output_dir).expanduser().resolve()
    DATA_DIR = ROOT / "data"
    REPORT_DIR = ROOT / "reports"


def free_http_json(url, headers=None, timeout=FREE_HTTP_TIMEOUT):
    response = get_default_client().request(
        "GET",
        url,
        headers={"Accept": "application/json,text/plain,*/*", **(headers or {})},
        timeout=timeout,
    )
    charset = response.encoding or response.apparent_encoding or "utf-8"
    return response.status_code, json.loads(response.content.decode(charset, errors="replace"))


def _free_news_item(title, summary, published_at, source, link=""):
    return {
        "title": compact_text(title),
        "snippet": compact_text(summary),
        "date": compact_text(published_at),
        "_published_at": compact_text(published_at),
        "source": compact_text(source),
        "link": compact_text(link),
    }


def _eastmoney_global_news(count=50):
    query = urllib.parse.urlencode(
        {
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "sortEnd": "",
            "pageSize": str(count),
            "req_trace": f"free-{int(time.time() * 1000)}",
        }
    )
    url = f"https://np-weblist.eastmoney.com/comm/web/getFastNewsList?{query}"
    _, data = free_http_json(url, {"Referer": "https://kuaixun.eastmoney.com/"})
    rows = (data.get("data") or {}).get("fastNewsList") or []
    return [
        _free_news_item(
            row.get("title"),
            row.get("summary") or row.get("content"),
            row.get("showTime") or row.get("time"),
            "东方财富全球资讯",
            row.get("url") or row.get("link"),
        )
        for row in rows
        if row.get("title") or row.get("summary")
    ]


def _cls_telegraph(count=50):
    params = {
        "appName": "CailianpressWeb",
        "last_time": "",
        "os": "web",
        "refresh_type": "1",
        "rn": str(count),
        "sv": "7.7.5",
    }
    query = urllib.parse.urlencode(params)
    signature_input = "&".join(f"{key}={params[key]}" for key in sorted(params))
    params["sign"] = hashlib.md5(hashlib.sha1(signature_input.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()
    url = "https://www.cls.cn/v1/roll/get_roll_list?" + urllib.parse.urlencode(params)
    _, data = free_http_json(url, {"Referer": "https://www.cls.cn/telegraph"})
    rows = (data.get("data") or {}).get("roll_data") or []
    return [
        _free_news_item(
            row.get("title") or row.get("brief"),
            row.get("content") or row.get("brief"),
            row.get("ctime") or row.get("time"),
            "财联社快讯",
        )
        for row in rows
        if row.get("title") or row.get("brief")
    ]


def _latest_ths_hot_rows(max_days=1, as_of=None):
    base_date = as_of or datetime.now()
    for offset in range(max_days):
        date = (base_date - timedelta(days=offset)).strftime("%Y-%m-%d")
        url = (
            "http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
        )
        try:
            _, data = free_http_json(url)
            rows = data.get("data") or []
            if rows:
                return date, rows
        except Exception as exc:
            print(f"warning: 同花顺热点请求失败({date}): {exc}", file=sys.stderr)
    return datetime.now().strftime("%Y-%m-%d"), []


def _ths_limit_up_reason_rows(trade_date=None, limit=200):
    """同花顺涨停揭秘备源：只取当日涨停原因，不依赖资讯新闻。"""
    trade_date = trade_date or datetime.now().strftime("%Y%m%d")
    params = {
        "page": 1,
        "limit": limit,
        "field": "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004",
        "filter": "HS,GEM2STAR",
        "order_field": "330324",
        "order_type": "0",
        "date": trade_date,
    }
    data = get_default_client().get_json(
        "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool",
        params=params,
        headers={"Referer": "https://data.10jqka.com.cn/limit_up/"},
        source="同花顺涨停揭秘",
    )
    info = ((data.get("data") or {}).get("info") or [])
    return [
        {
            "code": normalize_stock_code(row.get("code") or ""),
            "name": clean_stock_name(row.get("name") or ""),
            "reason": compact_text(row.get("reason_type") or ""),
            "题材归因": compact_text(row.get("reason_type") or ""),
            "source": "同花顺涨停揭秘",
        }
        for row in info
        if row.get("reason_type")
    ]


def free_search(query, count=12):
    """Return a normalized news response backed by free public endpoints."""
    items = []
    # Prefer the independent source; an Eastmoney-wide circuit must not poison
    # the candidate and trend stages before they start.
    for fetcher in (_cls_telegraph,):
        try:
            items.extend(fetcher(max(20, count * 3)))
        except Exception as exc:
            print(f"warning: 免费新闻源不可用: {exc}", file=sys.stderr)
    if not items:
        try:
            items.extend(_eastmoney_global_news(max(20, count * 3)))
        except Exception as exc:
            print(f"warning: 东财快讯备源不可用: {exc}", file=sys.stderr)
    try:
        hot_date, hot_rows = _latest_ths_hot_rows()
        for row in hot_rows[: max(20, count * 2)]:
            reason = row.get("reason") or row.get("题材归因") or ""
            name = row.get("name") or row.get("名称") or ""
            items.append(
                _free_news_item(
                    f"{name}：{reason}" if reason else name,
                    f"同花顺热点归因：{reason}" if reason else "当日强势股热点记录",
                    hot_date,
                    "同花顺热点",
                )
            )
    except Exception as exc:
        print(f"warning: 同花顺热点源不可用: {exc}", file=sys.stderr)
    return {"success": True, "result": [{"content": items[:count], "source": "free-public"}]}


def _stock_prefix(code):
    return market_prefix(code)


def _format_table(headers, rows):
    def cell(value):
        if value is None:
            return "未验证"
        if isinstance(value, float):
            if value != value:
                return ""
            if abs(value) >= 1000:
                return f"{value:,.0f}"
            return f"{value:.4f}".rstrip("0").rstrip(".")
        return compact_text(value).replace("|", "\\|")

    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(cell(row.get(header, "")) for header in headers) + " |")
    return "\n".join(lines)


def _parse_float(value):
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _tencent_quotes(codes):
    prefixed = [f"{_stock_prefix(code)}{normalize_code(code)}" for code in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    text = get_default_client().get_text(url, encoding="gbk", source="腾讯财经")
    result = {}
    for line in text.splitlines():
        match = re.search(r'v_(?:sh|sz|bj)(\d{6})="(.*?)";', line)
        if not match:
            continue
        code, payload = match.groups()
        values = payload.split("~")
        if len(values) < 50:
            continue
        result[code] = {
            "name": values[1],
            "price": _parse_float(values[3]),
            "pct_change": _parse_float(values[32]),
            "turnover_pct": _parse_float(values[38]),
            "amount_wan": _parse_float(values[37]),
            "volume": _parse_float(values[36]),
            "pe_ttm": _parse_float(values[39]),
            "float_mcap_yi": _parse_float(values[44]),
            "mcap_yi": _parse_float(values[45]),
            "pb": _parse_float(values[46]),
        }
    return result


def _eastmoney_history(code, days=260):
    secid = eastmoney_secid(code)
    params = urllib.parse.urlencode(
        {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "1",
            "beg": "0",
            "end": "20500000",
        }
    )
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{params}"
    _, data = free_http_json(url, {"Referer": "https://quote.eastmoney.com/"})
    rows = ((data.get("data") or {}).get("klines") or [])[-days:]
    parsed = []
    for row in rows:
        fields = row.split(",")
        if len(fields) < 7:
            continue
        close = _parse_float(fields[2])
        high = _parse_float(fields[3])
        pct = _parse_float(fields[8]) if len(fields) > 8 else None
        if close is not None:
            parsed.append({
                "date": fields[0],
                "close": close,
                "high": high,
                "volume": _parse_float(fields[5]) if len(fields) > 5 else None,
                "amount": _parse_float(fields[6]) if len(fields) > 6 else None,
                "pct": pct,
            })
    return parsed


def _baidu_history(code, days=260):
    params = urllib.parse.urlencode(
        {
            "all": "1",
            "isIndex": "false",
            "isBk": "false",
            "isBlock": "false",
            "isFutures": "false",
            "isStock": "true",
            "newFormat": "1",
            "group": "quotation_kline_ab",
            "finClientType": "pc",
            "code": code,
            "start_time": "",
            "ktype": "1",
        }
    )
    url = "https://finance.pae.baidu.com/selfselect/getstockquotation?" + params
    _, data = free_http_json(url, {"Origin": "https://gushitong.baidu.com", "Referer": "https://gushitong.baidu.com/"})
    if data.get("ResultCode") not in (0, "0", None):
        raise RuntimeError(f"百度K线返回 ResultCode={data.get('ResultCode')}")
    market = ((data.get("Result") or {}).get("newMarketData") or {})
    keys = market.get("keys") or []
    raw_rows = (market.get("marketData") or "").split(";")
    index = {key: position for position, key in enumerate(keys)}
    parsed = []
    for raw in raw_rows[-days:]:
        fields = raw.split(",")
        try:
            date = fields[index.get("time", 0)]
            close = _parse_float(fields[index["close"]])
            high = _parse_float(fields[index["high"]])
        except (KeyError, IndexError):
            continue
        if close is not None:
            parsed.append({
                "date": date,
                "close": close,
                "high": high,
                "volume": _parse_float(fields[index["volume"]]) if "volume" in index and len(fields) > index["volume"] else None,
                "amount": _parse_float(fields[index["amount"]]) if "amount" in index and len(fields) > index["amount"] else None,
                "pct": None,
            })
    if not parsed:
        raise RuntimeError("百度K线响应未包含可解析日线")
    return parsed


def _parse_ths_history_payload(text, days=260):
    """Parse 10jqka's JSONP daily-line payload into the common K-line schema."""
    match = re.search(r"\((\{.*\})\)\s*$", text, re.S)
    if not match:
        raise RuntimeError("同花顺K线响应不是预期 JSONP")
    data = json.loads(match.group(1))
    raw_rows = str(data.get("data") or "").split(";")
    parsed = []
    for raw in raw_rows[-days:]:
        fields = raw.split(",")
        if len(fields) < 7:
            continue
        close = _parse_float(fields[4])
        high = _parse_float(fields[2])
        if close is None or high is None:
            continue
        parsed.append(
            {
                "date": fields[0],
                "close": close,
                "high": high,
                "volume": _parse_float(fields[5]),
                "amount": _parse_float(fields[6]),
                "pct": None,
            }
        )
    if not parsed:
        raise RuntimeError("同花顺K线响应未包含可解析日线")
    return parsed


def _ths_history(code, days=260):
    """Independent daily K-line fallback recommended by reference.md."""
    text = get_default_client().get_text(
        f"https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js",
        headers={"Referer": "https://stock.10jqka.com.cn/", "Accept": "*/*"},
        source="同花顺K线",
    )
    return _parse_ths_history_payload(text, days=days)


def _mootdx_history(code, days=260):
    """Last-resort unadjusted K-line source; never drives formal return scoring."""
    from mootdx.quotes import Quotes

    servers = [
        ("119.97.185.59", 7709), ("124.70.133.119", 7709), ("116.205.183.150", 7709),
        ("123.60.73.44", 7709), ("116.205.163.254", 7709), ("121.36.225.169", 7709),
        ("123.60.70.228", 7709), ("124.71.9.153", 7709),
    ]
    started = time.monotonic()
    last_error = None
    for server in servers:
        try:
            with socket.create_connection(server, timeout=1.5):
                pass
            client = Quotes.factory(market="std", server=server)
            # A TCP handshake is a false-positive on some retired TDX servers.
            # Validate with a real A-share K-line request before selecting it.
            probe = client.bars(symbol="000001", frequency=9, offset=1)
            if probe is None or probe.empty:
                continue
            frame = client.bars(symbol=normalize_code(code), frequency=9, offset=days)
            if frame is None or frame.empty:
                continue
            parsed = []
            for _, row in frame.sort_values("datetime").iterrows():
                parsed.append({
                    "date": str(row.get("datetime") or "")[:10],
                    "close": _parse_float(row.get("close")),
                    "high": _parse_float(row.get("high")),
                    "volume": _parse_float(row.get("vol")),
                    "amount": _parse_float(row.get("amount")),
                    "pct": None,
                    "unadjusted": True,
                })
            get_default_client().events.append(
                SourceEvent(
                    source="通达信mootdx备源", url=f"tcp://{server[0]}:{server[1]}",
                    timestamp=datetime.now().isoformat(timespec="seconds"), status="fallback_success",
                    fallback=True, elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
            return parsed
        except Exception as exc:
            last_error = exc
    # The library's own best-IP path is only a final fallback: clean installs can
    # have an empty BESTIP configuration, so it must not be the primary route.
    for kwargs in ({"bestip": True}, {}):
        try:
            client = Quotes.factory(market="std", **kwargs)
            probe = client.bars(symbol="000001", frequency=9, offset=1)
            frame = client.bars(symbol=normalize_code(code), frequency=9, offset=days)
            if probe is None or probe.empty or frame is None or frame.empty:
                continue
            return [
                {"date": str(row.get("datetime") or "")[:10], "close": _parse_float(row.get("close")),
                 "high": _parse_float(row.get("high")), "volume": _parse_float(row.get("vol")),
                 "amount": _parse_float(row.get("amount")), "pct": None, "unadjusted": True}
                for _, row in frame.sort_values("datetime").iterrows()
            ]
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"mootdx 备源不可用: {last_error}")


def _trend_metrics(code, quote):
    # Eastmoney push2/push2his commonly share an IP-level circuit. Trend eligibility
    # must therefore be sourced from independent K-line providers before touching it.
    source = "同花顺K线独立备源"
    adjusted = True
    try:
        history = _ths_history(code, 260)
        if len(history) < 60:
            raise ValueError("同花顺K线样本不足60条")
    except Exception as ths_error:
        try:
            history = _baidu_history(code, 260)
            if len(history) < 60:
                raise ValueError("百度K线样本不足60条")
            source = "百度K线备源"
            get_default_client().events.append(
                SourceEvent(
                    source="K线", url="fallback", timestamp=datetime.now().isoformat(timespec="seconds"),
                    status="fallback_success", fallback=True, error=str(ths_error)[:240],
                )
            )
        except Exception as baidu_error:
            try:
                history = _eastmoney_history(code, 260)
                if len(history) < 60:
                    raise ValueError("东方财富前复权K线样本不足60条")
                source = "东方财富前复权K线"
            except Exception as eastmoney_error:
                try:
                    history = _mootdx_history(code, 260)
                    source = "通达信mootdx不复权备源"
                    adjusted = False
                except Exception:
                    raise RuntimeError(
                        f"K线独立源均不可用：同花顺={ths_error}; 百度={baidu_error}; 东方财富={eastmoney_error}"
                    ) from eastmoney_error
    closes = [row["close"] for row in history if row.get("close") is not None]
    highs = [row["high"] for row in history if row.get("high") is not None]
    current = quote.get("price") or (closes[-1] if closes else None)
    def period_return(period):
        if not adjusted:
            return None
        if len(closes) <= period:
            return None
        return (closes[-1] / closes[-period - 1] - 1) * 100
    ma5 = sum(closes[-5:]) / 5 if adjusted and len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if adjusted and len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if adjusted and len(closes) >= 20 else None
    ma60 = sum(closes[-60:]) / 60 if adjusted and len(closes) >= 60 else None
    high_52w = max(highs[-250:]) if adjusted and highs else None
    distance = ((current / high_52w) - 1) * 100 if current and high_52w else None
    volumes = [row.get("volume") for row in history if row.get("volume") is not None]
    amounts = [row.get("amount") for row in history if row.get("amount") is not None]
    volume_ratio_5d = None
    amount_ratio_5d = None
    if len(volumes) >= 25:
        recent = sum(volumes[-5:]) / 5
        baseline = sum(volumes[-25:-5]) / 20
        volume_ratio_5d = recent / baseline if baseline else None
    if len(amounts) >= 25:
        recent = sum(amounts[-5:]) / 5
        baseline = sum(amounts[-25:-5]) / 20
        amount_ratio_5d = recent / baseline if baseline else None
    return {
        "history": history,
        "close": current,
        "return_5": period_return(5),
        "return_10": period_return(10),
        "return_20": period_return(20),
        "return_60": period_return(60),
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "high_52w": high_52w,
        "distance_52w": distance,
        "volume_ratio_5d": volume_ratio_5d,
        "amount_ratio_5d": amount_ratio_5d,
        "history_source": source,
        "adjusted": adjusted,
    }


def _eastmoney_board_catalog(board_type="concept"):
    board_fs = {"industry": "m:90+t:2", "concept": "m:90+t:3"}[board_type]
    params = urllib.parse.urlencode({"pn": "1", "pz": "1000", "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f3", "fs": board_fs, "fields": "f12,f14,f3"})
    url = "https://push2.eastmoney.com/api/qt/clist/get?" + params
    _, data = free_http_json(url, {"Referer": "https://quote.eastmoney.com/"})
    return (data.get("data") or {}).get("diff") or []


def _eastmoney_board_members(board_code):
    params = urllib.parse.urlencode({"pn": "1", "pz": "500", "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f3", "fs": f"b:{board_code}", "fields": "f12,f14,f2,f3,f6,f20,f21"})
    url = "https://push2.eastmoney.com/api/qt/clist/get?" + params
    _, data = free_http_json(url, {"Referer": "https://quote.eastmoney.com/"})
    return (data.get("data") or {}).get("diff") or []


def _board_catalog_rows():
    rows = []
    for board_type in ("industry", "concept"):
        for raw in _eastmoney_board_catalog(board_type):
            change = _parse_float(raw.get("f3"))
            if raw.get("f12") and raw.get("f14") and change is not None:
                rows.append({"code": raw["f12"], "name": raw["f14"], "change_pct": change, "type": board_type})
    return rows


def _board_movers(limit=BOARD_MOVER_LIMIT, board_catalog=None):
    """Return today's strongest and weakest boards for candidate recall only."""
    rows = list(board_catalog) if board_catalog is not None else _board_catalog_rows()
    rows.sort(key=lambda row: row["change_pct"], reverse=True)
    selected = [dict(row, mover_side="涨幅前五") for row in rows[:limit]]
    selected += [dict(row, mover_side="跌幅前五") for row in rows[-limit:]]
    return list({row["code"]: row for row in selected}.values())


def _ths_hot_stocks(limit=HOT_STOCK_LIMIT):
    data = get_default_client().get_json(
        "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
        params={"stock_type": "a", "type": "hour", "list_type": "normal"},
        headers={"Referer": "https://q.10jqka.com.cn/"},
        source="同花顺热股",
    )
    raw_rows = (data.get("data") or {}).get("stock_list") or []
    rows = []
    for row in raw_rows[:limit]:
        raw_tags = ((row.get("tag") or {}).get("concept_tag") or [])[:3]
        tags = [compact_text(tag.get("name") if isinstance(tag, dict) else tag) for tag in raw_tags]
        rows.append({
            "股票代码": normalize_stock_code(row.get("code") or ""),
            "股票名称": clean_stock_name(row.get("name") or ""),
            "所属主题": "、".join(tag for tag in tags if tag),
            "热点触发": "同花顺热股前10",
            "候选来源": "同花顺热股",
        })
    return rows


def _xueqiu_hot_stocks(limit=HOT_STOCK_LIMIT):
    client = get_default_client()
    headers = {"Referer": "https://xueqiu.com/", "User-Agent": FREE_USER_AGENT}
    if os.environ.get("XUEQIU_COOKIE"):
        headers["Cookie"] = os.environ["XUEQIU_COOKIE"]
    client.request("GET", "https://xueqiu.com/", headers=headers, source="雪球热股入口")
    data = client.get_json(
        "https://stock.xueqiu.com/v5/stock/hot_stock/list.json",
        params={"_type": "10", "type": "12", "size": str(limit)},
        headers=headers,
        source="雪球热股",
    )
    raw_rows = (data.get("data") or {}).get("items") or []
    return [
        {
            "股票代码": normalize_stock_code(row.get("symbol") or row.get("code") or ""),
            "股票名称": clean_stock_name(row.get("name") or ""),
            "所属主题": "",
            "热点触发": "雪球热股前10",
            "候选来源": "雪球热股",
        }
        for row in raw_rows[:limit]
    ]


def _reason_themes(rows, limit=12):
    counts = {}
    for row in rows:
        reason = compact_text(row.get("reason") or row.get("题材归因") or "")
        for term in re.split(r"[+、，,;/|：:\s]+", reason):
            term = re.sub(r"(?:概念|板块)$", "", term).strip()
            if 2 <= len(term) <= 12 and not re.fullmatch(r"\d+(?:\.\d+)?%?", term):
                counts[term] = counts.get(term, 0) + 1
    return [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _theme_tags(text):
    """Normalize a source's concept labels into theme names."""
    tags = []
    for token in re.split(r"[+、，,;/|：:\s]+", compact_text(text or "")):
        token = re.sub(r"(?:概念|板块|产业链|赛道)$", "", token).strip()
        if 2 <= len(token) <= 16 and not re.fullmatch(r"\d+(?:\.\d+)?%?", token):
            tags.append(token)
    return list(dict.fromkeys(tags))


def dynamic_theme_names(
    ths_reason_rows=None,
    ths_hot_rows=None,
    xueqiu_hot_rows=None,
    board_catalog=None,
    board_movers=None,
    limit=12,
):
    """Build today's themes from market-data sources only; never from news text."""
    evidence = {}

    def add(name, source, weight=1, change=None):
        name = compact_text(name)
        if not name:
            return
        item = evidence.setdefault(name, {"sources": set(), "evidence": 0, "changes": []})
        item["sources"].add(source)
        item["evidence"] += weight
        if change is not None:
            item["changes"].append(change)

    for row in ths_reason_rows or []:
        for tag in _theme_tags(row.get("reason") or row.get("题材归因")):
            add(tag, row.get("source") or "同花顺热点归因")
    for row in ths_hot_rows or []:
        for tag in _theme_tags(row.get("所属主题") or row.get("concepts")):
            add(tag, "同花顺热股标签")
    for row in xueqiu_hot_rows or []:
        for tag in _theme_tags(row.get("所属主题") or row.get("concepts")):
            add(tag, "雪球热股标签")
    for row in board_movers or []:
        add(row.get("name"), "东方财富板块涨跌前五", weight=2, change=_parse_float(row.get("change_pct")))

    # Catalog is an Eastmoney naming dictionary. It is used only to retain
    # source labels when a reason/tag happens to match a board.
    catalog_names = {compact_text(row.get("name")): row for row in (board_catalog or []) if row.get("name")}
    for name, item in list(evidence.items()):
        for board_name, board in catalog_names.items():
            if name in board_name or board_name in name:
                item["sources"].add("东方财富板块目录")
                if board.get("change_pct") is not None:
                    item["changes"].append(_parse_float(board.get("change_pct")))
                break

    ranked = sorted(
        evidence.items(),
        key=lambda pair: (-len(pair[1]["sources"]), -pair[1]["evidence"], -max(pair[1]["changes"] or [float("-inf")]), pair[0]),
    )
    return [name for name, _ in ranked[:limit]]


def market_theme_rows(
    ths_reason_rows=None,
    ths_hot_rows=None,
    xueqiu_hot_rows=None,
    board_catalog=None,
    board_movers=None,
    limit=12,
):
    """Render source evidence for themes; this table is not a score input."""
    evidence = {}

    def add(name, source, weight=1, change=None, side=""):
        name = compact_text(name)
        if not name:
            return
        item = evidence.setdefault(name, {"sources": set(), "evidence": 0, "changes": [], "sides": set()})
        item["sources"].add(source)
        item["evidence"] += weight
        if change is not None:
            item["changes"].append(change)
        if side:
            item["sides"].add(side)

    for row in ths_reason_rows or []:
        for tag in _theme_tags(row.get("reason") or row.get("题材归因")):
            add(tag, row.get("source") or "同花顺热点归因")
    for row in ths_hot_rows or []:
        for tag in _theme_tags(row.get("所属主题") or row.get("concepts")):
            add(tag, "同花顺热股标签")
    for row in xueqiu_hot_rows or []:
        for tag in _theme_tags(row.get("所属主题") or row.get("concepts")):
            add(tag, "雪球热股标签")
    for row in board_movers or []:
        add(row.get("name"), "东方财富板块涨跌前五", weight=2, change=_parse_float(row.get("change_pct")), side=row.get("mover_side") or "")

    catalog_names = {compact_text(row.get("name")): row for row in (board_catalog or []) if row.get("name")}
    for name, item in list(evidence.items()):
        for board_name, board in catalog_names.items():
            if name in board_name or board_name in name:
                item["sources"].add("东方财富板块目录")
                if board.get("change_pct") is not None:
                    item["changes"].append(_parse_float(board.get("change_pct")))
                break

    rows = []
    for theme in dynamic_theme_names(ths_reason_rows, ths_hot_rows, xueqiu_hot_rows, board_catalog, board_movers, limit=limit):
        item = evidence[theme]
        source_count = len(item["sources"])
        changes = item["changes"]
        if source_count >= 2:
            quality, action = "多源市场热点", "进入候选池，仍需综合评分"
        elif source_count == 1:
            quality, action = "单源市场热点", "进入候选池，提示一日游风险"
        else:
            quality, action = "待确认", "不作为独立依据"
        side_text = "、".join(sorted(item["sides"]))
        support = "、".join(sorted(item["sources"]))
        if side_text:
            support += f"；{side_text}"
        rows.append({
            "主题": theme,
            "热度次数": item["evidence"],
            "近24小时": "市场当日",
            "来源数": source_count,
            "验证状态": "多源市场数据" if source_count >= 2 else "单源市场数据",
            "热点质量分": round(50 + min(35, source_count * 12 + item["evidence"] * 2), 1),
            "质量判断": quality,
            "支撑因素": support,
            "风险信号": "跌幅前五/一日游风险" if "跌幅前五" in side_text else "暂无额外市场源风险",
            "选股处理": action,
        })
    return rows


def _sina_growth(code):
    prefix = market_prefix(code)
    params = urllib.parse.urlencode(
        {
            "paperCode": f"{prefix}{code}",
            "source": "lrb",
            "type": "0",
            "page": "1",
            "num": "8",
        }
    )
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022?" + params
    _, data = free_http_json(url)
    report_list = ((data.get("result") or {}).get("data") or {}).get("report_list") or {}
    if isinstance(report_list, list):
        reports = report_list
    else:
        reports = [report_list[key] for key in sorted(report_list, reverse=True)]
    for report in reports:
        items = report.get("data") if isinstance(report, dict) else None
        if not items:
            continue
        values = {}
        for item in items:
            title = compact_text(item.get("item_title"))
            if title:
                values[title] = item.get("item_tongbi")
        revenue = next((values[key] for key in values if "营业收入" in key and "同比" not in key), None)
        profit = next((values[key] for key in values if "净利润" in key and "同比" not in key), None)
        if revenue not in (None, "") or profit not in (None, ""):
            # Sina item_tongbi is a decimal ratio; normalise downstream values to percent.
            revenue_raw, profit_raw = _parse_float(revenue), _parse_float(profit)
            return {
                "revenue_yoy": revenue_raw * 100 if revenue_raw is not None else None,
                "profit_yoy": profit_raw * 100 if profit_raw is not None else None,
                "revenue_yoy_raw": revenue_raw,
                "profit_yoy_raw": profit_raw,
                "growth_unit": "ratio",
                "growth_source": "新浪财经 item_tongbi",
            }
    return {}


def free_candidates(
    themes=None,
    ths_reason_rows=None,
    board_movers=None,
    board_catalog=None,
    ths_hot_rows=None,
    xueqiu_hot_rows=None,
):
    """Build an unscored, current-day candidate pool from hot lists and boards."""
    if ths_reason_rows is None:
        _, ths_reason_rows = _latest_ths_hot_rows()
    themes = list(dict.fromkeys((themes or []) + _reason_themes(ths_reason_rows)))
    discovered = []
    hot_sources = (("同花顺热股", _ths_hot_stocks, ths_hot_rows), ("雪球热股", _xueqiu_hot_stocks, xueqiu_hot_rows))
    for label, fetcher, cached_rows in hot_sources:
        try:
            discovered.extend(cached_rows if cached_rows is not None else fetcher(HOT_STOCK_LIMIT))
        except Exception as exc:
            print(f"warning: {label}不可用，本轮透明降级: {exc}", file=sys.stderr)

    try:
        movers = board_movers if board_movers is not None else _board_movers()
        matched_boards = []
        catalog = list(board_catalog) if board_catalog is not None else _board_catalog_rows()
        for board in catalog:
            name = compact_text(board.get("name"))
            if name and any(theme and (theme in name or name in theme) for theme in themes):
                matched_boards.append({"code": board.get("code"), "name": name, "type": "当日热门题材"})
        for board in movers:
            direction = board.get("mover_side") or "板块涨跌前后五"
            matched_boards.append({"code": board["code"], "name": board["name"], "type": direction})
        seen_boards = set()
        for board in matched_boards:
            if not board.get("code") or board["code"] in seen_boards:
                continue
            seen_boards.add(board["code"])
            for member in _eastmoney_board_members(board["code"])[:BOARD_MEMBER_LIMIT]:
                discovered.append(
                    {
                        "股票代码": normalize_stock_code(member.get("f12") or ""),
                        "股票名称": clean_stock_name(member.get("f14") or ""),
                        "所属主题": board["name"],
                        "热点触发": f"{board['name']}（{board['type']}）成分股",
                        "候选来源": f"板块扩展-{board['type']}",
                    }
                )
    except Exception as exc:
        print(f"warning: 当日板块候选扩展失败，本轮保留热股榜候选: {exc}", file=sys.stderr)

    rows_by_code = {}
    order = []
    for raw in discovered:
        code = normalize_stock_code(raw.get("股票代码") or "")
        name = clean_stock_name(raw.get("股票名称") or "")
        if not code or not name:
            continue
        if code not in rows_by_code:
            rows_by_code[code] = {
                "股票代码": code,
                "股票名称": name,
                "所属主题": compact_text(raw.get("所属主题") or "当日热门"),
                "热点触发": compact_text(raw.get("热点触发") or "当日热门候选"),
                "候选来源": compact_text(raw.get("候选来源") or "当日热门"),
                "主营关联度": "待后续板块归属验证",
                "主要风险": "需通过技术、估值成长、舆情和资金等后续评分",
            }
            order.append(code)
        else:
            current = rows_by_code[code]
            current["候选来源"] = "、".join(dict.fromkeys((current["候选来源"] + "、" + compact_text(raw.get("候选来源") or "")).split("、")))
            if not current.get("所属主题") and raw.get("所属主题"):
                current["所属主题"] = compact_text(raw["所属主题"])
    rows = [rows_by_code[code] for code in order][:MAX_CANDIDATES_FOR_TRENDS]
    if not rows:
        return {"success": True, "result": [], "candidates": []}
    headers = ["股票代码", "股票名称", "所属主题", "候选来源", "热点触发", "主营关联度", "主要风险"]
    return {
        "success": True,
        "candidates": rows,
        "result": [{"query": "当日动态热点候选股", "content": _format_table(headers, rows), "status": "success", "source": "同花顺热股/雪球热股/当日板块涨跌幅"}],
    }


def enforce_candidate_constraints(rows):
    """Apply hard market-cap and liquidity constraints after any candidate fallback."""
    if not rows:
        return []
    codes = [normalize_stock_code(row.get("股票代码", "")).split(".")[0] for row in rows]
    try:
        quotes = _tencent_quotes(codes)
    except Exception as exc:
        print(f"warning: 无法验证市值/成交额门槛，暂不发布候选: {exc}", file=sys.stderr)
        return []
    selected = []
    for row in rows:
        code = normalize_stock_code(row.get("股票代码", ""))
        quote = quotes.get(code.split(".")[0], {})
        cap = _parse_float(row.get("市值(亿元)")) or quote.get("mcap_yi")
        amount = _parse_float(row.get("成交额(万元)")) or quote.get("amount_wan")
        if cap is None or cap < MIN_MARKET_CAP_YI or amount is None or amount < MIN_DAILY_AMOUNT_WAN:
            continue
        updated = dict(row)
        updated["市值(亿元)"] = cap
        updated["成交额(万元)"] = amount
        selected.append(updated)
    return selected[:MAX_CANDIDATES_FOR_TRENDS]


def free_trends(candidate_rows):
    codes = [normalize_stock_code(row.get("股票代码", "")).split(".")[0] for row in candidate_rows]
    try:
        quotes = _tencent_quotes(codes)
    except Exception as exc:
        print(f"warning: 腾讯行情源不可用: {exc}", file=sys.stderr)
        quotes = {}
    metrics = {}
    for row in candidate_rows:
        code = normalize_stock_code(row.get("股票代码", ""))
        bare = code.split(".")[0]
        quote = quotes.get(bare, {})
        try:
            metrics[code] = _trend_metrics(bare, quote)
        except Exception as exc:
            print(f"warning: 历史行情源不可用({code}): {exc}", file=sys.stderr)
            metrics[code] = {"history": []}
        metrics[code].update({"name": row.get("股票名称", ""), "quote": quote})

    history_sources = sorted({data.get("history_source") for data in metrics.values() if data.get("history_source")})
    history_source_label = "/".join(history_sources) or "历史K线源不可用"

    sections = []
    for label, key, title in (("5", "return_5", "最近5日涨跌幅"), ("10", "return_10", "最近10日涨跌幅"), ("20", "return_20", "最近20日涨跌幅"), ("60", "return_60", "最近60日涨跌幅")):
        rows = [{"股票代码": code, "股票名称": data["name"], f"{label}日涨跌幅(%)": data.get(key, "")} for code, data in metrics.items()]
        sections.append({"query": f"截至最新交易日{title}", "content": _format_table(["股票代码", "股票名称", f"{label}日涨跌幅(%)"], rows), "status": "success", "source": history_source_label})
    ma_rows = []
    for code, data in metrics.items():
        ma_rows.append({"股票代码": code, "股票名称": data["name"], "收盘价(元)": data.get("close", ""), "5日均线(元)": data.get("ma5", ""), "10日均线(元)": data.get("ma10", ""), "20日均线(元)": data.get("ma20", ""), "60日均线(元)": data.get("ma60", "")})
    sections.append({"query": "截至最新交易日5日10日20日和60日均线价格以及当日收盘价", "content": _format_table(["股票代码", "股票名称", "收盘价(元)", "5日均线(元)", "10日均线(元)", "20日均线(元)", "60日均线(元)"], ma_rows), "status": "success", "source": history_source_label})
    high_rows = []
    valuation_rows = []
    growth_rows = []
    for code, data in metrics.items():
        quote = data.get("quote", {})
        high_rows.append({"股票代码": code, "股票名称": data["name"], "收盘价_元": data.get("close", ""), "过去52周最高价_元": data.get("high_52w", ""), "距离52周高点位置_百分比": data.get("distance_52w", "")})
        valuation_rows.append({"股票代码": code, "股票名称": data["name"], "市盈率(TTM)": quote.get("pe_ttm", ""), "市净率": quote.get("pb", ""), "总市值(亿元)": quote.get("mcap_yi", ""), "成交额(万元)": quote.get("amount_wan", ""), "换手率(%)": quote.get("turnover_pct", ""), "5日量能比": data.get("volume_ratio_5d", "")})
        try:
            data["growth"] = _sina_growth(code.split(".")[0])
        except Exception as exc:
            print(f"warning: 新浪财报源不可用({code}): {exc}", file=sys.stderr)
            data["growth"] = {}
        growth = data["growth"]
        growth_rows.append({"股票代码": code, "股票名称": data["name"], "营业收入同比增速(%)": growth.get("revenue_yoy", ""), "净利润同比增速(%)": growth.get("profit_yoy", "")})
    sections.append({"query": "截至最新交易日当前股价与过去52周最高价", "content": _format_table(["股票代码", "股票名称", "收盘价_元", "过去52周最高价_元", "距离52周高点位置_百分比"], high_rows), "status": "success", "source": history_source_label})
    sections.append({"query": "免费行情估值指标市盈率市净率成交额量能", "content": _format_table(["股票代码", "股票名称", "市盈率(TTM)", "市净率", "总市值(亿元)", "成交额(万元)", "换手率(%)", "5日量能比"], valuation_rows), "status": "success", "source": "腾讯财经"})
    sections.append({"query": "营业收入同比和净利润同比成长与业绩", "content": _format_table(["股票代码", "股票名称", "营业收入同比增速(%)", "净利润同比增速(%)"], growth_rows), "status": "success", "source": "公开财报接口"})
    return {"success": True, "result": sections}


def extract_search_items(search_response):
    items = []
    for result in search_response.get("result", []):
        for item in result.get("content", []):
            if item.get("title") or item.get("snippet"):
                items.append(item)
    return items


def parse_item_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if text.isdigit() and len(text) in {10, 13}:
        try:
            timestamp = int(text) / (1000 if len(text) == 13 else 1)
            return datetime.fromtimestamp(timestamp)
        except (OverflowError, OSError, ValueError):
            pass
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).replace(tzinfo=None)
    except ValueError:
        pass

    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def latest_a_share_trade_datetime(now=None):
    current = now or datetime.now()
    for offset in range(15):
        candidate = current - timedelta(days=offset)
        try:
            if is_xshg_trade_day(candidate):
                return candidate
        except RuntimeError:
            break
    # Keep a conservative weekend fallback if the optional calendar cannot be
    # imported; the main workflow still performs strict verification first.
    if current.weekday() == 5:
        return current - timedelta(days=1)
    if current.weekday() == 6:
        return current - timedelta(days=2)
    return current


def verified_a_share_trade_date(now=None, allow_previous=False):
    """Verify today's session, or optionally return the latest prior session.

    A weekend-only calculation is unsafe around Chinese market holidays.  If the
    calendar dependency is unavailable, strict callers must not visit other market
    sources or produce an empty-date report.
    """
    current = now or datetime.now()
    if is_xshg_trade_day(current):
        return current
    if allow_previous:
        previous = latest_a_share_trade_datetime(current)
        if previous.date() != current.date() and is_xshg_trade_day(previous):
            return previous
    return None


def chinese_date(value):
    return f"{value.year}年{value.month}月{value.day}日"


def filter_fresh_news(items, today=None, lookback_days=NEWS_LOOKBACK_DAYS):
    today = today or datetime.now()
    start = today - timedelta(days=lookback_days)
    end = today + timedelta(days=1)
    filtered = []
    seen_titles = set()

    for item in items:
        published_at = parse_item_datetime(item.get("date"))
        if published_at is None or published_at < start or published_at > end:
            continue

        title = compact_text(item.get("title", ""))
        snippet = compact_text(item.get("snippet", ""))
        link = str(item.get("link") or "")
        if any(domain in link for domain in BLOCKED_NEWS_DOMAINS):
            continue
        if not title or not snippet:
            continue

        text = f"{title} {snippet}"
        irrelevant = ("围棋", "棋手", "人机大战", "足球", "篮球", "演唱会", "电影")
        market_context = ("A股", "沪", "深", "创业板", "科创", "涨停", "指数", "板块", "公司", "产业", "上市", "ETF")
        if any(term in text for term in irrelevant):
            continue
        if not (any(term in text for term in market_context) or any(term in text for term in HOT_TOPIC_MARKERS)):
            continue
        normalized_title = re.sub(r"\W+", "", title.lower())
        if normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)

        filtered.append(
            {
                **item,
                "_published_at": published_at.isoformat(),
                "_has_link": link.startswith(("https://", "http://")),
                "link": link if link.startswith(("https://", "http://")) else "",
            }
        )

    filtered.sort(key=lambda item: (item["_published_at"], item["_has_link"]), reverse=True)
    return filtered


def fresh_search_items(search_response):
    return filter_fresh_news(extract_search_items(search_response))


def combine_fresh_news(*item_groups, limit=12):
    combined = []
    seen_titles = set()
    for group in item_groups:
        for item in group:
            title = compact_text(item.get("title", ""))
            normalized_title = re.sub(r"\W+", "", title.lower())
            fingerprint = normalized_title + re.sub(r"\W+", "", compact_text(item.get("snippet", "")).lower())[:80]
            if fingerprint in seen_titles:
                continue
            seen_titles.add(fingerprint)
            combined.append(item)
    combined.sort(key=lambda item: item.get("_published_at", ""), reverse=True)
    return combined[:limit]


def compact_text(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def first_table_rows(text, limit=12):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells:
            rows.append(cells)
    if len(rows) <= 1:
        return rows
    return [rows[0]] + rows[1 : limit + 1]


def markdown_rows(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def rows_to_dicts(text):
    rows = markdown_rows(text)
    if len(rows) < 2:
        return []
    headers = rows[0]
    dicts = []
    for row in rows[1:]:
        padded = row + [""] * max(0, len(headers) - len(row))
        dicts.append(dict(zip(headers, padded[: len(headers)])))
    return dicts


def drop_markdown_columns(markdown_text, blocked_keywords):
    rows = markdown_rows(markdown_text)
    if len(rows) < 2:
        return markdown_text
    headers = rows[0]
    keep_indexes = [
        idx
        for idx, header in enumerate(headers)
        if not any(keyword in header for keyword in blocked_keywords)
    ]
    filtered_rows = [[row[idx] if idx < len(row) else "" for idx in keep_indexes] for row in rows]
    lines = [
        "| " + " | ".join(filtered_rows[0]) + " |",
        "| " + " | ".join("---" for _ in filtered_rows[0]) + " |",
    ]
    for row in filtered_rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def clean_missing_cell(value):
    text = compact_text(value)
    return text in {"", "-", "None", "none", "NULL", "null", "暂无", "未验证", "N/M"}


def normalize_stock_code(value):
    text = compact_text(value).upper()
    match = re.search(r"\b(SH|SZ|BJ)(\d{6})\b", text)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    match = re.search(r"(\d{6})\.(SH|SZ|BJ)", text)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.search(r"\b(\d{6})\b", text)
    if not match:
        return ""
    code = match.group(1)
    if code.startswith(("60", "68", "90")):
        suffix = "SH"
    elif code.startswith(("00", "30", "20")):
        suffix = "SZ"
    elif code.startswith(("43", "83", "87", "92")):
        suffix = "BJ"
    else:
        suffix = ""
    return f"{code}.{suffix}" if suffix else code


def row_stock_code(row):
    for key in ("股票代码", "证券代码", "wind_code", "代码"):
        code = normalize_stock_code(row.get(key, ""))
        if code:
            return code
    for value in row.values():
        code = normalize_stock_code(value)
        if code:
            return code
    return ""


def row_stock_name(row):
    for key in ("股票名称", "证券简称", "matched_company_name", "公司名称", "简称", "名称"):
        name = compact_text(row.get(key, ""))
        if name and not normalize_stock_code(name):
            return re.sub(r"(股份有限公司|科技股份有限公司|集团股份有限公司)$", "", name)
    return ""


def row_theme(row):
    for key in ("所属主题", "热点主题", "申万行业分类", "申万行业", "所属行业", "行业", "主营关联度"):
        value = compact_text(row.get(key, ""))
        if value:
            return value[:80]
    return "热点关联"


def dedupe_markdown_by_column(markdown_text, column_name):
    rows = markdown_rows(markdown_text)
    if len(rows) < 2 or column_name not in rows[0]:
        return markdown_text
    key_index = rows[0].index(column_name)
    kept = [rows[0]]
    seen = set()
    for row in rows[1:]:
        key = row[key_index] if key_index < len(row) else ""
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    lines = [
        "| " + " | ".join(kept[0]) + " |",
        "| " + " | ".join("---" for _ in kept[0]) + " |",
    ]
    for row in kept[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def drop_sparse_markdown_columns(markdown_text, max_missing_ratio=0.35):
    rows = markdown_rows(markdown_text)
    if len(rows) < 3:
        return markdown_text
    headers = rows[0]
    body = rows[1:]
    keep_indexes = []
    protected = {"Index", "股票代码", "股票名称", "所属主题", "申万行业"}
    for idx, header in enumerate(headers):
        if header in protected:
            keep_indexes.append(idx)
            continue
        values = [row[idx] if idx < len(row) else "" for row in body]
        missing_ratio = sum(clean_missing_cell(value) for value in values) / max(1, len(values))
        if missing_ratio <= max_missing_ratio:
            keep_indexes.append(idx)
    filtered_rows = [[row[idx] if idx < len(row) else "" for idx in keep_indexes] for row in rows]
    lines = [
        "| " + " | ".join(filtered_rows[0]) + " |",
        "| " + " | ".join("---" for _ in filtered_rows[0]) + " |",
    ]
    for row in filtered_rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def markdown_from_dict_rows(rows, headers):
    kept_rows = []
    for row in rows:
        values = [compact_text(row.get(header, "")) for header in headers]
        if any(not clean_missing_cell(value) for value in values):
            kept_rows.append(values)
    if not kept_rows:
        return ""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in kept_rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def compact_pct_change_range_markdown(section, rows):
    grouped = {}
    for row in rows:
        if row_item_key(row).lower() != "pct_change":
            continue
        value = extract_short_momentum(row)
        code = row_stock_code(row)
        name = clean_stock_name(row_stock_name(row))
        if value is None or not code:
            continue
        item = grouped.setdefault(code, {"股票代码": code, "股票名称": name, "values": [], "dates": []})
        item["values"].append(value)
        date = compact_text(row.get("time_scope_value", ""))
        if date:
            item["dates"].append(date)
    if not grouped:
        return ""

    field = momentum_field_for_text(section["query"])
    label_by_field = {
        "short_momentum": "5日累计涨跌幅(%)",
        "medium_momentum": "20日累计涨跌幅(%)",
        "long_momentum": "60日累计涨跌幅(%)",
    }
    value_label = label_by_field.get(field, "区间累计涨跌幅(%)")
    output_rows = []
    for item in grouped.values():
        cumulative = cumulative_percent(item["values"])
        dates = sorted(item["dates"])
        output_rows.append(
            {
                "股票代码": item["股票代码"],
                "股票名称": item["股票名称"],
                "统计区间": f"{dates[0]}->{dates[-1]}" if dates else "",
                value_label: f"{cumulative:.2f}" if cumulative is not None else "暂无",
                "样本数": len(item["values"]),
            }
        )
    output_rows.sort(key=lambda row: row["股票代码"])
    return markdown_from_dict_rows(output_rows, ["股票代码", "股票名称", "统计区间", value_label, "样本数"])


def compact_close_history_markdown(rows):
    grouped = {}
    for row in rows:
        if row_item_key(row).lower() != "close":
            continue
        value = row_metric_value(row)
        code = row_stock_code(row)
        name = clean_stock_name(row_stock_name(row))
        date = compact_text(row.get("time_scope_value", ""))
        if value is None or not code or not date:
            continue
        item = grouped.setdefault(code, {"股票代码": code, "股票名称": name, "history": []})
        item["history"].append((date, value))
    if not grouped:
        return ""

    output_rows = []
    for item in grouped.values():
        latest_first = [
            close
            for _, close in sorted(item["history"], key=lambda pair: pair[0], reverse=True)
        ]
        dates = sorted(date for date, _ in item["history"])
        ma20 = sum(latest_first[:20]) / 20 if len(latest_first) >= 20 else None
        ma60 = sum(latest_first[:60]) / 60 if len(latest_first) >= 60 else None
        output_rows.append(
            {
                "股票代码": item["股票代码"],
                "股票名称": item["股票名称"],
                "统计区间": f"{dates[0]}->{dates[-1]}" if dates else "",
                "最新收盘价(元)": f"{latest_first[0]:.2f}" if latest_first else "暂无",
                "本地20日均线(元)": f"{ma20:.2f}" if ma20 is not None else "暂无",
                "本地60日均线(元)": f"{ma60:.2f}" if ma60 is not None else "暂无",
                "样本数": len(latest_first),
            }
        )
    output_rows.sort(key=lambda row: row["股票代码"])
    return markdown_from_dict_rows(
        output_rows,
        ["股票代码", "股票名称", "统计区间", "最新收盘价(元)", "本地20日均线(元)", "本地60日均线(元)", "样本数"],
    )


def compact_fin_section_content(section):
    rows = rows_to_dicts(section["content"])
    if not rows:
        return ""

    if "涨跌幅" in section["query"] and any(row_item_key(row).lower() == "pct_change" for row in rows):
        compacted = compact_pct_change_range_markdown(section, rows)
        if compacted:
            return compacted

    if any(row_item_key(row).lower() == "close" and "range" in compact_text(row.get("subject_type", "")) for row in rows):
        compacted = compact_close_history_markdown(rows)
        if compacted:
            return compacted

    if {"wind_code", "item_name", "item_value"}.issubset(rows[0].keys()):
        compact_rows = []
        for row in rows:
            value = first_present(row, ("item_value", "指标值", "value"))
            if value is None:
                continue
            compact_rows.append(
                {
                    "股票代码": row_stock_code(row),
                    "股票名称": clean_stock_name(row_stock_name(row)),
                    "指标": row_item_name(row),
                    "数值": compact_text(value),
                    "单位": compact_text(row.get("item_unit", "")),
                    "时间": compact_text(row.get("time_scope_value", "")),
                }
            )
        return markdown_from_dict_rows(compact_rows, ["股票代码", "股票名称", "指标", "数值", "单位", "时间"])

    content = drop_markdown_columns(
        section["content"],
        (
            "Index",
            "entity_order",
            "comp_code",
            "source_table",
            "subject_type",
            "time_scope_type",
            "item_group",
            "item_key",
            "value_type",
            "rank_no",
            "context_type",
            "context_value",
        ),
    )
    return drop_sparse_markdown_columns(content, max_missing_ratio=0.2)


def parse_number(value):
    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def first_present(row, keys):
    for key in keys:
        value = row.get(key)
        if value is not None and not clean_missing_cell(value):
            return value
    return None


def first_key_containing(row, *needles):
    for key, value in row.items():
        if all(needle in key for needle in needles) and not clean_missing_cell(value):
            return value
    return None


def row_item_key(row):
    return compact_text(row.get("item_key") or row.get("指标代码") or row.get("指标") or "")


def row_item_name(row):
    return compact_text(row.get("item_name") or row.get("指标名称") or row_item_key(row))


TREND_MOMENTUM_KEYS = {"pct_change", "avg_pct_change"}
TREND_MOMENTUM_NAME_KEYWORDS = ("涨跌幅", "区间平均涨跌幅")
TREND_MOMENTUM_DIRECT_COLUMNS = (
    "最近5个交易日涨跌幅(%)",
    "最近5日涨跌幅(%)",
    "5日涨跌幅(%)",
    "近5日涨跌幅(%)",
    "最近10日涨跌幅(%)",
    "10日涨跌幅(%)",
    "近10日涨跌幅(%)",
    "最近20日涨跌幅(%)",
    "20日涨跌幅(%)",
    "近20日涨跌幅(%)",
    "最近60日涨跌幅(%)",
    "60日涨跌幅(%)",
    "近60日涨跌幅(%)",
    "涨跌幅(%)",
    "区间平均涨跌幅",
)
NON_MOMENTUM_KEYS = {
    "amount",
    "sum_amount",
    "avg_amount",
    "close",
    "open",
    "high",
    "low",
    "high_52w",
    "low_52w",
    "total_mv",
    "float_mv",
    "pe_ttm",
    "pb_lf",
    "ps_ttm",
}


def row_metric_value(row):
    return parse_number(first_present(row, ("item_value", "指标值", "value", "数值")))


def normalized_row_key_name(row):
    item_key = row_item_key(row).lower()
    item_name = row_item_name(row)
    return item_key, item_name, f"{item_key} {item_name}".lower()


def is_sane_percent(value, limit=80):
    return value is not None and -limit <= value <= limit


def cumulative_percent(values):
    multiplier = 1.0
    used = 0
    for value in values:
        if value is None:
            continue
        multiplier *= 1 + value / 100
        used += 1
    if not used:
        return None
    return (multiplier - 1) * 100


def direct_number(row, exact_keys=(), contains_all=()):
    value = first_present(row, exact_keys)
    if value is None and contains_all:
        value = first_key_containing(row, *contains_all)
    return parse_number(value)


def direct_close_value(row):
    return direct_number(
        row,
        (
            "收盘价_元",
            "当前股价_元",
            "当前收盘价_元",
            "当日收盘价(元)",
            "收盘价(元)",
            "当前股价(元)",
        ),
        ("收盘价",),
    )


def direct_ma20_value(row):
    return direct_number(
        row,
        ("20日均线价格(元)", "20日均线(元)", "20日均线_元"),
        ("20日均线",),
    )


def direct_ma5_value(row):
    return direct_number(row, ("5日均线价格(元)", "5日均线(元)", "5日均线_元"), ("5日均线",))


def direct_ma10_value(row):
    return direct_number(row, ("10日均线价格(元)", "10日均线(元)", "10日均线_元"), ("10日均线",))


def direct_ma60_value(row):
    return direct_number(
        row,
        ("60日均线价格(元)", "60日均线(元)", "60日均线_元"),
        ("60日均线",),
    )


def direct_high_52w_value(row):
    return direct_number(
        row,
        (
            "过去52周最高价_元",
            "52周最高价(元)",
            "52周最高价_元",
            "近1年最高价(元)",
            "过去52周最高价(元)",
        ),
        ("52周", "最高"),
    ) or direct_number(row, contains_all=("1年", "最高"))


def direct_market_cap_value(row):
    return direct_number(row, ("市值(亿元)", "总市值(亿元)", "总市值"), ("市值",))


def direct_amount_value(row):
    return direct_number(row, ("成交额(万元)", "成交额"), ("成交额",))


def direct_volume_ratio_value(row):
    return direct_number(row, ("5日量能比", "量能比", "成交量放大倍数"), ("量能",))


def momentum_field_for_text(text):
    text = compact_text(text)
    has_5 = bool(re.search(r"((最近|近)5(日|个交易日)|5(日|个交易日)(的)?(区间)?涨跌幅)", text))
    has_10 = bool(re.search(r"((最近|近)10(日|个交易日)|10(日|个交易日)(的)?(区间)?涨跌幅)", text))
    has_20 = bool(re.search(r"((最近|近)20(日|个交易日)|20(日|个交易日)(的)?(区间)?涨跌幅)", text))
    has_60 = bool(re.search(r"((最近|近)60(日|个交易日)|60(日|个交易日)(的)?(区间)?涨跌幅)", text))
    if sum([has_5, has_10, has_20, has_60]) > 1:
        return "short_momentum"
    if has_60:
        return "long_momentum"
    if has_20:
        return "medium_momentum"
    if has_10:
        return "momentum_10d"
    return "short_momentum"


def momentum_field_for_row(row, query=""):
    row_text = " ".join([row_item_key(row), row_item_name(row), " ".join(row.keys())])
    if re.search(r"((最近|近)(5|10|20|60)(日|个交易日)|(5|10|20|60)(日|个交易日)(的)?(区间)?涨跌幅)", row_text):
        return momentum_field_for_text(row_text)
    return momentum_field_for_text(query)


def extract_short_momentum(row):
    direct_value = parse_number(first_present(row, TREND_MOMENTUM_DIRECT_COLUMNS))
    if is_sane_percent(direct_value):
        return direct_value

    item_key, item_name, normalized = normalized_row_key_name(row)
    if item_key in NON_MOMENTUM_KEYS:
        return None
    if item_key not in TREND_MOMENTUM_KEYS and not any(keyword in item_name for keyword in TREND_MOMENTUM_NAME_KEYWORDS):
        return None
    value = row_metric_value(row)
    unit = compact_text(row.get("item_unit", ""))
    if unit and unit != "%" and "percent" not in compact_text(row.get("value_type", "")).lower():
        return None
    if not is_sane_percent(value):
        return None
    return value


def apply_momentum_row(item, row, query=""):
    value = extract_short_momentum(row)
    if value is None:
        return
    field = momentum_field_for_row(row, query=query)
    item_key = row_item_key(row).lower()
    time_scope_type = compact_text(row.get("time_scope_type", ""))
    subject_type = compact_text(row.get("subject_type", ""))
    if item_key == "pct_change" and time_scope_type == "trade_date" and "range" in subject_type:
        item.setdefault(f"_{field}_daily_changes", []).append(value)
        return
    item[field] = value
    if field == "short_momentum":
        item["short_momentum"] = value


def apply_market_metric_row(item, row, use_valuation=False):
    item_key, item_name, normalized = normalized_row_key_name(row)
    value = row_metric_value(row)
    time_scope_type = compact_text(row.get("time_scope_type", ""))
    subject_type = compact_text(row.get("subject_type", ""))

    close = direct_close_value(row)
    ma5 = direct_ma5_value(row)
    ma10 = direct_ma10_value(row)
    ma20 = direct_ma20_value(row)
    ma60 = direct_ma60_value(row)
    high_52w = direct_high_52w_value(row)
    market_cap = direct_market_cap_value(row)
    amount_wan = direct_amount_value(row)
    volume_ratio = direct_volume_ratio_value(row)
    if close is not None:
        item["close"] = close
    if ma5 is not None:
        item["ma5"] = ma5
    if ma10 is not None:
        item["ma10"] = ma10
    if ma20 is not None:
        item["ma20"] = ma20
    if ma60 is not None:
        item["ma60"] = ma60
    if high_52w is not None:
        item["high_52w"] = high_52w
    if market_cap is not None:
        item["market_cap_yi"] = market_cap
    if amount_wan is not None:
        item["amount_wan"] = amount_wan
    if volume_ratio is not None:
        item["volume_ratio_5d"] = volume_ratio

    if value is not None:
        if item_key == "close" and time_scope_type == "trade_date" and "range" in subject_type:
            date = compact_text(row.get("time_scope_value", ""))
            item.setdefault("_close_history", []).append((date, value))
            return
        if item_key == "close" or "收盘价" in item_name or "当前股价" in item_name:
            item["close"] = value
        elif item_key in {"ma5", "avg_close_5d"} or "5日均线" in item_name:
            item["ma5"] = value
        elif item_key in {"ma10", "avg_close_10d"} or "10日均线" in item_name:
            item["ma10"] = value
        elif item_key in {"ma20", "avg_close_20d"} or "20日均线" in item_name:
            item["ma20"] = value
        elif item_key in {"ma60", "avg_close_60d"} or "60日均线" in item_name:
            item["ma60"] = value
        elif item_key in {"high_52w", "max_high"} or "52周" in item_name or "1年最高" in item_name or "区间最高价" in item_name:
            item["high_52w"] = value
        elif use_valuation and (item_key == "pe_ttm" or "市盈率" in item_name):
            item["pe_ttm"] = value
        elif use_valuation and (item_key == "pb_lf" or "市净率" in item_name):
            item["pb"] = value

    ma20_distance = parse_number(row.get("偏离20日均线_百分比"))
    ma60_distance = parse_number(row.get("偏离60日均线_百分比"))
    distance_52w_high = parse_number(row.get("距离52周高点位置_百分比") or row.get("距52周最高价距离(%)"))
    if ma20_distance is not None:
        item["ma20_distance"] = ma20_distance
    if ma60_distance is not None:
        item["ma60_distance"] = ma60_distance
    if distance_52w_high is not None:
        item["distance_52w_high"] = distance_52w_high


def technical_data_quality(fin_trends_response):
    source_rows = []
    name_to_key = {}
    for section in extract_fin_sections(fin_trends_response):
        for row in rows_to_dicts(section["content"]):
            code = row_stock_code(row)
            name = clean_stock_name(row_stock_name(row))
            if code and name:
                name_to_key[name] = code
            source_rows.append((section["query"], row))

    coverage = {}
    for query, row in source_rows:
        code = row_stock_code(row)
        name = clean_stock_name(row_stock_name(row))
        key = code or name_to_key.get(name) or name
        if not key:
            continue
        item = coverage.setdefault(
            key,
            {
                "code": code or key,
                "name": name,
                "momentum": False,
                "momentum_5d": False,
                "momentum_10d": False,
                "momentum_20d": False,
                "momentum_60d": False,
                "ma5": False,
                "ma10": False,
                "ma20": False,
                "ma60": False,
                "close": False,
                "high_52w": False,
                "close_history": [],
            },
        )
        if extract_short_momentum(row) is not None:
            item["momentum"] = True
            field = momentum_field_for_row(row, query=query)
            if field == "short_momentum":
                item["momentum_5d"] = True
            elif field == "momentum_10d":
                item["momentum_10d"] = True
            elif field == "medium_momentum":
                item["momentum_20d"] = True
            elif field == "long_momentum":
                item["momentum_60d"] = True
        item_key, item_name, _ = normalized_row_key_name(row)
        value = row_metric_value(row)
        time_scope_type = compact_text(row.get("time_scope_type", ""))
        subject_type = compact_text(row.get("subject_type", ""))
        if item_key == "close" and time_scope_type == "trade_date" and "range" in subject_type and value is not None:
            item["close_history"].append((compact_text(row.get("time_scope_value", "")), value))
        elif direct_close_value(row) is not None or item_key == "close" or "收盘价" in item_name or "当前股价" in item_name:
            item["close"] = True
        if direct_ma5_value(row) is not None or item_key in {"ma5", "avg_close_5d"} or "5日均线" in item_name:
            item["ma5"] = True
        if direct_ma10_value(row) is not None or item_key in {"ma10", "avg_close_10d"} or "10日均线" in item_name:
            item["ma10"] = True
        if direct_ma20_value(row) is not None or item_key in {"ma20", "avg_close_20d"} or "20日均线" in item_name:
            item["ma20"] = True
        if direct_ma60_value(row) is not None or item_key in {"ma60", "avg_close_60d"} or "60日均线" in item_name:
            item["ma60"] = True
        if direct_high_52w_value(row) is not None or item_key in {"high_52w", "max_high"} or "52周" in item_name or "1年最高" in item_name or "区间最高价" in item_name:
            item["high_52w"] = True
    for item in coverage.values():
        close_history = item.get("close_history", [])
        if close_history:
            item["close"] = True
        if len(close_history) >= 5:
            item["ma5"] = True
        if len(close_history) >= 10:
            item["ma10"] = True
        if len(close_history) >= 20:
            item["ma20"] = True
        if len(close_history) >= 60:
            item["ma60"] = True
    total = len(coverage)
    return {
        "total": total,
        "momentum": sum(1 for item in coverage.values() if item["momentum"]),
        "momentum_5d": sum(1 for item in coverage.values() if item["momentum_5d"]),
        "momentum_10d": sum(1 for item in coverage.values() if item["momentum_10d"]),
        "momentum_20d": sum(1 for item in coverage.values() if item["momentum_20d"]),
        "momentum_60d": sum(1 for item in coverage.values() if item["momentum_60d"]),
        "ma5": sum(1 for item in coverage.values() if item["ma5"]),
        "ma10": sum(1 for item in coverage.values() if item["ma10"]),
        "ma20": sum(1 for item in coverage.values() if item["ma20"]),
        "ma60": sum(1 for item in coverage.values() if item["ma60"]),
        "close": sum(1 for item in coverage.values() if item["close"]),
        "high_52w": sum(1 for item in coverage.values() if item["high_52w"]),
    }


def technical_data_quality_note(fin_trends_response):
    quality = technical_data_quality(fin_trends_response)
    total = quality["total"]
    if total == 0:
        return "数据完整性：本轮未识别到可用于技术评分的股票代码。"
    return (
        "数据完整性："
        f"识别候选 {total} 只；"
        f"真实涨跌幅覆盖 {quality['momentum']} 只；"
        f"5/10/20/60日涨跌幅覆盖 {quality['momentum_5d']}/{quality['momentum_10d']}/{quality['momentum_20d']}/{quality['momentum_60d']} 只；"
        f"5/10日均线覆盖 {quality['ma5']}/{quality['ma10']} 只；"
        f"收盘价覆盖 {quality['close']} 只；"
        f"52周高点覆盖 {quality['high_52w']} 只。"
        "脚本已禁止把成交额、总市值、PE/PB/PS 等非趋势字段替代为趋势动量。"
    )


def format_momentum_summary(short_momentum, momentum_10d, medium_momentum, long_momentum):
    parts = []
    if short_momentum is not None:
        parts.append(f"5日 {short_momentum:.2f}%")
    if momentum_10d is not None:
        parts.append(f"10日 {momentum_10d:.2f}%")
    if medium_momentum is not None:
        parts.append(f"20日 {medium_momentum:.2f}%")
    if long_momentum is not None:
        parts.append(f"60日 {long_momentum:.2f}%")
    return " / ".join(parts) if parts else "暂无"


def trend_quality_is_sufficient(fin_trends_response):
    quality = technical_data_quality(fin_trends_response)
    total = max(quality.get("total", 0), EXPECTED_STOCK_COUNT, 1)
    required = trend_required_count(total)
    return (
        quality.get("momentum_5d", 0) >= required
        and quality.get("momentum_10d", 0) >= required
        and quality.get("close", 0) >= required
        and quality.get("ma5", 0) >= required
        and quality.get("ma10", 0) >= required
    )


def formal_technical_top5_rows(fin_trends_response):
    rows = build_local_technical_top5(fin_trends_response)
    if not rows or not trend_quality_is_sufficient(fin_trends_response):
        return []
    return rows


def clean_stock_name(value):
    text = compact_text(value)
    for suffix in (
        "科技股份有限公司",
        "集团股份有限公司",
        "股份有限公司",
        "有限责任公司",
        "有限公司",
    ):
        text = text.replace(suffix, "")
    text = text.replace("中科", "").replace("成都", "").replace("苏州", "").replace("无锡", "")
    text = text.replace("深圳市", "").replace("浙江", "").replace("富士康", "")
    return text


def find_metric_by_name(metrics, name):
    target = clean_stock_name(name)
    for item in metrics.values():
        item_name = clean_stock_name(item.get("name", ""))
        short_name = clean_stock_name(item.get("short_name", ""))
        if target and (target in item_name or item_name in target or target == short_name):
            return item
    return None


def markdown_table_to_html(text, limit=12):
    rows = first_table_rows(text, limit=limit)
    if not rows:
        return f"<p>{escape(compact_text(text)[:1200])}</p>"
    header = rows[0]
    body = rows[1:]
    html = ["<div class=\"table-wrap\"><table><thead><tr>"]
    html.extend(f"<th>{escape(cell)}</th>" for cell in header)
    html.append("</tr></thead><tbody>")
    for row in body:
        html.append("<tr>")
        html.extend(f"<td>{escape(cell)}</td>" for cell in row)
        html.append("</tr>")
    html.append("</tbody></table></div>")
    return "".join(html)


def compact_valuation_markdown(fin_trends_response):
    valuation_by_code = collect_valuation_metrics(fin_trends_response)
    if not valuation_has_coverage(valuation_by_code):
        return "估值覆盖不足：可用 PE/PB 未达到候选池的 80%，本轮不将其视为完整估值比较。"
    if not any(item.get("pb") is not None for item in valuation_by_code.values()):
        return "估值覆盖不足：市净率（PB）全量未验证；本轮不将仅有 PE 的结果视为完整估值比较。"

    headers = ["股票代码", "股票名称", "市盈率(TTM)", "市净率(LF)"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for code, item in sorted(valuation_by_code.items()):
        if item.get("pe_ttm") is None and item.get("pb") is None:
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    code,
                    item["name"],
                    format_pe(item.get("pe_ttm")),
                    f"{item['pb']:g}" if item.get("pb") is not None else "暂无",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def format_pe(value):
    if value is None:
        return "未验证"
    if value <= 0:
        return "N/M"
    if value >= 500:
        return f"{value:g}（极高，不参与横比）"
    return f"{value:g}"


def collect_valuation_metrics(fin_trends_response):
    valuation_by_code = {}
    for section in extract_fin_sections(fin_trends_response):
        if not any(keyword in section["query"] for keyword in ("市盈率", "市净率", "估值")):
            continue
        for row in rows_to_dicts(section["content"]):
            code = row_stock_code(row)
            name = row_stock_name(row)
            item_key = row_item_key(row)
            item_name = row_item_name(row)
            value = parse_number(first_present(row, ("item_value", "指标值", "value")))
            pe_value = direct_number(row, ("市盈率(TTM)", "市盈率", "PE(TTM)"), ("市盈率",))
            pb_value = direct_number(row, ("市净率", "市净率(LF)", "PB"), ("市净率",))
            if not code or not name or (value is None and pe_value is None and pb_value is None):
                continue
            item = valuation_by_code.setdefault(code, {"name": clean_stock_name(name)})
            normalized_key = compact_text(item_key or item_name).lower()
            normalized_name = compact_text(item_name or item_key).lower()
            if pe_value is not None:
                item["pe_ttm"] = pe_value
            elif pb_value is not None:
                item["pb"] = pb_value
            elif "pe" in normalized_key or "市盈率" in normalized_name:
                item["pe_ttm"] = value
            elif "pb" in normalized_key or "市净率" in normalized_name:
                item["pb"] = value
    return valuation_by_code


def valuation_has_coverage(valuation_by_code, min_coverage=0.8):
    usable = [
        item
        for item in valuation_by_code.values()
        if item.get("pe_ttm") is not None or item.get("pb") is not None
    ]
    return len(usable) >= EXPECTED_STOCK_COUNT * min_coverage


def valuation_is_complete(valuation_by_code):
    return valuation_has_coverage(valuation_by_code)


def collect_growth_metrics(fin_trends_response):
    growth_by_code = {}
    for section in extract_fin_sections(fin_trends_response):
        if not any(keyword in section["query"] for keyword in ("营业收入", "净利润", "同比")):
            continue
        for row in rows_to_dicts(section["content"]):
            code = row_stock_code(row)
            name = row_stock_name(row)
            if not code or not name:
                continue
            item = growth_by_code.setdefault(code, {"name": clean_stock_name(name)})
            item_name = row_item_name(row)
            value = parse_number(first_present(row, ("item_value", "指标值", "value")))
            revenue_value = parse_number(
                first_present(row, ("营业收入同比增速(%)", "营业收入同比增速_百分号", "营收同比(%)"))
                or first_key_containing(row, "营业收入", "同比")
                or first_key_containing(row, "营收", "同比")
            )
            profit_value = parse_number(
                first_present(row, ("净利润同比增速(%)", "净利润同比增速_百分号", "净利润同比(%)"))
                or first_key_containing(row, "净利润", "同比")
            )
            if revenue_value is not None:
                item["revenue_yoy"] = revenue_value
            if profit_value is not None:
                item["profit_yoy"] = profit_value
            if value is None:
                continue
            if "营业收入" in item_name or row.get("item_key") == "yoy_or":
                item["revenue_yoy"] = value
            elif "净利润" in item_name or row.get("item_key") in {"yoyprofit", "yoy_net_profit"}:
                item["profit_yoy"] = value
    return growth_by_code


def compact_growth_markdown(fin_trends_response):
    growth_by_code = collect_growth_metrics(fin_trends_response)
    complete = {
        code: item
        for code, item in growth_by_code.items()
        if item.get("revenue_yoy") is not None and item.get("profit_yoy") is not None
    }
    if len(complete) < EXPECTED_STOCK_COUNT * 0.8:
        return ""
    headers = ["股票代码", "股票名称", "营收同比(%)", "净利润同比(%)"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for code, item in sorted(complete.items()):
        lines.append(
            "| "
            + " | ".join(
                [
                    code,
                    item["name"],
                    f"{item['revenue_yoy']:.2f}",
                    f"{item['profit_yoy']:.2f}",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def section_has_enough_coverage(markdown_text, expected=EXPECTED_STOCK_COUNT, min_coverage=0.8):
    rows = rows_to_dicts(markdown_text)
    if not rows:
        return False
    codes = set()
    names = set()
    for row in rows:
        code = row.get("证券代码") or row.get("股票代码") or row.get("wind_code")
        name = row.get("证券简称") or row.get("股票名称") or row.get("matched_company_name") or row.get("公司名称")
        if code:
            codes.add(code)
        elif name:
            names.add(clean_stock_name(name))
    return (len(codes) if codes else len(names)) >= expected * min_coverage


def display_fin_sections(fin_response):
    sections = []
    for section in extract_fin_sections(fin_response):
        is_valuation = any(keyword in section["query"] for keyword in ("市盈率", "市净率", "估值"))
        is_growth = any(keyword in section["query"] for keyword in ("营业收入", "净利润", "同比"))
        if is_valuation or is_growth:
            continue
        content = compact_fin_section_content(section)
        rows = rows_to_dicts(content)
        value_headers = [key for key in (rows[0].keys() if rows else []) if key not in {"股票代码", "股票名称", "证券代码", "证券简称"}]
        has_values = any(not clean_missing_cell(row.get(key, "")) for row in rows for key in value_headers)
        if content and has_values and section_has_enough_coverage(content):
            sections.append({**section, "content": content})
    return sections


def candidate_display_markdown(fin_candidates_response, valuation_allowed=False):
    sections = extract_fin_sections(fin_candidates_response)
    if not sections:
        return ""
    content = sections[0]["content"]
    if not valuation_allowed:
        content = drop_markdown_columns(content, ("市盈率", "市净率", "估值"))
    content = dedupe_markdown_by_column(content, "股票代码")
    content = drop_sparse_markdown_columns(content)
    return content


def extract_fin_sections(fin_response):
    sections = []
    for item in fin_response.get("result", []):
        query = compact_text(item.get("query", ""))
        content = item.get("content", "")
        status = item.get("status", "")
        source = item.get("source", "")
        sections.append(
            {
                "query": query,
                "content": content,
                "status": status,
                "source": source,
            }
        )
    return sections


def fin_section_title(query, content=""):
    if "涨跌幅" in query:
        return "最近涨跌幅"
    if "均线" in query:
        if "20日均线" not in content and "60日均线" not in content:
            return "当前股价"
        return "均线与当前股价"
    if "成交额" in query:
        return "成交额变化"
    if "52周" in query or "一年最高" in query or "1年最高" in query:
        return "52周价格位置"
    if any(keyword in query for keyword in ("市盈率", "市净率", "估值")):
        return "估值指标"
    if any(keyword in query for keyword in ("营业收入", "净利润", "同比")):
        return "成长与业绩"
    return "金融数据查询结果"


def _momentum_component(value, preferred_high):
    if value is None:
        return None
    if value < -10:
        return 10.0
    if value < 0:
        return max(20.0, 55.0 + value * 3.5)
    if value <= preferred_high:
        return min(95.0, 65.0 + value / preferred_high * 30.0)
    if value <= preferred_high * 2:
        return max(60.0, 95.0 - (value - preferred_high) / preferred_high * 35.0)
    return 45.0


def calculate_technical_score(item):
    """Short-horizon score; market cap and amount are intentionally excluded."""
    components = [
        (_momentum_component(item.get("short_momentum"), 6), 0.30, "5日趋势"),
        (_momentum_component(item.get("momentum_10d"), 12), 0.25, "10日趋势"),
        (_momentum_component(item.get("medium_momentum"), 20), 0.12, "20日趋势"),
        (_momentum_component(item.get("long_momentum"), 35), 0.05, "60日趋势"),
    ]
    close, ma5, ma10, ma20 = (item.get(key) for key in ("close", "ma5", "ma10", "ma20"))
    ma_score = None
    if None not in (close, ma5, ma10):
        if close >= ma5 >= ma10 and (ma20 is None or ma10 >= ma20):
            ma_score = 95.0
        elif close >= ma10 and ma5 >= ma10:
            ma_score = 78.0
        elif close >= ma10:
            ma_score = 62.0
        else:
            ma_score = 30.0
    components.append((ma_score, 0.18, "短期均线"))
    volume_ratio = item.get("volume_ratio_5d")
    volume_score = None
    if volume_ratio is not None:
        volume_score = 90.0 if 1.15 <= volume_ratio <= 2.0 else (65.0 if 0.8 <= volume_ratio < 1.15 else 45.0)
    components.append((volume_score, 0.10, "5日量能"))
    available = [(value, weight, label) for value, weight, label in components if value is not None]
    if not available:
        return None
    weight_sum = sum(weight for _, weight, _ in available)
    score = sum(value * weight for value, weight, _ in available) / weight_sum
    return round(score, 1)


def build_local_technical_top5(fin_trends_response, limit=5):
    metrics = {}
    sections = extract_fin_sections(fin_trends_response)
    valuation_by_code = collect_valuation_metrics(fin_trends_response)
    use_valuation = valuation_is_complete(valuation_by_code)
    growth_by_code = collect_growth_metrics(fin_trends_response)

    for section in sections:
        query = section["query"]
        rows = rows_to_dicts(section["content"])
        if "涨跌幅" in query:
            for row in rows:
                name = row_stock_name(row)
                code = row_stock_code(row)
                if not name or not code:
                    continue
                item = metrics.setdefault(code, {"code": code, "name": name})
                item["short_name"] = clean_stock_name(name)
                apply_market_metric_row(item, row, use_valuation=use_valuation)
                apply_momentum_row(item, row, query=query)
        elif "均线" in query:
            for row in rows:
                name = row_stock_name(row)
                code = row_stock_code(row)
                item_key = row_item_key(row)
                item_name = row_item_name(row)
                value = row_metric_value(row)
                ma5 = direct_ma5_value(row)
                ma10 = direct_ma10_value(row)
                ma20 = direct_ma20_value(row)
                ma60 = direct_ma60_value(row)
                close = direct_close_value(row)
                if not name:
                    continue
                item = metrics.setdefault(code, {"code": code, "name": name}) if code else find_metric_by_name(metrics, name)
                if item is None:
                    continue
                item["short_name"] = clean_stock_name(name)
                apply_market_metric_row(item, row, use_valuation=use_valuation)
                if value is not None:
                    normalized = f"{item_key} {item_name}".lower()
                    if "ma5" in normalized or "5日均线" in normalized:
                        ma5 = value
                    elif "ma10" in normalized or "10日均线" in normalized:
                        ma10 = value
                    elif "ma20" in normalized or "20日均线" in normalized:
                        ma20 = value
                    elif "ma60" in normalized or "60日均线" in normalized:
                        ma60 = value
                    elif item_key == "close" or "收盘价" in item_name or "当前股价" in item_name:
                        close = value
                if ma5 is not None:
                    item["ma5"] = ma5
                if ma10 is not None:
                    item["ma10"] = ma10
                if ma20 is not None:
                    item["ma20"] = ma20
                if ma60 is not None:
                    item["ma60"] = ma60
                if close is not None:
                    item["close"] = close
                item["above_ma20"] = "上方" in row.get("与20日均线位置关系", "") or (
                    item.get("close") is not None and item.get("ma20") is not None and item["close"] > item["ma20"]
                )
                item["above_ma60"] = "上方" in row.get("与60日均线位置关系", "") or (
                    item.get("close") is not None and item.get("ma60") is not None and item["close"] > item["ma60"]
                )
                ma20_distance = parse_number(row.get("偏离20日均线_百分比"))
                ma60_distance = parse_number(row.get("偏离60日均线_百分比"))
                distance_52w_high = parse_number(row.get("距离52周高点位置_百分比") or row.get("距52周最高价距离(%)"))
                if ma20_distance is not None:
                    item["ma20_distance"] = ma20_distance
                if ma60_distance is not None:
                    item["ma60_distance"] = ma60_distance
                if distance_52w_high is not None:
                    item["distance_52w_high"] = distance_52w_high
        elif "52周最高价" in query:
            for row in rows:
                name = row_stock_name(row)
                code = row_stock_code(row)
                item = metrics.setdefault(code, {"code": code, "name": name}) if code else find_metric_by_name(metrics, name)
                if item is not None:
                    apply_market_metric_row(item, row, use_valuation=use_valuation)
                    item_key = row_item_key(row)
                    item_name = row_item_name(row)
                    value = row_metric_value(row)
                    direct_distance = parse_number(row.get("距52周最高价距离(%)") or row.get("距离52周高点位置_百分比"))
                    if direct_distance is not None:
                        item["distance_52w_high"] = direct_distance
                    elif value is not None:
                        normalized = f"{item_key} {item_name}".lower()
                        if item_key == "close" or "收盘价" in item_name or "当前股价" in item_name:
                            item["close"] = value
                        elif "high_52w" in normalized or "52周" in item_name or "1年最高" in item_name:
                            item["high_52w"] = value
        elif any(keyword in query for keyword in ("收盘价", "当前股价", "最高价", "移动平均线")):
            for row in rows:
                name = row_stock_name(row)
                code = row_stock_code(row)
                if not name and not code:
                    continue
                item = metrics.setdefault(code, {"code": code, "name": name}) if code else find_metric_by_name(metrics, name)
                if item is None:
                    continue
                if name:
                    item["short_name"] = clean_stock_name(name)
                    item["name"] = item.get("name") or name
                apply_market_metric_row(item, row, use_valuation=use_valuation)
        elif any(keyword in query for keyword in ("市盈率", "市净率", "估值")):
            for row in rows:
                code = row_stock_code(row)
                name = row_stock_name(row)
                item_key = row_item_key(row)
                item_name = row_item_name(row)
                value = row_metric_value(row)
                pe_value = direct_number(row, ("市盈率(TTM)", "市盈率", "PE(TTM)"), ("市盈率",))
                pb_value = direct_number(row, ("市净率", "市净率(LF)", "PB"), ("市净率",))
                if value is None and pe_value is None and pb_value is None and direct_volume_ratio_value(row) is None:
                    continue
                item = metrics.setdefault(code, {"code": code, "name": name}) if code else find_metric_by_name(metrics, name)
                if item is None:
                    continue
                apply_market_metric_row(item, row, use_valuation=use_valuation)
                normalized_key = compact_text(item_key or item_name).lower()
                normalized_name = compact_text(item_name or item_key).lower()
                if pe_value is not None:
                    item["pe_ttm"] = pe_value
                elif pb_value is not None:
                    item["pb"] = pb_value
                elif "pe" in normalized_key or "市盈率" in normalized_name:
                    item["pe_ttm"] = value
                elif "pb" in normalized_key or "市净率" in normalized_name:
                    item["pb"] = value

    for item in metrics.values():
        for field in ("short_momentum", "momentum_10d", "medium_momentum", "long_momentum"):
            daily_changes = item.get(f"_{field}_daily_changes")
            cumulative = cumulative_percent(daily_changes or [])
            if cumulative is not None:
                item[field] = cumulative
        close_history = item.get("_close_history", [])
        if close_history:
            latest_first = [
                close
                for _, close in sorted(
                    ((date, close) for date, close in close_history if date and close is not None),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
            ]
            if latest_first:
                item["close"] = latest_first[0]
            if len(latest_first) >= 5:
                item["ma5"] = sum(latest_first[:5]) / 5
            if len(latest_first) >= 10:
                item["ma10"] = sum(latest_first[:10]) / 10
            if len(latest_first) >= 20:
                item["ma20"] = sum(latest_first[:20]) / 20
            if len(latest_first) >= 60:
                item["ma60"] = sum(latest_first[:60]) / 60
        if item.get("close") is not None and item.get("ma20") is not None:
            item["above_ma20"] = item["close"] > item["ma20"]
        if item.get("close") is not None and item.get("ma60") is not None:
            item["above_ma60"] = item["close"] > item["ma60"]

    rows = []
    for item in metrics.values():
        if item.get("distance_52w_high") is None and item.get("close") and item.get("high_52w"):
            item["distance_52w_high"] = (item["close"] / item["high_52w"] - 1) * 100
        short_momentum = item.get("short_momentum")
        momentum_10d = item.get("momentum_10d")
        medium_momentum = item.get("medium_momentum")
        long_momentum = item.get("long_momentum")
        momentum = short_momentum if short_momentum is not None else medium_momentum
        ma20 = item.get("ma20")
        ma60 = item.get("ma60")
        above_ma20 = item.get("above_ma20")
        above_ma60 = item.get("above_ma60")
        distance_52w_high = item.get("distance_52w_high")
        pe_ttm = item.get("pe_ttm")
        pb = item.get("pb")
        growth = growth_by_code.get(item["code"], {})
        revenue_yoy = growth.get("revenue_yoy")
        profit_yoy = growth.get("profit_yoy")
        if short_momentum is None and momentum_10d is None:
            continue

        score = calculate_technical_score(item)
        if score is None:
            continue
        reasons = []
        risks = []
        volume_ratio_5d = item.get("volume_ratio_5d")
        if short_momentum is not None and short_momentum > 0:
            reasons.append("5日趋势为正")
        elif short_momentum is not None:
            risks.append("5日趋势偏弱")
        if momentum_10d is not None and momentum_10d > 0:
            reasons.append("10日趋势为正")
        elif momentum_10d is not None:
            risks.append("10日趋势偏弱")
        ma5, ma10 = item.get("ma5"), item.get("ma10")
        if None not in (item.get("close"), ma5, ma10) and item["close"] >= ma5 >= ma10:
            reasons.append("股价站上5/10日均线且短期均线多头")
            structure = "短线趋势延续"
        elif None not in (item.get("close"), ma10) and item["close"] >= ma10:
            structure = "短线整理"
        else:
            structure = "回调观察"
            risks.append("短期均线结构偏弱或数据不完整")
        if volume_ratio_5d is not None and volume_ratio_5d >= MIN_VOLUME_RATIO_5D:
            reasons.append("近5日量能温和放大")
        elif volume_ratio_5d is not None and volume_ratio_5d < 0.8:
            risks.append("近5日量能不足")

        if distance_52w_high is not None:
            if distance_52w_high > -5:
                risks.append("距离52周高点较近")
                position_risk = "接近前高"
            elif distance_52w_high < -30:
                risks.append("距离前高较远，需确认修复持续性")
                position_risk = "低位修复"
            else:
                position_risk = "中位趋势"
        else:
            position_risk = "待确认"

        if momentum is not None and momentum > 0 and ma20 is not None and ma60 is not None and ma20 > ma60 and above_ma20:
            chan = "疑似中枢上沿突破/三买观察"
            candle = "趋势K线偏强，需用OHLC确认具体形态"
            support = "20日线可作为短线结构观察位"
        elif momentum is not None and momentum < 0 and ma20 is not None and ma60 is not None and ma20 > ma60:
            chan = "上涨中枢内回踩观察"
            candle = "回调阶段，观察是否缩量企稳"
            support = "关注60日线支撑是否有效"
        else:
            chan = "中枢/笔结构待确认"
            candle = "K线结构待OHLC确认"
            support = "等待重新站稳关键均线"

        if not risks:
            risks.append("若放量跌破20日线，技术结构转弱")

        valuation = []
        supplement = []
        if use_valuation and pe_ttm is not None:
            valuation.append(f"PE {pe_ttm:g}")
        if use_valuation and pb is not None:
            valuation.append(f"PB {pb:g}")
        if not use_valuation:
            if revenue_yoy is not None:
                supplement.append(f"营收同比 {revenue_yoy:g}%")
            if profit_yoy is not None:
                supplement.append(f"净利同比 {profit_yoy:g}%")

        score = round(max(0, min(100, score)), 1)
        rows.append(
            {
                "排名": 0,
                "股票代码": item["code"],
                "股票名称": re.sub(r"(股份有限公司|科技股份有限公司|集团股份有限公司)$", "", item["name"]),
                "综合研究分": score,
                "技术结构分": score,
                "结构状态": structure,
                "补充维度": " / ".join(valuation or supplement) if (valuation or supplement) else "暂无",
                "趋势动量": format_momentum_summary(short_momentum, momentum_10d, medium_momentum, long_momentum),
                "量价K线": candle,
                "缠论结构": chan,
                "位置风险": position_risk,
                "入选理由": "；".join(reasons[:3]) or "趋势结构待确认",
                "主要风险": "；".join(risks[:3]),
                "后续观察点": support,
            }
        )

    rows.sort(key=lambda row: row["综合研究分"], reverse=True)
    rows = rows[:limit]
    for idx, row in enumerate(rows, 1):
        row["排名"] = idx
    return rows


def technical_top5_markdown(fin_trends_response):
    partial_rows = build_local_technical_top5(fin_trends_response)
    rows = partial_rows if trend_quality_is_sufficient(fin_trends_response) else []
    if not rows:
        opening = "暂无足够趋势数据生成技术结构 TOP 5。"
        if partial_rows:
            opening = "趋势核心字段覆盖未达到正式技术结构 TOP 5 门槛，暂不发布正式排名。"
        return "\n\n".join(
            [
                opening,
                technical_data_quality_note(fin_trends_response),
                "建议：继续补齐真实5/10日涨跌幅、收盘价和5/10日均线；在补数达标前，本轮只作为候选观察池，不输出正式技术 TOP5。",
            ]
        )
    headers = [
        "排名",
        "股票代码",
        "股票名称",
        "综合研究分",
        "技术结构分",
        "结构状态",
        "补充维度",
        "趋势动量",
        "量价K线",
        "缠论结构",
        "位置风险",
        "入选理由",
        "主要风险",
        "后续观察点",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    lines.extend(
        [
            "",
            technical_data_quality_note(fin_trends_response),
            "",
            "说明：该 TOP 5 由脚本基于公开源可得趋势和均线数据本地计算。K线和缠论结构为简化框架判断。",
        ]
    )
    return "\n".join(lines)


def detect_themes(items, seed_themes=None):
    """Extract themes from today's evidence without a built-in theme dictionary."""
    counts = {compact_text(name): 1 for name in (seed_themes or []) if compact_text(name)}
    for item in items:
        text = compact_text(f"{item.get('title', '')} {item.get('snippet', '')}")
        candidates = re.findall(r"([A-Za-z0-9\u4e00-\u9fff]{2,12})(?:概念|板块|产业链|赛道)", text)
        if "：" in text or ":" in text:
            tail = re.split(r"[：:]", text, maxsplit=1)[-1]
            candidates.extend(re.split(r"[+、，,;/|\s]+", tail)[:4])
        for name in candidates:
            name = compact_text(name).strip("，。；：")
            if 2 <= len(name) <= 12 and name not in HOT_TOPIC_MARKERS:
                counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]


def interpret_hotspots(items, seed_themes=None):
    raw_themes = detect_themes(items, seed_themes=seed_themes)

    analyses = []
    for theme, count in raw_themes:
        related_items = [
            item
            for item in items
            if theme in f"{item.get('title', '')} {item.get('snippet', '')}"
        ]
        text = " ".join(f"{item.get('title', '')} {item.get('snippet', '')}" for item in related_items)
        positive_hits = [term for term in POSITIVE_HOTSPOT_TERMS if term in text]
        negative_hits = [term for term in NEGATIVE_HOTSPOT_TERMS if term in text]
        source_count = len({compact_text(item.get("source", "未知来源")) for item in related_items})
        recent_count = 0
        for item in related_items:
            published = parse_item_datetime(item.get("_published_at") or item.get("date"))
            if published and published >= datetime.now() - timedelta(days=1):
                recent_count += 1

        score = 45 + count * 8 + min(12, len(positive_hits) * 4) + min(10, recent_count * 3)
        score -= min(24, len(negative_hits) * 6)
        score = max(0, min(100, score))

        if score >= 75 and not negative_hits:
            quality = "高质量热点"
            action = "可进入重点候选池"
        elif score >= 60:
            quality = "可跟踪热点"
            action = "进入候选池，后续独立评分并提示追高风险"
        elif negative_hits:
            quality = "分化/过热热点"
            action = "只观察龙头和低位修复，不因热点直接加分"
        else:
            quality = "弱确认热点"
            action = "暂不作为核心选股依据"

        if negative_hits:
            risk = "、".join(negative_hits[:3])
        else:
            risk = "暂无明显退潮信号"
        if positive_hits:
            support = "、".join(positive_hits[:3])
        else:
            support = "缺少明确业绩/政策催化"

        analyses.append(
            {
                "主题": theme,
                "热度次数": count,
                "近24小时": recent_count,
                "来源数": source_count,
                "验证状态": "多源验证" if source_count >= 2 else "单源热度",
                "热点质量分": round(score, 1),
                "质量判断": quality,
                "支撑因素": support,
                "风险信号": risk,
                "选股处理": action,
            }
        )

    analyses.sort(key=lambda row: row["热点质量分"], reverse=True)
    return analyses


def hotspot_interpretation_markdown(rows):
    if not rows:
        return "暂无足够热点信息进行质量解读。"
    headers = ["主题", "市场证据", "独立来源", "验证状态", "质量判断", "支撑因素", "风险信号", "选股处理"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        display = dict(row, **{"市场证据": row.get("热度次数"), "独立来源": row.get("来源数")})
        lines.append("| " + " | ".join(str(display.get(header, "")) for header in headers) + " |")
    lines.extend(
        [
            "",
            "说明：市场源热点和板块涨跌只用于候选召回，不直接加分或降分；新闻仅作为可选的个股舆情补充。最终结果必须再通过技术趋势、成长估值、板块资金和事件风险检查。",
        ]
    )
    return "\n".join(lines)


def latest_theme_names(payloads, seed_themes=None):
    """Compatibility wrapper: theme names now come from market-source rows only."""
    return [row.get("主题") for row in (payloads.get("theme_rows") or []) if row.get("主题")][:12] or list(seed_themes or [])[:12]


def build_candidate_query(themes):
    theme_text = "、".join(themes)
    return (
        f"当前日期为{datetime.now().strftime('%Y年%m月%d日')}。请根据最近3天A股市场热点和产业催化，"
        f"围绕{theme_text}等方向筛选20到30只值得进一步研究的A股上市公司。"
        "这里只需要候选名单，不要查询行情、涨跌幅、财务、估值或成交额。"
        "必须只返回表格，列为：股票代码、股票名称、所属主题、热点触发、主营关联度、主要风险。"
        "不要返回全市场列表，不要给买入卖出建议。"
    )


def candidate_rows_from_response(fin_candidates_response, themes=None, limit=MAX_CANDIDATES_FOR_TRENDS):
    direct_candidates = fin_candidates_response.get("candidates") or []
    if direct_candidates:
        return [dict(row) for row in direct_candidates[:limit]]
    sections = extract_fin_sections(fin_candidates_response)
    if any(section["status"] != "success" or "SQL代码执行失败" in section["content"] for section in sections):
        return []

    rows = []
    seen = set()
    for section in sections:
        for row in rows_to_dicts(section["content"]):
            headers = set(row.keys())
            if not headers.intersection({"所属主题", "热点主题", "申万行业分类", "申万行业", "所属行业", "行业", "主营关联度"}):
                continue
            code = row_stock_code(row)
            name = row_stock_name(row)
            if not code or not name or code in seen:
                continue
            rows.append(
                {
                    "股票代码": code,
                    "股票名称": name,
                    "所属主题": row_theme(row),
                    "热点触发": compact_text(row.get("热点触发") or row.get("近期催化") or "")[:120],
                    "主营关联度": compact_text(row.get("主营关联度") or row.get("基本面概况") or "")[:120],
                    "主要风险": compact_text(row.get("主要风险") or row.get("风险") or "")[:120],
                    "市值(亿元)": row.get("市值(亿元)", ""),
                    "成交额(万元)": row.get("成交额(万元)", ""),
                }
            )
            seen.add(code)

    return rows[:limit]


def apply_hotspot_quality_to_candidates(rows, hotspot_rows):
    quality_by_theme = {row["主题"]: row for row in hotspot_rows}
    adjusted = []
    for row in rows:
        theme = row.get("所属主题", "")
        matched = None
        for name, info in quality_by_theme.items():
            if name in theme or theme in name:
                matched = info
                break
        if matched:
            row = dict(row)
            row["热点质量"] = matched["质量判断"]
            row["热点处理"] = matched["选股处理"]
            if matched["质量判断"] == "分化/过热热点":
                row["主要风险"] = compact_text(row.get("主要风险", "") + "；热点存在分化或过热信号")
            adjusted.append(row)
        else:
            row = dict(row)
            row["热点质量"] = "待确认"
            row["热点处理"] = "不因热点直接加分"
            adjusted.append(row)
    return adjusted


def diversify_candidates(rows, max_theme_ratio=0.40):
    """Prevent a multi-theme run from silently collapsing into one sector."""
    if not rows:
        return []
    theme_count = len({compact_text(row.get("所属主题", "待确认")) for row in rows})
    if theme_count < 2:
        return rows
    ceiling = max(1, int(len(rows) * max_theme_ratio + 0.999))
    selected, deferred, used = [], [], {}
    for row in rows:
        theme = compact_text(row.get("所属主题", "待确认"))
        if used.get(theme, 0) < ceiling:
            selected.append(row)
            used[theme] = used.get(theme, 0) + 1
        else:
            deferred.append(row)
    return selected + deferred


def candidate_rows_markdown(rows):
    if not rows:
        return ""
    headers = ["股票代码", "股票名称", "所属主题", "热点质量", "热点处理", "市值(亿元)", "成交额(万元)", "热点触发", "主营关联度", "主要风险"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(compact_text(row.get(header, "")) for header in headers) + " |")
    return "\n".join(lines)


def candidate_stock_text(candidate_rows):
    if not candidate_rows:
        return "无候选"
    return "、".join(f"{row['股票名称']}({row['股票代码']})" for row in candidate_rows)


def chunked_rows(rows, size):
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def build_trend_query(candidate_rows):
    stock_text = candidate_stock_text(candidate_rows)
    return (
        f"请对以下A股候选股票进行横向比较：{stock_text}。"
        "查询最近5日、10日、20日、60日涨跌幅，5日/10日/20日/60日均线趋势，成交额变化，距离52周高点位置，"
        "市盈率或市净率等估值水平，营业收入同比和净利润同比增速。"
        "请尽量使用完整表格返回，并标记趋势状态：强趋势、低位修复、回调观察、高位谨慎或趋势破坏。"
        "如果某些估值字段缺失，请保留其他完整字段，不要编造数字。"
    )


def trend_required_count(total):
    return max(1, int(total * TREND_COMPLETENESS_MIN_RATIO))


def build_supplemental_trend_queries(candidate_rows, fin_trends_response):
    quality = technical_data_quality(fin_trends_response)
    total = max(len(candidate_rows), quality.get("total", 0), 1)
    required = trend_required_count(total)
    stock_text = candidate_stock_text(candidate_rows)
    market_date = chinese_date(latest_a_share_trade_datetime())
    queries = []

    if quality.get("momentum_5d", 0) < required:
        queries.append(
            (
                "momentum_5d",
                f"查询{stock_text}截至{market_date}最近5个交易日的涨跌幅或区间平均涨跌幅。"
                "只返回表格，列为：股票代码、股票名称、统计区间、5日涨跌幅(%)。"
                "不要返回成交额、总市值、市盈率、市净率或其他非涨跌幅字段。",
            )
        )
    if quality.get("momentum_20d", 0) < required:
        queries.append(
            (
                "momentum_20d",
                f"查询{stock_text}截至{market_date}最近20个交易日的涨跌幅或区间平均涨跌幅。"
                "只返回表格，列为：股票代码、股票名称、统计区间、20日涨跌幅(%)。"
                "不要返回成交额、总市值、市盈率、市净率或其他非涨跌幅字段。",
            )
        )
    if quality.get("momentum_60d", 0) < required:
        queries.append(
            (
                "momentum_60d",
                f"查询{stock_text}截至{market_date}最近60个交易日的涨跌幅或区间平均涨跌幅。"
                "只返回表格，列为：股票代码、股票名称、统计区间、60日涨跌幅(%)。"
                "不要返回成交额、总市值、市盈率、市净率或其他非涨跌幅字段。",
            )
        )
    if quality.get("ma20", 0) < required or quality.get("ma60", 0) < required:
        queries.append(
            (
                "moving_average",
                f"查询{stock_text}在{market_date}的20日均线和60日均线价格，以及当日收盘价。"
                "必须返回完整表格，列为：股票代码、股票名称、交易日期、收盘价(元)、20日均线(元)、60日均线(元)。",
            )
        )
    if quality.get("high_52w", 0) < required:
        queries.append(
            (
                "high_52w",
                f"查询{stock_text}在{market_date}的当前股价、过去52周最高价。"
                "必须返回完整表格，列为：股票代码、股票名称、交易日期、收盘价(元)、过去52周最高价(元)。",
            )
        )

    return queries


def build_retry_trend_queries(candidate_rows, fin_trends_response):
    quality = technical_data_quality(fin_trends_response)
    total = max(len(candidate_rows), quality.get("total", 0), 1)
    required = trend_required_count(total)
    stock_text = candidate_stock_text(candidate_rows)
    market_date = chinese_date(latest_a_share_trade_datetime())
    queries = []
    if quality.get("ma20", 0) < required or quality.get("ma60", 0) < required:
        for index, chunk in enumerate(chunked_rows(candidate_rows, 10), 1):
            chunk_text = candidate_stock_text(chunk)
            queries.append(
                (
                    f"moving_average_history_{index}",
                    f"查询{chunk_text}截至{market_date}最近70个交易日的每日收盘价，用于本地计算20日均线和60日均线。"
                    "只返回表格，列为：股票代码、股票名称、交易日期、收盘价(元)。不要返回成交额、市值或估值。",
                )
            )
    if quality.get("high_52w", 0) < required:
        queries.append(
            (
                "high_52w_retry",
                f"查询{stock_text}在{market_date}前52周内的最高价，以及{market_date}的收盘价。"
                "只输出汇总表，不输出每日明细。列名固定为：股票代码、股票名称、交易日期、收盘价_元、过去52周最高价_元、距离52周高点位置_百分比。",
            )
        )
    return queries


def merge_fin_responses(base_response, supplemental_responses):
    merged = dict(base_response)
    result = list(base_response.get("result", []))
    success = bool(base_response.get("success", True))
    for response in supplemental_responses:
        result.extend(response.get("result", []))
        success = success and bool(response.get("success", True))
    merged["success"] = success
    merged["result"] = result
    return merged


def run_supplemental_trend_queries(_unused, _run_id, _candidate_rows, base_response):
    """Compatibility shim; free_trends already calculates all supplemental fields locally."""
    return base_response, []


def technical_candidates_markdown(payloads):
    rows = payloads.get("technical_candidates", [])
    if not rows:
        return "暂无可用技术候选。"
    headers = ["排名", "股票代码", "股票名称", "技术结构分", "结构状态", "趋势动量", "入选理由", "主要风险"]
    return _format_table(headers, rows)


def composite_top5_markdown(payloads):
    if not payloads.get("formal_ranking"):
        return "趋势核心字段覆盖不足，本轮仅输出候选观察池，不发布正式综合 TOP 5。"
    rows = payloads.get("composite_rankings", [])[:5]
    headers = [
        "综合排名", "技术排序", "股票代码", "股票名称", "综合研究分", "技术结构分",
        "市场题材分", "成长估值分", "风险扣分", "数据可信度", "风险标签",
    ]
    display = []
    for row in rows:
        item = dict(row)
        item["数据可信度"] = f"{row.get('confidence_grade', '低')}({row.get('confidence_score', 0):g})"
        display.append(item)
    return _format_table(headers, display)


def market_context_markdown(payloads):
    context = payloads.get("market_context") or {}
    sentiment = context.get("sentiment") or {}
    summary_headers = ["市场温度分", "行业上涨广度(%)", "涨停数", "炸板数", "跌停数", "炸板率(%)", "最高连板"]
    summary = [{
        "市场温度分": sentiment.get("market_temperature"),
        "行业上涨广度(%)": sentiment.get("industry_breadth_pct"),
        "涨停数": sentiment.get("limit_up_count"),
        "炸板数": sentiment.get("broken_count"),
        "跌停数": sentiment.get("limit_down_count"),
        "炸板率(%)": sentiment.get("break_rate_pct"),
        "最高连板": sentiment.get("max_limit_height"),
    }]
    industries = context.get("top_industries") or []
    industry_headers = ["行业", "涨跌幅(%)", "上涨家数", "下跌家数", "领涨股"]
    industry_rows = [
        {"行业": row.get("name"), "涨跌幅(%)": row.get("change_pct"), "上涨家数": row.get("up_count"),
         "下跌家数": row.get("down_count"), "领涨股": row.get("leader")}
        for row in industries[:8]
    ]
    parts = [_format_table(summary_headers, summary)]
    if industry_rows:
        parts.extend(["### 行业广度与领涨方向", _format_table(industry_headers, industry_rows)])
    fund_rows = context.get("board_fund_flow") or []
    if fund_rows:
        parts.extend([
            "### 板块主力资金流（行业·当日）",
            _format_table(
                ["板块", "主力净流入(元)", "主力净占比(%)", "涨跌幅(%)"],
                [{"板块": row.get("name"), "主力净流入(元)": row.get("main_net"),
                  "主力净占比(%)": row.get("main_pct"), "涨跌幅(%)": row.get("change_pct")}
                 for row in fund_rows[:8]],
            ),
        ])
    if context.get("errors"):
        parts.append("数据降级：以下字段未验证，未按零值参与评分。" + "；".join(context["errors"][:5]))
    return "\n\n".join(parts)


def valuation_review_markdown(payloads):
    valuations = payloads.get("valuations") or {}
    rows = []
    for candidate in payloads.get("technical_candidates", []):
        code = str(candidate.get("股票代码") or "")[:6]
        value = valuations.get(code, {})
        rows.append({
            "股票代码": code, "股票名称": candidate.get("股票名称"),
            "预测EPS": value.get("eps_current"), "下一年EPS": value.get("eps_next"),
            "预测机构数": value.get("analyst_count"), "前向PE": value.get("forward_pe"),
            "PEG": value.get("peg"), "营收同比(%)": value.get("revenue_yoy"),
            "净利同比(%)": value.get("profit_yoy"), "成长估值分": value.get("growth_valuation_score"),
            "覆盖": value.get("coverage_label") or "未取得",
        })
    if not rows:
        return "暂无成长估值数据。"
    return _format_table(
        ["股票代码", "股票名称", "预测EPS", "下一年EPS", "预测机构数", "前向PE", "PEG", "营收同比(%)", "净利同比(%)", "成长估值分", "覆盖"],
        rows,
    )


def risk_review_markdown(payloads):
    risks = payloads.get("risk_reviews") or {}
    rows = []
    for candidate in payloads.get("technical_candidates", []):
        code = str(candidate.get("股票代码") or "")[:6]
        risk = risks.get(code, {})
        announcements = risk.get("risky_announcements") or []
        evidence = "；".join(
            f"{item.get('date', '')} {item.get('title', '')}（{item.get('source', '')}）"
            for item in announcements if isinstance(item, dict)
        ) or "未发现已验证的重大风险公告"
        rows.append({
            "股票代码": code, "股票名称": candidate.get("股票名称"),
            "风险扣分": risk.get("risk_penalty", 0), "未来90日最大解禁(%)": risk.get("max_unlock_ratio_pct"),
            "近20日资金流(元)": risk.get("fund_flow_20d"), "融资余额变化(%)": risk.get("margin_growth_pct"),
            "龙虎榜机构净额(万元)": risk.get("institution_net_wan"),
            "复核覆盖": f"{float(risk.get('coverage') or 0) * 100:.0f}%",
            "风险标签": "；".join(risk.get("risk_flags") or []) or ("暂无显著信号" if risk.get("coverage") else "未验证"),
            "公告依据": evidence,
        })
    if not rows:
        return "暂无事件风险复核数据。"
    return _format_table(
        ["股票代码", "股票名称", "风险扣分", "未来90日最大解禁(%)", "近20日资金流(元)", "融资余额变化(%)", "龙虎榜机构净额(万元)", "复核覆盖", "风险标签", "公告依据"],
        rows,
    )


def source_quality_markdown(payloads):
    rows = payloads.get("source_matrix") or []
    if not rows:
        return "暂无数据来源追踪记录。"
    display = [
        {
            "数据源": row.get("source"), "最新取数时间": row.get("latest_at"), "请求数": row.get("requests"), "成功": row.get("success"),
            "失败": row.get("failed"), "备源次数": row.get("fallback"), "最近错误": row.get("last_error"),
        }
        for row in rows
    ]
    return _format_table(["数据源", "最新取数时间", "请求数", "成功", "失败", "备源次数", "最近错误"], display)


def make_html_report(run_id, payloads):
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ai_items = fresh_search_items(payloads["search_ai_news"][1])
    market_items = fresh_search_items(payloads["search_market_hotspots"][1])
    combined = combine_fresh_news(market_items, ai_items, limit=12)
    run_context = payloads.get("run_context") or {}
    market_date = run_context.get("market_date") or datetime.now().strftime("%Y-%m-%d")
    snapshot_label = "非交易日复盘" if run_context.get("non_trading_replay") else "交易日快照"
    hotspot_rows = payloads.get("theme_rows") or []
    hotspot_markdown = hotspot_interpretation_markdown(hotspot_rows)
    valuation_allowed = valuation_is_complete(collect_valuation_metrics(payloads["fin_trends"][1]))
    selected_candidates = payloads.get("selected_candidates", [])
    candidate_markdown = candidate_rows_markdown(selected_candidates) or candidate_display_markdown(
        payloads["fin_candidates"][1], valuation_allowed=valuation_allowed
    )
    trend_sections = display_fin_sections(payloads["fin_trends"][1])
    formal_top5_rows = payloads.get("composite_rankings", [])[:5] if payloads.get("formal_ranking") else []
    valuation_markdown = compact_valuation_markdown(payloads["fin_trends"][1])
    growth_markdown = compact_growth_markdown(payloads["fin_trends"][1])
    market_markdown = market_context_markdown(payloads)
    technical_candidates_md = technical_candidates_markdown(payloads)
    composite_markdown = composite_top5_markdown(payloads)
    enhanced_valuation_markdown = valuation_review_markdown(payloads)
    enhanced_risk_markdown = risk_review_markdown(payloads)
    quality_markdown = source_quality_markdown(payloads)
    supplement_count = len(payloads.get("fin_trend_supplements", []))
    if formal_top5_rows:
        conclusion_done = f"市场温度、题材共振、技术候选、成长估值、风险复核和综合 TOP5 已生成。补充查询 {supplement_count} 条。"
        conclusion_next = "将综合 TOP5 作为人工复核清单，结合盘面和个人风险偏好再判断。"
    else:
        conclusion_done = f"热点发现、候选池和财务数据已完成；趋势覆盖仍不足，当前仅作为候选观察池。补充查询 {supplement_count} 条。"
        conclusion_next = "先修复或改写趋势字段查询，等真实涨跌幅和均线覆盖达标后再发布正式 TOP5。"

    theme_cards = []
    for hotspot in hotspot_rows[:6]:
        theme_cards.append(
            f"""
            <article class="metric">
              <span>{escape(hotspot['主题'])}</span>
              <strong>{hotspot['热点质量分']:g}</strong>
              <small>市场证据 {hotspot['热度次数']} · 独立来源 {hotspot['来源数']}</small>
              <small>{escape(hotspot['验证状态'])}</small>
            </article>
            """
        )
    if not theme_cards:
        theme_cards.append(
            """
            <article class="metric">
              <span>热点识别</span>
              <strong>待确认</strong>
              <small>需要二次过滤</small>
            </article>
            """
        )

    news_html = []
    for item in combined[:8]:
        title = escape(item.get("title", "无标题"))
        link = item.get("link", "")
        date = escape(item.get("_published_at") or item.get("date", ""))
        snippet = escape(compact_text(item.get("snippet", ""))[:180])
        title_html = f"<a href=\"{escape(link)}\">{title}</a>" if link else title
        news_html.append(
            f"""
            <li>
              <div class="news-title">{title_html}</div>
              <div class="meta">{date}</div>
              <p>{snippet}</p>
            </li>
            """
        )
    if not news_html:
        news_html.append(
            """
            <li>
              <div class="news-title">最近3天暂无通过本地硬过滤的热点资讯</div>
              <div class="meta">请扩大时间范围或调整关键词</div>
              <p>工作流已经过滤掉旧日期、无摘要、无有效时间的搜索结果，避免把过期消息放入报告。</p>
            </li>
            """
        )

    candidate_html = ""
    if candidate_markdown:
        candidate_html = markdown_table_to_html(candidate_markdown, limit=12)
    trend_html = "".join(
        f"""
        <section class="subsection">
          <h3>{escape(fin_section_title(section["query"], section["content"]))}</h3>
          <div class="meta">{escape(section["source"])} · {escape(section["status"])}</div>
          {markdown_table_to_html(section["content"], limit=max(80, EXPECTED_STOCK_COUNT * 2))}
        </section>
        """
        for section in trend_sections
    )
    if not trend_html:
        trend_html = "<p>趋势字段未达标：未取得足够覆盖率的真实涨跌幅、收盘价和均线；本轮不展示不含数值的趋势名单。</p>"
    hotspot_html = markdown_table_to_html(hotspot_markdown, limit=8)
    valuation_html = markdown_table_to_html(valuation_markdown, limit=max(40, EXPECTED_STOCK_COUNT)) if valuation_markdown else ""
    growth_html = markdown_table_to_html(growth_markdown, limit=max(40, EXPECTED_STOCK_COUNT)) if growth_markdown else ""
    market_html = markdown_table_to_html(market_markdown, limit=12)
    technical_candidates_html = markdown_table_to_html(technical_candidates_md, limit=10)
    composite_html = markdown_table_to_html(composite_markdown, limit=5)
    enhanced_valuation_html = markdown_table_to_html(enhanced_valuation_markdown, limit=10)
    enhanced_risk_html = markdown_table_to_html(enhanced_risk_markdown, limit=10)
    quality_html = markdown_table_to_html(quality_markdown, limit=30)
    industry_rows = (payloads.get("market_context") or {}).get("top_industries") or []
    industry_markdown = _format_table(
        ["行业", "涨跌幅(%)", "上涨家数", "下跌家数", "领涨股"],
        [
            {"行业": row.get("name"), "涨跌幅(%)": row.get("change_pct"), "上涨家数": row.get("up_count"),
             "下跌家数": row.get("down_count"), "领涨股": row.get("leader")}
            for row in industry_rows[:8]
        ],
    ) if industry_rows else ""
    industry_html = markdown_table_to_html(industry_markdown, limit=8) if industry_markdown else ""

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>智能选股工作流报告 {escape(run_id)}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --ink: #14171f;
      --muted: #657083;
      --line: #d9dee7;
      --panel: #ffffff;
      --accent: #126b5a;
      --accent-2: #c2410c;
      --soft: #eef7f4;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.55;
    }}
    .page {{
      max-width: 1180px;
      width: 100%;
      margin: 0 auto;
      padding: 36px 36px 52px;
    }}
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 24px;
      align-items: end;
      padding-bottom: 22px;
      border-bottom: 2px solid var(--ink);
    }}
    h1 {{
      margin: 0;
      font-size: 40px;
      line-height: 1.12;
      letter-spacing: 0;
    }}
    .subtitle {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 16px;
    }}
    .stamp {{
      text-align: right;
      color: var(--muted);
      font-size: 14px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin: 22px 0;
    }}
    .metric {{
      min-height: 112px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .metric span, .metric small {{ display: block; color: var(--muted); }}
    .metric strong {{ display: block; margin: 8px 0; font-size: 34px; color: var(--accent); }}
    .section {{
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 22px;
    }}
    h2 {{ margin: 0 0 14px; font-size: 22px; }}
    h3 {{ margin: 18px 0 6px; font-size: 16px; }}
    .news-list {{ margin: 0; padding: 0; list-style: none; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .news-list li {{ background: #fbfcfd; border: 1px solid var(--line); border-radius: 8px; padding: 14px; min-height: 132px; }}
    .news-title {{ font-weight: 700; }}
    a {{ color: var(--accent); text-decoration: none; }}
    .meta {{ margin: 4px 0 8px; color: var(--muted); font-size: 12px; }}
    p {{ margin: 0; color: #303846; }}
    .table-wrap {{ width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ min-width: 720px; width: 100%; border-collapse: collapse; font-size: 12px; table-layout: fixed; }}
    th, td {{ padding: 8px 9px; border-bottom: 1px solid var(--line); vertical-align: top; overflow-wrap: anywhere; }}
    th {{ text-align: left; background: var(--soft); color: #20372f; }}
    tr:last-child td {{ border-bottom: 0; }}
    .callout {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 12px;
    }}
    .callout div {{
      border-left: 4px solid var(--accent);
      background: #fbfcfd;
      padding: 12px 14px;
    }}
    .risk {{ border-left-color: var(--accent-2) !important; }}
    .score-note {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-bottom: 14px;
    }}
    .score-note div {{
      background: #fbfcfd;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 12px;
    }}
    .score-note strong {{ display: block; color: var(--ink); font-size: 14px; }}
    footer {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 720px) {{
      .page {{ padding: 20px 14px 32px; }}
      header, .callout {{ grid-template-columns: 1fr; }}
      .stamp {{ text-align: left; }}
      .grid, .news-list {{ grid-template-columns: 1fr 1fr; }}
      h1 {{ font-size: 30px; }}
      .section {{ padding: 16px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header>
      <div>
        <h1>智能选股工作流报告</h1>
        <div class="subtitle">热点扫描 · 市场与题材 · 技术结构 · 成长估值 · 风险复核</div>
      </div>
      <div class="stamp">
        <div>Run ID：{escape(run_id)}</div>
        <div>{escape(generated_at)}</div>
        <div>A股 · 数据基准 {escape(market_date)} · {snapshot_label}</div>
      </div>
    </header>

    <section class="grid">
      {''.join(theme_cards)}
    </section>

    <section class="section">
      <h2>资讯背景（不参与题材发现）</h2>
      <ol class="news-list">
        {''.join(news_html)}
      </ol>
    </section>

    <section class="section">
      <h2>题材来源与筛选门槛</h2>
      {hotspot_html}
    </section>

    <section class="section">
      <h2>市场温度与题材共振</h2>
      {market_html}
      {f'<h3>行业广度与领涨方向</h3>{industry_html}' if industry_html else ''}
    </section>

    <section class="section">
      <h2>综合研究 TOP 5</h2>
      {composite_html}
    </section>

    <section class="section">
      <h2>{'技术候选 TOP 10' if payloads.get('formal_ranking') else '技术观察候选（趋势待确认）'}</h2>
      {technical_candidates_html}
    </section>

    <section class="section">
      <h2>候选股票池</h2>
      {candidate_html}
    </section>

    {f'<section class="section"><h2>估值指标</h2>{valuation_html}</section>' if valuation_html else ''}

    {f'<section class="section"><h2>成长与业绩指标</h2>{growth_html}</section>' if growth_html else ''}

    <section class="section">
      <h2>成长估值与机构覆盖</h2>
      {enhanced_valuation_html}
    </section>

    <section class="section">
      <h2>公告、解禁与资金风险体检</h2>
      {enhanced_risk_html}
    </section>

    <section class="section">
      <h2>历史股价与趋势</h2>
      {trend_html}
    </section>

    <section class="section">
      <h2>本轮结论</h2>
      <div class="callout">
        <div><strong>已跑通</strong><p>{escape(conclusion_done)}</p></div>
        <div><strong>下一步</strong><p>{escape(conclusion_next)}</p></div>
        <div class="risk"><strong>合规边界</strong><p>报告仅作投研辅助，不构成买卖建议。</p></div>
      </div>
    </section>
    <section class="section">
      <h2>数据来源与降级记录</h2>
      {quality_html}
    </section>
    <footer>由公开零 Key 财经数据源工作流生成。未读取或写入 API key。</footer>
  </main>
</body>
</html>
"""
    return html


def make_report(run_id, payloads):
    ai_items = fresh_search_items(payloads["search_ai_news"][1])
    market_items = fresh_search_items(payloads["search_market_hotspots"][1])
    combined = combine_fresh_news(market_items, ai_items, limit=12)
    run_context = payloads.get("run_context") or {}
    market_date = run_context.get("market_date") or datetime.now().strftime("%Y-%m-%d")
    snapshot_label = "非交易日复盘，使用最近交易日数据" if run_context.get("non_trading_replay") else "交易日数据"
    hotspot_text = hotspot_interpretation_markdown(payloads.get("theme_rows") or [])
    theme_source_text = _format_table(
        ["题材来源", "本轮记录数"],
        [{"题材来源": name, "本轮记录数": count} for name, count in (payloads.get("theme_sources") or {}).items()],
    ) if payloads.get("theme_sources") else "暂无题材来源记录。"
    valuation_allowed = valuation_is_complete(collect_valuation_metrics(payloads["fin_trends"][1]))
    selected_candidates = payloads.get("selected_candidates", [])
    candidates_text = candidate_rows_markdown(selected_candidates) or candidate_display_markdown(
        payloads["fin_candidates"][1], valuation_allowed=valuation_allowed
    )
    trend_sections = display_fin_sections(payloads["fin_trends"][1])
    trend_text = "\n\n".join(
        f"### {fin_section_title(section['query'], section['content'])}\n\n{section['content']}" for section in trend_sections
    )
    if not trend_text:
        trend_text = "趋势字段未达标：未取得足够覆盖率的真实涨跌幅、收盘价和均线；本轮不展示仅含代码和名称的趋势名单。"
    formal_top5_rows = payloads.get("composite_rankings", [])[:5] if payloads.get("formal_ranking") else []
    market_text = market_context_markdown(payloads)
    technical_candidates_text = technical_candidates_markdown(payloads)
    composite_text = composite_top5_markdown(payloads)
    risk_text = risk_review_markdown(payloads)
    enhanced_valuation_text = valuation_review_markdown(payloads)
    quality_text = source_quality_markdown(payloads)
    valuation_text = compact_valuation_markdown(payloads["fin_trends"][1])
    growth_text = compact_growth_markdown(payloads["fin_trends"][1])
    supplement_count = len(payloads.get("fin_trend_supplements", []))
    if formal_top5_rows:
        conclusion_lines = [
            f"- 本轮已完成：热点扫描、市场温度、题材共振、技术候选 TOP 10、成长估值、事件风险复核和综合 TOP 5；自动补充趋势查询 {supplement_count} 条。",
            "- 下一步建议：把综合 TOP 5 作为人工复核清单，结合盘面和个人风险偏好再判断。",
            "- 如果金融数据返回里某些字段缺失，本次报告会把这些字段标记为“暂无数据”，不硬编数字。",
        ]
    else:
        conclusion_lines = [
            f"- 本轮已完成：热点扫描、热点质量解读、候选方向识别、候选股/财务数据查询；自动补充趋势查询 {supplement_count} 条后仍未达到正式 TOP5 的趋势覆盖门槛。",
            "- 当前输出定位：候选观察池，不发布正式技术 TOP5，不作为已确认名单。",
            "- 下一步建议：等待公开行情源恢复，优先补齐真实 5/20/60 日涨跌幅、20/60 日均线和 52 周高点位置。",
        ]

    lines = [
        "# 智能选股工作流试跑报告",
        "",
        f"- 运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 范围：A股，{snapshot_label}（数据基准日：{market_date}）+ 最近3天资讯",
        f"- 候选池：根据最新热点动态筛选 {len(selected_candidates) if selected_candidates else '若干'} 只，不再固定少数股票",
        "- 说明：这是投研辅助报告，不构成买卖建议。",
        "",
        "## 1. 市场源热点题材（不依赖新闻）",
        "",
        hotspot_text,
        "",
        "## 2. 资讯背景（不参与题材发现）",
        "",
    ]

    for idx, item in enumerate(combined[:10], 1):
        title = item.get("title", "无标题")
        date = item.get("_published_at") or item.get("date", "")
        link = item.get("link", "")
        snippet = compact_text(item.get("snippet", ""))[:220]
        if link:
            lines.append(f"{idx}. [{title}]({link})")
        else:
            lines.append(f"{idx}. {title}")
        lines.append(f"   - 时间：{date}")
        if snippet:
            lines.append(f"   - 摘要：{snippet}")
    if not combined:
        lines.append("最近3天暂无通过本地硬过滤的热点资讯。已过滤旧日期、无摘要、无有效时间的搜索结果。")

    lines.extend(
        [
            "",
            "## 3. 题材来源与筛选门槛",
            "",
            theme_source_text,
            "",
            "说明：题材只由同花顺热点/涨停归因、同花顺热股标签、东方财富板块目录及板块涨跌前五构建；新闻不参与题材发现，也不直接给候选加分。",
            "",
            "## 4. 市场温度与题材共振",
            "",
            market_text,
            "",
            "## 5. 综合研究 TOP 5",
            "",
            composite_text,
            "",
            "## 6. " + ("技术候选 TOP 10" if payloads.get("formal_ranking") else "技术观察候选（趋势待确认）"),
            "",
            technical_candidates_text,
            "",
            "## 7. 候选股票池查询结果",
            "",
            candidates_text[:5000],
            "",
            "## 8. 成长估值与机构覆盖",
            "",
            enhanced_valuation_text,
            "",
            "## 9. 公告、解禁与资金风险体检",
            "",
            risk_text,
            "",
            "## 10. 估值指标",
            "",
            valuation_text or "估值指标覆盖不完整，本轮不展示、不参与评分。",
            "",
            "## 11. 成长与业绩指标",
            "",
            growth_text or "成长与业绩指标覆盖不完整，本轮不展示、不参与评分。",
            "",
            "## 12. 历史股价与趋势查询结果",
            "",
            trend_text,
            "",
            "## 13. 数据来源、降级与覆盖",
            "",
            quality_text,
            "",
            "## 14. 本轮流程结论",
            "",
            *conclusion_lines,
        ]
    )
    return "\n".join(lines) + "\n"


def main():
    global EXPECTED_STOCK_COUNT, MAX_CANDIDATES_FOR_TRENDS
    parser = argparse.ArgumentParser(description="Generate a zero-key free-source A-share stock research workflow report.")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for data/ and reports/ outputs. Defaults to ~/Documents/001/自动选股/.",
    )
    parser.add_argument(
        "--skip-image",
        action="store_true",
        help="Deprecated compatibility flag. Reports now contain Markdown and HTML only.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=MAX_CANDIDATES_FOR_TRENDS,
        help="Maximum candidates entering trend analysis (default: 50).",
    )
    parser.add_argument(
        "--enrichment-limit",
        type=int,
        default=10,
        help="Maximum technical candidates receiving valuation and risk review (default: 10).",
    )
    args = parser.parse_args()
    MAX_CANDIDATES_FOR_TRENDS = max(1, min(50, args.max_candidates))
    enrichment_limit = max(1, min(10, args.enrichment_limit))

    requested_time = datetime.now()
    verified_trade_time = verified_a_share_trade_date(requested_time, allow_previous=True)
    if verified_trade_time is None:
        print("skip: 未找到可核验的中国 A 股交易日；未访问市场数据、未写入报告。")
        return
    non_trading_replay = verified_trade_time.date() != requested_time.date()
    if non_trading_replay:
        print(
            f"notice: 今天不是交易日，本轮使用最近交易日 {verified_trade_time.strftime('%Y-%m-%d')} 数据复盘。",
            flush=True,
        )

    configure_output_dir(args.output_dir)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    load_keys()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    payloads = {}
    payloads["run_context"] = {
        "requested_date": requested_time.strftime("%Y-%m-%d"),
        "market_date": verified_trade_time.strftime("%Y-%m-%d"),
        "non_trading_replay": non_trading_replay,
    }

    print("running search_market_hotspots...", flush=True)
    status, response = 200, free_search(
        "A股 今日热点题材 板块涨跌 政策催化 资金流向 涨停跌停原因",
        count=12,
    )
    payloads["search_market_hotspots"] = (status, response)
    with (DATA_DIR / f"{run_id}-search_market_hotspots.json").open("w", encoding="utf-8") as file:
        json.dump({"status": status, "response": response}, file, ensure_ascii=False, indent=2)
    time.sleep(1)

    print("running search_ai_news...", flush=True)
    status, response = 200, free_search(
        "A股 今日产业催化 热门概念 上市公司订单 业绩 政策",
        count=12,
    )
    payloads["search_ai_news"] = (status, response)
    with (DATA_DIR / f"{run_id}-search_ai_news.json").open("w", encoding="utf-8") as file:
        json.dump({"status": status, "response": response}, file, ensure_ascii=False, indent=2)
    time.sleep(1)

    try:
        _, ths_reason_rows = _latest_ths_hot_rows(as_of=verified_trade_time)
    except Exception as exc:
        print(f"warning: 同花顺题材归因不可用: {exc}", file=sys.stderr)
        ths_reason_rows = []
    if not ths_reason_rows:
        try:
            ths_reason_rows = _ths_limit_up_reason_rows(trade_date=verified_trade_time.strftime("%Y%m%d"))
            if ths_reason_rows:
                print("notice: 已切换同花顺涨停揭秘题材归因备源", flush=True)
        except Exception as exc:
            print(f"warning: 同花顺涨停揭秘备源不可用: {exc}", file=sys.stderr)

    try:
        ths_hot_rows = _ths_hot_stocks(HOT_STOCK_LIMIT)
    except Exception as exc:
        print(f"warning: 同花顺热股不可用，本轮透明降级: {exc}", file=sys.stderr)
        ths_hot_rows = []
    try:
        xueqiu_hot_rows = _xueqiu_hot_stocks(HOT_STOCK_LIMIT)
    except Exception as exc:
        print(f"warning: 雪球热股不可用，本轮透明降级: {exc}", file=sys.stderr)
        xueqiu_hot_rows = []
    try:
        board_catalog = _board_catalog_rows()
        board_movers = _board_movers(board_catalog=board_catalog)
    except Exception as exc:
        print(f"warning: 板块涨跌前后五不可用: {exc}", file=sys.stderr)
        board_catalog = []
        board_movers = []
    themes = dynamic_theme_names(
        ths_reason_rows=ths_reason_rows,
        ths_hot_rows=ths_hot_rows,
        xueqiu_hot_rows=xueqiu_hot_rows,
        board_catalog=board_catalog,
        board_movers=board_movers,
    )
    theme_rows = market_theme_rows(
        ths_reason_rows=ths_reason_rows,
        ths_hot_rows=ths_hot_rows,
        xueqiu_hot_rows=xueqiu_hot_rows,
        board_catalog=board_catalog,
        board_movers=board_movers,
    )
    payloads["theme_rows"] = theme_rows
    ths_reason_source = "同花顺涨停揭秘备源" if any(row.get("source") == "同花顺涨停揭秘" for row in ths_reason_rows) else "同花顺热点归因"
    payloads["theme_sources"] = {
        ths_reason_source: len(ths_reason_rows),
        "同花顺热股前10": len(ths_hot_rows),
        "雪球热股前10": len(xueqiu_hot_rows),
        "东方财富板块目录": len(board_catalog),
        "东方财富板块涨跌前五": len(board_movers),
    }
    combined_news = combine_fresh_news(
        fresh_search_items(payloads["search_market_hotspots"][1]),
        fresh_search_items(payloads["search_ai_news"][1]),
        limit=20,
    )
    # News remains an optional downstream sentiment input; it does not create
    # themes or expand the candidate pool.
    hotspot_rows = theme_rows
    print("running fin_candidates...", flush=True)
    response = free_candidates(
        themes,
        ths_reason_rows=ths_reason_rows,
        board_movers=board_movers,
        board_catalog=board_catalog,
        ths_hot_rows=ths_hot_rows,
        xueqiu_hot_rows=xueqiu_hot_rows,
    )
    if not response.get("result"):
        response = {"success": True, "result": []}
    status = 200
    payloads["fin_candidates"] = (status, response)
    with (DATA_DIR / f"{run_id}-fin_candidates.json").open("w", encoding="utf-8") as file:
        json.dump({"status": status, "response": response}, file, ensure_ascii=False, indent=2)
    selected_candidates = enforce_candidate_constraints(candidate_rows_from_response(response, themes=themes))
    selected_candidates = apply_hotspot_quality_to_candidates(selected_candidates, hotspot_rows)
    payloads["selected_candidates"] = selected_candidates
    EXPECTED_STOCK_COUNT = max(5, len(selected_candidates))
    with (DATA_DIR / f"{run_id}-selected_candidates.json").open("w", encoding="utf-8") as file:
        json.dump(selected_candidates, file, ensure_ascii=False, indent=2)
    print(f"selected_candidates={len(selected_candidates)}", flush=True)
    time.sleep(1)

    print("running fin_trends...", flush=True)
    status, response = 200, free_trends(selected_candidates)
    with (DATA_DIR / f"{run_id}-fin_trends.json").open("w", encoding="utf-8") as file:
        json.dump({"status": status, "response": response}, file, ensure_ascii=False, indent=2)

    supplement_records = []
    payloads["fin_trends"] = (status, response)
    payloads["fin_trend_supplements"] = supplement_records
    if supplement_records:
        with (DATA_DIR / f"{run_id}-fin_trends_merged.json").open("w", encoding="utf-8") as file:
            json.dump({"status": status, "response": response, "supplements": supplement_records}, file, ensure_ascii=False, indent=2)
    time.sleep(1)

    technical_candidates = build_local_technical_top5(response, limit=enrichment_limit)
    formal_ranking = trend_quality_is_sufficient(response) and len(technical_candidates) >= 5
    payloads["formal_ranking"] = formal_ranking
    payloads["technical_candidates"] = technical_candidates
    with (DATA_DIR / f"{run_id}-technical-candidates.json").open("w", encoding="utf-8") as file:
        json.dump({"formal": formal_ranking, "candidates": technical_candidates}, file, ensure_ascii=False, indent=2)

    print("running market_context...", flush=True)
    # Per-stock concept validation is deliberately limited to the enriched list;
    # it is useful evidence, but must not turn a broad candidate scan into a
    # high-frequency Eastmoney batch.
    market_context = collect_market_context(
        technical_candidates,
        trade_date=verified_trade_time.strftime("%Y%m%d"),
        news_items=combined_news,
    )
    payloads["market_context"] = market_context
    with (DATA_DIR / f"{run_id}-market-context.json").open("w", encoding="utf-8") as file:
        json.dump(market_context, file, ensure_ascii=False, indent=2)

    technical_codes = [str(row.get("股票代码") or "")[:6] for row in technical_candidates]
    try:
        technical_quotes = _tencent_quotes(technical_codes) if technical_codes else {}
    except Exception as exc:
        print(f"warning: 技术候选实时价格补充失败: {exc}", file=sys.stderr)
        technical_quotes = {}
    price_by_code = {code: quote.get("price") for code, quote in technical_quotes.items()}
    growth_by_code = {str(code)[:6]: item for code, item in collect_growth_metrics(response).items()}

    print("running valuation_review...", flush=True)
    valuations = collect_valuations(technical_candidates, price_by_code, growth_by_code)
    payloads["valuations"] = valuations
    with (DATA_DIR / f"{run_id}-valuations.json").open("w", encoding="utf-8") as file:
        json.dump(valuations, file, ensure_ascii=False, indent=2)

    print("running risk_review...", flush=True)
    trade_date = verified_trade_time.strftime("%Y-%m-%d")
    risk_reviews = collect_risk_reviews(technical_candidates, trade_date)
    payloads["risk_reviews"] = risk_reviews
    with (DATA_DIR / f"{run_id}-risk-reviews.json").open("w", encoding="utf-8") as file:
        json.dump(risk_reviews, file, ensure_ascii=False, indent=2)

    composite_rankings = compose_rankings(
        technical_candidates, market_context, valuations, risk_reviews, formal=formal_ranking
    )
    payloads["composite_rankings"] = composite_rankings
    with (DATA_DIR / f"{run_id}-composite-rankings.json").open("w", encoding="utf-8") as file:
        json.dump({"formal": formal_ranking, "rankings": composite_rankings}, file, ensure_ascii=False, indent=2)

    trace = get_default_client().trace()
    payloads["source_trace"] = trace
    payloads["source_matrix"] = source_matrix(trace)
    with (DATA_DIR / f"{run_id}-source-trace.json").open("w", encoding="utf-8") as file:
        json.dump({"events": trace, "matrix": payloads["source_matrix"]}, file, ensure_ascii=False, indent=2)

    report = make_report(run_id, payloads)
    report_path = REPORT_DIR / f"{run_id}-workflow-report.md"
    report_path.write_text(report, encoding="utf-8")
    html = make_html_report(run_id, payloads)
    html_path = REPORT_DIR / f"{run_id}-workflow-report.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"report={report_path}")
    print(f"html={html_path}")
    if args.skip_image:
        print("notice: --skip-image is deprecated; PNG output has been removed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
