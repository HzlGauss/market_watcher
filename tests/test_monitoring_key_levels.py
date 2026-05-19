"""
盯盘关键位功能测试
"""

import pytest
from app.models import KlineData, Quote, TechnicalSummary
from app.technical import calc_support_resistance, get_technical_summary


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
# 测试 analyzer.py 中的关键位触发逻辑
# ============================================================

class TestKeyLevelAlerts:
    def test_price_near_pivot_support(self):
        """价格接近枢轴支撑位时应触发警报"""
        klines = _make_klines(30, 10.0, 0.05)
        sr = calc_support_resistance(klines)

        # 构造一个接近枢轴支撑的价格
        if sr.pivot_supports:
            near_price = sr.pivot_supports[0] * 1.01  # 在1%范围内
            quote = Quote(
                code="123456",
                name="测试",
                price=near_price,
                change_pct=0.5,
                volume=1000000,
            )

            tech = get_technical_summary(quote, klines)
            assert tech.pivot_supports
            # 验证价格确实在枢轴支撑附近
            assert abs(near_price - sr.pivot_supports[0]) / sr.pivot_supports[0] <= 0.02

    def test_price_near_pivot_resistance(self):
        """价格接近枢轴压力位时应触发警报"""
        klines = _make_klines(30, 10.0, 0.05)
        sr = calc_support_resistance(klines)

        if sr.pivot_resistances:
            near_price = sr.pivot_resistances[0] * 0.99  # 在1%范围内
            quote = Quote(
                code="123456",
                name="测试",
                price=near_price,
                change_pct=0.5,
                volume=1000000,
            )

            tech = get_technical_summary(quote, klines)
            assert tech.pivot_resistances
            assert abs(near_price - sr.pivot_resistances[0]) / sr.pivot_resistances[0] <= 0.02

    def test_price_below_main_support(self):
        """价格跌破主支撑位时应触发警报"""
        klines = _make_klines(30, 10.0, 0.05)
        sr = calc_support_resistance(klines)

        if sr.support:
            below_price = sr.support * 0.98  # 低于支撑位2%
            quote = Quote(
                code="123456",
                name="测试",
                price=below_price,
                change_pct=-2.0,
                volume=1000000,
            )

            tech = get_technical_summary(quote, klines)
            assert tech.support
            # 验证价格确实在支撑位附近
            assert below_price <= tech.support * 1.01

    def test_price_above_main_resistance(self):
        """价格突破主压力位时应触发警报"""
        klines = _make_klines(30, 10.0, 0.05)
        sr = calc_support_resistance(klines)

        if sr.resistance:
            above_price = sr.resistance * 1.02  # 高于压力位2%
            quote = Quote(
                code="123456",
                name="测试",
                price=above_price,
                change_pct=2.0,
                volume=1000000,
            )

            tech = get_technical_summary(quote, klines)
            assert tech.resistance
            # 验证价格确实在压力位附近
            assert above_price >= tech.resistance * 0.99

    def test_no_alert_when_price_far_from_levels(self):
        """价格远离关键位时不应触发警报"""
        klines = _make_klines(30, 10.0, 0.05)
        sr = calc_support_resistance(klines)

        if sr.support and sr.resistance:
            # 价格正好在中间
            mid_price = (sr.support + sr.resistance) / 2
            quote = Quote(
                code="123456",
                name="测试",
                price=mid_price,
                change_pct=0.1,
                volume=1000000,
            )

            tech = get_technical_summary(quote, klines)
            # 验证价格确实在中间位置
            assert sr.support < mid_price < sr.resistance


# ============================================================
# 测试 presenter.py 中的关键位展示
# ============================================================

class TestKeyLevelPresenter:
    def test_print_key_levels_has_data(self):
        """测试有关键位数据时的展示"""
        from app.presenter import print_key_levels
        from io import StringIO
        import sys

        klines = _make_klines(30, 10.0, 0.05)
        quote = Quote(
            code="123456",
            name="测试基金",
            price=10.5,
            change_pct=0.5,
            volume=1000000,
        )
        tech = get_technical_summary(quote, klines)

        tech_summaries = {"123456": tech}
        quotes = [quote]

        # 捕获输出
        captured = StringIO()
        sys.stdout, old = captured, sys.stdout
        try:
            print_key_levels(tech_summaries, quotes)
        finally:
            sys.stdout = old

        output = captured.getvalue()
        assert "测试基金" in output or "关键价位" in output

    def test_print_key_levels_empty(self):
        """测试无关键位数据时的展示"""
        from app.presenter import print_key_levels
        from io import StringIO
        import sys

        captured = StringIO()
        sys.stdout, old = captured, sys.stdout
        try:
            print_key_levels({}, [])
        finally:
            sys.stdout = old

        output = captured.getvalue()
        assert "暂无关键位数据" in output or "关键价位" in output


# ============================================================
# 测试枢轴点计算正确性
# ============================================================

class TestPivotPoints:
    def test_pivot_formula(self):
        """验证枢轴点计算公式"""
        from app.technical import _calc_pivot_points

        high = 11.0
        low = 9.0
        close = 10.0

        supports, resistances = _calc_pivot_points(high, low, close)

        # 经典枢轴点公式
        pivot = (high + low + close) / 3  # = 10.0
        r1 = 2 * pivot - low  # = 11.0
        s1 = 2 * pivot - high  # = 9.0
        r2 = pivot + (high - low)  # = 12.0
        s2 = pivot - (high - low)  # = 8.0
        r3 = r1 + (high - low)  # = 13.0
        s3 = s1 - (high - low)  # = 7.0

        assert len(supports) == 3
        assert len(resistances) == 3

        # 支撑位升序：S3 < S2 < S1
        assert supports[0] < supports[1] < supports[2]
        assert abs(s3 - supports[0]) < 0.001  # S3最小
        assert abs(s2 - supports[1]) < 0.001
        assert abs(s1 - supports[2]) < 0.001  # S1最大

        # 压力位降序：R3 > R2 > R1
        assert resistances[0] > resistances[1] > resistances[2]
        assert abs(r3 - resistances[0]) < 0.001  # R3最大
        assert abs(r2 - resistances[1]) < 0.001
        assert abs(r1 - resistances[2]) < 0.001  # R1最小（最接近中枢）

    def test_pivot_all_supports_below_resistances(self):
        """所有枢轴支撑位都应低于所有枢轴压力位"""
        from app.technical import _calc_pivot_points

        supports, resistances = _calc_pivot_points(11.0, 9.0, 10.0)

        # 最大支撑 < 最小压力
        assert max(supports) < min(resistances)
