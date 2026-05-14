"""
技术分析模块测试
"""

import pytest
from app.technical import (
    calc_rsi, rsi_signal, calc_macd, calc_kdj,
    calc_support_resistance, get_technical_summary, _ema,
    MACDResult, KDJResult, SupportResistance,
)
from app.models import KlineData, Quote, TechnicalSummary


# ============================================================
# 测试数据生成辅助
# ============================================================

def _make_closes_trending_up(n=30, base=10.0, step=0.05):
    """生成递增收盘价序列"""
    return [round(base + step * i, 4) for i in range(n)]


def _make_closes_trending_down(n=30, base=20.0, step=0.05):
    """生成递减收盘价序列"""
    return [round(base - step * i, 4) for i in range(n)]


def _make_klines(n=30, base=10.0, step=0.02):
    """生成 K线数据"""
    klines = []
    for i in range(n):
        c = round(base + step * i, 4)
        klines.append(KlineData(
            date=f"20260{min(i // 5, 9)}{1 + i % 5:02d}",
            open=c,
            high=c + 0.1,
            low=c - 0.1,
            close=c,
            volume=1000000 + i * 1000,
        ))
    return klines


# ============================================================
# EMA 测试
# ============================================================

class TestEMA:
    def test_basic_ema(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = _ema(values, 3)
        assert len(result) == 5
        assert result[0] == 1.0  # 第一个值等于第一个输入

    def test_ema_empty(self):
        assert _ema([], 3) == []

    def test_ema_constant(self):
        values = [5.0] * 10
        result = _ema(values, 3)
        # 常数序列 EMA 应等于常数
        for v in result:
            assert abs(v - 5.0) < 0.01


# ============================================================
# RSI 测试
# ============================================================

class TestRSI:
    def test_rsi_up_trend(self):
        closes = _make_closes_trending_up(30, 10.0, 0.1)
        rsi = calc_rsi(closes)
        assert rsi is not None
        assert rsi > 50  # 上涨趋势 RSI 应偏高

    def test_rsi_down_trend(self):
        closes = _make_closes_trending_down(30, 20.0, 0.1)
        rsi = calc_rsi(closes)
        assert rsi is not None
        assert rsi < 50  # 下跌趋势 RSI 应偏低

    def test_rsi_all_up(self):
        """持续上涨 → RSI 接近 100"""
        closes = list(range(1, 20))
        rsi = calc_rsi(closes)
        assert rsi is not None
        assert rsi >= 90

    def test_rsi_all_down(self):
        """持续下跌 → RSI 等于 0"""
        closes = list(range(20, 1, -1))
        rsi = calc_rsi(closes)
        assert rsi is not None
        assert rsi <= 10

    def test_rsi_insufficient_data(self):
        assert calc_rsi([1.0, 2.0, 3.0]) is None

    def test_rsi_empty(self):
        assert calc_rsi([]) is None

    def test_rsi_signal_buy(self):
        assert rsi_signal(15) == "严重超卖"
        assert rsi_signal(25) == "超卖"

    def test_rsi_signal_sell(self):
        assert rsi_signal(85) == "严重超买"
        assert rsi_signal(75) == "超买"

    def test_rsi_signal_neutral(self):
        assert rsi_signal(50) == "中性"
        assert rsi_signal(45) == "中性"

    def test_rsi_signal_none(self):
        assert rsi_signal(None) == "数据不足"


# ============================================================
# MACD 测试
# ============================================================

class TestMACD:
    def test_macd_up_trend(self):
        closes = _make_closes_trending_up(50, 10.0, 0.1)
        result = calc_macd(closes)
        assert result.dif is not None
        assert result.dea is not None
        assert result.histogram is not None
        # 上涨趋势 DIF 应大于 DEA
        assert result.dif > result.dea

    def test_macd_down_trend(self):
        closes = _make_closes_trending_down(50, 20.0, 0.1)
        result = calc_macd(closes)
        assert result.dif is not None
        assert result.dif < result.dea

    def test_macd_insufficient_data(self):
        result = calc_macd([1.0, 2.0, 3.0])
        assert result.signal == "数据不足"

    def test_macd_golden_cross(self):
        """构造金叉数据：先跌后涨（需要足够多的数据点）"""
        # 50 天跌 + 50 天涨 = 足够触发金叉
        closes = list(range(50, 20, -1)) + list(range(20, 70))
        result = calc_macd(closes)
        # 金叉或至少多头
        assert result.signal in ("金叉", "多头")

    def test_macd_death_cross(self):
        """构造死叉数据：先涨后跌"""
        closes = list(range(10, 30)) + list(range(30, 10, -1))
        result = calc_macd(closes)
        assert result.signal in ("死叉", "空头", "中性")

    def test_macd_empty(self):
        result = calc_macd([])
        assert result.signal == "数据不足"


# ============================================================
# KDJ 测试
# ============================================================

class TestKDJ:
    def test_kdj_up_trend(self):
        klines = _make_klines(20, 10.0, 0.1)
        highs = [k.high for k in klines]
        lows = [k.low for k in klines]
        closes = [k.close for k in klines]
        result = calc_kdj(highs, lows, closes)
        assert result.k is not None
        assert result.d is not None
        assert result.j is not None

    def test_kdj_j_formula(self):
        """J = 3K - 2D"""
        klines = _make_klines(20, 10.0, 0.05)
        result = calc_kdj(
            [k.high for k in klines],
            [k.low for k in klines],
            [k.close for k in klines],
        )
        if result.k is not None and result.d is not None:
            expected_j = 3 * result.k - 2 * result.d
            assert abs(result.j - round(expected_j, 2)) < 0.1

    def test_kdj_insufficient_data(self):
        result = calc_kdj([1.0], [0.9], [0.95])
        assert result.signal == "数据不足"

    def test_kdj_empty(self):
        result = calc_kdj([], [], [])
        assert result.signal == "数据不足"

    def test_kdj_overbought(self):
        """收盘价持续在高位 → KDJ 偏高"""
        highs = [11.0] * 20
        lows = [10.0] * 20
        closes = [10.95] * 20  # 接近最高价
        result = calc_kdj(highs, lows, closes)
        assert result.k is not None
        assert result.k > 50

    def test_kdj_oversold(self):
        """收盘价持续在低位 → KDJ 偏低"""
        highs = [11.0] * 20
        lows = [10.0] * 20
        closes = [10.05] * 20  # 接近最低价
        result = calc_kdj(highs, lows, closes)
        assert result.k is not None
        assert result.k < 50


# ============================================================
# 支撑/压力位 测试
# ============================================================

class TestSupportResistance:
    def test_basic_sr(self):
        klines = _make_klines(25, 10.0, 0.1)
        result = calc_support_resistance(klines)
        assert result.support is not None
        assert result.resistance is not None
        assert result.resistance > result.support

    def test_sr_empty(self):
        result = calc_support_resistance([])
        assert result.support is None
        assert result.resistance is None
        assert result.atr is None

    def test_sr_atr_positive(self):
        klines = _make_klines(25, 10.0, 0.1)
        result = calc_support_resistance(klines)
        assert result.atr is not None
        assert result.atr > 0

    def test_sr_lookback_smaller_than_data(self):
        klines = _make_klines(40, 10.0, 0.05)
        result = calc_support_resistance(klines, lookback=10)
        assert result.support is not None
        # 回看窗口更小，支撑/压力范围更窄
        assert result.resistance - result.support < 2.0


# ============================================================
# 汇总函数 测试
# ============================================================

class TestTechnicalSummary:
    def test_summary_with_data(self):
        klines = _make_klines(35, 10.0, 0.08)
        quote = Quote(code="510300", name="沪深300ETF", price=12.5, change_pct=1.5)
        result = get_technical_summary(quote, klines)

        assert isinstance(result, TechnicalSummary)
        assert result.rsi is not None
        assert result.rsi_signal
        assert result.macd_signal
        assert result.kdj_signal

    def test_summary_empty_klines(self):
        quote = Quote(code="510300", name="沪深300ETF", price=12.5)
        result = get_technical_summary(quote, [])
        assert result.rsi is None
        assert result.rsi_signal == "数据不足"
        assert result.macd_signal == "数据不足"
        assert result.kdj_signal == "数据不足"

    def test_summary_signals_list(self):
        """强趋势应该产生信号"""
        klines = _make_klines(40, 10.0, 0.15)
        quote = Quote(code="510300", name="沪深300ETF", price=14.0, change_pct=3.0)
        result = get_technical_summary(quote, klines)
        # 强上涨趋势应该有 RSI 超买或 MACD 信号
        assert len(result.signals) >= 1 or result.macd_signal in ("多头", "金叉")
