"""
技术分析模块 —— 纯 Python 实现常见技术指标

不依赖 numpy/pandas，保持依赖最小。
提供 K线数据获取、RSI、MACD、KDJ、支撑/压力位计算。
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.models import KlineData, Quote, TechnicalSummary
from app.http_client import sina_client
from app.utils import log


def estimate_full_day_volume(quote: Quote) -> Optional[float]:
    """估算全天成交量（用于午盘时段）

    午盘时 quote.volume 只有半天量，直接与历史全日均量对比会导致量比虚低。
    如果当前是午盘（< 12:30），将当前量乘以 2 作为全天量估算。

    Returns:
        估算的全天成交量，如果已是收盘后则返回原始 volume
    """
    if quote.volume is None or quote.volume <= 0:
        return None
    now = datetime.now()
    if now.hour < 12 or (now.hour == 12 and now.minute < 30):
        return quote.volume * 2
    return quote.volume


# ============================================================
# K线数据获取
# ============================================================

def fetch_historical_kline(code: str, market: str, days: int = 30) -> list[KlineData]:
    """获取日K线数据（新浪主源 + AKShare 兜底）

    Args:
        code: 股票代码
        market: 市场标识 (SH/SZ/HK)
        days: 获取天数

    Returns:
        K线数据列表（按日期升序）
    """
    prefix = {"SH": "sh", "SZ": "sz", "HK": "hk"}.get(market, "sh")
    sina_code = f"{prefix}{code}"

    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sina_code}&scale=240&"
        f"&ma=no&datalen={days}"
    )

    resp = sina_client.get(url)
    if resp is not None:
        try:
            data = resp.json()
            if data:
                results: list[KlineData] = []
                for item in data:
                    results.append(KlineData(
                        date=item.get("day", ""),
                        open=_sf(item.get("open")),
                        high=_sf(item.get("high")),
                        low=_sf(item.get("low")),
                        close=_sf(item.get("close")),
                        volume=_sf(item.get("volume")),
                    ))
                if results:
                    return results
        except Exception as e:
            log.warning(f"K线数据解析失败 {code}: {e}")

    # 新浪数据为空或解析失败，尝试 AKShare 兜底
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq", start_date="", end_date="")
        if df is not None and not df.empty:
            df = df.tail(days).reset_index(drop=True)
            results = []
            for _, row in df.iterrows():
                results.append(KlineData(
                    date=str(row.get("日期", "")),
                    open=_sf(row.get("开盘")),
                    high=_sf(row.get("最高")),
                    low=_sf(row.get("最低")),
                    close=_sf(row.get("收盘")),
                    volume=_sf(row.get("成交量")),
                ))
            if results:
                return results
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"AKShare K线数据获取失败 {code}: {e}")

    log.warning(f"K线数据获取失败: {code}")
    return []


def _sf(val) -> Optional[float]:
    """安全浮点转换"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ============================================================
# 近期走势计算
# ============================================================

@dataclass
class PeriodReturn:
    """指定周期涨跌幅"""
    label: str = ""
    days: int = 0
    return_pct: Optional[float] = None
    start_price: Optional[float] = None
    end_price: Optional[float] = None
    high_price: Optional[float] = None
    low_price: Optional[float] = None


def calc_period_returns(klines: list[KlineData], periods: list[tuple[int, str]] = None) -> list[PeriodReturn]:
    """计算多个周期的涨跌幅

    Args:
        klines: K线数据（按时间升序）
        periods: [(天数, 标签), ...] 如 [(5, "近1周"), (10, "近半月")]

    Returns:
        PeriodReturn 列表
    """
    if periods is None:
        periods = [
            (5, "近1周"),
            (10, "近半月"),
            (20, "近1月"),
            (40, "近两月"),
        ]

    results: list[PeriodReturn] = []

    for days, label in periods:
        if len(klines) < days + 1:
            continue

        # 取最近 days 个交易日的区间
        window = klines[-(days + 1):]
        start_kline = window[0]
        end_kline = window[-1]

        if start_kline.close is None or end_kline.close is None:
            continue

        # 计算区间最高/最低
        closes = [k.close for k in window if k.close is not None]
        high_price = max(closes) if closes else None
        low_price = min(closes) if closes else None

        return_pct = ((end_kline.close - start_kline.close) / start_kline.close) * 100

        results.append(PeriodReturn(
            label=label,
            days=days,
            return_pct=round(return_pct, 2),
            start_price=start_kline.close,
            end_price=end_kline.close,
            high_price=high_price,
            low_price=low_price,
        ))

    return results


# ============================================================
# EMA 辅助
# ============================================================

def _ema(values: list[float], period: int) -> list[float]:
    """计算 EMA（指数移动平均）"""
    if not values:
        return []
    multiplier = 2.0 / (period + 1)
    ema_vals = [values[0]]
    for i in range(1, len(values)):
        ema_vals.append(values[i] * multiplier + ema_vals[-1] * (1 - multiplier))
    return ema_vals


# ============================================================
# RSI —— 相对强弱指标
# ============================================================

def calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """计算 RSI

    Args:
        closes: 收盘价序列
        period: 周期（默认14）

    Returns:
        RSI 值 (0-100)，数据不足时返回 None
    """
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # 平滑处理后续值
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1 + rs), 2)


def rsi_signal(rsi: Optional[float]) -> str:
    """RSI 信号判断"""
    if rsi is None:
        return "数据不足"
    if rsi >= 80:
        return "严重超买"
    if rsi >= 70:
        return "超买"
    if rsi <= 20:
        return "严重超卖"
    if rsi <= 30:
        return "超卖"
    return "中性"


# ============================================================
# MACD —— 指数平滑异同移动平均线
# ============================================================

@dataclass
class MACDResult:
    dif: Optional[float] = None
    dea: Optional[float] = None
    histogram: Optional[float] = None
    signal: str = ""


def calc_macd(closes: list[float], short: int = 12, long: int = 26, signal_period: int = 9) -> MACDResult:
    """计算 MACD

    Args:
        closes: 收盘价序列
        short: 快线周期
        long: 慢线周期
        signal_period: 信号线周期

    Returns:
        MACDResult 包含 DIF、DEA、柱状图和信号
    """
    if len(closes) < long + signal_period:
        return MACDResult(signal="数据不足")

    ema_short = _ema(closes, short)
    ema_long = _ema(closes, long)

    dif_vals = [s - l for s, l in zip(ema_short, ema_long)]
    dea_vals = _ema(dif_vals, signal_period)

    dif = round(dif_vals[-1], 4)
    dea = round(dea_vals[-1], 4)
    histogram = round(2 * (dif - dea), 4)

    # 金叉 / 死叉判断（比较前一日和当日）
    signal = "中性"
    if len(dif_vals) >= 2 and len(dea_vals) >= 2:
        prev_cross = dif_vals[-2] - dea_vals[-2]
        curr_cross = dif_vals[-1] - dea_vals[-1]
        if prev_cross <= 0 and curr_cross > 0:
            signal = "金叉"
        elif prev_cross >= 0 and curr_cross < 0:
            signal = "死叉"
        elif curr_cross > 0:
            signal = "多头"
        else:
            signal = "空头"

    return MACDResult(dif=dif, dea=dea, histogram=histogram, signal=signal)


# ============================================================
# KDJ —— 随机指标
# ============================================================

@dataclass
class KDJResult:
    k: Optional[float] = None
    d: Optional[float] = None
    j: Optional[float] = None
    signal: str = ""


def calc_kdj(highs: list[float], lows: list[float], closes: list[float], n: int = 9) -> KDJResult:
    """计算 KDJ

    Args:
        highs: 最高价序列
        lows: 最低价序列
        closes: 收盘价序列
        n: 周期（默认9）

    Returns:
        KDJResult 包含 K、D、J 和信号
    """
    length = min(len(highs), len(lows), len(closes))
    if length < n:
        return KDJResult(signal="数据不足")

    # 计算 RSV 序列
    rsv_vals = []
    for i in range(n - 1, length):
        hh = max(highs[i - n + 1: i + 1])
        ll = min(lows[i - n + 1: i + 1])
        if hh == ll:
            rsv_vals.append(50.0)
        else:
            rsv = (closes[i] - ll) / (hh - ll) * 100
            rsv_vals.append(rsv)

    if not rsv_vals:
        return KDJResult(signal="数据不足")

    # SMA 计算 K 和 D
    k_vals = [rsv_vals[0]]
    d_vals = [rsv_vals[0]]
    for rsv in rsv_vals[1:]:
        k_vals.append(2 / 3 * k_vals[-1] + 1 / 3 * rsv)
        d_vals.append(2 / 3 * d_vals[-1] + 1 / 3 * k_vals[-1])

    k = round(k_vals[-1], 2)
    d = round(d_vals[-1], 2)
    j = round(3 * k - 2 * d, 2)

    # 信号判断
    signal = "中性"
    if k >= 80 or j >= 100:
        signal = "超买"
    elif k <= 20 or j <= 0:
        signal = "超卖"
    elif len(k_vals) >= 2:
        if k_vals[-2] < d_vals[-2] and k_vals[-1] > d_vals[-1]:
            signal = "金叉"
        elif k_vals[-2] > d_vals[-2] and k_vals[-1] < d_vals[-1]:
            signal = "死叉"

    return KDJResult(k=k, d=d, j=j, signal=signal)


# ============================================================
# 支撑 / 压力位
# ============================================================

@dataclass
class SupportResistance:
    support: Optional[float] = None
    resistance: Optional[float] = None
    atr: Optional[float] = None
    swing_supports: list[float] = None
    swing_resistances: list[float] = None
    pivot_supports: list[float] = None
    pivot_resistances: list[float] = None
    volume_clusters: list[float] = None


def _calc_atr(klines: list[KlineData], period: int = 14) -> Optional[float]:
    """计算 ATR（平均真实波动幅度）

    Args:
        klines: K线数据
        period: 周期（默认14）

    Returns:
        ATR 值
    """
    if len(klines) < 2:
        return None

    true_ranges = []
    for i in range(1, len(klines)):
        h = klines[i].high
        l = klines[i].low
        prev_c = klines[i - 1].close
        if h is not None and l is not None and prev_c is not None:
            true_ranges.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))

    if not true_ranges:
        return None

    true_ranges = true_ranges[-period:]
    return round(sum(true_ranges) / len(true_ranges), 4)


def _find_swing_points(klines: list[KlineData], left: int = 3, right: int = 3) -> tuple[list[float], list[float]]:
    """寻找摆动高低点（Swing High/Low）

    摆动高点：左右各N天内都是最高点
    摆动低点：左右各N天内都是最低点

    Args:
        klines: K线数据（按时间升序）
        left: 左侧天数
        right: 右侧天数

    Returns:
        (swing_lows, swing_highs) 排序后的摆动低点和高点列表
    """
    if len(klines) < left + right + 1:
        return [], []

    swing_highs = []
    swing_lows = []

    for i in range(left, len(klines) - right):
        window = klines[i - left:i + right + 1]
        current = klines[i]

        if current.high is None or current.low is None:
            continue

        window_highs = [k.high for k in window if k.high is not None]
        window_lows = [k.low for k in window if k.low is not None]

        if not window_highs or not window_lows:
            continue

        if current.high == max(window_highs):
            swing_highs.append(current.high)

        if current.low == min(window_lows):
            swing_lows.append(current.low)

    swing_lows.sort()
    swing_highs.sort(reverse=True)

    return swing_lows, swing_highs


def _calc_pivot_points(high: float, low: float, close: float) -> tuple[list[float], list[float]]:
    """计算经典枢轴点（Pivot Points）

    Args:
        high: 昨日最高价
        low: 昨日最低价
        close: 昨日收盘价

    Returns:
        (supports, resistances) 支撑位和压力位列表
    """
    pivot = (high + low + close) / 3

    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    r3 = r1 + (high - low)
    s3 = s1 - (high - low)

    supports = sorted([s3, s2, s1])
    resistances = sorted([r1, r2, r3], reverse=True)

    return supports, resistances


def _find_volume_clusters(klines: list[KlineData], num_clusters: int = 3) -> list[float]:
    """寻找成交密集区（Volume Profile）

    按价格区间统计成交量，找到成交量最大的几个区间

    Args:
        klines: K线数据
        num_clusters: 返回的密集区数量

    Returns:
        成交密集区的价格列表（按成交量降序）
    """
    if not klines:
        return []

    valid_klines = [k for k in klines if k.high and k.low and k.volume and k.volume > 0]
    if not valid_klines:
        return []

    all_highs = [k.high for k in valid_klines]
    all_lows = [k.low for k in valid_klines]
    price_min = min(all_lows)
    price_max = max(all_highs)

    price_range = price_max - price_min
    if price_range == 0:
        return [price_max]

    num_bins = 20
    bin_width = price_range / num_bins
    bins = {i: 0.0 for i in range(num_bins)}

    for k in valid_klines:
        avg_price = (k.high + k.low) / 2
        bin_idx = int((avg_price - price_min) / bin_width)
        bin_idx = max(0, min(bin_idx, num_bins - 1))
        bins[bin_idx] += k.volume

    sorted_bins = sorted(bins.items(), key=lambda x: x[1], reverse=True)
    clusters = []

    for bin_idx, volume in sorted_bins[:num_clusters]:
        cluster_price = price_min + (bin_idx + 0.5) * bin_width
        clusters.append(round(cluster_price, 3))

    clusters.sort(reverse=True)
    return clusters


def calc_support_resistance(klines: list[KlineData], lookback: int = 20) -> SupportResistance:
    """计算支撑位、压力位和 ATR（增强版）

    综合多种方法：
    1. 摆动高低点（Swing High/Low）- 技术转折点
    2. 枢轴点（Pivot Points）- 日内关键位
    3. 成交密集区（Volume Profile）- 量价关键位
    4. 区间极值 - 简单参考

    Args:
        klines: K线数据（按时间升序）
        lookback: 回看天数

    Returns:
        SupportResistance 包含多种支撑压力位
    """
    if not klines:
        return SupportResistance()

    window = klines[-min(lookback, len(klines)):]

    # 1. ATR 计算
    atr = _calc_atr(window)

    # 2. 摆动高低点
    swing_lows, swing_highs = _find_swing_points(window, left=3, right=3)
    swing_supports = [round(p, 3) for p in swing_lows[-3:]] if swing_lows else []
    swing_resistances = [round(p, 3) for p in swing_highs[-3:]] if swing_highs else []

    # 3. 枢轴点（使用最后一根完整K线，排除当天）
    pivot_supports = []
    pivot_resistances = []
    if len(window) >= 2:
        prev_kline = window[-2]
        if prev_kline.high and prev_kline.low and prev_kline.close:
            p_supports, p_resistances = _calc_pivot_points(
                prev_kline.high, prev_kline.low, prev_kline.close
            )
            pivot_supports = [round(p, 3) for p in p_supports]
            pivot_resistances = [round(p, 3) for p in p_resistances]

    # 4. 成交密集区
    volume_clusters = _find_volume_clusters(window, num_clusters=3)

    # 5. 综合判断主支撑/压力位
    all_supports = swing_supports + pivot_supports + volume_clusters
    all_resistances = swing_resistances + pivot_resistances + volume_clusters

    valid_supports = [s for s in all_supports if s is not None]
    valid_resistances = [r for r in all_resistances if r is not None]

    support = min(valid_supports) if valid_supports else None
    resistance = max(valid_resistances) if valid_resistances else None

    return SupportResistance(
        support=round(support, 3) if support else None,
        resistance=round(resistance, 3) if resistance else None,
        atr=atr,
        swing_supports=swing_supports,
        swing_resistances=swing_resistances,
        pivot_supports=pivot_supports,
        pivot_resistances=pivot_resistances,
        volume_clusters=volume_clusters,
    )


# ============================================================
# 布林带（Bollinger Bands）
# ============================================================

@dataclass
class BollingerResult:
    upper: Optional[float] = None
    middle: Optional[float] = None
    lower: Optional[float] = None
    width: Optional[float] = None
    signal: str = ""


def _stddev(values: list[float]) -> float:
    """计算总体标准差（布林带使用总体标准差，除以 n）"""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return variance ** 0.5


def calc_bollinger(closes: list[float], period: int = 20, multiplier: float = 2.0) -> BollingerResult:
    """计算布林带

    Args:
        closes: 收盘价序列
        period: 周期（默认20）
        multiplier: 标准差倍数（默认2.0）

    Returns:
        BollingerResult 包含上轨、中轨、下轨、带宽和信号
    """
    if len(closes) < period:
        return BollingerResult(signal="数据不足")

    window = closes[-period:]
    middle = sum(window) / period

    std = _stddev(window)
    upper = middle + multiplier * std
    lower = middle - multiplier * std
    width = (upper - lower) / middle * 100 if middle > 0 else 0.0

    price = closes[-1]
    signal = "中性"
    if price >= upper:
        signal = "触及上轨"
    elif price <= lower:
        signal = "触及下轨"
    elif price > middle:
        signal = "偏强"
    else:
        signal = "偏弱"

    return BollingerResult(
        upper=round(upper, 3),
        middle=round(middle, 3),
        lower=round(lower, 3),
        width=round(width, 2),
        signal=signal,
    )


# ============================================================
# OBV —— 能量潮指标
# ============================================================

@dataclass
class OBVResult:
    obv: Optional[float] = None
    signal: str = ""


def _calc_linear_slope(series: list[float]) -> float:
    """用最小二乘法计算线性回归斜率，比首尾两点更稳健

    Args:
        series: 数值序列

    Returns:
        斜率值
    """
    n = len(series)
    if n < 2:
        return 0.0

    x_mean = (n - 1) / 2.0
    y_mean = sum(series) / n

    numerator = sum((i - x_mean) * (series[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    return numerator / denominator if denominator > 0 else 0.0


def _price_trend(klines: list[KlineData], period: int) -> str:
    """判断价格在给定周期内的趋势

    Returns:
        "up", "down", 或 "flat"
    """
    if len(klines) < period:
        return "flat"

    recent = klines[-period:]
    start_price = recent[0].close
    end_price = recent[-1].close

    if start_price is None or end_price is None:
        return "flat"

    change_pct = (end_price - start_price) / start_price * 100

    if change_pct > 2:
        return "up"
    elif change_pct < -2:
        return "down"
    else:
        return "flat"


def calc_obv(klines: list[KlineData]) -> OBVResult:
    """计算 OBV（On Balance Volume，能量潮）

    OBV 将成交量与价格走势结合：
    - 当日收盘价 > 前日收盘价，OBV = 前日 OBV + 当日成交量
    - 当日收盘价 < 前日收盘价，OBV = 前日 OBV - 当日成交量
    - 当日收盘价 == 前日收盘价，OBV = 前日 OBV

    信号判断逻辑：
    1. 量价背离（最有价值）：价格上涨但 OBV 下降 → 顶背离/主力出货
                        价格下跌但 OBV 上升 → 底背离/主力吸筹
    2. OBV 趋势加速：资金加速流入/流出
    3. OBV 趋势拐点：资金流向发生变化

    Args:
        klines: K线数据（按时间升序）

    Returns:
        OBVResult 包含最新 OBV 值和趋势信号
    """
    if len(klines) < 2:
        return OBVResult(signal="数据不足")

    valid_klines = [k for k in klines if k.close is not None and k.volume is not None]
    if len(valid_klines) < 2:
        return OBVResult(signal="数据不足")

    obv = 0.0
    obv_series = [obv]

    for i in range(1, len(valid_klines)):
        prev_close = valid_klines[i - 1].close
        curr_close = valid_klines[i].close
        curr_volume = valid_klines[i].volume

        if curr_close > prev_close:
            obv += curr_volume
        elif curr_close < prev_close:
            obv -= curr_volume
        # 相等时不变

        obv_series.append(obv)

    # 需要足够的历史数据才能判断趋势
    signal = "中性"
    min_length = 15  # 至少需要 15 个交易日

    if len(obv_series) >= min_length:
        # 使用线性回归斜率，更稳健
        # 近期：最近 5 天，远期：前 10 天
        recent_period = 5
        earlier_period = 10

        recent_obv = obv_series[-recent_period:]
        earlier_obv = obv_series[-(recent_period + earlier_period): -recent_period]

        recent_slope = _calc_linear_slope(recent_obv)
        earlier_slope = _calc_linear_slope(earlier_obv)

        # 计算 OBV 变化百分比（避免绝对值大小影响判断）
        obv_mid = obv_series[-(recent_period + earlier_period)]
        obv_end = obv_series[-1]
        obv_change_pct = ((obv_end - obv_mid) / abs(obv_mid)) * 100 if obv_mid != 0 else 0

        # ========== 优先判断量价背离（最有价值的信号）==========
        recent_price_trend = _price_trend(valid_klines, recent_period)

        # 计算近期 OBV 的整体方向
        obv_recent_change = (obv_series[-1] - obv_series[-recent_period])
        obv_trend = "up" if obv_recent_change > 0 else "down"

        # 顶背离：价格上涨，但 OBV 下降
        if recent_price_trend == "up" and obv_trend == "down":
            signal = "顶背离⚠️"
        # 底背离：价格下跌，但 OBV 上升
        elif recent_price_trend == "down" and obv_trend == "up":
            signal = "底背离"

        # ========== 无量价背离时，判断 OBV 趋势 ==========
        else:
            # 使用斜率的方向和相对大小来判断
            recent_dir = 1 if recent_slope > 0 else -1
            earlier_dir = 1 if earlier_slope > 0 else -1

            recent_abs = abs(recent_slope)
            earlier_abs = abs(earlier_slope)

            # 加速：方向相同，近期斜率绝对值更大
            if recent_dir == earlier_dir and recent_abs > earlier_abs * 1.2:
                if recent_dir == 1:
                    signal = "资金加速流入"
                else:
                    signal = "资金加速流出"

            # 减速：方向相同，近期斜率绝对值更小
            elif recent_dir == earlier_dir and recent_abs < earlier_abs * 0.8:
                if recent_dir == 1:
                    signal = "资金流入放缓"
                else:
                    signal = "资金流出放缓"

            # 拐点：方向相反
            elif recent_dir != earlier_dir:
                if recent_dir == 1:
                    signal = "资金转向流入"
                else:
                    signal = "资金转向流出"

            # 持续：方向和强度都差不多
            elif recent_dir == 1:
                signal = "资金持续流入"
            else:
                signal = "资金持续流出"

    return OBVResult(
        obv=round(obv, 2),
        signal=signal,
    )


# ============================================================
# 辅助判断函数
# ============================================================

def is_stagflation(quote: Quote, klines: list[KlineData], price_threshold: float = 0.5, vol_threshold: float = 1.5) -> bool:
    """判断滞涨：涨幅小但放量

    价格上涨幅度很小但成交量明显放大，可能预示主力出货或上方抛压沉重。

    Args:
        quote: 当前行情
        klines: 历史K线
        price_threshold: 涨幅阈值（百分比），低于此值认为是小涨幅
        vol_threshold: 量比阈值，高于此值认为是放量

    Returns:
        True 表示存在滞涨现象
    """
    if quote.change_pct is None:
        return False

    if quote.change_pct < 0:
        return False

    if quote.change_pct > price_threshold:
        return False

    vol = estimate_full_day_volume(quote)
    if vol is None or vol <= 0:
        return False

    hist = [k.volume for k in klines[:-1] if k.volume and k.volume > 0][-10:]
    if len(hist) < 3:
        return False

    avg_vol = sum(hist) / len(hist)
    if avg_vol <= 0:
        return False

    ratio = vol / avg_vol
    return ratio >= vol_threshold


def is_above_ma_support(price: Optional[float], klines: list[KlineData], period: int = 20) -> bool:
    """判断股价站稳均线支撑

    检查当前价格是否站在 N 日均线之上，且均线呈上行趋势。

    Args:
        price: 当前价格
        klines: 历史K线
        period: 均线周期

    Returns:
        True 表示站稳均线支撑
    """
    if price is None or len(klines) < period + 1:
        return False

    closes = [k.close for k in klines if k.close is not None]
    if len(closes) < period + 1:
        return False

    # 计算当前均线和前一日均线
    curr_ma = sum(closes[-period:]) / period
    prev_ma = sum(closes[-(period + 1):-1]) / period

    # 价格在均线之上，且均线走平或向上
    return price >= curr_ma and prev_ma >= curr_ma * 0.998


def is_breakabove_bb_middle(price: Optional[float], klines: list[KlineData], period: int = 20) -> bool:
    """判断股价突破布林带中轨

    检查当前价格是否从下方突破布林带中轨（MA20），
    表示可能进入强势区间。

    Args:
        price: 当前价格
        klines: 历史K线
        period: 布林带中轨周期（MA20）

    Returns:
        True 表示从下方突破中轨
    """
    if price is None or len(klines) < period + 1:
        return False

    closes = [k.close for k in klines if k.close is not None]
    if len(closes) < period + 1:
        return False

    middle = sum(closes[-period:]) / period

    # 前一日价格在布林中轨下方，当日在上方
    prev_price = closes[-2]
    return prev_price < middle and price >= middle


def is_low_volume(klines: list[KlineData], lookback: int = 20) -> bool:
    """判断地量：成交量创近期新低

    检查当日成交量是否为近期 lookback 天内的最低值附近。

    Args:
        klines: 历史K线（含当日）
        lookback: 回看天数

    Returns:
        True 表示成交量处于近期地量水平
    """
    if len(klines) < lookback:
        return False

    volumes = [k.volume for k in klines[-lookback:] if k.volume is not None and k.volume > 0]
    if len(volumes) < 5:
        return False

    curr_volume = volumes[-1]
    min_volume = min(volumes)

    # 当日成交量在近期最低量的 1.2 倍以内
    return curr_volume <= min_volume * 1.2


# ============================================================
# 量价关系分析
# ============================================================

def analyze_volume_price(quote: Quote, klines: list[KlineData]) -> str:
    """分析量价关系

    对比今日成交量与 N 日均量，判断放量/缩量/平量，
    结合涨跌方向给出量价关系标注。

    Args:
        quote: 当前行情
        klines: 历史K线（含当日）

    Returns:
        量价关系描述字符串，如 "放量上涨", "缩量下跌", "平量震荡" 等
    """
    if not klines:
        return "数据不足"
    vol = estimate_full_day_volume(quote)
    if vol is None or vol <= 0:
        return "数据不足"

    # 取近 10 日（不含当日）的平均成交量作为基准
    hist = [k.volume for k in klines[:-1] if k.volume and k.volume > 0][-10:]
    if len(hist) < 3:
        return "数据不足"

    avg_vol = sum(hist) / len(hist)
    ratio = vol / avg_vol if avg_vol > 0 else 1.0
    change_pct = quote.change_pct or 0

    # 量比标注
    if ratio >= 1.5:
        vol_label = "放量"
    elif ratio <= 0.6:
        vol_label = "缩量"
    else:
        vol_label = "平量"

    # 结合方向
    if change_pct > 0.5:
        return f"{vol_label}上涨（量比{ratio:.2f}）"
    elif change_pct < -0.5:
        return f"{vol_label}下跌（量比{ratio:.2f}）"
    else:
        return f"{vol_label}震荡（量比{ratio:.2f}）"


# ============================================================
# 汇总
# ============================================================

def get_technical_summary(quote: Quote, klines: list[KlineData]) -> TechnicalSummary:
    """汇总所有技术指标

    Args:
        quote: 当前行情
        klines: 历史K线数据

    Returns:
        TechnicalSummary 包含所有指标和文字信号
    """
    closes = [k.close for k in klines if k.close is not None]
    highs = [k.high for k in klines if k.high is not None]
    lows = [k.low for k in klines if k.low is not None]

    rsi = calc_rsi(closes)
    macd = calc_macd(closes)
    kdj = calc_kdj(highs, lows, closes)
    sr = calc_support_resistance(klines)
    bb = calc_bollinger(closes)
    obv = calc_obv(klines)

    signals = []
    if rsi and rsi_signal(rsi) in ("超买", "严重超买"):
        signals.append(f"RSI超买({rsi})")
    elif rsi and rsi_signal(rsi) in ("超卖", "严重超卖"):
        signals.append(f"RSI超卖({rsi})")

    if macd.signal in ("金叉", "死叉"):
        signals.append(f"MACD{macd.signal}")
    if kdj.signal in ("金叉", "死叉", "超买", "超卖"):
        signals.append(f"KDJ{kdj.signal}")
    if bb.signal == "触及上轨":
        signals.append(f"布林触及上轨(带宽{bb.width}%)")
    elif bb.signal == "触及下轨":
        signals.append(f"布林触及下轨(带宽{bb.width}%)")
    if obv.signal and obv.signal not in ("中性", "数据不足"):
        signals.append(f"OBV{obv.signal}")

    return TechnicalSummary(
        rsi=rsi,
        rsi_signal=rsi_signal(rsi),
        macd_dif=macd.dif,
        macd_dea=macd.dea,
        macd_histogram=macd.histogram,
        macd_signal=macd.signal,
        kdj_k=kdj.k,
        kdj_d=kdj.d,
        kdj_j=kdj.j,
        kdj_signal=kdj.signal,
        support=sr.support,
        resistance=sr.resistance,
        swing_supports=sr.swing_supports or [],
        swing_resistances=sr.swing_resistances or [],
        pivot_supports=sr.pivot_supports or [],
        pivot_resistances=sr.pivot_resistances or [],
        volume_clusters=sr.volume_clusters or [],
        atr=sr.atr,
        bb_upper=bb.upper,
        bb_middle=bb.middle,
        bb_lower=bb.lower,
        bb_width=bb.width,
        bb_signal=bb.signal,
        obv=obv.obv,
        signals=signals,
    )
