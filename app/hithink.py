"""
同花顺官方金融数据服务 (fuyao.aicubes.cn) 客户端封装

直连 REST，复用 app.http_client 的 HttpClient（重试/SSL/连接池）。
返回纯 dict / list，不引入 pandas；所有函数在 key 缺失或请求失败时返回空，
供上层作为兜底源（新浪 → 同花顺 → AKShare）或主源（涨跌停/龙虎榜）使用。

API Key 从环境变量 HITHINK_FINANCE_API_KEY 按次读取（避免模块 import 时序问题）。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.http_client import hithink_client
from app.helpers import _detect_market
from app.utils import log

# 同花顺日期均为 Asia/Shanghai 00:00，统一用 +8 时区格式化，避免机器时区差异
_SH_TZ = timezone(timedelta(hours=8))


def _api_key() -> str:
    """读取同花顺 API Key（空则上层直接短路）。"""
    return os.environ.get("HITHINK_FINANCE_API_KEY", "")


def thscode(code: str, market: str = "") -> str:
    """6 位代码 → 带交易所后缀 thscode（600519.SH / 002463.SZ）。"""
    c = str(code).strip()
    m = _detect_market(c, market)
    return f"{c}.{m}"


def _get(path: str, params: Optional[dict] = None, _retries: int = 2):
    """带鉴权 GET，返回 payload 的 data 字段；key 缺失 / 失败 / 业务错误返回 None。

    HTTP 层 429/5xx 由 HttpClient 重试；业务层 code=429（限流，HTTP 200 返回）在此
    额外重试 _retries 次（退避）。
    """
    key = _api_key()
    if not key:
        return None
    for attempt in range(_retries + 1):
        resp = hithink_client.get(path, params=params, headers={"X-api-key": key})
        if resp is None:
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
        code = payload.get("code")
        if code == 0:
            return payload.get("data")
        if code == 429 and attempt < _retries:
            wait = 1.5 * (attempt + 1)
            log.warning(f"同花顺接口限流(code=429)，{wait:.1f}s 后重试({attempt + 1}/{_retries})...")
            time.sleep(wait)
            continue
        log.warning(f"同花顺接口返回错误: code={code} {payload.get('message')}")
        return None
    return None


def _fetch_all_pages(path: str, params: Optional[dict] = None) -> list[dict]:
    """分页接口拉全量（size=200，循环 pages）。"""
    items: list[dict] = []
    page = 1
    while True:
        p = dict(params or {})
        p["page"] = page
        p["size"] = 200
        data = _get(path, p)
        if not data:
            break
        items.extend(data.get("item", []) or [])
        pages = (data.get("pagination") or {}).get("pages") or 0
        if page >= pages:
            break
        page += 1
    return items


def _fmt_date(date_ms) -> str:
    """毫秒戳 → YYYY-MM-DD（Asia/Shanghai）。"""
    if not date_ms:
        return ""
    try:
        return datetime.fromtimestamp(int(date_ms) / 1000, tz=_SH_TZ).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _ymd_to_ms(date: str) -> int:
    """YYYYMMDD → Asia/Shanghai 00:00 毫秒戳。"""
    try:
        dt = datetime.strptime(str(date), "%Y%m%d")
        return int(dt.replace(tzinfo=_SH_TZ).timestamp() * 1000)
    except Exception:
        return 0


# ============================================================
# 行情快照
# ============================================================

def fetch_snapshot(thscodes: list[str]) -> dict[str, dict]:
    """批量实时快照，返回 {ticker: item}。

    item 字段: thscode/ticker/last_price/price_change/price_change_ratio_pct/
               open_price/high_price/low_price/prev_price/volume/turnover
    """
    if not thscodes:
        return {}
    data = _get("/api/a-share/prices/snapshot", {"thscodes": ",".join(thscodes)})
    if not data:
        return {}
    return {str(it.get("ticker", "")): it for it in (data.get("item") or [])}


# ============================================================
# 历史日K线
# ============================================================

def fetch_daily_kline(code: str, market: str = "", days: int = 60, adjust: str = "forward") -> list[dict]:
    """日K线（升序），返回 [{date, open, high, low, close, volume}]。

    同花顺仅支持 interval=1d（日线）；分钟线无此能力，勿调用。
    """
    start = datetime.now(_SH_TZ) - timedelta(days=int(days) * 2 + 5)  # 覆盖周末/节假日冗余
    end = datetime.now(_SH_TZ)
    params = {
        "thscode": thscode(code, market),
        "interval": "1d",
        "start": int(start.timestamp() * 1000),
        "end": int(end.timestamp() * 1000),
        "adjust": adjust,
    }
    data = _get("/api/a-share/prices/historical", params)
    if not data:
        return []
    items = sorted((data.get("item") or []), key=lambda x: x.get("date_ms", 0))
    out = [
        {
            "date": _fmt_date(it.get("date_ms")),
            "open": it.get("open_price"),
            "high": it.get("high_price"),
            "low": it.get("low_price"),
            "close": it.get("close_price"),
            "volume": it.get("volume"),
        }
        for it in items
    ]
    return out[-int(days):]


# ============================================================
# 涨跌停 / 炸板池
# ============================================================

def fetch_limit_pool(kind: str, date: str = "") -> list[dict]:
    """涨停/跌停/炸板池，返回 item 列表。

    kind: 'limit_up' | 'limit_down' | 'limit_break'
    date: YYYYMMDD，空则取最近交易日。
    """
    path = {
        "limit_up": "/api/a-share/special-data/limit-up-pool",
        "limit_down": "/api/a-share/special-data/limit-down-pool",
        "limit_break": "/api/a-share/special-data/limit-break-pool",
    }.get(kind)
    if not path:
        return []
    params: dict = {}
    if date:
        params["date_ms"] = _ymd_to_ms(date)
    return _fetch_all_pages(path, params)


# ============================================================
# 龙虎榜
# ============================================================

def fetch_dragon_tiger(date: str = "", board_type: str = "all") -> list[dict]:
    """龙虎榜榜单，返回 stock_items 列表。

    date: YYYY-MM-DD，空则取最近交易日；board_type: all/org/hot_money。
    """
    params: dict = {"board_type": board_type}
    if date:
        params["date"] = date
    data = _get("/api/a-share/special-data/dragon-tiger-list", params)
    if not data:
        return []
    return list(data.get("stock_items") or [])


# ============================================================
# 交易日历
# ============================================================

def fetch_trade_days() -> list[str]:
    """A 股近一年交易日列表（YYYYMMDD，升序）。"""
    data = _get("/api/a-share/calendar/trading-days")
    if not data:
        return []
    days = [str(it.get("date", "")) for it in (data.get("item") or [])]
    return [d for d in days if d]
