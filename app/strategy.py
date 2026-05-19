"""
组合策略引擎 —— 多指标共振判断

基于经典技术分析方法，将独立的技术指标组合成高胜率的交易信号。
所有策略都基于日线数据，在收盘后信号最准确。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from app.models import KlineData, Quote, TechnicalSummary


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
    if not klines or quote.volume is None or quote.volume <= 0:
        return False
    hist = [k.volume for k in klines[:-1] if k.volume and k.volume > 0][-10:]
    if len(hist) < 3:
        return False
    avg_vol = sum(hist) / len(hist)
    if avg_vol <= 0:
        return False
    ratio = quote.volume / avg_vol
    return lower <= ratio <= upper


def _is_volume_amplifying(quote: Quote, klines: list[KlineData], threshold: float = 1.2) -> bool:
    """成交量放大：当日量比 ≥ threshold"""
    if not klines or quote.volume is None or quote.volume <= 0:
        return False
    hist = [k.volume for k in klines[:-1] if k.volume and k.volume > 0][-10:]
    if len(hist) < 3:
        return False
    avg_vol = sum(hist) / len(hist)
    if avg_vol <= 0:
        return False
    ratio = quote.volume / avg_vol
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
