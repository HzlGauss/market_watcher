"""
数据获取模块 —— 新浪财经实时行情 + 北向资金
"""

from __future__ import annotations
import re
import time
from typing import Optional

from app.models import Quote, WatchItem, MARKET_PREFIX, NorthFlowData
from app.config import Config
from app.utils import log
from app.http_client import sina_client, eastmoney_client

# ============================================================
# 常量
# ============================================================

SINA_API = "https://hq.sinajs.cn/list="
NORTH_FLOW_API = "https://push2.eastmoney.com/api/qt/kamt.kline/get"


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
    """批量获取实时行情（新浪财经）

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
            ))

        return results

    except Exception as e:
        log.error(f"数据解析异常: {e}")
        return []


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
    """获取全球市场数据（美股、A50期货、港股、汇率）"""
    result = {}

    try:
        # 获取美股数据
        us_stocks = ["gb_USTECH", "gb_US30", "gb_US500"]
        url = f"{SINA_API}{','.join(us_stocks)}"
        resp = sina_client.get(url)

        if resp:
            resp.encoding = "gbk"
            text = resp.text
            for line in text.split("\n"):
                if "USTECH" in line:
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        fields = match.group(1).split(",")
                        if len(fields) > 3:
                            result["纳斯达克"] = f"{fields[3]} ({fields[4]}%)"
                elif "US30" in line:
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        fields = match.group(1).split(",")
                        if len(fields) > 3:
                            result["道琼斯"] = f"{fields[3]} ({fields[4]}%)"
                elif "US500" in line:
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        fields = match.group(1).split(",")
                        if len(fields) > 3:
                            result["标普500"] = f"{fields[3]} ({fields[4]}%)"

        # 获取A50期货
        a50_url = f"{SINA_API}gb_NQH2"
        resp = sina_client.get(a50_url)
        if resp:
            resp.encoding = "gbk"
            match = re.search(r'"([^"]+)"', resp.text)
            if match:
                fields = match.group(1).split(",")
                if len(fields) > 3:
                    result["A50期货"] = f"{fields[3]} ({fields[4]}%)"

        # 获取恒生指数
        hsi_url = f"{SINA_API}hkHSI"
        resp = sina_client.get(hsi_url)
        if resp:
            resp.encoding = "gbk"
            match = re.search(r'"([^"]+)"', resp.text)
            if match:
                fields = match.group(1).split(",")
                if len(fields) > 3:
                    result["恒生指数"] = f"{fields[3]} ({fields[4]}%)"

        # 获取汇率
        forex_url = f"{SINA_API}fx_susdcny"
        resp = sina_client.get(forex_url)
        if resp:
            resp.encoding = "gbk"
            match = re.search(r'"([^"]+)"', resp.text)
            if match:
                fields = match.group(1).split(",")
                if len(fields) > 1:
                    result["汇率"] = f"USD/CNY {fields[1]}"

    except Exception as e:
        log.warning(f"全球市场数据获取失败: {e}")

    return result
