"""
关键位动态行为分析模块测试
"""

import pytest
from app.technical import (
    analyze_key_level_behavior,
    KeyLevelBehavior,
    _count_level_tests,
    _calc_level_confluence,
    _assess_level_strength,
    _merge_nearby_levels,
)
from app.models import KlineData


# ============================================================
# 测试数据生成辅助
# ============================================================

def _make_klines(prices: list[tuple[float, float, float, float]]) -> list[KlineData]:
    """从 (open, high, low, close) 列表生成K线"""
    klines = []
    for i, (o, h, l, c) in enumerate(prices):
        klines.append(KlineData(
            date=f"2026-08-{1 + i:02d}",
            open=o,
            high=h,
            low=l,
            close=c,
            volume=1000000 + i * 10000,
        ))
    return klines


def _make_trending(points: list[float], amplitude: float = 0.05) -> list[KlineData]:
    """从价格列表生成简单K线（开盘=收盘，最高/最低加点振幅）"""
    klines = []
    for i, p in enumerate(points):
        klines.append(KlineData(
            date=f"2026-08-{1 + i:02d}",
            open=p,
            high=p + amplitude,
            low=p - amplitude,
            close=p,
            volume=1000000 + i * 10000,
        ))
    return klines


# ============================================================
# _merge_nearby_levels 测试
# ============================================================

class TestMergeNearbyLevels:
    def test_merges_close_levels(self):
        """ATR=0.2, zone=0.1: 1.20和1.24应该合并"""
        levels = [1.20, 1.24, 1.50]
        result = _merge_nearby_levels(levels, atr=0.2, zone_mult=0.5)
        # 1.20, 1.24 距离 0.04 <= 0.1 → 合并为 1.22
        # 1.50 距离远，单独保留
        assert len(result) == 2
        assert pytest.approx(1.22, 0.01) in result

    def test_keeps_far_levels_separate(self):
        """远距离的位要保留各自独立"""
        levels = [1.20, 1.50, 1.80]
        result = _merge_nearby_levels(levels, atr=0.1, zone_mult=0.5)
        assert len(result) == 3

    def test_empty_list(self):
        assert _merge_nearby_levels([], atr=0.2) == []

    def test_single_level(self):
        assert _merge_nearby_levels([1.50], atr=0.2) == [1.50]


# ============================================================
# _count_level_tests 测试
# ============================================================

class TestCountLevelTests:
    def test_counts_support_touches(self):
        """价格低点多次触及支撑位"""
        klines = _make_trending([10.0, 9.95, 10.1, 9.98, 10.05, 10.2, 10.15,
                                 10.3, 10.25, 10.4, 10.35, 10.5, 10.45, 10.6, 10.55])
        # 低点在 9.93-10.53 之间，支撑位 10.0
        weighted, raw = _count_level_tests(klines, level=10.0, atr=0.2, is_support=True)
        # 有几根K线的低点接近10.0
        assert raw >= 1

    def test_counts_resistance_touches(self):
        """价格高点多次触及压力位"""
        klines = _make_trending([10.0, 10.15, 10.05, 10.2, 10.1, 10.3, 10.25,
                                 10.4, 10.35, 10.5, 10.45, 10.6, 10.55, 10.7, 10.65])
        weighted, raw = _count_level_tests(klines, level=10.5, atr=0.2, is_support=False)
        assert raw >= 1

    def test_recent_tests_weighted_more(self):
        """近期触及权重更高"""
        klines = _make_klines([(10.0, 10.2, 9.80, 10.1)] * 20)
        weighted, _ = _count_level_tests(klines, level=9.85, atr=0.2, is_support=True)
        # 所有测试都在近期，权重应接近原始次数
        assert weighted > 0

    def test_zero_atr_returns_zero(self):
        klines = _make_trending([10.0] * 10)
        weighted, raw = _count_level_tests(klines, level=10.0, atr=0, is_support=True)
        assert weighted == 0
        assert raw == 0


# ============================================================
# _calc_level_confluence 测试
# ============================================================

class TestCalcLevelConfluence:
    def test_high_confluence(self):
        """多个方法指向同一区域"""
        all_levels = [1.20, 1.21, 1.22]  # 3个位很接近
        count = _calc_level_confluence(1.20, all_levels, atr=0.2, zone_mult=0.5)
        assert count == 3

    def test_low_confluence(self):
        """只有一个方法"""
        all_levels = [1.20, 1.50, 1.80]
        count = _calc_level_confluence(1.20, all_levels, atr=0.2, zone_mult=0.5)
        assert count == 1


# ============================================================
# _assess_level_strength 测试
# ============================================================

class TestAssessLevelStrength:
    def test_strong_support_with_confluence_and_tests(self):
        """多次测试 + 多方法共振 → 强"""
        klines = _make_klines([
            (10.0, 10.2, 9.85, 10.1),
            (10.1, 10.3, 9.88, 10.2),
            (10.2, 10.4, 9.82, 10.3),
            (10.3, 10.5, 9.90, 10.4),
        ] * 5)  # 20根K线，低点多次在 9.82-9.90，支撑位10.0
        supp, res, st, rt, summary = _assess_level_strength(
            klines, support=9.88, resistance=None, atr=0.2,
            all_supports=[9.88, 9.90, 9.86], all_resistances=[],
        )
        assert supp in ("强", "中")

    def test_no_levels_returns_empty(self):
        supp, res, st, rt, summary = _assess_level_strength(
            [], support=None, resistance=None, atr=None,
            all_supports=[], all_resistances=[],
        )
        assert supp == ""
        assert res == ""


# ============================================================
# analyze_key_level_behavior 测试
# ============================================================

class TestAnalyzeKeyLevelBehavior:
    """核心检测函数测试"""

    # ---- 压力位受阻回落 ----
    def test_resistance_rejection(self):
        """价格接近压力位后回落"""
        # 构造：价格逐步上升到接近压力位 10.5，然后回落
        klines = _make_klines([
            (10.0, 10.15, 9.95, 10.1),
            (10.1, 10.25, 10.05, 10.2),
            (10.2, 10.35, 10.15, 10.3),
            (10.3, 10.48, 10.25, 10.4),  # 高点 10.48 接近压力位 10.5
            (10.4, 10.52, 10.30, 10.30),  # 突破触及 10.52 然后收低
            (10.3, 10.35, 10.15, 10.2),   # 回落
        ])
        result = analyze_key_level_behavior(
            klines, current_price=10.2, support=9.8, resistance=10.5,
            atr=0.2, swing_supports=[9.8], swing_resistances=[10.5],
        )
        assert result.has_resistance_rejection
        assert "10.5" in result.resistance_rejection_detail

    def test_no_rejection_when_price_broke_through(self):
        """价格突破压力位后继续上涨，不算受阻"""
        klines = _make_klines([
            (10.0, 10.2, 9.9, 10.15),
            (10.2, 10.5, 10.1, 10.45),
            (10.5, 10.7, 10.4, 10.6),  # 突破了
            (10.6, 10.8, 10.5, 10.7),  # 继续上涨
        ])
        result = analyze_key_level_behavior(
            klines, current_price=10.7, support=9.8, resistance=10.4,
            atr=0.3, swing_supports=[9.8], swing_resistances=[10.4],
        )
        # 当前价高于压力位，不应该是受阻回落
        assert not result.has_resistance_rejection

    def test_no_rejection_when_price_far_from_resistance(self):
        """价格远离压力位"""
        klines = _make_trending([10.0, 10.1, 10.2, 10.3, 10.4], amplitude=0.02)
        result = analyze_key_level_behavior(
            klines, current_price=10.4, support=9.5, resistance=12.0,
            atr=0.2, swing_supports=[9.5], swing_resistances=[12.0],
        )
        assert not result.has_resistance_rejection

    # ---- 支撑位有效确认 ----
    def test_support_confirmation(self):
        """价格回踩支撑位后反弹"""
        klines = _make_klines([
            (10.5, 10.6, 10.4, 10.45),
            (10.4, 10.5, 10.3, 10.35),
            (10.3, 10.4, 10.22, 10.25),
            (10.2, 10.3, 10.05, 10.1),  # 低点 10.05 接近支撑位 10.0
            (10.1, 10.2, 10.0, 10.15),  # 触及 10.0
            (10.15, 10.3, 10.1, 10.25),  # 反弹
        ])
        result = analyze_key_level_behavior(
            klines, current_price=10.25, support=10.0, resistance=10.8,
            atr=0.2, swing_supports=[10.0], swing_resistances=[10.8],
        )
        assert result.has_support_confirmation
        assert "10.0" in result.support_confirmation_detail

    def test_no_confirmation_when_price_is_far(self):
        """现价离支撑位很远，但没真正测试过"""
        klines = _make_trending([12.0, 12.1, 12.2, 12.3, 12.4], amplitude=0.05)
        result = analyze_key_level_behavior(
            klines, current_price=12.4, support=10.0, resistance=13.0,
            atr=0.2, swing_supports=[10.0], swing_resistances=[13.0],
        )
        assert not result.has_support_confirmation

    # ---- 跌破支撑位 ----
    def test_support_breakdown(self):
        """当前价跌破支撑位"""
        klines = _make_trending([10.5, 10.3, 10.1, 9.9, 9.7], amplitude=0.05)
        result = analyze_key_level_behavior(
            klines, current_price=9.7, support=10.0, resistance=11.0,
            atr=0.2, swing_supports=[10.0], swing_resistances=[11.0],
        )
        assert result.has_support_breakdown

    def test_no_breakdown_when_above_support(self):
        """价格在支撑位上方"""
        klines = _make_trending([10.5, 10.3, 10.1, 10.2, 10.4], amplitude=0.05)
        result = analyze_key_level_behavior(
            klines, current_price=10.4, support=10.0, resistance=11.0,
            atr=0.2, swing_supports=[10.0], swing_resistances=[11.0],
        )
        assert not result.has_support_breakdown

    def test_no_breakdown_for_trivial_break(self):
        """极小的跌破不触发（ATR=0.1, 跌破0.1%不算）"""
        klines = _make_trending([10.5, 10.3, 10.1, 10.0, 9.99], amplitude=0.05)
        result = analyze_key_level_behavior(
            klines, current_price=9.99, support=10.0, resistance=11.0,
            atr=0.1, swing_supports=[10.0], swing_resistances=[11.0],
        )
        # 跌破 0.1%，ATR=0.1，阈值需要 atr/price*30 = 0.1/10*30 = 0.3%
        # 实际跌破 (10-9.99)/10 = 0.1% < 0.3%，不应触发
        assert not result.has_support_breakdown

    # ---- 突破后回踩确认 ----
    def test_breakout_retest(self):
        """突破压力位后回踩确认站稳"""
        klines = _make_klines([
            (10.0, 10.2, 9.9, 10.1),
            (10.1, 10.3, 10.0, 10.2),
            (10.2, 10.55, 10.2, 10.5),  # 突破压力位 10.4
            (10.5, 10.6, 10.35, 10.45),  # 回踩低点 10.35 接近原压力位 10.4
            (10.45, 10.7, 10.4, 10.55),  # 站稳
        ])
        result = analyze_key_level_behavior(
            klines, current_price=10.55, support=9.5, resistance=10.4,
            atr=0.2, swing_supports=[9.5], swing_resistances=[10.4],
        )
        assert result.has_breakout_retest
        assert "10.4" in result.breakout_retest_detail

    def test_no_retest_without_breakout(self):
        """没突破过就不可能有回踩确认"""
        klines = _make_trending([9.8, 9.9, 10.0, 9.95, 10.05], amplitude=0.05)
        result = analyze_key_level_behavior(
            klines, current_price=10.05, support=9.5, resistance=10.5,
            atr=0.2, swing_supports=[9.5], swing_resistances=[10.5],
        )
        # 现价 < 压力位，不可能有突破后回踩
        assert not result.has_breakout_retest

    # ---- 强度评估 ----
    def test_level_strength_assessed(self):
        """关键位强度应被评估"""
        klines = _make_trending([10.0 + i * 0.05 for i in range(30)], amplitude=0.03)
        result = analyze_key_level_behavior(
            klines, current_price=11.5, support=10.0, resistance=12.0,
            atr=0.2, swing_supports=[10.0, 9.95], swing_resistances=[12.0, 12.1],
            pivot_supports=[10.05], pivot_resistances=[11.95],
            volume_clusters=[10.1, 11.9],
        )
        # 支撑位 10.0 附近有多个方法共振（9.95, 10.05, 10.1）
        assert result.support_strength in ("强", "中", "弱")
        # 压力位 12.0：测试数据价格范围 10.0-11.45，未触及压力位，强度可为空或弱
        assert result.resistance_strength in ("强", "中", "弱", "")
        assert result.strength_summary != ""

    # ---- 边界情况 ----
    def test_empty_klines_returns_default(self):
        result = analyze_key_level_behavior(
            [], current_price=10.0, support=9.5, resistance=10.5, atr=0.2,
        )
        assert not result.has_resistance_rejection
        assert not result.has_support_confirmation
        assert not result.has_support_breakdown
        assert not result.has_breakout_retest

    def test_too_few_klines_returns_default(self):
        klines = _make_trending([10.0, 10.1, 10.0], amplitude=0.05)
        result = analyze_key_level_behavior(
            klines, current_price=10.0, support=9.5, resistance=10.5, atr=0.2,
        )
        assert not result.has_resistance_rejection

    def test_no_support_resistance_returns_default(self):
        klines = _make_trending([10.0 + i * 0.05 for i in range(20)])
        result = analyze_key_level_behavior(
            klines, current_price=11.0, support=None, resistance=None, atr=0.2,
        )
        # 没有支撑/压力位，所有信号应默认 False
        assert not result.has_resistance_rejection
        assert not result.has_support_confirmation
        assert not result.has_support_breakdown
        assert not result.has_breakout_retest
