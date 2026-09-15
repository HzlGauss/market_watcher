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

# 场内基金（ETF/LOF）代码号段：沪 51/56/58，深 15/16/18。
# 同花顺按资产域拆分端点——A 股行情与基金场内行情分属 a-share / fund 两套接口，
# ETF 代码传给 a-share 端点会报 1002 Unknown thscode，必须先按号段分流。
_ETF_CODE_PREFIXES = ("51", "56", "58", "15", "16", "18")


def _is_etf_code(code: str) -> bool:
    """6 位代码是否为场内基金（ETF/LOF），按号段判断。"""
    return str(code).strip().startswith(_ETF_CODE_PREFIXES)


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


def fetch_meta_search(q: str, asset_type: str = "", limit: int = 10) -> list[dict]:
    """标的检索/消歧：按 thscode/ticker/中文名/英文名 跨市场搜索。

    asset_type: a-share / a-share-index / fund-otc / fund-etf / fund-lof / ...（逗号分隔）。
    """
    params: dict = {"q": q, "limit": min(int(limit), 50)}
    if asset_type:
        params["asset_type"] = asset_type
    data = _get("/api/meta/tickers/search", params)
    return list(data.get("item") or []) if data else []


# ============================================================
# 行情快照
# ============================================================

def fetch_snapshot(thscodes: list[str]) -> dict[str, dict]:
    """批量实时快照，返回 {ticker: item}。

    A 股走 /api/a-share/prices/snapshot（批量）；ETF/LOF 走
    /api/fund/market/snapshot（仅支持单只，逐只请求后合并）。
    混传 ETF 到 a-share 快照会让整批 1002 失败，故按号段先分流。

    item 字段: thscode/ticker/last_price/price_change/price_change_ratio_pct/
               open_price/high_price/low_price/prev_price/volume/turnover
    """
    if not thscodes:
        return {}
    a_share = [ts for ts in thscodes if not _is_etf_code(str(ts).split(".", 1)[0])]
    etfs = [ts for ts in thscodes if _is_etf_code(str(ts).split(".", 1)[0])]

    out: dict[str, dict] = {}

    if a_share:
        data = _get("/api/a-share/prices/snapshot", {"thscodes": ",".join(a_share)})
        if data:
            for it in data.get("item") or []:
                out[str(it.get("ticker", ""))] = it

    for ts in etfs:
        data = _get("/api/fund/market/snapshot", {"thscode": ts})
        if data:
            for it in data.get("item") or []:
                out[str(it.get("ticker", ""))] = it

    return out


# ============================================================
# 历史日K线
# ============================================================

def fetch_daily_kline(code: str, market: str = "", days: int = 60, adjust: str = "forward") -> list[dict]:
    """日K线（升序），返回 [{date, open, high, low, close, volume}]。

    同花顺仅支持 interval=1d（日线）；分钟线无此能力，勿调用。
    """
    start = datetime.now(_SH_TZ) - timedelta(days=int(days) * 2 + 5)  # 覆盖周末/节假日冗余
    end = datetime.now(_SH_TZ)
    c = str(code).strip()
    # ETF/LOF/封闭基金号段（51/56/58/15/16/18 开头）走基金场内历史行情端点；
    # 该端点仅 ETF 支持历史、固定前复权且不接受 adjust 参数。
    is_etf = _is_etf_code(c)
    params = {
        "thscode": thscode(code, market),
        "interval": "1d",
        "start": int(start.timestamp() * 1000),
        "end": int(end.timestamp() * 1000),
    }
    path = "/api/fund/market/historical" if is_etf else "/api/a-share/prices/historical"
    if not is_etf:
        params["adjust"] = adjust
    data = _get(path, params)
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


# ============================================================
# 估值快照
# ============================================================

def fetch_valuations_snapshot(thscodes: list[str]) -> list[dict]:
    """批量 A 股估值快照（PE-TTM/MRQ、PB、PS、PCF），一次最多 100 只。

    返回 item 列表，字段: thscode/ticker/name/pe_ttm/pe_mrq/pb_mrq/ps_ttm/pcf_ttm。
    估值允许为 null（未披露）或负数（亏损/负现金流），调用方不得补零或取绝对值。
    """
    codes = [thscode(c) for c in thscodes]
    if not codes:
        return []
    data = _get("/api/a-share/valuations/snapshot", {"thscodes": ",".join(codes)})
    return list(data.get("item") or []) if data else []


# ============================================================
# 财务报表（利润表 / 资产负债表 / 现金流量表 / 财务指标）
# ============================================================

def _fin_params(code: str, market: str, period: str, limit: int) -> dict:
    return {"thscode": thscode(code, market), "period": period, "limit": int(limit)}


def _fin_items(path: str, code: str, market: str, period: str, limit: int) -> list[dict]:
    """三张报表共用：拉最近 N 期序列（最新在前），附加可读 report_date/period_end 字段。"""
    data = _get(path, _fin_params(code, market, period, limit))
    if not data:
        return []
    items = data.get("item") or []
    for it in items:
        it["report_date"] = _fmt_date(it.get("report_date_ms"))
        it["period_end"] = _fmt_date(it.get("period_end_ms"))
    return items


def fetch_income_statements(code: str, market: str = "", period: str = "annual", limit: int = 4) -> list[dict]:
    """利润表多期序列（最新在前）。period: annual/quarterly；limit: 1–20。"""
    return _fin_items("/api/a-share/financials/income-statements", code, market, period, limit)


def fetch_balance_sheets(code: str, market: str = "", period: str = "annual", limit: int = 4) -> list[dict]:
    """资产负债表多期序列（最新在前）。"""
    return _fin_items("/api/a-share/financials/balance-sheets", code, market, period, limit)


def fetch_cash_flow_statements(code: str, market: str = "", period: str = "annual", limit: int = 4) -> list[dict]:
    """现金流量表多期序列（最新在前）。"""
    return _fin_items("/api/a-share/financials/cash-flow-statements", code, market, period, limit)


def _latest_report(code: str, market: str = "") -> str:
    """推导最新报告期 YYYY-N（用最近一期财报的期末月份映射季度）。"""
    items = fetch_income_statements(code, market, period="quarterly", limit=1)
    if not items:
        return ""
    end_ms = items[0].get("period_end_ms")
    if not end_ms:
        return ""
    dt = datetime.fromtimestamp(int(end_ms) / 1000, tz=_SH_TZ)
    n = ((dt.month - 1) // 3) + 1  # 3月→1 6月→2 9月→3 12月→4
    return f"{dt.year}-{n}"


def fetch_financial_indicators(code: str, market: str = "", report: str = "") -> dict[str, dict]:
    """单期财务指标（成长/盈利/偿债/运营/现金流五类），返回 {ability: {index_id: value}}。

    report: YYYY-N（如 2025-4 年报）；空则自动推导最新报告期。
    value 为字符串（含百分号/单位），缺失为 null；调用方按需解析。
    """
    if not report:
        report = _latest_report(code, market)
        if not report:
            return {}
    data = _get("/api/a-share/financials/indicators", {"thscode": thscode(code, market), "report": report})
    if not data:
        return {}
    out: dict[str, dict] = {}
    for ab in data.get("abilities") or []:
        name = str(ab.get("ability") or "")
        inds = {
            str(it.get("index_id") or ""): it.get("value")
            for it in (ab.get("indicators") or [])
            if isinstance(it, dict)
        }
        if name:
            out[name] = inds
    return out


# ============================================================
# 指数 / 板块
# ============================================================

def fetch_ths_index_list(tag: str = "cn_concept") -> list[dict]:
    """同花顺指数目录（cn_concept/region/tszs/industry），返回 [{thscode, name}]。"""
    data = _get("/api/a-share-index/catalog/ths-index-list", {"tag": tag})
    return list(data.get("item") or []) if data else []


def fetch_ths_constituents(index_thscode: str) -> list[dict]:
    """单个板块/标准指数的当前成分股，返回 [{thscode, ticker, name}]。

    index_thscode 需带后缀：886042.TI（同花顺板块）或 000300.SH（标准指数）。
    """
    data = _get("/api/a-share-index/constituents/ths-stock-list", {"thscode": index_thscode})
    return list(data.get("item") or []) if data else []


def fetch_index_snapshot(thscodes: list[str]) -> dict[str, dict]:
    """批量指数/板块行情快照，返回 {thscode: item}。"""
    if not thscodes:
        return {}
    data = _get("/api/a-share-index/prices/snapshot", {"thscodes": ",".join(thscodes)})
    out: dict[str, dict] = {}
    if data:
        for it in data.get("item") or []:
            ts = it.get("thscode") or it.get("ticker")
            if ts:
                out[str(ts)] = it
    return out


def fetch_index_kline(index_thscode: str, days: int = 250) -> list[dict]:
    """单只指数/板块历史日K（升序），返回 [{date, open, high, low, close, volume}]。

    指数无复权概念；index_thscode 需带后缀（.TI/.SH/.SZ）。
    """
    start = datetime.now(_SH_TZ) - timedelta(days=int(days) * 2 + 10)
    end = datetime.now(_SH_TZ)
    data = _get(
        "/api/a-share-index/prices/historical",
        {
            "thscode": index_thscode,
            "interval": "1d",
            "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
        },
    )
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
# 特色数据（连板梯队 / 异动 / 飙升 / 热股）
# ============================================================

def fetch_limit_up_ladder() -> dict:
    """近 30 交易日连板梯队矩阵，返回原始 data（含 window/item）。"""
    return _get("/api/a-share/special-data/limit-up-ladder") or {}


def fetch_anomaly_list(tag_codes: str = "") -> list[dict]:
    """当日全市场个股异动原因。tag_codes: LIMIT_UP/LIMIT_DOWN/... 逗号分隔，空=全量。"""
    params: dict = {}
    if tag_codes:
        params["tag_codes"] = tag_codes
    data = _get("/api/a-share/special-data/anomaly-analysis-list", params)
    return list(data.get("item") or []) if data else []


def fetch_anomaly_stock(thscodes: list[str]) -> list[dict]:
    """按 thscode 批量查询当日个股异动原因（1–50 只）。"""
    if not thscodes:
        return []
    codes = [thscode(c) for c in thscodes]
    data = _get("/api/a-share/special-data/anomaly-analysis-stock", {"thscodes": ",".join(codes)})
    return list(data.get("item") or []) if data else []


def fetch_skyrocket_list(period: str = "day") -> list[dict]:
    """A 股飙升榜。period: day/hour。"""
    data = _get("/api/a-share/special-data/skyrocket-list", {"period": period})
    return list(data.get("item") or []) if data else []


def fetch_hot_stock_list(period: str = "day") -> list[dict]:
    """当前热股榜。period: day/hour。"""
    data = _get("/api/a-share/special-data/hot-stock-list", {"period": period})
    return list(data.get("item") or []) if data else []


def fetch_hot_stock_list_history(date: str) -> list[dict]:
    """指定日期历史热股排名。date: YYYY-MM-DD（最近一年窗口内）。"""
    data = _get("/api/a-share/special-data/hot-stock-list-history", {"date": date})
    return list(data.get("item") or []) if data else []


def fetch_hot_stock_rank_trend(code: str, market: str = "", start_date: str = "", end_date: str = "") -> list[dict]:
    """单只股票在日期区间内热榜排名走势。start/end: YYYY-MM-DD。"""
    params: dict = {"thscode": thscode(code, market)}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    data = _get("/api/a-share/special-data/hot-stock-rank-trend", params)
    return list(data.get("item") or []) if data else []


# ============================================================
# 集合竞价
# ============================================================

def fetch_auction_snapshot(thscodes: list[str], stage: str = "final") -> list[dict]:
    """A 股集合竞价快照（1–100 只）。stage: live/final。"""
    if not thscodes:
        return []
    codes = [thscode(c) for c in thscodes]
    data = _get("/api/a-share/auction/snapshot", {"thscodes": ",".join(codes), "stage": stage})
    return list(data.get("item") or []) if data else []


def fetch_auction_snapshot_meta(thscodes: list[str], stage: str = "final") -> dict:
    """竞价快照原始 data（含 data_status/auction_phase/timestamp + item），供判断数据是否就绪。"""
    if not thscodes:
        return {}
    codes = [thscode(c) for c in thscodes]
    data = _get("/api/a-share/auction/snapshot", {"thscodes": ",".join(codes), "stage": stage})
    return data or {}


def fetch_auction_benchmark(date: str = "") -> list[dict]:
    """集合竞价短线强弱基准（短线风向标）。date: YYYY-MM-DD，空=上海时区当日。"""
    params: dict = {}
    if date:
        params["date"] = date
    data = _get("/api/a-share/auction/short-term-benchmark", params)
    return list(data.get("item") or []) if data else []


# ============================================================
# 公募基金（场内 ETF/LOF + 场外开放式基金）
# ============================================================

def _fund_thscode(code: str, market: str = "") -> str:
    """基金代码 → thscode：场内(51/56/58/15/16/18)走交易所后缀，场外走 .OF。"""
    c = str(code).strip()
    if _is_etf_code(c):
        return thscode(c, market)
    return f"{c}.OF"


def fetch_fund_profile(code: str, market: str = "") -> list[dict]:
    """基金基本资料（含 manager_info/rate_info/trade_rule），返回 item 列表。"""
    data = _get("/api/fund/profile/detail", {"thscode": _fund_thscode(code, market)})
    return list(data.get("item") or []) if data else []


def fetch_fund_nav(code: str, market: str = "", rng: str = "year", nav_type: str = "unit,adj") -> list[dict]:
    """基金净值序列。rng: week/month/tmonth/hyear/year/twoyear/tyear/fyear，空=最新点。"""
    params: dict = {"thscode": _fund_thscode(code, market)}
    if rng:
        params["range"] = rng
    params["nav_type"] = nav_type
    data = _get("/api/fund/performance/nav", params)
    return list(data.get("item") or []) if data else []


def fetch_fund_returns(code: str, market: str = "") -> list[dict]:
    """基金区间收益（近月/季/半年/年/三年/五年/今年/成立以来 + 同类平均/名次）。"""
    data = _get("/api/fund/performance/returns", {"thscode": _fund_thscode(code, market)})
    return list(data.get("item") or []) if data else []


def fetch_fund_drawdowns(code: str, market: str = "") -> list[dict]:
    """基金固定区间最大回撤。"""
    data = _get("/api/fund/performance/drawdowns", {"thscode": _fund_thscode(code, market)})
    return list(data.get("item") or []) if data else []


def fetch_fund_holdings(code: str, market: str = "") -> dict:
    """基金定期披露重仓股 + 汇总字段（股/债/基金占比、换手率、集中度），返回原始 data。"""
    return _get("/api/fund/portfolio/holdings", {"thscode": _fund_thscode(code, market)}) or {}


def fetch_fund_holders(code: str, market: str = "", merge_scope: str = "all") -> list[dict]:
    """基金持有人结构。merge_scope: all/merged/separate。"""
    data = _get("/api/fund/holders/detail", {"thscode": _fund_thscode(code, market), "merge_scope": merge_scope})
    return list(data.get("item") or []) if data else []


def fetch_fund_holders_top(code: str, market: str = "", limit: int = 10) -> list[dict]:
    """基金前十大持有人。"""
    data = _get("/api/fund/holders/top", {"thscode": _fund_thscode(code, market), "limit": min(int(limit), 10)})
    return list(data.get("item") or []) if data else []


def fetch_fund_industry_allocation(code: str, market: str = "") -> list[dict]:
    """基金行业配置。"""
    data = _get("/api/fund/portfolio/industry-allocation", {"thscode": _fund_thscode(code, market)})
    return list(data.get("item") or []) if data else []


def fetch_fund_dividends(code: str, market: str = "") -> dict:
    """基金分红记录 + 汇总，返回原始 data。"""
    return _get("/api/fund/corporate-actions/dividends", {"thscode": _fund_thscode(code, market)}) or {}


def fetch_fund_diagnostics(code: str, market: str = "") -> list[dict]:
    """基金诊断详情（维度/概率/区间/韧性）。"""
    data = _get("/api/fund/diagnostics/detail", {"thscode": _fund_thscode(code, market)})
    return list(data.get("item") or []) if data else []


def fetch_fund_manager_detail(manager_id: str) -> list[dict]:
    """基金经理详情与雷达对比。manager_id 来自 fetch_fund_profile 的 manager_info[].manager_id。"""
    data = _get("/api/fund/managers/detail", {"manager_id": manager_id})
    return list(data.get("item") or []) if data else []


def fetch_fund_manager_performance(manager_id: str, rng: str = "year") -> list[dict]:
    """基金经理业绩。rng: month/tmonth/year/nowyear/now。"""
    data = _get("/api/fund/managers/performance", {"manager_id": manager_id, "range": rng})
    return list(data.get("item") or []) if data else []


def fetch_fund_manager_style(manager_id: str) -> list[dict]:
    """基金经理投资风格。"""
    data = _get("/api/fund/managers/investment-style", {"manager_id": manager_id})
    return list(data.get("item") or []) if data else []


def fetch_fund_company(company_id: str) -> list[dict]:
    """基金公司详情。company_id 来自 fetch_fund_profile 的 company_id。"""
    data = _get("/api/fund/companies/detail", {"company_id": company_id})
    return list(data.get("item") or []) if data else []
