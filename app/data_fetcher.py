"""
数据获取模块 —— 新浪财经实时行情 + 北向资金
"""

from __future__ import annotations
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from datetime import datetime
from app.models import Quote, WatchItem, MARKET_PREFIX, NorthFlowData, MarketNews, MarketBreadth, FundFlowDetail, FundFlowDaily, SectorBoard, MarginData
from app.config import Config
from app.utils import log
from app.http_client import sina_client, eastmoney_client

# ============================================================
# 常量
# ============================================================

SINA_API = "https://hq.sinajs.cn/list="
NORTH_FLOW_API = "https://push2.eastmoney.com/api/qt/kamt.kline/get"
STOCK_FLOW_API = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
STOCK_FLOW_DAILY_API = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
TENCENT_API = "https://qt.gtimg.cn/q="


# ============================================================
# 新浪财经实时行情
# ============================================================

def _build_sina_codes(items: list[WatchItem]) -> str:
    """构建新浪财经 API 的代码列表 (sh510300,sz159915,...)"""
    codes = []
    for item in items:
        prefix = MARKET_PREFIX.get(item.market, "sh")
        codes.append(f"{prefix}{item.code}")
    return ",".join(codes)


def _parse_float(val: str | None) -> Optional[float]:
    """安全解析浮点数"""
    if val is None:
        return None
    val = str(val).strip()
    if not val or val == "0.000":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def fetch_quotes(items: list[WatchItem]) -> list[Quote]:
    """批量获取实时行情（新浪财经 + 腾讯财经量比/换手率）

    Args:
        items: 盯盘标的列表

    Returns:
        行情数据列表，网络异常时返回空列表
    """
    if not items:
        return []

    sina_codes = _build_sina_codes(items)
    url = f"{SINA_API}{sina_codes}"

    resp = sina_client.get(url)
    if resp is None:
        log.warning("新浪财经API请求失败")
        return []

    try:
        resp.encoding = "gbk"
        text = resp.text.strip()

        if not text:
            log.warning("新浪财经返回空数据")
            return []

        results: list[Quote] = []
        lines = text.strip().split("\n")

        for i, line in enumerate(lines):
            if i >= len(items):
                break

            match = re.search(r'"(.*)"', line)
            if not match:
                continue

            fields = match.group(1).split(",")
            if len(fields) < 32:
                continue

            item = items[i]
            pre_close = _parse_float(fields[2])
            price = _parse_float(fields[3])

            change_pct: Optional[float] = None
            change_amt: Optional[float] = None
            if price is not None and pre_close is not None and pre_close != 0:
                change_amt = round(price - pre_close, 3)
                change_pct = round((price - pre_close) / pre_close * 100, 2)

            high = _parse_float(fields[4])
            low = _parse_float(fields[5])

            amplitude: Optional[float] = None
            if high is not None and low is not None and pre_close is not None and pre_close != 0:
                amplitude = round((high - low) / pre_close * 100, 2)

            name = fields[0].strip() if fields[0] else ""

            # 计算分时均价（黄线）= 成交额 / 成交量
            vol_val = _parse_float(fields[8])
            amt_val = _parse_float(fields[9])
            avg_price: Optional[float] = None
            if vol_val and amt_val and vol_val > 0:
                avg_price = round(amt_val / vol_val, 3)

            # 量比在 fields[35]，换手率在 fields[37]
            results.append(Quote(
                code=item.code,
                name=name,
                type=item.type,
                price=price,
                change_pct=change_pct,
                change_amt=change_amt,
                pre_close=pre_close,
                open=_parse_float(fields[1]),
                high=high,
                low=low,
                volume=vol_val,
                amount=amt_val,
                amplitude=amplitude,
                avg_price=avg_price,
                # 新浪API不提供量比和换手率，稍后从腾讯API补充
                turnover_rate=None,
                volume_ratio=None,
            ))

        # 从腾讯财经补充量比和换手率
        try:
            tencent_data = fetch_tencent_data(items)
            code_map = {q.code: q for q in results}
            for code, data in tencent_data.items():
                if code in code_map:
                    q = code_map[code]
                    if data.get("volume_ratio") is not None:
                        q.volume_ratio = data["volume_ratio"]
                    if data.get("turnover_rate") is not None:
                        q.turnover_rate = data["turnover_rate"]
                    if data.get("bid_volume") is not None:
                        q.bid_volume = data["bid_volume"]
                    if data.get("ask_volume") is not None:
                        q.ask_volume = data["ask_volume"]
                    if data.get("bid_ask_ratio") is not None:
                        q.bid_ask_ratio = data["bid_ask_ratio"]
        except Exception:
            pass  # 腾讯API失败时忽略，不影响主流程

        return results

    except Exception as e:
        log.error(f"数据解析异常: {e}")
        return []


# ============================================================
# 腾讯财经数据源（量比、换手率）
# ============================================================

def _build_tencent_codes(items: list[WatchItem]) -> str:
    """构建腾讯财经API的代码列表 (sh510300,sz159915,...)"""
    codes = []
    for item in items:
        prefix = MARKET_PREFIX.get(item.market, "sh")
        codes.append(f"{prefix}{item.code}")
    return ",".join(codes)


def fetch_tencent_data(items: list[WatchItem]) -> dict[str, dict[str, Optional[float]]]:
    """从腾讯财经API获取量比和换手率

    Returns:
        {code: {volume_ratio: x, turnover_rate: y}}
    """
    if not items:
        return {}

    tencent_codes = _build_tencent_codes(items)
    url = f"{TENCENT_API}{tencent_codes}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://stockapp.finance.qq.com/",
    }

    try:
        resp = sina_client._session.get(url, headers=headers, timeout=10)
        if resp is None:
            return {}
        resp.encoding = "gbk"
        text = resp.text.strip()
    except Exception:
        return {}

    result: dict[str, dict[str, Optional[float]]] = {}

    try:
        import re
        lines = text.strip().split("\n")

        for i, line in enumerate(lines):
            if i >= len(items):
                break

            match = re.search(r'="(.*)"', line)
            if not match:
                continue

            fields = match.group(1).split('~')
            if len(fields) < 50:
                continue

            code = items[i].code

            # 换手率在 fields[38]，量比在 fields[49]
            # 外盘（主动买入）在 fields[6]，内盘（主动卖出）在 fields[7]
            # 委比在 fields[33]（百分比形式）
            turnover = _parse_float(fields[38]) if len(fields) > 38 else None
            volume_ratio = _parse_float(fields[49]) if len(fields) > 49 else None
            bid_volume = _parse_float(fields[6]) if len(fields) > 6 else None
            ask_volume = _parse_float(fields[7]) if len(fields) > 7 else None
            bid_ask_ratio = _parse_float(fields[33]) if len(fields) > 33 and fields[33] else None

            result[code] = {
                "volume_ratio": volume_ratio,
                "turnover_rate": turnover,
                "bid_volume": bid_volume,
                "ask_volume": ask_volume,
                "bid_ask_ratio": bid_ask_ratio,
            }

        return result

    except Exception as e:
        log.warning(f"腾讯财经数据解析异常: {e}")
        return {}


# ============================================================
# 个股资金流（东方财富）
# ============================================================

def _get_secid(code: str, market: str) -> str:
    """将股票代码转换为东方财富 secid

    上海市场 secid=1.{code}，深圳市场 secid=0.{code}
    """
    prefix = "1" if market.upper() == "SH" else "0"
    return f"{prefix}.{code}"


def fetch_main_net_inflow(code: str, market: str = "SH") -> Optional[float]:
    """获取个股主力净流入（元）

    Args:
        code: 股票代码
        market: 市场标识 (SH/SZ)

    Returns:
        主力净流入金额（元），失败返回 None（静默失败，不记录日志）
    """
    secid = _get_secid(code, market)
    # lmt=1 只取最新一条数据，klt=1 为1分钟粒度
    url = (f"{STOCK_FLOW_API}?secid={secid}"
           f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&lmt=1&klt=1")

    import requests
    try:
        resp = requests.get(url, timeout=3, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        })
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("data") is None:
            return None
        # 数据在 klines 数组中，取最后一条的 f52（主力净流入）
        klines = data["data"].get("klines")
        if klines and len(klines) > 0:
            last = klines[-1].split(",")
            if len(last) > 1:
                return _parse_float(last[1])
        # 兼容旧格式：直接取顶层 f52
        return _parse_float(data["data"].get("f52"))
    except Exception:
        pass  # 静默失败，走新浪兜底

    # 东方财富断连，回退到新浪
    sina_flow = _fetch_fund_flow_sina(code, market)
    return sina_flow.main_net if sina_flow else None


def fetch_fund_flow_detail(code: str, market: str = "SH") -> Optional[FundFlowDetail]:
    """获取个股当日资金流向明细（超大单/大单/中单/小单）

    使用东方财富 fflow/daykline/get 接口，klt=101（日线）+ lmt=1 取最新一天。
    交易时段内返回当日实时累计值，收盘后返回当日完整数据。
    每条格式：日期,f52(主力),f53(小单),f54(中单),f55(大单),f56(超大单),
              f57(主力占比),f58(小单占比),f59(中单占比),f60(大单占比),f61(超大单占比)

    Args:
        code: 股票代码
        market: 市场标识 (SH/SZ)

    Returns:
        FundFlowDetail 或 None（静默失败）
    """
    secid = _get_secid(code, market)
    import requests
    url = (f"{STOCK_FLOW_DAILY_API}?secid={secid}"
           f"&fields1=f1,f2,f3,f7"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&lmt=1&klt=101&ut=b2884a393a59ad64002292a3e90d46a5")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/",
    }
    try:
        # 东财资金流接口易触发限频(RemoteDisconnected)，退避重试 3 次
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=5, headers=headers)
                break
            except requests.exceptions.RequestException:
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if resp.status_code != 200:
            return None
        data = resp.json().get("data")
        if not data:
            return None

        klines = data.get("klines")
        if not klines or len(klines) == 0:
            return None

        # 解析最后一条 kline: 日期,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61
        parts = klines[-1].split(",")
        if len(parts) < 6:
            return None

        def _pct(i: int) -> Optional[float]:
            """取第 i 列的净占比（%），越界或非法返回 None"""
            return _parse_float(parts[i]) if len(parts) > i else None

        flow = FundFlowDetail(
            main_net=_parse_float(parts[1]) if len(parts) > 1 else None,           # f52 主力净流入
            small_net=_parse_float(parts[2]) if len(parts) > 2 else None,          # f53 小单（散户）
            medium_net=_parse_float(parts[3]) if len(parts) > 3 else None,         # f54 中单
            large_net=_parse_float(parts[4]) if len(parts) > 4 else None,          # f55 大单
            super_large_net=_parse_float(parts[5]) if len(parts) > 5 else None,    # f56 超大单
            # 直接使用东方财富提供的净占比（避免用新浪成交额二次换算造成口径不一致）
            main_pct=_pct(6),          # f57 主力净占比
            small_pct=_pct(7),         # f58 小单净占比
            medium_pct=_pct(8),        # f59 中单净占比
            large_pct=_pct(9),         # f60 大单净占比
            super_large_pct=_pct(10),  # f61 超大单净占比
        )
        if flow.main_net is None and flow.super_large_net is None:
            return None
        return flow
    except Exception:
        pass  # 静默失败，走新浪兜底

    # 东方财富断连，回退到新浪资金流（新浪无小单/中单/大单分类，拆单检测会退化）
    return _fetch_fund_flow_sina(code, market)


def fetch_fund_flow_history(code: str, market: str = "SH", days: int = 10) -> list[FundFlowDaily]:
    """获取个股最近 N 日主力资金流序列（用于「持续低吸」判定）

    复用 STOCK_FLOW_DAILY_API（fflow/daykline/get），klt=101（日线）+ lmt=days，
    返回完整多日序列而非仅最新一天。klines 按日期升序（最旧→最新）。

    每条格式：日期,f52(主力),f53(小单),f54(中单),f55(大单),f56(超大单),
              f57(主力占比),f58(小单占比),f59(中单占比),f60(大单占比),f61(超大单占比)

    Args:
        code: 股票代码
        market: 市场标识 (SH/SZ)
        days: 取最近多少天（默认 10）

    Returns:
        list[FundFlowDaily]（按日期升序），失败返回空列表（静默失败）
    """
    secid = _get_secid(code, market)
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/",
    }

    def _fetch_klines(lmt: int) -> list[str]:
        """按指定 lmt 拉取原始 klines 列表，失败返回空列表"""
        url = (f"{STOCK_FLOW_DAILY_API}?secid={secid}"
               f"&fields1=f1,f2,f3,f7"
               f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
               f"&lmt={lmt}&klt=101&ut=b2884a393a59ad64002292a3e90d46a5")
        # 东财资金流接口易触发限频(RemoteDisconnected)，退避重试 3 次（对齐 fetch_fund_flow_detail）
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=5, headers=headers)
                break
            except requests.exceptions.RequestException:
                if attempt == 2:
                    return []
                time.sleep(1.5 * (attempt + 1))
        if resp is None or resp.status_code != 200:
            return []
        data = resp.json().get("data")
        if not data:
            return []
        return data.get("klines") or []

    try:
        klines = _fetch_klines(days)
        if not klines:
            # lmt=days 个别情况下可能返回空，回退到 lmt=0（全部历史）再取尾部 days 天
            klines = _fetch_klines(0)
            if len(klines) > days:
                klines = klines[-days:]
        if not klines:
            return []

        result: list[FundFlowDaily] = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 6:
                continue
            main_net = _parse_float(parts[1]) if len(parts) > 1 else None
            super_large_net = _parse_float(parts[5]) if len(parts) > 5 else None
            if main_net is None and super_large_net is None:
                # 无任何资金流数据的天，跳过（避免污染「净流入天数」统计）
                continue
            result.append(FundFlowDaily(
                date=parts[0],
                main_net=main_net,                                             # f52 主力净流入
                large_net=_parse_float(parts[4]) if len(parts) > 4 else None,  # f55 大单
                super_large_net=super_large_net,                               # f56 超大单
                main_pct=_parse_float(parts[6]) if len(parts) > 6 else None,   # f57 主力占比
            ))
        return result
    except Exception:
        return []  # 静默失败，调用方按空处理


def _fetch_fund_flow_sina(code: str, market: str = "SH") -> Optional[FundFlowDetail]:
    """新浪资金流兜底（东方财富 fflow 断连时使用）

    新浪接口提供：netamount(主力净流入)、r0_net(超大单净流入)。
    不提供大单/中单/小单分类，故这些字段为 None（拆单检测无法用新浪兜底）。

    Args:
        code: 股票代码
        market: 市场标识 (SH/SZ)

    Returns:
        FundFlowDetail 或 None（静默失败）
    """
    prefix = {"SH": "sh", "SZ": "sz"}.get(market, "sh")
    sina_code = f"{prefix}{code}"
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"MoneyFlow.ssl_qsfx_zjlrqs?daima={sina_code}"
    )
    try:
        import requests
        resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data or not isinstance(data, list) or not data:
            return None
        item = data[0]
        main_net = _parse_float(item.get("netamount"))
        super_large_net = _parse_float(item.get("r0_net"))
        if main_net is None and super_large_net is None:
            return None
        return FundFlowDetail(
            main_net=main_net,
            super_large_net=super_large_net,
            large_net=None,
            medium_net=None,
            small_net=None,
        )
    except Exception:
        return None  # 静默失败


def enrich_quotes_with_flow(quotes: list[Quote]) -> None:
    """为 Quote 列表批量补充资金流向明细（原地修改）

    使用线程池并发请求，每只股票独立请求东方财富资金流接口。
    同时填充 main_net_inflow（向后兼容）和 fund_flow（资金明细）两个字段。
    """
    if not quotes:
        return

    def _fetch_one(q: Quote) -> tuple[str, Optional[FundFlowDetail]]:
        time.sleep(0.3)  # 降低并发请求频率，避免触发东财限频
        flow = fetch_fund_flow_detail(q.code, q.code.startswith(("6", "9")) and "SH" or "SZ")
        return (q.code, flow)

    try:
        # 并发度降至 3：东财资金流接口对高并发敏感，过高会 RemoteDisconnected 导致回退新浪
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(_fetch_one, q) for q in quotes]
            for fut in futures:
                try:
                    code, flow = fut.result(timeout=5)
                    for q in quotes:
                        if q.code == code:
                            q.fund_flow = flow
                            # 向后兼容：同时填充 main_net_inflow
                            if flow is not None and flow.main_net is not None:
                                q.main_net_inflow = flow.main_net
                            break
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"资金流批量获取失败: {e}")


# ============================================================
# AKShare 数据源
# ============================================================

def _fetch_quotes_akshare(items: list[WatchItem]) -> list[Quote]:
    """通过 AKShare 获取行情（东方财富后端，返回全市场数据后本地过滤）"""
    import akshare as ak

    df = ak.stock_zh_a_spot_em()

    item_map = {item.code: item for item in items}
    results: list[Quote] = []

    for _, row in df.iterrows():
        code = str(row["代码"])
        if code not in item_map:
            continue

        item = item_map[code]
        vol_val = _parse_float(row.get("成交量"))
        amt_val = _parse_float(row.get("成交额"))
        avg_p = round(amt_val / vol_val, 3) if vol_val and amt_val and vol_val > 0 else None
        results.append(Quote(
            code=code,
            name=str(row.get("名称", "")),
            type=item.type,
            price=_parse_float(row.get("最新价")),
            change_pct=_parse_float(row.get("涨跌幅")),
            change_amt=_parse_float(row.get("涨跌额")),
            pre_close=_parse_float(row.get("昨收")),
            open=_parse_float(row.get("今开")),
            high=_parse_float(row.get("最高")),
            low=_parse_float(row.get("最低")),
            volume=vol_val,
            amount=amt_val,
            avg_price=avg_p,
            amplitude=_parse_float(row.get("振幅")),
            pe_ratio=_parse_float(row.get("市盈率(动态)")),
            pb_ratio=_parse_float(row.get("市净率")),
            market_cap=_parse_float(row.get("总市值")),
            turnover_rate=_parse_float(row.get("换手率")),
            volume_ratio=_parse_float(row.get("量比")),
            upper_limit=_parse_float(row.get("涨停价")),
            lower_limit=_parse_float(row.get("跌停价")),
        ))

    return results


def fetch_quotes_rich(items: list[WatchItem]) -> list[Quote]:
    """获取丰富字段行情（新浪主源 + AKShare 补查）

    新浪财经稳定轻量，负责基础行情；
    AKShare 补查 PE/PB/市值等额外字段，查不到时这些字段留 None。
    """
    if not items:
        return []

    quotes = fetch_quotes(items)
    if not quotes:
        log.warning("新浪财经无数据，尝试 AKShare...")
        try:
            return _fetch_quotes_akshare(items)
        except ImportError:
            log.debug("AKShare 未安装")
        except Exception as e:
            log.warning(f"AKShare 获取失败: {e}")
        return []

    try:
        _enrich_from_akshare(quotes)
    except Exception:
        pass

    # 补充主力资金流向（东方财富API，批量获取）
    try:
        enrich_quotes_with_flow(quotes)
    except Exception:
        pass

    return quotes


def _enrich_from_akshare(quotes: list[Quote]) -> None:
    """用 AKShare 补查 PE/PB/市值等丰富字段（原地修改）"""
    import akshare as ak
    from time import sleep

    code_map = {q.code: q for q in quotes}

    for code, q in code_map.items():
        try:
            df = ak.stock_zh_a_spot_individual_em(code)
            if df.empty:
                continue
            row = df.iloc[0]
            q.pe_ratio = _parse_float(row.get("市盈率(动态)"))
            q.pb_ratio = _parse_float(row.get("市净率"))
            q.market_cap = _parse_float(row.get("总市值"))
            if q.turnover_rate is None:
                q.turnover_rate = _parse_float(row.get("换手率"))
            q.upper_limit = _parse_float(row.get("涨停价"))
            q.lower_limit = _parse_float(row.get("跌停价"))
            sleep(0.3)
        except Exception:
            continue


# ============================================================
# 北向资金（带缓存）
# ============================================================

class NorthFlowFetcher:
    """北向资金获取器，内置缓存避免频繁请求"""

    def __init__(self, config: Config, cache_seconds: int = 1800) -> None:
        self._config = config
        self._cache_seconds = cache_seconds
        self._cache: Optional[NorthFlowData] = None
        self._last_fetch: float = 0.0

    def fetch(self) -> Optional[NorthFlowData]:
        """获取北向资金数据（带缓存）"""
        if not self._config.north_flow_enabled:
            return None

        now = time.time()
        if self._cache is not None and (now - self._last_fetch) < self._cache_seconds:
            return self._cache

        url = (f"{NORTH_FLOW_API}?fields1=f1,f2,f3,f4,f5"
               f"&fields2=f51,f52,f53,f54,f55&klt=1&lmt=1")

        resp = eastmoney_client.get(url)
        if resp is None:
            log.warning("北向资金API请求失败")
            return self._cache

        try:
            data = resp.json()

            if data.get("data") is None:
                return self._cache  # 返回缓存

            raw = data["data"]

            def _parse(key: str) -> tuple[float, float, str]:
                items = raw.get(key, [])
                if items:
                    parts = items[0].split(",")
                    if len(parts) >= 3:
                        return (
                            float(parts[1]) if parts[1] else 0.0,
                            float(parts[2]) if parts[2] else 0.0,
                            parts[0],
                        )
                return 0.0, 0.0, ""

            hk2sh_net, hk2sh_quota, date = _parse("hk2sh")
            hk2sz_net, hk2sz_quota, _ = _parse("hk2sz")

            self._cache = NorthFlowData(
                hk2sh_net=hk2sh_net,
                hk2sz_net=hk2sz_net,
                total_net=hk2sh_net + hk2sz_net,
                hk2sh_quota=hk2sh_quota,
                hk2sz_quota=hk2sz_quota,
                date=date,
            )
            self._last_fetch = now
            return self._cache

        except Exception as e:
            log.warning(f"北向资金解析失败: {e}")
            return self._cache


# ============================================================
# 全市场广度数据
# ============================================================

# 市场广度数据内存缓存
_breadth_cache: Optional["MarketBreadth"] = None
_breadth_fetch_time: float = 0.0
BREADTH_CACHE_SECONDS = 300  # 5分钟缓存


def fetch_market_breadth(force_refresh: bool = False) -> Optional["MarketBreadth"]:
    """获取全市场广度数据（涨跌家数、成交额、涨跌停等）

    通过东方财富 AKShare stock_zh_a_spot_em() 获取全 A 股快照，
    聚合计算涨跌分布、量能和极端情绪指标。
    带 5 分钟内存缓存，避免频繁全市场查询。

    Args:
        force_refresh: 是否强制刷新缓存

    Returns:
        MarketBreadth 对象，获取失败返回 None
    """
    global _breadth_cache, _breadth_fetch_time

    import time
    now = time.time()

    # 检查缓存
    if (not force_refresh
            and _breadth_cache is not None
            and (now - _breadth_fetch_time) < BREADTH_CACHE_SECONDS):
        return _breadth_cache

    try:
        import akshare as ak

        # 重试机制：盘中 API 可能因负载高而断连，最多重试 2 次
        df = None
        last_error = None
        for attempt in range(3):
            try:
                df = ak.stock_zh_a_spot_em()
                if df is not None and not df.empty:
                    break
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(2)
                    continue
        if df is None or df.empty:
            if last_error:
                log.info(f"全市场数据获取失败(已重试): {last_error}，使用旧缓存")
            else:
                log.info("全市场数据为空，使用旧缓存")
            return _breadth_cache  # 返回旧缓存

        # 聚合计算广度数据
        total = len(df)
        up_count = int((df["涨跌幅"] > 0).sum())
        down_count = int((df["涨跌幅"] < 0).sum())
        flat_count = total - up_count - down_count

        # 涨跌停（约 ±9.5% 以上）
        limit_up = int((df["涨跌幅"] >= 9.5).sum())
        limit_down = int((df["涨跌幅"] <= -9.5).sum())

        # 全市场成交额（亿元）
        total_amount = df["成交额"].sum() / 1e8 if "成交额" in df.columns else 0.0

        # 全市场成交量（万手）—— 如缺则用0
        total_volume = 0.0
        if "成交量" in df.columns:
            total_volume = df["成交量"].sum() / 1e4  # 转为万手

        # 主力净流入（亿元）—— 如缺则用0
        main_net = 0.0
        if "主力净流入" in df.columns:
            main_net = df["主力净流入"].sum() / 1e8

        # 取上证指数作为参考
        index_name = "上证指数"
        index_price = 0.0
        index_change_pct = 0.0

        sh_row = df[df["代码"] == "000001"]
        if not sh_row.empty:
            row = sh_row.iloc[0]
            index_price = float(row.get("最新价", 0) or 0)
            index_change_pct = float(row.get("涨跌幅", 0) or 0)
        else:
            # 兜底：取沪深300
            hs300 = df[df["代码"] == "000300"]
            if not hs300.empty:
                row = hs300.iloc[0]
                index_name = "沪深300"
                index_price = float(row.get("最新价", 0) or 0)
                index_change_pct = float(row.get("涨跌幅", 0) or 0)

        breadth = MarketBreadth(
            up_count=up_count,
            down_count=down_count,
            flat_count=flat_count,
            total_count=total,
            limit_up=limit_up,
            limit_down=limit_down,
            total_amount=round(total_amount, 1),
            total_volume=round(total_volume, 1),
            index_name=index_name,
            index_price=round(index_price, 2),
            index_change_pct=round(index_change_pct, 2),
            main_net_inflow=round(main_net, 1),
            update_time=time.strftime("%H:%M:%S"),
        )

        _breadth_cache = breadth
        _breadth_fetch_time = now
        log.info(
            f"市场广度: {breadth.breadth_label} | "
            f"涨{up_count}/跌{down_count}/平{flat_count} | "
            f"涨停{limit_up}/跌停{limit_down} | "
            f"成交{total_amount:.0f}亿 | "
            f"{index_name} {index_change_pct:+.2f}%"
        )

        return breadth

    except ImportError:
        log.info("AKShare 未安装，使用旧缓存或跳过")
    except Exception as e:
        log.info(f"AKShare 全市场数据获取失败: {e}，使用旧缓存")

    # 旧缓存延长有效期（获取失败时缓存从5分钟延长到2小时，避免AI分析无数据）
    if _breadth_cache is not None and (now - _breadth_fetch_time) < 7200:
        return _breadth_cache
    return None


# ============================================================
# 全球市场数据获取
# ============================================================

def fetch_global_markets() -> dict[str, str]:
    """获取全球市场数据（美股、A50期货、港股、汇率），并发请求加速"""

    def _fetch_us_stocks() -> dict[str, str]:
        """获取美股三大指数"""
        result: dict[str, str] = {}
        url = f"{SINA_API}gb_ixic,gb_dji,gb_inx"
        resp = sina_client.get(url)
        if not resp:
            return result
        resp.encoding = "gbk"
        for line in resp.text.split("\n"):
            match = re.search(r'"([^"]+)"', line)
            if not match:
                continue
            fields = match.group(1).split(",")
            if len(fields) < 3:
                continue
            if "ixic" in line:
                result["纳斯达克"] = f"{fields[1]} ({fields[2]}%)"
            elif "dji" in line:
                result["道琼斯"] = f"{fields[1]} ({fields[2]}%)"
            elif "inx" in line:
                result["标普500"] = f"{fields[1]} ({fields[2]}%)"
        return result

    def _fetch_single(name: str, code: str, url: str) -> dict[str, str]:
        """获取单个市场数据"""
        resp = sina_client.get(url)
        if not resp:
            return {}
        resp.encoding = "gbk"
        match = re.search(r'"([^"]+)"', resp.text)
        if not match:
            return {}
        fields = match.group(1).split(",")
        if len(fields) > 3:
            return {name: f"{fields[3]} ({fields[4]}%)"}
        return {}

    def _fetch_a50_futures() -> dict[str, str]:
        """通过东方财富获取A50期指当月连续"""
        try:
            resp = eastmoney_client.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={"secid": "104.CN00Y", "fields": "f2,f3,f14"},
            )
            if not resp:
                return {}
            data = resp.json()
            d = (data or {}).get("data") or {}
            price = d.get("f2")
            chg_pct = d.get("f3")
            if price is not None and chg_pct is not None:
                return {"A50期指当月连续": f"{price:.2f} ({chg_pct:+.2f}%)"}
        except Exception as e:
            log.warning(f"A50期货数据获取失败: {e}")
        return {}

    def _fetch_forex() -> dict[str, str]:
        """获取汇率"""
        resp = sina_client.get(f"{SINA_API}fx_susdcny")
        if not resp:
            return {}
        resp.encoding = "gbk"
        match = re.search(r'"([^"]+)"', resp.text)
        if not match:
            return {}
        fields = match.group(1).split(",")
        if len(fields) > 1:
            return {"汇率": f"USD/CNY {fields[1]}"}
        return {}

    def _fetch_us_treasury_yield() -> dict[str, str]:
        """获取美国10年期国债收益率"""
        try:
            resp = eastmoney_client.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={"secid": "122.DXY_TL", "fields": "f2,f3"},
            )
            if not resp:
                return {}
            data = resp.json()
            d = (data or {}).get("data") or {}
            yield_val = d.get("f2")
            if yield_val is not None:
                return {"美债10Y收益率": f"{yield_val:.2f}%"}
        except Exception as e:
            log.warning(f"美债收益率获取失败: {e}")
        return {}

    def _fetch_hk_index() -> dict[str, str]:
        """获取恒生指数（港股字段索引不同于美股/A股）"""
        resp = sina_client.get(f"{SINA_API}hkHSI")
        if not resp:
            return {}
        resp.encoding = "gbk"
        match = re.search(r'"([^"]+)"', resp.text)
        if not match:
            return {}
        fields = match.group(1).split(",")
        # 港股格式: 名称,今开,昨收,现价,最高,最低,涨跌额,涨跌幅,...
        if len(fields) > 7:
            price = fields[3]
            chg_pct = fields[7]
            return {"恒生指数": f"{price} ({chg_pct}%)"}
        return {}

    tasks = {
        "us": lambda: _fetch_us_stocks(),
        "a50": _fetch_a50_futures,
        "hsi": _fetch_hk_index,
        "forex": _fetch_forex,
        "treasury": _fetch_us_treasury_yield,
    }

    result: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(task): name for name, task in tasks.items()}
        for future in futures:
            try:
                result.update(future.result())
            except Exception as e:
                log.warning(f"全球市场数据[{futures[future]}]获取失败: {e}")

    return result


# ============================================================
# 市场快讯
# ============================================================

def fetch_market_news(start_hour: int, end_hour: int, max_count: int = 15) -> list[MarketNews]:
    """获取指定时间窗口内的市场快讯

    数据源：新浪财经滚动新闻（新浪 API 比东方财富更稳定）

    Args:
        start_hour: 开始小时 (0-23)
        end_hour:   结束小时 (0-23)
        max_count:  最多返回条数

    Returns:
        快讯列表，按时间倒序。网络异常时返回空列表。
    """
    from datetime import datetime

    # 新浪财经滚动新闻 API（lid=2510 = 财经要闻，比 2512 更纯净）
    url = "https://feed.mix.sina.com.cn/api/roll/get"
    params = {
        "pageid": "153",
        "lid": "2510",
        "num": str(max_count * 3),
        "versionNumber": "1.2.4",
    }

    try:
        import requests as _req
        resp = _req.get(url, params=params, timeout=10,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    except Exception as e:
        log.debug(f"快讯获取失败: {e}")
        return []

    if resp is None or resp.status_code != 200:
        return []

    try:
        data = resp.json()
        items = (data.get("result", {}) or {}).get("data", []) or []
    except Exception as e:
        log.warning(f"快讯解析失败: {e}")
        return []

    # 非财经内容黑名单
    NEWS_BLACKLIST = [
        # 彩票
        "双色球", "大乐透", "福彩", "体彩", "排列", "七星彩",
        "彩票", "竞彩", "足彩", "开奖", "预测奖号",
        # 体育
        "国乒", "乒乓", "女排", "男排", "篮球", "足球", "NBA",
        "CBA", "中超", "欧冠", "英超", "F1", "斯巴达", "勇士赛",
        "拳击", "散打", "格斗", "武", "拜师", "夺冠", "冠军",
        # 娱乐
        "订婚", "钻戒", "新娘", "婚礼",
        # 非财经广告
        "专家招募", "APP",
    ]

    news_list: list[MarketNews] = []
    for item in items:
        title = item.get("title", "")
        if not title:
            continue
        # 过滤非财经内容
        if any(kw in title for kw in NEWS_BLACKLIST):
            continue

        ctime = item.get("ctime", "")
        if not ctime:
            continue

        try:
            news_time = datetime.fromtimestamp(int(ctime))
        except (ValueError, TypeError):
            try:
                news_time = datetime.strptime(str(ctime), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                news_time = datetime.now()

        hour = news_time.hour
        if start_hour <= hour < end_hour:
            news_list.append(MarketNews(
                time=f"{news_time.hour:02d}:{news_time.minute:02d}",
                title=title,
                category=item.get("media_name", "") or "",
                content=(item.get("intro", "") or "")[:200],
                url=item.get("url", ""),
            ))

        if len(news_list) >= max_count:
            break

    return news_list


# ============================================================
# 后台数据缓存器（量比、换手率、主力净流入）
# ============================================================

import threading
from typing import Dict, Optional

class BackgroundDataCache:
    """后台数据缓存器：持续获取量比、换手率、主力净流入

    独立于盯盘循环运行，每隔一定时间刷新数据。
    盯盘时直接从缓存读取，不阻塞主循环。
    """

    def __init__(self, items: list[WatchItem], refresh_interval: int = 60) -> None:
        """
        Args:
            items: 盯盘标的列表
            refresh_interval: 刷新间隔（秒），默认60秒
        """
        self._items = items
        self._refresh_interval = refresh_interval
        self._lock = threading.Lock()

        # 缓存数据: {code: {volume_ratio: x, turnover_rate: y, main_net_inflow: z, fund_flow: FundFlowDetail}}
        self._cache: Dict[str, Dict[str, Optional[float | FundFlowDetail]]] = {}
        self._last_update: float = 0.0

        # 控制线程
        self._stop_event = threading.Event()
        self._tencent_thread: Optional[threading.Thread] = None  # 量比、换手率线程
        self._flow_thread: Optional[threading.Thread] = None     # 主力净流入线程

    def start(self) -> None:
        """启动后台刷新线程"""
        if self._tencent_thread and self._tencent_thread.is_alive():
            return
        if self._flow_thread and self._flow_thread.is_alive():
            return
        self._stop_event.clear()
        # 启动两个独立线程：一个获取量比/换手率，一个获取主力净流入
        self._tencent_thread = threading.Thread(target=self._run_tencent, daemon=True)
        self._tencent_thread.start()
        self._flow_thread = threading.Thread(target=self._run_flow, daemon=True)
        self._flow_thread.start()
        # 立即获取一次量比和换手率 + 主力资金流向
        self._refresh_tencent()
        try:
            self._refresh_flow()
        except Exception:
            pass  # 首次获取失败不影响，后续线程会重试

    def stop(self) -> None:
        """停止后台刷新线程"""
        self._stop_event.set()
        if self._tencent_thread:
            self._tencent_thread.join(timeout=3)
        if self._flow_thread:
            self._flow_thread.join(timeout=3)

    def get_data(self, code: str) -> Dict[str, Optional[float]]:
        """获取指定代码的缓存数据"""
        with self._lock:
            return self._cache.get(code, {}).copy()

    def get_all_data(self) -> Dict[str, Dict[str, Optional[float]]]:
        """获取所有缓存数据"""
        with self._lock:
            return {k: v.copy() for k, v in self._cache.items()}

    def is_fresh(self) -> bool:
        """检查缓存是否已更新过"""
        return self._last_update > 0

    def _run_tencent(self) -> None:
        """后台刷新循环：量比和换手率（腾讯财经）"""
        while not self._stop_event.is_set():
            try:
                self._refresh_tencent()
            except Exception as e:
                log.warning(f"腾讯数据刷新异常: {e}")
            # 等待下一个刷新周期
            for _ in range(self._refresh_interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def _run_flow(self) -> None:
        """后台刷新循环：主力净流入（东方财富）"""
        # 首次等待5秒再开始（start()中已做一次即时获取）
        for _ in range(5):
            if self._stop_event.is_set():
                return
            time.sleep(1)
        # 主力净流入刷新间隔更长（300秒=5分钟），因为API容易被屏蔽
        flow_interval = 300
        while not self._stop_event.is_set():
            try:
                self._refresh_flow()
            except Exception:
                pass  # 静默失败
            # 等待下一个刷新周期
            for _ in range(flow_interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def _refresh_tencent(self) -> None:
        """刷新量比和换手率（腾讯财经）"""
        if not self._items:
            return

        # 从腾讯财经获取量比和换手率
        try:
            tencent_data = fetch_tencent_data(self._items)
        except Exception as e:
            log.warning(f"腾讯数据获取失败: {e}")
            return

        # 更新缓存（只更新量比、换手率、外盘、内盘、委比，不覆盖主力净流入）
        with self._lock:
            for code, data in tencent_data.items():
                if code not in self._cache:
                    self._cache[code] = {"volume_ratio": None, "turnover_rate": None, "main_net_inflow": None, "bid_volume": None, "ask_volume": None, "bid_ask_ratio": None, "fund_flow": None}
                if data.get("volume_ratio") is not None:
                    self._cache[code]["volume_ratio"] = data["volume_ratio"]
                if data.get("turnover_rate") is not None:
                    self._cache[code]["turnover_rate"] = data["turnover_rate"]
                if data.get("bid_volume") is not None:
                    self._cache[code]["bid_volume"] = data["bid_volume"]
                if data.get("ask_volume") is not None:
                    self._cache[code]["ask_volume"] = data["ask_volume"]
                if data.get("bid_ask_ratio") is not None:
                    self._cache[code]["bid_ask_ratio"] = data["bid_ask_ratio"]
            self._last_update = time.time()

    def _refresh_flow(self) -> None:
        """刷新资金流向明细（东方财富，静默失败）"""
        if not self._items:
            return

        for item in self._items:
            if self._stop_event.is_set():
                return
            code = item.code
            # 获取完整资金流向明细
            detail = fetch_fund_flow_detail(code, item.market)
            with self._lock:
                if code not in self._cache:
                    self._cache[code] = {"volume_ratio": None, "turnover_rate": None, "main_net_inflow": None, "bid_volume": None, "ask_volume": None, "bid_ask_ratio": None, "fund_flow": None}
                if detail is not None:
                    self._cache[code]["fund_flow"] = detail
                    # 向后兼容：同时存 main_net_inflow
                    if detail.main_net is not None:
                        self._cache[code]["main_net_inflow"] = detail.main_net
            time.sleep(0.5)  # 请求间隔


# ============================================================
# 行业板块数据获取
# ============================================================

# 板块数据缓存（避免每次扫描都拉取全量板块数据）
_sector_cache: dict = {"_ts": 0.0, "_data": []}
_SECTOR_CACHE_TTL = 300  # 板块数据缓存 5 分钟


def fetch_stock_industry_map(codes: list[str]) -> dict[str, str]:
    """批量获取个股所属行业（带日级缓存）

    首次调用时拉取全市场行业映射并缓存到 state/industry_cache.json，
    同日后续调用直接读缓存，避免重复拉取 5000+ 条全市场数据。

    Args:
        codes: 股票代码列表

    Returns:
        {code: industry} 映射字典
    """
    import json
    from pathlib import Path

    today = datetime.now().strftime("%Y%m%d")
    cache_dir = Path(__file__).resolve().parent.parent / "state"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "industry_cache.json"

    full_map: dict[str, str] = {}
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("_date") == today:
                full_map = {k: v for k, v in cached.items() if not k.startswith("_")}
        except Exception:
            pass

    if not full_map:
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                industry = str(row.get("行业", ""))
                if code and industry:
                    full_map[code] = industry
            cache_data = {"_date": today, **full_map}
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False)
            log.info(f"行业分类已缓存: {len(full_map)} 只个股 → {cache_path}")
        except Exception as e:
            log.debug(f"行业分类获取失败: {e}")
            return {}

    return {code: full_map[code] for code in codes if code in full_map}


def fetch_stock_listing_date_map(codes: list[str]) -> dict[str, str]:
    """批量获取个股上市日期（带日级缓存，用于新股/次新股过滤）

    数据源：东方财富 clist 全 A 股列表（f26=上市日期 YYYYMMDD）。
    首次调用拉取全市场并缓存到 state/listing_cache.json，同日后续调用直接读缓存。

    Args:
        codes: 股票代码列表

    Returns:
        {code: 上市日期(YYYYMMDD)} 映射字典
    """
    import json
    from pathlib import Path

    today = datetime.now().strftime("%Y%m%d")
    cache_dir = Path(__file__).resolve().parent.parent / "state"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "listing_cache.json"

    full_map: dict[str, str] = {}
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("_date") == today:
                full_map = {k: v for k, v in cached.items() if not k.startswith("_")}
        except Exception:
            pass

    if not full_map:
        try:
            url = (
                "https://push2.eastmoney.com/api/qt/clist/get"
                "?pn=1&pz=6000&po=1&np=1&fltt=2&invt=2&fid=f12"
                "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                "&fields=f12,f14,f26"
            )
            resp = eastmoney_client.get(url, timeout=15)
            if resp is not None:
                data = resp.json()
                diff = (data.get("data") or {}).get("diff") or []
                # 分页时 diff 可能是 dict（键为页码序号），归一为 list
                if isinstance(diff, dict):
                    diff = list(diff.values())
                for item in diff:
                    code = str(item.get("f12", ""))
                    list_date = item.get("f26")
                    if code and list_date not in (None, "", "-"):
                        full_map[code] = str(list_date)
            if full_map:
                cache_data = {"_date": today, **full_map}
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache_data, f, ensure_ascii=False)
                log.info(f"上市日期已缓存: {len(full_map)} 只个股 → {cache_path}")
        except Exception as e:
            log.debug(f"上市日期获取失败: {e}")
            return {}

    return {code: full_map[code] for code in codes if code in full_map}


def _etf_name_to_industry(name: str) -> str:
    """从 ETF 名称推断所属行业板块"""
    mapping = {
        "银行": "银行", "金融": "银行",
        "芯片": "半导体", "半导体": "半导体",
        "证券": "券商", "券商": "券商",
        "军工": "军工", "国防": "军工",
        "医药": "医药", "医疗": "医药", "生物医药": "医药",
        "白酒": "酿酒", "酒": "酿酒",
        "新能源": "新能源", "光伏": "新能源", "锂电": "新能源",
        "人工智能": "人工智能", "AI": "人工智能",
        "通信": "通信", "5G": "通信",
        "消费": "消费", "食品饮料": "食品饮料", "食品": "食品饮料",
        "汽车": "汽车", "智能汽车": "汽车", "新能源车": "汽车",
        "养殖": "农牧", "农业": "农牧",
        "房地产": "房地产", "地产": "房地产",
        "煤炭": "煤炭", "有色": "有色", "钢铁": "钢铁", "稀土": "有色",
        "化工": "化工",
        "传媒": "传媒", "游戏": "传媒",
        "计算机": "计算机", "软件": "计算机", "信创": "计算机",
        "电力": "电力", "绿色电力": "电力",
        "恒生科技": "港股科技", "港股科技": "港股科技",
        "恒生互联": "港股互联网", "港股互联网": "港股互联网",
        "恒生中国": "港股", "H股": "港股",
        "恒生": "港股", "中概": "中概互联",
        "科技": "科技", "科创": "科创",
        "消费电子": "消费电子", "消电": "消费电子",
        "红利": "红利", "中证红利": "红利",
        "创业": "创业板", "创成长": "创业板",
        "沪深300": "沪深300", "上证50": "上证50", "中证500": "中证500", "中证1000": "中证1000",
        "红利低波": "红利",
        "兴全趋势": "混合基金",
        "华夏翔阳": "混合基金",
    }
    for keyword, sector in mapping.items():
        if keyword in name:
            return sector
    return ""


def enrich_quotes_with_industry(quotes: list[Quote]) -> None:
    """为 Quote 列表补充行业分类（原地修改）

    优先用 ETF 名称推断，个股回退到东方财富行业映射。
    """
    from app.models import Quote
    etf_codes: list[str] = []
    stock_codes: list[str] = []
    for q in quotes:
        if q.industry:
            continue
        # ETF 用名称推断
        inferred = _etf_name_to_industry(q.name)
        if inferred:
            q.industry = inferred
        else:
            stock_codes.append(q.code)

    if stock_codes:
        industry_map = fetch_stock_industry_map(stock_codes)
        for q in quotes:
            if not q.industry and q.code in industry_map:
                q.industry = industry_map[q.code]


def fetch_sector_boards() -> list[SectorBoard]:
    """获取东方财富行业板块实时数据（带内存缓存 5 分钟）

    Returns:
        SectorBoard 列表，按涨跌幅降序排列
    """
    global _sector_cache
    now = time.time()
    if now - _sector_cache["_ts"] < _SECTOR_CACHE_TTL and _sector_cache["_data"]:
        return _sector_cache["_data"]

    boards: list[SectorBoard] = []
    try:
        import akshare as ak
        df = ak.stock_board_industry_index_em()
        for _, row in df.iterrows():
            boards.append(SectorBoard(
                code=str(row.get("板块代码", "")),
                name=str(row.get("板块名称", "")),
                change_pct=_safe_float(row.get("最新价")),
                amount=_safe_float(row.get("成交额")),
                leader_stock=str(row.get("领涨股", "")),
                leader_change_pct=_safe_float(row.get("领涨股-涨跌幅")),
                main_net_inflow=_safe_float(row.get("主力净流入")),
                stock_count=int(row.get("公司家数", 0)) if row.get("公司家数") else 0,
            ))
    except Exception as e:
        log.debug(f"行业板块数据获取失败: {e}")
        return _sector_cache["_data"]  # 返回旧缓存

    boards.sort(key=lambda b: b.change_pct or 0, reverse=True)
    _sector_cache = {"_ts": now, "_data": boards}
    log.debug(f"行业板块数据已更新: {len(boards)} 个板块")
    return boards


def _safe_float(val) -> Optional[float]:
    """安全转换为 float"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def fetch_major_indices() -> list[Quote]:
    """批量获取核心大盘指数

    Returns:
        Quote 列表（上证、深证、创业板、科创50、沪深300、中证500、中证1000）
    """
    indices = [
        WatchItem(name="上证指数", code="000001", market="SH", type="指数"),
        WatchItem(name="深证成指", code="399001", market="SZ", type="指数"),
        WatchItem(name="创业板指", code="399006", market="SZ", type="指数"),
        WatchItem(name="科创50", code="000688", market="SH", type="指数"),
        WatchItem(name="沪深300", code="000300", market="SH", type="指数"),
        WatchItem(name="中证500", code="000905", market="SH", type="指数"),
        WatchItem(name="中证1000", code="000852", market="SH", type="指数"),
    ]
    try:
        return fetch_quotes_rich(indices)
    except Exception:
        return []


# 大盘指数K线缓存
_index_klines_cache: dict = {"_ts": 0, "_data": {}}
_INDEX_KLINES_TTL = 600  # 10分钟


def fetch_index_klines(codes: list[str]) -> dict[str, list]:
    """批量获取大盘指数60日K线（带缓存）"""
    global _index_klines_cache
    now = time.time()
    if now - _index_klines_cache["_ts"] < _INDEX_KLINES_TTL and _index_klines_cache["_data"]:
        return {k: v for k, v in _index_klines_cache["_data"].items() if k in codes}

    from app.technical import fetch_historical_kline
    result = {}
    for code in codes:
        market = "SH" if code.startswith(("0", "5", "6", "9")) else "SZ"
        try:
            kls = fetch_historical_kline(code, market, days=60)
            if kls:
                result[code] = kls
        except Exception:
            pass
    _index_klines_cache = {"_ts": now, "_data": result}
    return result


# 两融数据缓存
_margin_cache: dict = {"_ts": 0.0, "_data": None}
_MARGIN_CACHE_TTL = 1800  # 30分钟（两融是日频数据）


def fetch_margin_data(force_refresh: bool = False) -> Optional["MarginData"]:
    """获取全市场两融数据（融资融券余额，替代已停披露的北向资金）

    数据来源：上交所 + 深交所融资融券余额（AKShare）
    日频数据，带30分钟缓存。

    Returns:
        MarginData 或 None（获取失败）
    """
    global _margin_cache
    now = time.time()
    if (not force_refresh and _margin_cache["_data"] is not None
            and now - _margin_cache["_ts"] < _MARGIN_CACHE_TTL):
        return _margin_cache["_data"]

    try:
        import akshare as ak
        from datetime import datetime as _dt, timedelta

        # 上交所 + 深交所两融数据（两接口签名不同）
        end = _dt.now()
        start = end - timedelta(days=30)
        sh_df = ak.stock_margin_sse(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        # 深交所接口只接受单日 date，回溯找最近有数据的一天
        sz_df = None
        for back in range(0, 10):
            d = (end - timedelta(days=back)).strftime("%Y%m%d")
            try:
                sz_df = ak.stock_margin_szse(date=d)
                if sz_df is not None and not sz_df.empty:
                    break
            except Exception:
                continue
        if sz_df is None:
            sz_df = ak.stock_margin_szse()  # 默认日期兜底

        # 取最新一条（两市日期可能不同，用最新的一天）
        if sh_df.empty and sz_df.empty:
            return _margin_cache["_data"]

        # 上交所：数值单位是「元」，需除以 1e8 转亿
        sh_latest = sh_df.iloc[-1] if not sh_df.empty else None
        sh_prev = sh_df.iloc[-2] if len(sh_df) >= 2 else None
        # 深交所：数值单位已是「亿元」
        sz_latest = sz_df.iloc[-1] if (sz_df is not None and not sz_df.empty) else None

        fin_bal_yi = 0.0    # 融资余额（亿）
        prev_fin_bal_yi = 0.0
        sec_bal_yi = 0.0    # 融券余额（亿）
        date = ""

        # 上交所（元 → 亿）
        if sh_latest is not None:
            fin_bal_yi += float(sh_latest.get("融资余额", 0) or 0) / 1e8
            sec_bal_yi += float(sh_latest.get("融券余量金额", 0) or 0) / 1e8
            if sh_prev is not None:
                prev_fin_bal_yi += float(sh_prev.get("融资余额", 0) or 0) / 1e8
            date = str(sh_latest.get("信用交易日期", "")) or date

        # 深交所（已是亿元）
        if sz_latest is not None:
            fin_bal_yi += float(sz_latest.get("融资余额", 0) or 0)
            sec_bal_yi += float(sz_latest.get("融券余额", 0) or 0)

        # 融资净买入 = 上交所今日融资余额 - 昨日融资余额（深交所无历史，只取单日）
        sh_fin_today = 0.0
        sh_fin_prev = 0.0
        if sh_latest is not None:
            sh_fin_today = float(sh_latest.get("融资余额", 0) or 0) / 1e8
        if sh_prev is not None:
            sh_fin_prev = float(sh_prev.get("融资余额", 0) or 0) / 1e8
        net_buy = sh_fin_today - sh_fin_prev if sh_fin_prev > 0 else 0.0
        total = fin_bal_yi + sec_bal_yi

        data = MarginData(
            financing_balance=round(fin_bal_yi, 1),
            financing_net_buy=round(net_buy, 1),
            securities_lending_balance=round(sec_bal_yi, 1),
            total_balance=round(total, 1),
            date=date,
        )
        _margin_cache = {"_ts": now, "_data": data}
        return data
    except Exception as e:
        log.debug(f"两融数据获取失败: {e}")
        return _margin_cache["_data"]

