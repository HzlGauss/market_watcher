"""
数据获取模块 —— 新浪财经实时行情 + 北向资金
"""

from __future__ import annotations
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.models import Quote, WatchItem, MARKET_PREFIX, NorthFlowData, MarketNews
from app.config import Config
from app.utils import log
from app.http_client import sina_client, eastmoney_client

# ============================================================
# 常量
# ============================================================

SINA_API = "https://hq.sinajs.cn/list="
NORTH_FLOW_API = "https://push2.eastmoney.com/api/qt/kamt.kline/get"
STOCK_FLOW_API = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
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
                volume=_parse_float(fields[8]),
                amount=_parse_float(fields[9]),
                amplitude=amplitude,
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
    url = (f"{STOCK_FLOW_API}?secid={secid}"
           f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")

    # 使用无重试的session，避免产生大量WARNING日志
    import requests
    try:
        resp = requests.get(url, timeout=3, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        })
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("data") is None or not data["data"].get("f52"):
            return None
        return _parse_float(data["data"]["f52"])
    except Exception:
        return None  # 静默失败


def enrich_quotes_with_flow(quotes: list[Quote]) -> None:
    """为 Quote 列表批量补充主力净流入（原地修改）

    使用线程池并发请求，每只股票独立请求东方财富资金流接口。
    """
    if not quotes:
        return

    def _fetch_one(q: Quote) -> tuple[str, Optional[float]]:
        flow = fetch_main_net_inflow(q.code, q.code.startswith(("6", "9")) and "SH" or "SZ")
        return (q.code, flow)

    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_fetch_one, q) for q in quotes]
            for fut in futures:
                try:
                    code, flow = fut.result(timeout=5)
                    for q in quotes:
                        if q.code == code:
                            q.main_net_inflow = flow
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
            volume=_parse_float(row.get("成交量")),
            amount=_parse_float(row.get("成交额")),
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

    Args:
        start_hour: 开始小时 (0-23)
        end_hour:   结束小时 (0-23)
        max_count:  最多返回条数

    Returns:
        快讯列表，按时间倒序。网络异常时返回空列表。
    """
    from datetime import datetime

    url = "https://www.eastmoney.com/commweb/api/newsFlow"
    params = {"client": "web", "channel": "65", "pageSize": str(max_count * 2)}

    try:
        resp = eastmoney_client.get(url, params=params)
    except Exception as e:
        log.debug(f"快讯获取失败: {e}")
        return []

    if resp is None:
        return []

    try:
        data = resp.json()
        items = (data or {}).get("data", []) or []
    except Exception as e:
        log.warning(f"快讯解析失败: {e}")
        return []

    news_list: list[MarketNews] = []
    for item in items:
        if not item.get("title"):
            continue

        ctime = item.get("ctime", "")
        if not ctime:
            continue

        try:
            news_time = datetime.strptime(ctime, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            news_time = datetime.now()

        hour = news_time.hour
        if start_hour <= hour < end_hour:
            news_list.append(MarketNews(
                time=f"{news_time.hour:02d}:{news_time.minute:02d}",
                title=item["title"],
                category=item.get("category", ""),
                content=(item.get("content", "") or "")[:200],
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

        # 缓存数据: {code: {volume_ratio: x, turnover_rate: y, main_net_inflow: z}}
        self._cache: Dict[str, Dict[str, Optional[float]]] = {}
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
        # 立即获取一次量比和换手率
        self._refresh_tencent()

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
        # 首次等待10秒再开始，避免启动时请求过多
        for _ in range(10):
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
                    self._cache[code] = {"volume_ratio": None, "turnover_rate": None, "main_net_inflow": None, "bid_volume": None, "ask_volume": None, "bid_ask_ratio": None}
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
        """刷新主力净流入（东方财富，静默失败）"""
        if not self._items:
            return

        for item in self._items:
            if self._stop_event.is_set():
                return
            code = item.code
            # 每只股票单独请求，加延迟避免限流
            flow = fetch_main_net_inflow(code, item.market)
            with self._lock:
                if code not in self._cache:
                    self._cache[code] = {"volume_ratio": None, "turnover_rate": None, "main_net_inflow": None, "bid_volume": None, "ask_volume": None}
                if flow is not None:
                    self._cache[code]["main_net_inflow"] = flow
            time.sleep(0.5)  # 请求间隔

