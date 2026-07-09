"""
组合策略引擎 —— 多指标共振判断

基于经典技术分析方法，将独立的技术指标组合成高胜率的交易信号。
所有策略都基于日线数据，在收盘后信号最准确。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from app.models import KlineData, Quote, TechnicalSummary
from app.technical import (
    is_stagflation,
    is_above_ma_support,
    is_breakabove_bb_middle,
    is_low_volume,
    calc_obv,
    estimate_full_day_volume,
)


# ============================================================
# 组合信号数据模型
# ============================================================

@dataclass
class CombinationSignal:
    """组合策略信号"""
    strategy_name: str = ""
    direction: str = ""
    confidence: str = ""
    matched_conditions: int = 0
    total_conditions: int = 0
    description: str = ""

    @property
    def is_buy(self) -> bool:
        return self.direction == "buy"

    @property
    def is_sell(self) -> bool:
        return self.direction == "sell"

    @property
    def is_triggering(self) -> bool:
        ratio = self.matched_conditions / self.total_conditions if self.total_conditions > 0 else 0
        return ratio >= 0.75

    def to_alert_text(self) -> str:
        if not self.is_triggering:
            return ""
        arrow = "🟢" if self.is_buy else "🔴"
        level = "⭐⭐⭐" if self.confidence == "high" else ("⭐⭐" if self.confidence == "medium" else "⭐")
        return f"{arrow} {level} [{self.strategy_name}] {self.description} ({self.matched_conditions}/{self.total_conditions}条件满足)"


# ============================================================
# 辅助判断函数
# ============================================================

def _is_rsi_breakabove_50(curr: Optional[float], prev: Optional[float]) -> bool:
    """RSI 突破 50：前一日 ≤ 50 且当日 > 50"""
    if curr is None or prev is None:
        return False
    return prev <= 50 and curr > 50


def _is_rsi_breakbelow_50(curr: Optional[float], prev: Optional[float]) -> bool:
    """RSI 跌破 50：前一日 ≥ 50 且当日 < 50"""
    if curr is None or prev is None:
        return False
    return prev >= 50 and curr < 50


def _is_rsi_rising_from_low(curr: Optional[float], prev: Optional[float]) -> bool:
    """RSI 从低位回升：前一日 < 30 且当日 RSI 上升"""
    if curr is None or prev is None:
        return False
    return prev < 30 and curr > prev


def _is_kdj_rising_from_oversold(curr_kdj: KDJSnapshot, prev_kdj: KDJSnapshot) -> bool:
    """KDJ 从超卖区回升：前一日 K ≤ 20 且当日 K 上升"""
    if curr_kdj.k is None or prev_kdj.k is None:
        return False
    return prev_kdj.k <= 20 and curr_kdj.k > prev_kdj.k


def _is_price_new_high(price: Optional[float], klines: list[KlineData], lookback: int = 20) -> bool:
    """股价是否达到 N 日新高"""
    if price is None or len(klines) < lookback:
        return False
    window = klines[-lookback:]
    highest = max((k.high for k in window if k.high is not None), default=0)
    return price >= highest


def _is_macd_top_divergence(closes: list[float], dif_vals: list[float], lookback: int = 20) -> bool:
    """
    MACD 顶背离简化判断：
    价格创近期新高，但 DIF 未创同期新高
    """
    if len(closes) < lookback or len(dif_vals) < lookback:
        return False
    window = closes[-lookback:]
    dif_window = dif_vals[-lookback:]

    price_high_idx = max(range(len(window)), key=lambda i: window[i])
    dif_high_idx = max(range(len(dif_window)), key=lambda i: dif_window[i])

    if price_high_idx != dif_high_idx and price_high_idx > dif_high_idx:
        return True
    return False


def _is_macd_below_zero_flattening(dif: Optional[float], dea: Optional[float],
                                    prev_dif: Optional[float]) -> bool:
    """MACD 在 0 轴下方走平：DIF < 0 且 DIF 变化很小"""
    if dif is None or prev_dif is None:
        return False
    if dif >= 0:
        return False
    return abs(dif - prev_dif) < 0.005


def _is_volume_moderately_increasing(quote: Quote, klines: list[KlineData],
                                      lower: float = 1.2, upper: float = 1.8) -> bool:
    """成交量温和放大：当日量比在 [lower, upper] 区间内"""
    vol = estimate_full_day_volume(quote)
    if not klines or vol is None or vol <= 0:
        return False
    hist = [k.volume for k in klines[:-1] if k.volume and k.volume > 0][-10:]
    if len(hist) < 3:
        return False
    avg_vol = sum(hist) / len(hist)
    if avg_vol <= 0:
        return False
    ratio = vol / avg_vol
    return lower <= ratio <= upper


def _is_volume_amplifying(quote: Quote, klines: list[KlineData], threshold: float = 1.2) -> bool:
    """成交量放大：当日量比 ≥ threshold"""
    vol = estimate_full_day_volume(quote)
    if not klines or vol is None or vol <= 0:
        return False
    hist = [k.volume for k in klines[:-1] if k.volume and k.volume > 0][-10:]
    if len(hist) < 3:
        return False
    avg_vol = sum(hist) / len(hist)
    if avg_vol <= 0:
        return False
    ratio = vol / avg_vol
    return ratio >= threshold


# ============================================================
# KDJ 快照（用于存储前一日值）
# ============================================================

@dataclass
class KDJSnapshot:
    k: Optional[float] = None
    d: Optional[float] = None
    j: Optional[float] = None


# ============================================================
# 策略 1: 趋势启动确认（做多信号）
# ============================================================

def check_trend_start(
    tech: TechnicalSummary,
    prev_tech: Optional[TechnicalSummary],
) -> CombinationSignal:
    """
    趋势启动确认（多指标共振）

    MACD金叉 + RSI突破50 + KDJ从超卖区回升 → 高胜率做多信号

    条件：
    1. MACD 金叉（或从空头转为多头）
    2. RSI 突破 50（从前日 ≤ 50 到当日 > 50）
    3. KDJ 从超卖区回升（前日 K ≤ 20，当日 K 上升）
    """
    sig = CombinationSignal(
        strategy_name="趋势启动",
        direction="buy",
        total_conditions=3,
    )
    conditions = []

    # 条件1: MACD 金叉或从空头转多头
    macd_ok = tech.macd_signal in ("金叉", "多头")
    if macd_ok:
        sig.matched_conditions += 1
        conditions.append("MACD金叉/多头")

    # 条件2: RSI 突破 50
    if prev_tech and _is_rsi_breakabove_50(tech.rsi, prev_tech.rsi):
        sig.matched_conditions += 1
        conditions.append(f"RSI突破50({prev_tech.rsi}→{tech.rsi})")

    # 条件3: KDJ 从超卖区回升
    if prev_tech and prev_tech.kdj_k is not None and tech.kdj_k is not None:
        prev_kdj = KDJSnapshot(k=prev_tech.kdj_k, d=prev_tech.kdj_d, j=prev_tech.kdj_j)
        curr_kdj = KDJSnapshot(k=tech.kdj_k, d=tech.kdj_d, j=tech.kdj_j)
        if _is_kdj_rising_from_oversold(curr_kdj, prev_kdj):
            sig.matched_conditions += 1
            conditions.append(f"KDJ超卖回升(K:{prev_tech.kdj_k}→{tech.kdj_k})")

    # 置信度判断
    if sig.matched_conditions == 3:
        sig.confidence = "high"
    elif sig.matched_conditions == 2:
        sig.confidence = "medium"
    else:
        sig.confidence = "low"

    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 2: 逃顶组合
# ============================================================

def check_top_escape(
    tech: TechnicalSummary,
    quote: Quote,
    klines: list[KlineData],
    closes: list[float],
    dif_vals: list[float],
) -> CombinationSignal:
    """
    逃顶组合

    股价新高 + MACD顶背离 + RSI>80 + KDJ死叉 → 立即减仓
    采用 3/4 条件满足即触发
    """
    sig = CombinationSignal(
        strategy_name="逃顶组合",
        direction="sell",
        total_conditions=4,
    )
    conditions = []

    # 条件1: 股价新高
    if _is_price_new_high(quote.price, klines, 20):
        sig.matched_conditions += 1
        conditions.append("股价20日新高")

    # 条件2: MACD 顶背离
    if _is_macd_top_divergence(closes, dif_vals, 20):
        sig.matched_conditions += 1
        conditions.append("MACD顶背离")

    # 条件3: RSI > 80
    if tech.rsi is not None and tech.rsi > 80:
        sig.matched_conditions += 1
        conditions.append(f"RSI>80({tech.rsi})")

    # 条件4: KDJ 死叉
    if tech.kdj_signal == "死叉":
        sig.matched_conditions += 1
        conditions.append("KDJ死叉")

    # 置信度判断
    if sig.matched_conditions >= 3:
        sig.confidence = "high" if sig.matched_conditions == 4 else "medium"
    else:
        sig.confidence = "low"

    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 3: 震荡套利
# ============================================================

def check_oscillation_arbitrage(
    tech: TechnicalSummary,
    bb_width: Optional[float] = None,
) -> list[CombinationSignal]:
    """
    震荡套利策略

    卖出信号：RSI>70触及布林带上轨 + KDJ死叉
    买入信号：RSI<30触及布林带下轨 + KDJ金叉

    可选：布林带带宽 < 15% 时信号更可靠（震荡收缩）
    """
    signals: list[CombinationSignal] = []

    # ---- 卖出信号 ----
    sell_sig = CombinationSignal(
        strategy_name="震荡套利",
        direction="sell",
        total_conditions=2,
    )
    sell_conditions = []

    if tech.rsi is not None and tech.rsi > 70 and tech.bb_signal == "触及上轨":
        sell_sig.matched_conditions += 1
        sell_conditions.append(f"RSI>70({tech.rsi})+布林上轨")

    if tech.kdj_signal == "死叉":
        sell_sig.matched_conditions += 1
        sell_conditions.append("KDJ死叉")

    if sell_sig.matched_conditions >= 2:
        width_note = ""
        if bb_width is not None and bb_width < 15:
            width_note = " (带宽收窄，信号更可靠)"
            sell_sig.confidence = "high"
        else:
            sell_sig.confidence = "medium"
        sell_sig.description = " + ".join(sell_conditions) + width_note
        signals.append(sell_sig)

    # ---- 买入信号 ----
    buy_sig = CombinationSignal(
        strategy_name="震荡套利",
        direction="buy",
        total_conditions=2,
    )
    buy_conditions = []

    if tech.rsi is not None and tech.rsi < 30 and tech.bb_signal == "触及下轨":
        buy_sig.matched_conditions += 1
        buy_conditions.append(f"RSI<30({tech.rsi})+布林下轨")

    if tech.kdj_signal == "金叉":
        buy_sig.matched_conditions += 1
        buy_conditions.append("KDJ金叉")

    if buy_sig.matched_conditions >= 2:
        width_note = ""
        if bb_width is not None and bb_width < 15:
            width_note = " (带宽收窄，信号更可靠)"
            buy_sig.confidence = "high"
        else:
            buy_sig.confidence = "medium"
        buy_sig.description = " + ".join(buy_conditions) + width_note
        signals.append(buy_sig)

    return signals


# ============================================================
# 策略 4: 双翼齐飞底部形态
# ============================================================

def check_double_wing_bottom(
    tech: TechnicalSummary,
    prev_tech: Optional[TechnicalSummary],
    quote: Quote,
    klines: list[KlineData],
) -> CombinationSignal:
    """
    "双翼齐飞"底部形态

    KDJ与RSI在低位同步向上 + MACD在0轴下方走平 + 成交量温和放大
    → 较强共振看涨信号

    简化版（3条件）：
    1. KDJ 金叉（低位）
    2. RSI 从低位回升（前日 < 30，当日上升）
    3. 成交量温和放大（量比 1.2-1.8）
    """
    sig = CombinationSignal(
        strategy_name="双翼齐飞",
        direction="buy",
        total_conditions=3,
    )
    conditions = []

    # 条件1: KDJ 金叉且处于低位（K < 50）
    if tech.kdj_signal == "金叉" and tech.kdj_k is not None and tech.kdj_k < 50:
        sig.matched_conditions += 1
        conditions.append(f"KDJ低位金叉(K={tech.kdj_k})")

    # 条件2: RSI 从低位回升
    if prev_tech and _is_rsi_rising_from_low(tech.rsi, prev_tech.rsi):
        sig.matched_conditions += 1
        conditions.append(f"RSI低位回升({prev_tech.rsi}→{tech.rsi})")

    # 条件3: 成交量温和放大
    if _is_volume_moderately_increasing(quote, klines):
        sig.matched_conditions += 1
        conditions.append("成交量温和放大")

    # 置信度判断
    if sig.matched_conditions == 3:
        sig.confidence = "high"
    elif sig.matched_conditions == 2:
        sig.confidence = "medium"
    else:
        sig.confidence = "low"

    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 5: 低位放量启动（量价配合）
# ============================================================

def check_low_volume_breakout(
    tech: TechnicalSummary,
    quote: Quote,
    klines: list[KlineData],
) -> CombinationSignal:
    """
    低位放量启动策略

    股价在低位盘整后，突然放量上涨，配合技术指标共振。
    条件：
    1. 成交量放大（量比 ≥ 1.5）
    2. 股价上涨（涨幅 > 0）
    3. OBV 资金流入信号（加速/持续/转向流入，或底背离）
    4. 站稳均线支撑（MA20）
    """
    sig = CombinationSignal(
        strategy_name="低位放量启动",
        direction="buy",
        total_conditions=4,
    )
    conditions = []

    # 条件1: 成交量放大
    if _is_volume_amplifying(quote, klines, 1.5):
        sig.matched_conditions += 1
        conditions.append("成交量显著放大")

    # 条件2: 股价上涨
    if quote.change_pct is not None and quote.change_pct > 0:
        sig.matched_conditions += 1
        conditions.append(f"股价上涨({quote.change_pct:.2f}%)")

    # 条件3: OBV 资金入场信号
    obv_result = calc_obv(klines)
    if obv_result.signal in ("资金加速流入", "资金持续流入", "资金转向流入", "底背离"):
        sig.matched_conditions += 1
        conditions.append(f"OBV{obv_result.signal}")

    # 条件4: 站稳均线支撑
    if is_above_ma_support(quote.price, klines, 20):
        sig.matched_conditions += 1
        conditions.append("站稳MA20支撑")

    # 置信度判断
    if sig.matched_conditions >= 3:
        sig.confidence = "high" if sig.matched_conditions == 4 else "medium"
    else:
        sig.confidence = "low"

    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 6: 高位放量滞警（量价配合）
# ============================================================

def check_high_volume_stagflation(
    tech: TechnicalSummary,
    quote: Quote,
    klines: list[KlineData],
) -> CombinationSignal:
    """
    高位放量滞警策略

    股价处于高位，成交量放大但价格涨幅很小，警惕主力出货。
    条件：
    1. 滞涨现象（涨幅小但放量）
    2. RSI > 70（超买区域）
    3. 股价处于布林带上轨附近或之上
    4. OBV 资金流出信号（加速/持续/转向流出，或顶背离）
    """
    sig = CombinationSignal(
        strategy_name="高位放量滞警",
        direction="sell",
        total_conditions=4,
    )
    conditions = []

    # 条件1: 滞涨
    if is_stagflation(quote, klines):
        sig.matched_conditions += 1
        conditions.append("滞涨(涨幅小但放量)")

    # 条件2: RSI 超买
    if tech.rsi is not None and tech.rsi > 70:
        sig.matched_conditions += 1
        conditions.append(f"RSI超买({tech.rsi})")

    # 条件3: 触及布林带上轨
    if tech.bb_signal == "触及上轨":
        sig.matched_conditions += 1
        conditions.append("触及布林上轨")

    # 条件4: OBV 资金离场信号
    obv_result = calc_obv(klines)
    if obv_result.signal in ("资金加速流出", "资金持续流出", "资金转向流出", "顶背离⚠️"):
        sig.matched_conditions += 1
        conditions.append(f"OBV{obv_result.signal}")

    # 置信度判断
    if sig.matched_conditions >= 3:
        sig.confidence = "high" if sig.matched_conditions == 4 else "medium"
    else:
        sig.confidence = "low"

    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 7: 缩量洗盘识别（量价配合）
# ============================================================

def check_shrinking_volume_washout(
    tech: TechnicalSummary,
    quote: Quote,
    klines: list[KlineData],
) -> CombinationSignal:
    """
    缩量洗盘识别策略

    上升趋势中短暂回调，成交量明显萎缩，可能是主力洗盘。
    条件：
    1. 股价下跌（涨幅 < 0）
    2. 成交量萎缩（量比 ≤ 0.6）
    3. 股价仍在均线之上（未破支撑）
    4. RSI 未进入超卖区（> 30）
    """
    sig = CombinationSignal(
        strategy_name="缩量洗盘",
        direction="buy",
        total_conditions=4,
    )
    conditions = []

    # 条件1: 股价下跌
    if quote.change_pct is not None and quote.change_pct < 0:
        sig.matched_conditions += 1
        conditions.append(f"股价回调({quote.change_pct:.2f}%)")

    # 条件2: 成交量萎缩（使用估算的全天量，避免午盘半天量导致量比虚低）
    vol = estimate_full_day_volume(quote)
    if vol is not None and vol > 0:
        hist = [k.volume for k in klines[:-1] if k.volume and k.volume > 0][-10:]
        if len(hist) >= 3:
            avg_vol = sum(hist) / len(hist)
            ratio = vol / avg_vol if avg_vol > 0 else 1.0
            if ratio <= 0.6:
                sig.matched_conditions += 1
                conditions.append(f"缩量(量比{ratio:.2f})")

    # 条件3: 站稳均线支撑
    if is_above_ma_support(quote.price, klines, 20):
        sig.matched_conditions += 1
        conditions.append("未破MA20支撑")

    # 条件4: RSI 未超卖
    if tech.rsi is not None and tech.rsi > 30:
        sig.matched_conditions += 1
        conditions.append(f"RSI未超卖({tech.rsi})")

    # 置信度判断
    if sig.matched_conditions >= 3:
        sig.confidence = "high" if sig.matched_conditions == 4 else "medium"
    else:
        sig.confidence = "low"

    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 8: 放量突破确认（量价配合）
# ============================================================

def check_volume_breakout(
    tech: TechnicalSummary,
    quote: Quote,
    klines: list[KlineData],
) -> CombinationSignal:
    """
    放量突破确认策略

    放量突破关键价位（如布林带中轨、前期高点等），确认突破有效性。
    条件：
    1. 成交量显著放大（量比 ≥ 1.8）
    2. 股价明显上涨（涨幅 > 1%）
    3. 突破布林带中轨
    4. OBV 趋势为资金入场或放量上涨
    """
    sig = CombinationSignal(
        strategy_name="放量突破确认",
        direction="buy",
        total_conditions=4,
    )
    conditions = []

    # 条件1: 成交量显著放大
    if _is_volume_amplifying(quote, klines, 1.8):
        sig.matched_conditions += 1
        conditions.append("成交量显著放大(≥1.8倍)")

    # 条件2: 股价明显上涨
    if quote.change_pct is not None and quote.change_pct > 1:
        sig.matched_conditions += 1
        conditions.append(f"股价大涨({quote.change_pct:.2f}%)")

    # 条件3: 突破布林带中轨
    if is_breakabove_bb_middle(quote.price, klines, 20):
        sig.matched_conditions += 1
        conditions.append("突破布林中轨")

    # 条件4: OBV 资金入场信号
    obv_result = calc_obv(klines)
    if obv_result.signal in ("资金加速流入", "资金持续流入", "资金转向流入", "底背离"):
        sig.matched_conditions += 1
        conditions.append(f"OBV{obv_result.signal}")

    # 置信度判断
    if sig.matched_conditions >= 3:
        sig.confidence = "high" if sig.matched_conditions == 4 else "medium"
    else:
        sig.confidence = "low"

    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 9: 地量地价反转（量价配合）
# ============================================================

def check_low_volume_reversal(
    tech: TechnicalSummary,
    quote: Quote,
    klines: list[KlineData],
) -> CombinationSignal:
    """
    地量地价反转策略

    长期下跌后成交量缩至地量水平，价格企稳，可能出现反转。
    条件：
    1. 地量（成交量创近期新低）
    2. 股价止跌企稳（涨跌幅在 ±1% 以内）
    3. RSI 超卖后回升（< 30 或从超卖区回升）
    4. KDJ 超卖或金叉
    """
    sig = CombinationSignal(
        strategy_name="地量地价反转",
        direction="buy",
        total_conditions=4,
    )
    conditions = []

    # 条件1: 地量
    if is_low_volume(klines, 20):
        sig.matched_conditions += 1
        conditions.append("成交量地量")

    # 条件2: 股价止跌企稳
    if quote.change_pct is not None and abs(quote.change_pct) <= 1:
        sig.matched_conditions += 1
        conditions.append(f"股价企稳({quote.change_pct:.2f}%)")

    # 条件3: RSI 超卖
    if tech.rsi is not None and tech.rsi <= 30:
        sig.matched_conditions += 1
        conditions.append(f"RSI超卖({tech.rsi})")

    # 条件4: KDJ 超卖或金叉
    if tech.kdj_signal in ("超卖", "金叉"):
        sig.matched_conditions += 1
        conditions.append(f"KDJ{tech.kdj_signal}")

    # 置信度判断
    if sig.matched_conditions >= 3:
        sig.confidence = "high" if sig.matched_conditions == 4 else "medium"
    else:
        sig.confidence = "low"

    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 10: 均线多头回踩买入
# ============================================================

def check_ma_bullish_pullback(
    tech: TechnicalSummary,
    quote: Quote,
    klines: list[KlineData],
) -> CombinationSignal:
    """均线多头排列中的回踩买入信号

    在上升趋势中，价格回踩 MA20 附近是经典的低吸买点。
    均线排列的"多头"状态提供了趋势滤网，避免在下跌趋势中接飞刀。

    条件（4选3触发）：
    1. 均线处于多头状态（多头排列 或 多头回调）
    2. 当前价格距 MA20 在 3% 以内（回踩到支撑附近）
    3. RSI 不处于超买区（< 65，有上行空间）
    4. 近期缩量（量比 < 0.9，回调动能衰减）
    """
    sig = CombinationSignal(
        strategy_name="均线多头回踩",
        direction="buy",
        total_conditions=4,
    )
    conditions = []

    # 条件1: 均线多头状态
    if tech.ma_alignment in ("多头排列", "多头回调"):
        sig.matched_conditions += 1
        conditions.append(f"均线{tech.ma_alignment}")

    # 条件2: 价格接近 MA20
    if tech.ma20 and quote.price and tech.ma20 > 0:
        distance_pct = abs(quote.price - tech.ma20) / tech.ma20 * 100
        if distance_pct <= 3.0 and quote.price > tech.ma20 * 0.97:
            sig.matched_conditions += 1
            direction = "上方" if quote.price >= tech.ma20 else "下方"
            conditions.append(f"距MA20({tech.ma20:.2f}){distance_pct:.1f}%({direction})")

    # 条件3: RSI 不超买
    if tech.rsi is not None and tech.rsi < 65:
        sig.matched_conditions += 1
        conditions.append(f"RSI={tech.rsi:.0f}(非超买)")

    # 条件4: 缩量回调
    if quote.volume_ratio is not None and quote.volume_ratio < 0.9:
        sig.matched_conditions += 1
        conditions.append(f"缩量(量比{quote.volume_ratio:.2f})")

    # 置信度
    sig.confidence = _confidence_from_ratio(sig.matched_conditions, sig.total_conditions)
    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 11: 均线空头反弹卖出
# ============================================================

def check_ma_bearish_bounce(
    tech: TechnicalSummary,
    quote: Quote,
    klines: list[KlineData],
) -> CombinationSignal:
    """均线空头排列中的反弹卖出信号

    在下跌趋势中，价格反弹至 MA20 附近是减仓/做空时机。
    均线排列的"空头"状态提供了趋势滤网，避免在上涨趋势中过早卖出。

    条件（4选3触发）：
    1. 均线处于空头状态（空头排列 或 空头反弹）
    2. 当前价格距 MA20 在 3% 以内（反弹到压力附近）
    3. RSI 不处于超卖区（> 35，仍有下行风险）
    4. 反弹量能不足（量比 < 1.0，无量反弹持续性差）
    """
    sig = CombinationSignal(
        strategy_name="均线空头反弹",
        direction="sell",
        total_conditions=4,
    )
    conditions = []

    # 条件1: 均线空头状态
    if tech.ma_alignment in ("空头排列", "空头反弹"):
        sig.matched_conditions += 1
        conditions.append(f"均线{tech.ma_alignment}")

    # 条件2: 价格接近 MA20
    if tech.ma20 and quote.price and tech.ma20 > 0:
        distance_pct = abs(quote.price - tech.ma20) / tech.ma20 * 100
        if distance_pct <= 3.0 and quote.price < tech.ma20 * 1.03:
            sig.matched_conditions += 1
            direction = "上方" if quote.price >= tech.ma20 else "下方"
            conditions.append(f"距MA20({tech.ma20:.2f}){distance_pct:.1f}%({direction})")

    # 条件3: RSI 不超卖
    if tech.rsi is not None and tech.rsi > 35:
        sig.matched_conditions += 1
        conditions.append(f"RSI={tech.rsi:.0f}(非超卖)")

    # 条件4: 反弹无量
    if quote.volume_ratio is not None and quote.volume_ratio < 1.0:
        sig.matched_conditions += 1
        conditions.append(f"无量反弹(量比{quote.volume_ratio:.2f})")

    # 置信度
    sig.confidence = _confidence_from_ratio(sig.matched_conditions, sig.total_conditions)
    sig.description = " + ".join(conditions) if conditions else "条件不足"
    return sig


# ============================================================
# 策略 12: 均线交叉信号（金叉/死叉）
# ============================================================

def check_ma_cross(
    tech: TechnicalSummary,
    prev_tech: Optional[TechnicalSummary],
) -> Optional[CombinationSignal]:
    """均线 MA5/MA10 交叉信号

    检测短期均线与中期均线的交叉，作为趋势转变的早期信号。
    - 金叉（MA5上穿MA10）且 MA20 上行 → 趋势转多信号
    - 死叉（MA5下穿MA10）且 MA20 下行 → 趋势转空信号
    - 如果 MA20 方向与交叉方向相反 → 仅缠绕信号，不触发

    条件：
    1. 检测到 MA5/MA10 交叉（需要 prev_tech 对比）
    2. MA20 方向与交叉方向一致（金叉时 MA20 上行，死叉时 MA20 下行）
    """
    if prev_tech is None:
        return None

    ma5, ma10 = tech.ma5, tech.ma10
    prev_ma5, prev_ma10 = prev_tech.ma5, prev_tech.ma10

    if any(v is None for v in [ma5, ma10, prev_ma5, prev_ma10]):
        return None

    # 检测金叉
    golden_cross = prev_ma5 <= prev_ma10 and ma5 > ma10
    # 检测死叉
    death_cross = prev_ma5 >= prev_ma10 and ma5 < ma10

    if golden_cross and tech.ma_alignment not in ("空头排列",):
        # MA20 趋势确认
        if tech.ma_alignment_detail and "上行" in tech.ma_alignment_detail:
            confidence = "high"
            desc = f"MA5({ma5:.2f})上穿MA10({ma10:.2f})，MA20上行确认，趋势转多信号可信"
        else:
            confidence = "medium"
            desc = f"MA5({ma5:.2f})上穿MA10({ma10:.2f})，关注MA20能否跟随上行确认"

        return CombinationSignal(
            strategy_name="均线金叉",
            direction="buy",
            confidence=confidence,
            matched_conditions=2,
            total_conditions=2,
            description=desc,
        )

    if death_cross and tech.ma_alignment not in ("多头排列",):
        if tech.ma_alignment_detail and "下行" in tech.ma_alignment_detail:
            confidence = "high"
            desc = f"MA5({ma5:.2f})下穿MA10({ma10:.2f})，MA20下行确认，趋势转空信号可信"
        else:
            confidence = "medium"
            desc = f"MA5({ma5:.2f})下穿MA10({ma10:.2f})，关注MA20能否跟随下行确认"

        return CombinationSignal(
            strategy_name="均线死叉",
            direction="sell",
            confidence=confidence,
            matched_conditions=2,
            total_conditions=2,
            description=desc,
        )

    return None


# ============================================================
# 辅助：根据匹配比例计算置信度
# ============================================================

def _confidence_from_ratio(matched: int, total: int) -> str:
    """辅助函数：根据条件满足比例返回置信度字符串"""
    if total == 0:
        return "low"
    ratio = matched / total
    if ratio >= 1.0:
        return "high"
    elif ratio >= 0.75:
        return "medium"
    else:
        return "low"


# ============================================================
# 统一入口：评估所有策略
# ============================================================

def evaluate_all_strategies(
    tech: TechnicalSummary,
    prev_tech: Optional[TechnicalSummary],
    quote: Quote,
    klines: list[KlineData],
    dif_vals: Optional[list[float]] = None,
    closes: Optional[list[float]] = None,
) -> list[CombinationSignal]:
    """
    对所有组合策略进行评估，返回所有触发的信号

    Args:
        tech: 当前 TechnicalSummary
        prev_tech: 前一日 TechnicalSummary（用于判断趋势变化）
        quote: 当前行情
        klines: K线数据
        dif_vals: MACD 的 DIF 值序列（用于顶背离检测，可选）
        closes: 收盘价序列（用于顶背离检测，可选）

    Returns:
        所有触发中的组合信号列表（matched/total >= 0.75）
    """
    signals: list[CombinationSignal] = []

    # 策略1: 趋势启动
    trend_sig = check_trend_start(tech, prev_tech)
    if trend_sig.is_triggering:
        signals.append(trend_sig)

    # 策略2: 逃顶组合
    if closes and dif_vals:
        escape_sig = check_top_escape(tech, quote, klines, closes, dif_vals)
        if escape_sig.is_triggering:
            signals.append(escape_sig)

    # 策略3: 震荡套利
    arb_sigs = check_oscillation_arbitrage(tech, tech.bb_width)
    for s in arb_sigs:
        if s.is_triggering:
            signals.append(s)

    # 策略4: 双翼齐飞
    bottom_sig = check_double_wing_bottom(tech, prev_tech, quote, klines)
    if bottom_sig.is_triggering:
        signals.append(bottom_sig)

    # 策略5: 低位放量启动
    low_vol_sig = check_low_volume_breakout(tech, quote, klines)
    if low_vol_sig.is_triggering:
        signals.append(low_vol_sig)

    # 策略6: 高位放量滞警
    stag_sig = check_high_volume_stagflation(tech, quote, klines)
    if stag_sig.is_triggering:
        signals.append(stag_sig)

    # 策略7: 缩量洗盘识别
    washout_sig = check_shrinking_volume_washout(tech, quote, klines)
    if washout_sig.is_triggering:
        signals.append(washout_sig)

    # 策略8: 放量突破确认
    breakout_sig = check_volume_breakout(tech, quote, klines)
    if breakout_sig.is_triggering:
        signals.append(breakout_sig)

    # 策略9: 地量地价反转
    reversal_sig = check_low_volume_reversal(tech, quote, klines)
    if reversal_sig.is_triggering:
        signals.append(reversal_sig)

    # 策略10: 均线多头回踩买入
    pullback_sig = check_ma_bullish_pullback(tech, quote, klines)
    if pullback_sig.is_triggering:
        signals.append(pullback_sig)

    # 策略11: 均线空头反弹卖出
    bounce_sig = check_ma_bearish_bounce(tech, quote, klines)
    if bounce_sig.is_triggering:
        signals.append(bounce_sig)

    # 策略12: 均线金叉/死叉
    cross_sig = check_ma_cross(tech, prev_tech)
    if cross_sig is not None and cross_sig.is_triggering:
        signals.append(cross_sig)

    return signals


# ============================================================
# 便捷函数：生成所有信号的警报文本
# ============================================================

def get_strategy_alert_texts(
    tech: TechnicalSummary,
    prev_tech: Optional[TechnicalSummary],
    quote: Quote,
    klines: list[KlineData],
    dif_vals: Optional[list[float]] = None,
    closes: Optional[list[float]] = None,
) -> list[str]:
    """
    获取所有触发策略的警报文本列表（可直接加入 Alert messages）
    """
    signals = evaluate_all_strategies(tech, prev_tech, quote, klines, dif_vals, closes)
    return [s.to_alert_text() for s in signals if s.to_alert_text()]


# ============================================================
# 便捷函数：计算 MACD 的 DIF 值序列（用于顶背离检测）
# ============================================================

def calc_macd_dif_series(closes: list[float], short: int = 12, long: int = 26) -> list[float]:
    """计算 MACD 的 DIF 值序列"""
    from app.technical import _ema
    if len(closes) < long:
        return []
    ema_short = _ema(closes, short)
    ema_long = _ema(closes, long)
    return [s - l for s, l in zip(ema_short, ema_long)]
