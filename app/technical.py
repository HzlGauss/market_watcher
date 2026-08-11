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

def fetch_historical_kline(code: str, market: str, days: int = 30, scale: int = 240) -> list[KlineData]:
    """获取K线数据（新浪主源 + AKShare 兜底）

    Args:
        code: 股票代码
        market: 市场标识 (SH/SZ/HK)
        days: 获取多少天的数据
        scale: K线周期。240=日线, 60=60分钟, 30=30分钟, 15=15分钟, 5=5分钟

    Returns:
        K线数据列表（按时间升序）
    """
    prefix = {"SH": "sh", "SZ": "sz", "HK": "hk"}.get(market, "sh")
    sina_code = f"{prefix}{code}"

    # 分钟线需要更多 bar 数来覆盖足够天数
    if scale < 240:
        bars_per_day = 240 // scale
        datalen = max(days * bars_per_day, 120)
    else:
        datalen = days

    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sina_code}&scale={scale}"
        f"&ma=no&datalen={datalen}"
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


def calc_sma(values: list[float], period: int) -> list[float]:
    """计算 SMA（简单移动平均）

    Args:
        values: 价格序列（按时间升序）
        period: 均线周期

    Returns:
        SMA 值序列（长度与输入相同，前 period-1 个值为 None）
    """
    if not values or len(values) < period:
        return []
    sma_vals: list[Optional[float]] = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        sma_vals.append(sum(values[i - period + 1:i + 1]) / period)
    return sma_vals  # type: ignore[return-value]


# ============================================================
# MA Alignment —— 均线排列分析
# ============================================================

@dataclass
class MAAlignment:
    """均线排列分析结果"""
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    alignment: str = "数据不足"  # 多头排列 / 空头排列 / 缠绕 / 多头回调 / 空头反弹 / 数据不足
    detail: str = ""

    @property
    def is_bullish(self) -> bool:
        """是否多头排列"""
        return self.alignment == "多头排列"

    @property
    def is_bearish(self) -> bool:
        """是否空头排列"""
        return self.alignment == "空头排列"

    @property
    def is_sideways(self) -> bool:
        """是否缠绕（无明显方向）"""
        return self.alignment == "缠绕"


def calc_ma_alignment(klines: list["KlineData"], periods: tuple = (5, 10, 20, 60)) -> MAAlignment:
    """计算均线排列状态

    使用 SMA 计算多条均线，判断排列方向：
    - 多头排列: MA5 > MA10 > MA20 > MA60 — 各级别均线依次向上
    - 空头排列: MA5 < MA10 < MA20 < MA60 — 各级别均线依次向下
    - 多头回调: MA20 向上（短均线跌破中均线但长均线仍向上）— 上升趋势中的回调
    - 空头反弹: MA20 向下（短均线上穿中均线但长均线仍向下）— 下降趋势中的反弹
    - 缠绕: 均线交错，无明显方向

    Args:
        klines: K线数据（按时间升序），需要足够的数据覆盖最大周期
        periods: 均线周期元组，默认 (5, 10, 20, 60)

    Returns:
        MAAlignment 对象，包含各均线值和排列状态
    """
    if not klines:
        return MAAlignment()

    closes = [k.close for k in klines if k.close is not None]
    if not closes:
        return MAAlignment()

    max_period = max(periods)
    if len(closes) < max_period:
        return MAAlignment(detail=f"数据不足（需要至少{max_period}根K线，当前{len(closes)}根）")

    # 计算各周期 SMA
    ma_values: dict[int, Optional[float]] = {}
    for p in periods:
        sma_series = calc_sma(closes, p)
        if sma_series:
            last_val = sma_series[-1]
            ma_values[p] = round(last_val, 3) if last_val is not None else None
        else:
            ma_values[p] = None

    ma5, ma10, ma20, ma60 = (ma_values.get(p) for p in periods)

    # 判断排列状态
    if any(v is None for v in [ma5, ma10, ma20]):
        return MAAlignment(
            ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
            alignment="数据不足",
            detail="均线数据不完整"
        )

    # 辅助：判断均线趋势方向（通过前一日对比）
    has_ma60 = ma60 is not None
    prev_closes = closes[:-1]

    # MA20 趋势（前一日 MA20 vs 当日 MA20）
    ma20_rising = False
    ma20_falling = False
    if len(prev_closes) >= 20:
        prev_sma = calc_sma(prev_closes, 20)
        if prev_sma and prev_sma[-1] is not None:
            ma20_rising = ma20 > prev_sma[-1]
            ma20_falling = ma20 < prev_sma[-1]

    # 主判断：均线排列（必须使用 assert 告知 type checker ma5/ma10/ma20 非 None）
    assert ma5 is not None and ma10 is not None and ma20 is not None

    if ma5 > ma10 > ma20:
        if has_ma60 and ma60 is not None and ma20 > ma60:
            alignment = "多头排列"
            detail = "MA5>MA10>MA20>MA60，各级均线顺向向上，趋势强劲"
        else:
            alignment = "多头排列"
            detail = "MA5>MA10>MA20，短期均线多头排列"
    elif ma5 < ma10 < ma20:
        if has_ma60 and ma60 is not None and ma20 < ma60:
            alignment = "空头排列"
            detail = "MA5<MA10<MA20<MA60，各级均线顺向向下，趋势疲弱"
        else:
            alignment = "空头排列"
            detail = "MA5<MA10<MA20，短期均线空头排列"
    elif ma5 < ma10 and ma20_rising:
        # 短均线在下方，但中长均线仍在上行 → 上升趋势中的回调
        alignment = "多头回调"
        detail = f"MA5({ma5})<MA10({ma10})，但MA20({ma20})仍在上行，上升趋势中的短期回调"
    elif ma5 > ma10 and ma20_falling:
        # 短均线上穿，但中长均线仍在下降 → 下降趋势中的反弹
        alignment = "空头反弹"
        detail = f"MA5({ma5})>MA10({ma10})，但MA20({ma20})仍在下行，下降趋势中的短期反弹"
    else:
        # 均线交错缠绕
        alignment = "缠绕"
        if ma20_rising:
            detail = "均线缠绕，MA20缓慢上行，偏多震荡"
        elif ma20_falling:
            detail = "均线缠绕，MA20缓慢下行，偏空震荡"
        else:
            detail = "均线缠绕，无明显方向"

    return MAAlignment(
        ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
        alignment=alignment,
        detail=detail,
    )


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
    ma = calc_ma_alignment(klines)

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

    # 均线排列信号
    if ma.alignment == "多头排列":
        signals.append("均线多头排列")
    elif ma.alignment == "多头回调":
        signals.append("均线多头回调(回踩中)")
    elif ma.alignment == "空头排列":
        signals.append("均线空头排列")
    elif ma.alignment == "空头反弹":
        signals.append("均线空头反弹(反压中)")

    # ---- 跳空缺口检测 ----
    gap = detect_gap(klines, quote.price or 0, quote.open or 0)
    if gap.has_gap:
        if gap.gap_type == "向上跳空":
            if gap.is_filled:
                signals.append(f"向上跳空{gap.gap_pct:+.1f}%(已回补)")
            elif gap.filled_pct >= 30:
                signals.append(f"向上跳空{gap.gap_pct:+.1f}%(回补{gap.filled_pct:.0f}%)")
            else:
                signals.append(f"向上跳空{gap.gap_pct:+.1f}%(未回补)")
        else:
            if gap.is_filled:
                signals.append(f"向下跳空{gap.gap_pct:+.1f}%(已回补)")
            elif gap.filled_pct >= 30:
                signals.append(f"向下跳空{gap.gap_pct:+.1f}%(回补{gap.filled_pct:.0f}%)")
            else:
                signals.append(f"向下跳空{gap.gap_pct:+.1f}%(未回补)")

    # ---- 关键位突破检测 ----
    breakout = check_key_level_breakout(klines, quote.price or 0, period=20)
    if breakout.has_breakout:
        signals.append(breakout.detail)

    # ---- 关键位动态行为分析 ----
    key_level = analyze_key_level_behavior(
        klines,
        quote.price or 0,
        support=sr.support,
        resistance=sr.resistance,
        atr=sr.atr,
        swing_supports=sr.swing_supports,
        swing_resistances=sr.swing_resistances,
        pivot_supports=sr.pivot_supports,
        pivot_resistances=sr.pivot_resistances,
        volume_clusters=sr.volume_clusters,
    )
    if key_level.has_resistance_rejection:
        signals.append(f"🔴 受压回落: {key_level.resistance_rejection_detail}")
    if key_level.has_support_confirmation:
        signals.append(f"🟢 支撑确认: {key_level.support_confirmation_detail}")
    if key_level.has_support_breakdown:
        signals.append(f"🚨 跌破支撑: {key_level.support_breakdown_detail}")
    if key_level.has_breakout_retest:
        signals.append(f"✅ 突破回踩确认: {key_level.breakout_retest_detail}")
    if key_level.support_strength in ("强", "中"):
        signals.append(f"📊 支撑{key_level.support_strength}度: {key_level.strength_summary}")
    if key_level.resistance_strength in ("强", "中"):
        signals.append(f"📊 压力{key_level.resistance_strength}度: {key_level.strength_summary}")

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
        obv_signal=obv.signal,
        ma5=ma.ma5,
        ma10=ma.ma10,
        ma20=ma.ma20,
        ma60=ma.ma60,
        ma_alignment=ma.alignment,
        ma_alignment_detail=ma.detail,
        has_gap=gap.has_gap,
        gap_type=gap.gap_type,
        gap_pct=gap.gap_pct,
        gap_detail=gap.detail,
        gap_filled_pct=gap.filled_pct,
        breakout_type=breakout.breakout_type,
        breakout_detail=breakout.detail,
        has_resistance_rejection=key_level.has_resistance_rejection,
        resistance_rejection_detail=key_level.resistance_rejection_detail,
        has_support_confirmation=key_level.has_support_confirmation,
        support_confirmation_detail=key_level.support_confirmation_detail,
        has_support_breakdown=key_level.has_support_breakdown,
        support_breakdown_detail=key_level.support_breakdown_detail,
        has_breakout_retest=key_level.has_breakout_retest,
        breakout_retest_detail=key_level.breakout_retest_detail,
        support_strength=key_level.support_strength,
        resistance_strength=key_level.resistance_strength,
        strength_summary=key_level.strength_summary,
        signals=signals,
    )


# ============================================================
# 跳空缺口 & 关键位突破分析
# ============================================================

@dataclass
class GapInfo:
    """跳空缺口信息"""
    has_gap: bool = False
    gap_type: str = ""          # "向上跳空" / "向下跳空" / ""
    gap_pct: float = 0.0        # 跳空幅度(%)
    gap_upper: float = 0.0      # 缺口上沿 = max(今开, 昨收)
    gap_lower: float = 0.0      # 缺口下沿 = min(今开, 昨收)
    gap_size: float = 0.0       # 缺口宽度(绝对值)
    filled_pct: float = 0.0     # 回补进度(0-100%)
    is_filled: bool = False     # 是否已完全回补
    detail: str = ""


def detect_gap(klines: list[KlineData], current_price: float, current_open: float) -> GapInfo:
    """检测今日是否存在跳空缺口并计算回补进度

    判断逻辑：
    - 今日开盘价 vs 昨日收盘价
    - 向上跳空: 今开 > 昨收（缺口区间 = 昨收 ~ 今开）
    - 向下跳空: 今开 < 昨收（缺口区间 = 今开 ~ 昨收）

    Args:
        klines: K线数据（至少2根，最后一根为今日）
        current_price: 当前价格
        current_open: 今日开盘价

    Returns:
        GapInfo 对象
    """
    if len(klines) < 2 or current_open <= 0:
        return GapInfo()

    yesterday = klines[-2]
    if yesterday.close is None or yesterday.close <= 0:
        return GapInfo()

    prev_close = yesterday.close
    gap_pct = (current_open - prev_close) / prev_close * 100

    info = GapInfo()

    if gap_pct >= 0.5:  # 向上跳空 ≥ 0.5%
        info.gap_type = "向上跳空"
        info.gap_lower = prev_close
        info.gap_upper = current_open
        info.gap_size = current_open - prev_close
        info.gap_pct = round(gap_pct, 2)
        info.has_gap = True
    elif gap_pct <= -0.5:  # 向下跳空 ≥ 0.5%
        info.gap_type = "向下跳空"
        info.gap_lower = current_open
        info.gap_upper = prev_close
        info.gap_size = prev_close - current_open
        info.gap_pct = round(gap_pct, 2)
        info.has_gap = True
    else:
        return info

    # 计算回补进度
    if info.has_gap and current_price > 0:
        if info.gap_type == "向上跳空":
            # 现价越接近缺口下沿(昨收)，回补越多
            if current_price <= info.gap_lower:
                info.filled_pct = 100.0
                info.is_filled = True
            else:
                filled = (info.gap_upper - current_price) / info.gap_size * 100
                info.filled_pct = round(max(0, filled), 1)
            info.detail = (
                f"{info.gap_type} {info.gap_pct:+.1f}% "
                f"(缺口{info.gap_lower:.2f}~{info.gap_upper:.2f}), "
                f"回补{info.filled_pct:.0f}%"
            )
        else:  # 向下跳空
            if current_price >= info.gap_upper:
                info.filled_pct = 100.0
                info.is_filled = True
            else:
                filled = (current_price - info.gap_lower) / info.gap_size * 100
                info.filled_pct = round(max(0, filled), 1)
            info.detail = (
                f"{info.gap_type} {info.gap_pct:+.1f}% "
                f"(缺口{info.gap_lower:.2f}~{info.gap_upper:.2f}), "
                f"回补{info.filled_pct:.0f}%"
            )

    return info


@dataclass
class BreakoutInfo:
    """关键位突破信息"""
    has_breakout: bool = False
    breakout_type: str = ""     # "突破近期高点" / "跌破近期低点" / ""
    level: float = 0.0          # 被突破的关键价位
    level_desc: str = ""        # 关键位描述（如"N日最高"）
    detail: str = ""


def check_key_level_breakout(
    klines: list[KlineData],
    current_price: float,
    period: int = 20,
) -> BreakoutInfo:
    """检查当前价是否突破近期关键高低点

    检测：
    - 突破 N 日最高点（看多信号）
    - 跌破 N 日最低点（看空信号）

    Args:
        klines: K线数据（不含今日，或今日为非最后一根）
        current_price: 当前价格
        period: 回看天数

    Returns:
        BreakoutInfo 对象
    """
    if len(klines) < period or current_price <= 0:
        return BreakoutInfo()

    # 取最近 N 日（不含今日）的 K 线
    window = klines[-period-1:-1] if len(klines) > period else klines[:-1]
    if len(window) < 5:
        return BreakoutInfo()

    highs = [k.high for k in window if k.high is not None]
    lows = [k.low for k in window if k.low is not None]
    if not highs or not lows:
        return BreakoutInfo()

    period_high = max(highs)
    period_low = min(lows)
    n_days = len(window)

    info = BreakoutInfo()

    if current_price > period_high:
        info.has_breakout = True
        info.breakout_type = "突破近期高点"
        info.level = period_high
        info.level_desc = f"{n_days}日最高"
        info.detail = (
            f"当前价{current_price:.2f}突破{n_days}日高点{period_high:.2f}"
        )
    elif current_price < period_low:
        info.has_breakout = True
        info.breakout_type = "跌破近期低点"
        info.level = period_low
        info.level_desc = f"{n_days}日最低"
        info.detail = (
            f"当前价{current_price:.2f}跌破{n_days}日低点{period_low:.2f}"
        )

    return info


# ============================================================
# 支撑/压力位动态行为分析
# ============================================================

@dataclass
class KeyLevelBehavior:
    """支撑/压力位动态行为分析结果

    检测价格在关键位（支撑/压力位）上的动态行为：
    受压回落、支撑确认、跌破支撑、突破后回踩确认、关键位强度。
    """
    # 压力位受阻回落
    has_resistance_rejection: bool = False
    resistance_rejection_level: Optional[float] = None
    resistance_rejection_detail: str = ""

    # 支撑位有效确认
    has_support_confirmation: bool = False
    support_confirmation_level: Optional[float] = None
    support_confirmation_detail: str = ""

    # 跌破支撑位
    has_support_breakdown: bool = False
    support_breakdown_level: Optional[float] = None
    support_breakdown_detail: str = ""

    # 突破后回踩确认
    has_breakout_retest: bool = False
    breakout_retest_level: Optional[float] = None
    breakout_retest_detail: str = ""

    # 支撑/压力位强度
    support_strength: str = ""       # "强" / "中" / "弱"
    resistance_strength: str = ""    # "强" / "中" / "弱"
    support_tests: int = 0
    resistance_tests: int = 0
    strength_summary: str = ""


def _merge_nearby_levels(levels: list[float], atr: float, zone_mult: float = 0.5) -> list[float]:
    """合并距离过近的关键位，返回去重后的关键位列表

    两个价位在 ATR * zone_mult 范围内视为同一个区域，
    取平均值作为合并后的价位。
    """
    if not levels or atr <= 0:
        return list(levels)

    merged: list[float] = []
    zone = atr * zone_mult
    remaining = sorted(set(levels))

    while remaining:
        pivot_level = remaining.pop(0)
        group = [pivot_level]
        # 收集所有在 zone 内的价位
        i = 0
        while i < len(remaining):
            if abs(remaining[i] - pivot_level) <= zone:
                group.append(remaining.pop(i))
            else:
                i += 1
        merged.append(round(sum(group) / len(group), 3))

    merged.sort()
    return merged


def _count_level_tests(
    klines: list[KlineData],
    level: float,
    atr: float,
    is_support: bool = True,
    zone_mult: float = 0.5,
) -> tuple[int, float]:
    """统计历史K线中关键位被测试的次数，带时间衰减

    Args:
        klines: K线数据
        level: 关键价位
        atr: ATR
        is_support: True=支撑位(看低点)，False=压力位(看高点)
        zone_mult: 接近区域的ATR倍数

    Returns:
        (weighted_tests, raw_tests) 时间衰减后的测试次数和原始次数
    """
    if atr <= 0 or level <= 0:
        return 0, 0

    zone = atr * zone_mult
    n = len(klines)
    total_weight = 0.0
    raw_count = 0

    for idx, k in enumerate(klines):
        if is_support:
            price = k.low
        else:
            price = k.high
        if price is None:
            continue

        if abs(price - level) <= zone:
            raw_count += 1
            # 时间衰减：最近 5 根权重 1.0，5-10 根 0.5，>10 根 0.25
            bars_ago = n - 1 - idx
            if bars_ago <= 5:
                weight = 1.0
            elif bars_ago <= 10:
                weight = 0.5
            else:
                weight = 0.25
            total_weight += weight

    return round(total_weight, 1), raw_count


def _calc_level_confluence(
    level: float,
    all_levels: list[float],
    atr: float,
    zone_mult: float = 0.5,
) -> int:
    """计算有多少个不同方法的关键位收敛在同一区域

    Returns:
        收敛的方法数 (≥3 → 强, 2 → 中, 1 → 弱)
    """
    if atr <= 0 or level <= 0:
        return 1
    zone = atr * zone_mult
    count = sum(1 for lv in all_levels if abs(lv - level) <= zone)
    return count


def _assess_level_strength(
    klines: list[KlineData],
    support: Optional[float],
    resistance: Optional[float],
    atr: Optional[float],
    all_supports: list[float],
    all_resistances: list[float],
) -> tuple[str, str, int, int, str]:
    """评估支撑位和压力位的强度

    Returns:
        (support_strength, resistance_strength, support_tests, resistance_tests, summary)
    """
    if atr is None or atr <= 0:
        return "", "", 0, 0, ""

    sup_strength = ""
    res_strength = ""
    sup_tests = 0
    res_tests = 0
    parts: list[str] = []

    # 支撑位强度
    if support is not None and support > 0:
        weighted, _ = _count_level_tests(klines, support, atr, is_support=True)
        sup_tests = max(1, round(weighted)) if weighted > 0 else 0
        confluence = _calc_level_confluence(support, all_supports, atr)
        if confluence >= 3 and weighted >= 3:
            sup_strength = "强"
        elif confluence >= 2 and weighted >= 2:
            sup_strength = "中"
        elif weighted >= 0.5:
            sup_strength = "弱"
        parts.append(f"支撑{support:.3f}: {sup_strength}({confluence}法共振,{weighted:.0f}次测试)")

    # 压力位强度
    if resistance is not None and resistance > 0:
        weighted, _ = _count_level_tests(klines, resistance, atr, is_support=False)
        res_tests = max(1, round(weighted)) if weighted > 0 else 0
        confluence = _calc_level_confluence(resistance, all_resistances, atr)
        if confluence >= 3 and weighted >= 3:
            res_strength = "强"
        elif confluence >= 2 and weighted >= 2:
            res_strength = "中"
        elif weighted >= 0.5:
            res_strength = "弱"
        parts.append(f"压力{resistance:.3f}: {res_strength}({confluence}法共振,{weighted:.0f}次测试)")

    return sup_strength, res_strength, sup_tests, res_tests, "; ".join(parts)


def analyze_key_level_behavior(
    klines: list[KlineData],
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    atr: Optional[float],
    swing_supports: Optional[list[float]] = None,
    swing_resistances: Optional[list[float]] = None,
    pivot_supports: Optional[list[float]] = None,
    pivot_resistances: Optional[list[float]] = None,
    volume_clusters: Optional[list[float]] = None,
) -> KeyLevelBehavior:
    """分析价格在支撑/压力位上的动态行为

    检测 5 类信号：
    1. 压力位受阻回落：近期高点接近压力位后回落
    2. 支撑位有效确认：近期低点接近支撑位后反弹
    3. 跌破支撑位：当前价跌破关键支撑
    4. 突破后回踩确认：突破压力位后回落并站稳原压力位上方
    5. 支撑/压力位强度：多方法共振 + 历史测试次数评估

    Args:
        klines: 历史K线数据（至少 10 根）
        current_price: 当前价格
        support: 主支撑位
        resistance: 主压力位
        atr: ATR 值
        swing_supports: 摆动低点支撑位列表
        swing_resistances: 摆动高点压力位列表
        pivot_supports: 枢轴支撑位列表
        pivot_resistances: 枢轴压力位列表
        volume_clusters: 成交密集区列表

    Returns:
        KeyLevelBehavior 对象
    """
    result = KeyLevelBehavior()

    if not klines or len(klines) < 5 or current_price <= 0:
        return result

    if atr is None or atr <= 0:
        atr = current_price * 0.02  # 回退：假设 2% ATR

    # 收集所有支撑和压力位
    swing_supports = swing_supports or []
    swing_resistances = swing_resistances or []
    pivot_supports = pivot_supports or []
    pivot_resistances = pivot_resistances or []
    volume_clusters = volume_clusters or []
    all_sups = swing_supports + pivot_supports + volume_clusters
    all_res = swing_resistances + pivot_resistances + volume_clusters

    # 接近阈值
    proximity_zone = atr * 0.8       # "接近" 关键位的区域
    meaningful_move = atr * 0.4      # "有效" 反弹/回落的最小幅度
    retest_zone = atr * 0.6          # 回踩确认的容忍区域
    lookback_bars = min(15, len(klines) - 1)

    recent_bars = klines[-lookback_bars - 1:]  # 包含最近 lookback 根K线（不含可能的当日）

    # ---- 1. 压力位受阻回落 ----
    if resistance is not None and resistance > 0:
        recent_highs = [(k.high, k.date) for k in recent_bars if k.high is not None]
        for high, date in recent_highs:
            if abs(high - resistance) <= proximity_zone:
                # 高点接近压力位，检查是否有回落
                decline = high - current_price
                if decline >= meaningful_move and current_price < resistance:
                    decline_pct = (decline / high) * 100
                    result.has_resistance_rejection = True
                    result.resistance_rejection_level = resistance
                    result.resistance_rejection_detail = (
                        f"价格在{date}触及压力{resistance:.3f}(高点{high:.3f})后回落，"
                        f"当前{current_price:.3f}低于该高点{decline_pct:.1f}%，压力有效"
                    )
                    break

    # ---- 2. 支撑位有效确认 ----
    if support is not None and support > 0:
        recent_lows = [(k.low, k.date) for k in recent_bars if k.low is not None]
        for low, date in recent_lows:
            if abs(low - support) <= proximity_zone:
                # 低点接近支撑位，检查是否有反弹
                bounce = current_price - low
                if bounce >= meaningful_move and current_price > support:
                    bounce_pct = (bounce / low) * 100
                    result.has_support_confirmation = True
                    result.support_confirmation_level = support
                    result.support_confirmation_detail = (
                        f"价格在{date}回踩支撑{support:.3f}(低点{low:.3f})后反弹，"
                        f"当前{current_price:.3f}高于该低点{bounce_pct:.1f}%，支撑有效"
                    )
                    break

    # ---- 3. 跌破支撑位 ----
    if support is not None and support > 0 and current_price < support:
        breakdown_pct = (support - current_price) / support * 100
        # 跌破幅度需要有意义（超过 0.3 × ATR 的百分比）
        if breakdown_pct >= (atr / current_price * 30):  # at least 30% of daily ATR as %
            result.has_support_breakdown = True
            result.support_breakdown_level = support
            result.support_breakdown_detail = (
                f"当前价{current_price:.3f}跌破主支撑{support:.3f}，"
                f"偏离{breakdown_pct:.1f}%，支撑可能失效"
            )

    # ---- 4. 突破压力位后回踩确认 ----
    if resistance is not None and resistance > 0 and current_price > resistance:
        # 1) 先找是否有近期突破（高点 > 压力位）
        breakout_found = False
        breakout_bar_idx = -1
        for i, k in enumerate(recent_bars):
            if k.high is not None and k.high > resistance:
                breakout_found = True
                breakout_bar_idx = i
                break

        if breakout_found:
            # 2) 突破后是否有回踩（低点回到原压力位 ± retest_zone）
            for j in range(breakout_bar_idx + 1, len(recent_bars)):
                k = recent_bars[j]
                if k.low is not None and abs(k.low - resistance) <= retest_zone:
                    # 回踩确认：低点触及原压力位（现为支撑），且当前价站稳上方
                    result.has_breakout_retest = True
                    result.breakout_retest_level = resistance
                    result.breakout_retest_detail = (
                        f"突破压力{resistance:.3f}后在{k.date}回踩确认"
                        f"(低点{k.low:.3f})，当前{current_price:.3f}站稳上方，突破有效"
                    )
                    break

    # ---- 5. 支撑/压力位强度 ----
    sup_str, res_str, sup_t, res_t, str_summary = _assess_level_strength(
        klines, support, resistance, atr, all_sups, all_res
    )
    result.support_strength = sup_str
    result.resistance_strength = res_str
    result.support_tests = sup_t
    result.resistance_tests = res_t
    result.strength_summary = str_summary

    return result


# ============================================================
# 市场状态识别 + 多信号共振评分
# ============================================================

@dataclass
class MarketRegime:
    """市场状态评估"""
    regime: str = ""          # "趋势上涨"/"趋势下跌"/"震荡偏多"/"震荡偏空"/"窄幅震荡"
    confidence: str = ""      # "高"/"中"/"低"
    suggestion: str = ""      # 策略建议
    trend_strength: float = 0.0  # 趋势强度 0-100
    bb_squeeze: bool = False  # 布林带收窄（变盘前兆）


def detect_market_regime(
    tech: TechnicalSummary,
    price: float,
    atr: Optional[float] = None,
) -> MarketRegime:
    """识别当前市场状态（趋势/震荡），指导策略选择

    规则：
    - 均线多头/空头 + BB 带宽 > 中位值 → 趋势市
    - 均线缠绕 + BB 带宽 < 中位值 → 震荡市
    - 趋势强度 = |price - MA20| / ATR，> 2 为强趋势
    """
    result = MarketRegime()

    if not price or price <= 0:
        return result

    ma_align = tech.ma_alignment
    bb_width = tech.bb_width
    rsi = tech.rsi

    # 趋势强度
    if tech.ma20 and tech.ma20 > 0 and atr and atr > 0:
        result.trend_strength = round(abs(price - tech.ma20) / atr, 1)
    else:
        result.trend_strength = 1.0

    # BB 收窄检测
    if bb_width and bb_width < 5.0:
        result.bb_squeeze = True

    # 状态判断
    if ma_align in ("多头排列",):
        if result.trend_strength >= 2.0:
            result.regime = "趋势上涨"
            result.confidence = "高"
        else:
            result.regime = "趋势上涨"
            result.confidence = "中"
    elif ma_align in ("空头排列",):
        if result.trend_strength >= 2.0:
            result.regime = "趋势下跌"
            result.confidence = "高"
        else:
            result.regime = "趋势下跌"
            result.confidence = "中"
    elif ma_align in ("多头回调",) and rsi and rsi < 50:
        result.regime = "震荡偏多"
        result.confidence = "中"
    elif ma_align in ("空头反弹",) and rsi and rsi > 50:
        result.regime = "震荡偏空"
        result.confidence = "中"
    elif result.bb_squeeze:
        result.regime = "窄幅震荡"
        result.confidence = "高"
    else:
        result.regime = "震荡"
        result.confidence = "低"

    # 策略建议
    if result.regime.startswith("趋势上涨"):
        result.suggestion = "顺势做多，回调买入，不逆势做空"
    elif result.regime.startswith("趋势下跌"):
        result.suggestion = "反弹减仓，不抄底"
    elif result.regime.startswith("震荡"):
        result.suggestion = "高抛低吸，支撑买压力卖"
    elif result.regime == "窄幅震荡":
        result.suggestion = "观望等突破，做T空间小"

    return result


def calc_composite_score(tech: TechnicalSummary, price: float) -> dict:
    """多信号共振加权评分（0-100）

    权重分配：
    - 趋势（MA 排列）: 25分
    - 动量（RSI + KDJ）: 25分
    - 量价（量比 + OBV）: 20分
    - 资金（主力流向）: 15分（外部传入）
    - 关键位（支撑/压力）: 15分

    Returns:
        {"score": int, "label": str, "breakdown": dict, "signals": list}
    """
    score = 50  # 中性起始
    breakdown: dict[str, int] = {"趋势": 0, "动量": 0, "量价": 0, "资金": 0, "关键位": 0}
    signals: list[str] = []

    # 趋势 25分
    ma = tech.ma_alignment
    if ma == "多头排列":
        breakdown["趋势"] = 20
        signals.append("MA多头")
    elif ma == "多头回调":
        breakdown["趋势"] = 10
        signals.append("MA多头回调(回踩)")
    elif ma == "空头反弹":
        breakdown["趋势"] = -10
        signals.append("MA空头反弹(反压)")
    elif ma == "空头排列":
        breakdown["趋势"] = -20
        signals.append("MA空头")
    # MA20 斜率辅助
    if tech.ma5 and tech.ma20 and tech.ma5 > tech.ma20:
        breakdown["趋势"] += 5 if breakdown["趋势"] > 0 else -5

    # 动量 25分（RSI + KDJ）
    rsi = tech.rsi
    if rsi is not None:
        if 40 <= rsi <= 60:
            breakdown["动量"] = 0  # 中性
        elif 30 <= rsi < 40:
            breakdown["动量"] = 10
            signals.append("RSI偏低(超卖边缘)")
        elif 60 < rsi <= 70:
            breakdown["动量"] = -10
            signals.append("RSI偏高(超买边缘)")
        elif rsi < 30:
            breakdown["动量"] = 15
            signals.append("RSI超卖")
        elif rsi > 70:
            breakdown["动量"] = -15
            signals.append("RSI超买")
    # KDJ 交叉
    kdj_sig = tech.kdj_signal
    if "金叉" in kdj_sig:
        breakdown["动量"] += 10 if breakdown["动量"] >= 0 else 5
    elif "死叉" in kdj_sig:
        breakdown["动量"] -= 10

    # 量价 20分
    obv_sig = tech.obv_signal
    bb_sig = tech.bb_signal
    if obv_sig and obv_sig not in ("中性", "数据不足"):
        if "流入" in obv_sig or "背离" in obv_sig:
            breakdown["量价"] += 10
            signals.append(f"OBV{obv_sig}")
        elif "流出" in obv_sig:
            breakdown["量价"] -= 10
            signals.append(f"OBV{obv_sig}")
    # BB 位置 + 带宽
    if "下轨" in bb_sig:
        breakdown["量价"] += 5
        signals.append("BB下轨(超跌)")
    elif "上轨" in bb_sig:
        breakdown["量价"] -= 5
        signals.append("BB上轨(超涨)")
    if tech.bb_width is not None and tech.bb_width < 5.0:
        breakdown["量价"] += 3
        signals.append("BB挤压(变盘临近)")

    # 均线乖离（中期极端）
    if tech.ma60 and price and tech.ma60 > 0:
        dev = (price - tech.ma60) / tech.ma60 * 100
        if dev > 25:
            breakdown["趋势"] -= 10
            signals.append(f"MA60乖离+{dev:.0f}%(顶部)")
        elif dev < -20:
            breakdown["趋势"] += 10
            signals.append(f"MA60乖离{dev:.0f}%(底部)")

    # 关键位 15分
    if tech.has_support_confirmation:
        breakdown["关键位"] = 10
        signals.append("支撑确认")
    elif tech.has_resistance_rejection:
        breakdown["关键位"] = -10
        signals.append("压力受阻")
    if tech.has_breakout_retest:
        breakdown["关键位"] += 5
        signals.append("突破回踩")
    if tech.has_support_breakdown:
        breakdown["关键位"] -= 10
        signals.append("跌破支撑")

    # 汇总
    for v in breakdown.values():
        score += v
    score = max(0, min(100, score))

    if score >= 70:
        label = "🟢 强烈看多"
    elif score >= 60:
        label = "🟢 偏多"
    elif score >= 45:
        label = "⚪ 中性"
    elif score >= 35:
        label = "🟡 偏空"
    else:
        label = "🔴 强烈看空"

    return {"score": score, "label": label, "breakdown": breakdown, "signals": signals}
