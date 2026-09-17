"""
做T监控线程模块

负责监控持仓标的的支撑压力位，识别做T买入/卖出信号。
使用 5 分钟 K 线计算技术指标，确保日内信号敏感度。
"""
import threading
import time
import logging
import os
from datetime import datetime
from typing import List, Optional, Dict

from .data_pool import SharedDataPool, KLine
from .models import Quote, WatchItem, KlineData
from .technical import calc_support_resistance, TechnicalSummary, get_technical_summary, detect_stage
from .technical import fetch_historical_kline
from .http_client import serverchan_client

API_BASE = "https://sctapi.ftqq.com"


log = logging.getLogger(__name__)


class T0Signal:
    """做T信号"""
    SIGNAL_BUY = 'buy'
    SIGNAL_SELL = 'sell'
    SIGNAL_NONE = 'none'

    def __init__(self, code: str, name: str, signal_type: str, reason: str,
                 price: float, support: float, resistance: float,
                 risk_reward: float = 0.0,
                 buy_price: float = 0.0, sell_price: float = 0.0):
        self.code = code
        self.name = name
        self.signal_type = signal_type
        self.reason = reason
        self.price = price
        self.support = support
        self.resistance = resistance
        self.risk_reward = risk_reward  # 盈亏比
        self.buy_price = buy_price      # 建议买入挂单价
        self.sell_price = sell_price    # 建议卖出挂单价
        self.timestamp = time.time()

    @property
    def rr_quality(self) -> str:
        """盈亏比质量评级"""
        if self.risk_reward >= 2.5:
            return "优秀"
        elif self.risk_reward >= 1.8:
            return "良好"
        elif self.risk_reward >= 1.2:
            return "一般"
        else:
            return "偏低"

    @property
    def is_weak(self) -> bool:
        """是否为弱信号（盈亏比不足）"""
        return self.risk_reward < 1.2 and self.risk_reward > 0

    def is_valid(self, max_age: float = 300) -> bool:
        """判断信号是否有效（默认5分钟内）"""
        return time.time() - self.timestamp < max_age

    def __repr__(self):
        return (f"T0Signal(code={self.code}, type={self.signal_type}, "
                f"reason={self.reason}, RR={self.risk_reward:.1f})")


class PositionSignal:
    """加减仓信号（基于日K周期阶段 detect_stage）"""
    ACTION_ADD = 'add'      # 加仓
    ACTION_REDUCE = 'reduce'  # 减仓

    def __init__(self, code: str, name: str, action: str, stage: str,
                 confidence: int, reasons: list[str], price: float,
                 suggested_price: float = 0.0):
        self.code = code
        self.name = name
        self.action = action
        self.stage = stage
        self.confidence = confidence
        self.reasons = reasons or []
        self.price = price
        self.suggested_price = suggested_price  # 建议挂单价（加仓=买入、减仓=卖出）
        self.timestamp = time.time()

    def is_valid(self, max_age: float = 900) -> bool:
        """判断信号是否有效（默认15分钟内）"""
        return time.time() - self.timestamp < max_age

    @property
    def confidence_label(self) -> str:
        """置信度标签"""
        return "高" if self.confidence >= 65 else ("中" if self.confidence >= 45 else "低")

    @property
    def action_label(self) -> str:
        """醒目标签：右侧加仓 / 左侧埋伏 / 左侧接刀 / 减仓（按阶段区分风险）"""
        if self.action == self.ACTION_REDUCE:
            return "🔴 减仓"
        if self.stage == "启动期":
            return "🟢 加仓(右侧)"
        if self.stage == "磨底期":
            return "🟡 加仓(左侧埋伏)"
        return "🟠 加仓(左侧接刀·高风险)"

    def __repr__(self):
        return f"PositionSignal(code={self.code}, action={self.action}, stage={self.stage})"


# 周期阶段 → 加减仓动作映射（主升浪/震荡市不发，其余发）
_STAGE_TO_ACTION = {
    "启动期": PositionSignal.ACTION_ADD,     # 放量突破启动，右侧加仓
    "磨底期": PositionSignal.ACTION_ADD,     # 缩量+超卖+抛压衰竭，左侧分批埋伏
    "下跌期": PositionSignal.ACTION_ADD,     # 仍在下跌，左侧接刀（高风险，轻仓试探）
    "赶顶期": PositionSignal.ACTION_REDUCE,  # 超买+乖离过大/放天量，止盈减仓
    "派发期": PositionSignal.ACTION_REDUCE,  # 跌破 MA20 + 高位回落，离场减仓
}


def _compute_suggested_prices(sr, price: float, quote: Quote) -> dict:
    """计算做T的买入/卖出挂单建议价格

    买入挂单价: 取下面最近的支撑/筹码峰/日内低点，上方留ATR/4缓冲（避免挂太低不成交）
    卖出挂单价: 取上面最近的压力/筹码峰/日内高点，下方留ATR/4缓冲（避免挂太高不成交）

    Returns:
        {"buy_price": float, "sell_price": float}
    """
    support = sr.support or price
    resistance = sr.resistance or price
    clusters = sr.volume_clusters or []
    atr = sr.atr or price * 0.005

    # ---- 买入挂单价（不高于现价，VWAP 作为上限参考） ----
    buy_refs = [support] + [c for c in clusters if c < price]
    if quote.low:
        buy_refs.append(quote.low)
    nearest_below = max(c for c in buy_refs if c < price) if any(c < price for c in buy_refs) else min(support, price)
    buy_p = nearest_below + atr / 4
    buy_p = min(buy_p, price)  # 不高于现价
    # 买入价若高于均价，说明偏贵，下调到均价附近（但不能低于原支撑位）
    if quote.avg_price and quote.avg_price > 0 and buy_p > quote.avg_price:
        buy_p = round(max(buy_p * 0.5 + quote.avg_price * 0.5, nearest_below), 3)

    # ---- 卖出挂单价（不低于现价，VWAP 作为下限参考） ----
    sell_refs = [resistance] + [c for c in clusters if c > price]
    if quote.high:
        sell_refs.append(quote.high)
    nearest_above = min(c for c in sell_refs if c > price) if any(c > price for c in sell_refs) else max(resistance, price)
    sell_p = nearest_above - atr / 4
    sell_p = max(sell_p, price)  # 不低于现价
    # 卖出价若低于均价，说明偏便宜，上调到均价附近（但不能高于原压力位）
    if quote.avg_price and quote.avg_price > 0 and sell_p < quote.avg_price:
        sell_p = round(min(sell_p * 0.5 + quote.avg_price * 0.5, nearest_above), 3)

    return {
        "buy_price": round(buy_p, 3),
        "sell_price": round(sell_p, 3),
    }


class T0MonitorThread(threading.Thread):
    """
    做T监控线程

    定时扫描持仓标的，计算支撑压力位（基于5分钟K线），识别做T买入/卖出信号。
    """

    # 5分钟K线缓存刷新间隔（秒）
    KLINE_CACHE_TTL = 300
    # 日K缓存刷新间隔（秒）——加减仓用，日K慢变、无需频繁刷新
    DAILY_CACHE_TTL = 300

    def __init__(self,
                 watch_items: List[WatchItem],
                 data_pool: SharedDataPool,
                 interval: int = 30,
                 enable_sound: bool = True,
                 enable_push: bool = False,
                 sessions: Optional[dict] = None,
                 t0_enabled: bool = True,
                 position_enabled: bool = False,
                 position_push: bool = False,
                 position_interval: int = 300):
        """
        初始化做T监控线程

        Args:
            watch_items: 监控标的列表
            data_pool: 共享数据池（用于行情价格）
            interval: 扫描间隔（秒），默认30秒
            enable_sound: 是否启用声音提示
            enable_push: 是否启用微信推送（做T信号）
            sessions: 交易时段配置（用于收盘后停止扫描）
            t0_enabled: 是否启用做T信号扫描（False 时仅做加减仓扫描）
            position_enabled: 是否启用加减仓监控（持仓 + 日K周期阶段）
            position_push: 是否启用加减仓信号微信推送
            position_interval: 加减仓扫描节流间隔（秒），默认300秒
        """
        super().__init__(daemon=True, name="T0Monitor")
        self._watch_items = watch_items
        self._data_pool = data_pool
        self._interval = interval
        self._enable_sound = enable_sound
        self._enable_push = enable_push
        self._t0_enabled = t0_enabled
        self._sessions = sessions
        self._running = False
        self._last_signals: Dict[str, T0Signal] = {}
        self._last_signal_time: Dict[str, float] = {}   # 信号冷却计时
        # 5分钟K线缓存：{code: (klines, fetch_time)}
        self._klines_cache: Dict[str, tuple[List[KlineData], float]] = {}
        # 加减仓：日K缓存 + 信号缓存 + 节流计时
        self._position_enabled = position_enabled
        self._position_push = position_push
        self._position_interval = max(30, int(position_interval))
        self._daily_cache: Dict[str, tuple[List[KlineData], float]] = {}
        self._last_position_signals: Dict[str, PositionSignal] = {}
        self._last_position_scan_ts = 0.0
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        """返回线程是否正在运行"""
        return self._running

    def start(self):
        """启动线程"""
        log.info(f"做T监控线程启动（5min K线），扫描间隔: {self._interval}秒")
        self._running = True
        super().start()

    def stop(self):
        """停止线程"""
        log.info("做T监控线程停止中...")
        self._running = False
        self._stop_event.set()
        # 最多等一个扫描间隔+缓冲，避免残留扫描/推送
        self.join(timeout=self._interval + 5)

    def run(self):
        """线程主循环"""
        while self._running:
            try:
                self._scan()
            except Exception as e:
                log.error(f"做T监控扫描失败: {e}")

            # 用 Event.wait 替代 sleep：stop() 可立即打断，不残留一轮
            self._stop_event.wait(self._interval)

        log.info("做T监控线程已停止")

    def _get_t0_klines(self, item: WatchItem) -> Optional[List[KlineData]]:
        """获取做T用的5分钟K线（带缓存）

        每次刷新时带 0.3s 间隔避免触发频率限制。
        """
        now = time.time()
        cached = self._klines_cache.get(item.code)
        if cached and (now - cached[1]) < self.KLINE_CACHE_TTL:
            return cached[0]

        try:
            # 5分钟K线，取 80 根覆盖约 1.7 个交易日
            time.sleep(0.3)  # 降低请求频率，避免 Sina 456 限频
            klines = fetch_historical_kline(item.code, item.market, days=2, scale=5)
            if klines:
                self._klines_cache[item.code] = (klines, now)
                log.debug(f"T0 K线刷新: {item.code} ({len(klines)}根5min)")
                return klines
        except Exception as e:
            log.warning(f"T0 K线获取失败 {item.code}: {e}")

        # 返回旧缓存（即使过期也比没有好）
        if cached:
            return cached[0]
        return None

    def _scan(self):
        """扫描所有标的，识别做T信号，一轮只发一次通知"""
        # 交易时段检查：收盘后停止扫描，避免用停更数据重复发信号
        if self._sessions:
            from app.helpers import is_trading_time
            from datetime import datetime as _dt
            in_trading, reason = is_trading_time(_dt.now(), self._sessions)
            if not in_trading:
                log.debug(f"T0 非交易时段({reason})，跳过扫描")
                return

        if not self._data_pool.is_fresh(max_age=120):
            log.debug("数据池数据过期，跳过扫描")
            return

        if self._t0_enabled:
            log.debug("开始做T信号扫描...")

            signals: list[T0Signal] = []

            for item in self._watch_items:
                quote = self._data_pool.get_quote(item.code)
                if quote is None:
                    continue

                # 使用5分钟K线（非共享池中的60分钟K线）
                # 每个标的首次获取带0.3s间隔
                t0_klines = self._get_t0_klines(item)
                if t0_klines is None:
                    continue

                signal = self._evaluate_signal(item, quote, t0_klines)

                if signal and signal.signal_type != T0Signal.SIGNAL_NONE:
                    signals.append(signal)

            if signals:
                self._handle_signals(signals)

        # 加减仓信号（持仓 + 日K周期阶段，节流扫描）
        if self._position_enabled:
            pos_signals = self._scan_position_signals()
            if pos_signals:
                self._handle_position_signals(pos_signals)

    def _evaluate_signal(self, item: WatchItem, quote: Quote,
                         t0_klines: List[KlineData]) -> Optional[T0Signal]:
        """评估单个标的的做T信号（基于5分钟K线）"""
        # 获取当前价格
        price = quote.price or quote.pre_close or 0
        if price <= 0:
            return None

        # 计算支撑压力位（传现价，取下方最近支撑/上方最近压力，避免最宽带失真）
        sr = calc_support_resistance(t0_klines, lookback=40, price=price)

        if sr.support is None or sr.resistance is None:
            return None

        # 计算技术指标（基于5分钟K线）
        tech = get_technical_summary(quote, t0_klines)

        # 计算盈亏比：买入侧 reward=上方空间/risk=下方空间，卖出侧反过来。
        upside = sr.resistance - price   # 到压力的空间
        downside = price - sr.support    # 到支撑的空间
        if downside > 0:
            risk_reward = round(upside / downside, 2)   # 买入盈亏比
        else:
            risk_reward = 0.0  # 价格已跌破支撑
        if upside > 0:
            sell_risk_reward = round(downside / upside, 2)  # 卖出盈亏比
        else:
            sell_risk_reward = 0.0  # 价格已突破压力

        # 区间宽度至少需要覆盖交易成本 + 有利润空间
        range_width = sr.resistance - sr.support
        min_width = price * 0.008  # 至少 0.8%（覆盖双向成本 ~0.15% + 利润空间）
        if range_width < min_width:
            return None

        # 日内振幅检查：太小无利润空间，跳过
        intrabar = self._calc_intraday_bias(quote)
        if intrabar is None:
            return None
        if intrabar["amplitude_low"]:
            return None

        # 窄幅震荡过滤（回测：此状态下胜率45.6%，均收益-0.32%）
        from app.technical import detect_market_regime
        regime = detect_market_regime(tech, price, sr.atr)
        if regime.regime == "窄幅震荡":
            return None

        # 计算建议挂单价格
        suggested = _compute_suggested_prices(sr, price, quote)

        # 判断买入信号
        buy_reasons = self._check_buy_conditions(quote, tech, sr, price, risk_reward, intrabar)

        # 判断卖出信号
        sell_reasons = self._check_sell_conditions(quote, tech, sr, price, sell_risk_reward, intrabar)

        # 判断信号：选条件数更多的一方，附带概率和空间评估
        buy_count = len(buy_reasons)
        sell_count = len(sell_reasons)
        BUY_TOTAL = 13   # 买入总条件数
        SELL_TOTAL = 12  # 卖出总条件数

        # RR 门槛：根据支撑/压力强度动态调整
        # 强支撑（3重共振）→ 0.15, 中支撑（2重）→ 0.2, 弱支撑（单一）→ 0.3
        sup_info2 = self._find_nearby_strong_levels(price, sr, sr.atr or price * 0.02, is_buy=True)
        res_info2 = self._find_nearby_strong_levels(price, sr, sr.atr or price * 0.02, is_buy=False)
        sup_confluence = len([lv for lv, cnt, d in sup_info2.get("levels", []) if cnt >= 2])
        res_confluence = len([lv for lv, cnt, d in res_info2.get("levels", []) if cnt >= 2])
        MIN_RR_BUY = 0.15 if sup_confluence >= 2 else (0.2 if sup_confluence >= 1 else 0.3)
        MIN_RR_SELL = 0.15 if res_confluence >= 2 else (0.2 if res_confluence >= 1 else 0.3)

        # 市场状态自适应：趋势市调整 RR 阈值
        if regime.regime == "趋势上涨":
            MIN_RR_BUY = max(0.10, MIN_RR_BUY - 0.05)  # 顺势买入门槛更低
            MIN_RR_SELL = min(0.40, MIN_RR_SELL + 0.10)  # 逆势卖出门槛更高
        elif regime.regime == "趋势下跌":
            MIN_RR_BUY = min(0.40, MIN_RR_BUY + 0.10)
            MIN_RR_SELL = max(0.10, MIN_RR_SELL - 0.05)

        # 信号冷却：同一标的至少间隔 300 秒才允许翻转方向
        COOLDOWN_SEC = 300
        now_ts = time.time()

        # 计算概率和空间（真实条件数略高于 BUY_TOTAL/SELL_TOTAL，clamp 到 100 防超）
        buy_prob = min(100, round(buy_count / BUY_TOTAL * 100)) if buy_count > 0 else 0
        sell_prob = min(100, round(sell_count / SELL_TOTAL * 100)) if sell_count > 0 else 0
        upside_pct = round((sr.resistance - price) / price * 100, 1) if price > 0 else 0
        downside_pct = round((price - sr.support) / price * 100, 1) if price > 0 else 0

        def _build_signal(reasons_list, sig_type, prob, space_pct, space_label, rr):
            reason = "; ".join(reasons_list)
            # 概率评估
            prob_label = "高" if prob >= 50 else ("中" if prob >= 30 else "低")
            prob_note = f"[概率{prob_label}({prob}%满足条件)]"
            # 空间评估
            if space_pct >= 3:
                space_note = f"[{space_label}空间大({space_pct}%)]"
            elif space_pct >= 1.5:
                space_note = f"[{space_label}空间中({space_pct}%)]"
            else:
                space_note = f"[{space_label}空间小({space_pct}%)]"
            # RR 过大警告：目标位太远，日内触及概率低
            rr_note = ""
            MAX_RR = 4.0
            if rr > MAX_RR:
                rr_note = f"[⚠️RR={rr:.1f}x目标偏远，日内触及概率低] "
            return f"{rr_note}{prob_note}{space_note} {reason}"

        # 冷却检查：同一标的短时间内不得翻转信号方向
        last_sig = self._last_signals.get(item.code)
        last_ts = self._last_signal_time.get(item.code, 0)
        in_cooldown = last_sig is not None and (now_ts - last_ts) < COOLDOWN_SEC

        if buy_count > sell_count and buy_reasons:
            # RR 过滤（动态门槛）
            if risk_reward < MIN_RR_BUY:
                log.debug(f"{item.name}: 买入条件{buy_count}个但RR={risk_reward}<{MIN_RR_BUY}(支撑{sup_confluence}重)，跳过")
                return None
            # 冷却过滤
            if in_cooldown and last_sig.signal_type == T0Signal.SIGNAL_SELL:
                log.info(f"{item.name}: 买入信号冷却中(上次为卖出，{now_ts-last_ts:.0f}秒前)")
                return None
            # 冲突标注
            conflict_note = ""
            if sell_count >= buy_count - 1 and sell_count >= 4:
                conflict_note = "⚠️多空分歧(买卖条件接近) "
            reason = conflict_note + _build_signal(buy_reasons, T0Signal.SIGNAL_BUY, buy_prob, upside_pct, "上涨", risk_reward)
            sig = T0Signal(code=item.code, name=item.name, signal_type=T0Signal.SIGNAL_BUY,
                           reason=reason, price=price, support=sr.support, resistance=sr.resistance,
                           risk_reward=risk_reward, buy_price=suggested["buy_price"], sell_price=suggested["sell_price"])
            self._last_signal_time[item.code] = now_ts
            return sig

        if sell_count > buy_count and sell_reasons:
            if sell_risk_reward < MIN_RR_SELL:
                log.debug(f"{item.name}: 卖出条件{sell_count}个但RR={sell_risk_reward}<{MIN_RR_SELL}(压力{res_confluence}重)，跳过")
                return None
            if in_cooldown and last_sig.signal_type == T0Signal.SIGNAL_BUY:
                log.info(f"{item.name}: 卖出信号冷却中(上次为买入，{now_ts-last_ts:.0f}秒前)")
                return None
            conflict_note = ""
            if buy_count >= sell_count - 1 and buy_count >= 4:
                conflict_note = "⚠️多空分歧(买卖条件接近) "
            reason = conflict_note + _build_signal(sell_reasons, T0Signal.SIGNAL_SELL, sell_prob, downside_pct, "下跌", sell_risk_reward)
            sig = T0Signal(code=item.code, name=item.name, signal_type=T0Signal.SIGNAL_SELL,
                           reason=reason, price=price, support=sr.support, resistance=sr.resistance,
                           risk_reward=sell_risk_reward, buy_price=suggested["buy_price"], sell_price=suggested["sell_price"])
            self._last_signal_time[item.code] = now_ts
            return sig

        # 条件数相等时，优先选有背离信号的（但也受RR限制）
        if buy_reasons and sell_reasons:
            buy_has_divergence = any("背离" in r for r in buy_reasons)
            sell_has_divergence = any("背离" in r for r in sell_reasons)
            if buy_has_divergence and not sell_has_divergence and risk_reward >= MIN_RR_BUY and not (in_cooldown and last_sig.signal_type == T0Signal.SIGNAL_SELL):
                reason = "⚠️多空分歧(背离偏多) " + _build_signal(buy_reasons, T0Signal.SIGNAL_BUY, buy_prob, upside_pct, "上涨", risk_reward)
                sig = T0Signal(code=item.code, name=item.name, signal_type=T0Signal.SIGNAL_BUY,
                               reason=reason, price=price, support=sr.support, resistance=sr.resistance,
                               risk_reward=risk_reward, buy_price=suggested["buy_price"], sell_price=suggested["sell_price"])
                self._last_signal_time[item.code] = now_ts
                return sig
            if sell_has_divergence and not buy_has_divergence and sell_risk_reward >= MIN_RR_SELL and not (in_cooldown and last_sig.signal_type == T0Signal.SIGNAL_BUY):
                reason = "⚠️多空分歧(背离偏空) " + _build_signal(sell_reasons, T0Signal.SIGNAL_SELL, sell_prob, downside_pct, "下跌", sell_risk_reward)
                sig = T0Signal(code=item.code, name=item.name, signal_type=T0Signal.SIGNAL_SELL,
                               reason=reason, price=price, support=sr.support, resistance=sr.resistance,
                               risk_reward=sell_risk_reward, buy_price=suggested["buy_price"], sell_price=suggested["sell_price"])
                self._last_signal_time[item.code] = now_ts
                return sig

        # 仅一侧有信号（也需要 RR 过滤）
        if buy_reasons and risk_reward >= MIN_RR_BUY and not (in_cooldown and last_sig.signal_type == T0Signal.SIGNAL_SELL):
            reason = _build_signal(buy_reasons, T0Signal.SIGNAL_BUY, buy_prob, upside_pct, "上涨", risk_reward)
            sig = T0Signal(code=item.code, name=item.name, signal_type=T0Signal.SIGNAL_BUY,
                           reason=reason, price=price, support=sr.support, resistance=sr.resistance,
                           risk_reward=risk_reward, buy_price=suggested["buy_price"], sell_price=suggested["sell_price"])
            self._last_signal_time[item.code] = now_ts
            return sig

        if sell_reasons and sell_risk_reward >= MIN_RR_SELL and not (in_cooldown and last_sig.signal_type == T0Signal.SIGNAL_BUY):
            reason = _build_signal(sell_reasons, T0Signal.SIGNAL_SELL, sell_prob, downside_pct, "下跌", sell_risk_reward)
            sig = T0Signal(code=item.code, name=item.name, signal_type=T0Signal.SIGNAL_SELL,
                           reason=reason, price=price, support=sr.support, resistance=sr.resistance,
                           risk_reward=sell_risk_reward, buy_price=suggested["buy_price"], sell_price=suggested["sell_price"])
            self._last_signal_time[item.code] = now_ts
            return sig

        return None

    # ---------------------------------------------------------------- 加减仓信号

    def _get_daily_klines(self, item: WatchItem) -> Optional[List[KlineData]]:
        """获取加减仓用的日K线（本地 duckdb + 远端补缺口，带缓存）"""
        now = time.time()
        cached = self._daily_cache.get(item.code)
        if cached and (now - cached[1]) < self.DAILY_CACHE_TTL:
            return cached[0]

        try:
            from .kline_local import fetch_daily_hybrid
            klines = fetch_daily_hybrid(item.code, item.market, days=60)
            if klines:
                self._daily_cache[item.code] = (klines, now)
                return klines
        except Exception as e:
            log.warning(f"加减仓日K获取失败 {item.code}: {e}")

        if cached:
            return cached[0]
        return None

    def _evaluate_position_signal(self, item: WatchItem, quote: Quote) -> Optional[PositionSignal]:
        """评估单个持仓的加减仓信号（基于日K周期阶段 detect_stage）"""
        price = quote.price or quote.pre_close or 0
        if price <= 0:
            return None

        klines = self._get_daily_klines(item)
        if not klines or len(klines) < 20:
            return None

        try:
            stage_result = detect_stage(klines)
        except Exception as e:
            log.debug(f"加减仓阶段判定失败 {item.code}: {e}")
            return None

        action = _STAGE_TO_ACTION.get(stage_result.stage)
        if not action:
            return None

        reasons = list(stage_result.reasons or [])
        # 左侧加仓信号补一句操作口径：区分「埋伏」与「接刀」，避免与 detect_stage 原文冲突
        if action == PositionSignal.ACTION_ADD:
            if stage_result.stage == "磨底期":
                reasons.insert(0, "左侧埋伏：缩量超卖抛压衰竭，分批、破位止损")
            elif stage_result.stage == "下跌期":
                reasons.insert(0, "左侧接刀：仍在下跌，仅轻仓试探、破位止损")

        # 建议挂单价：加仓=支撑上方低吸、减仓=压力下方高抛（复用日K支撑压力位）
        suggested = self._suggested_position_price(klines, price, action)

        return PositionSignal(
            code=item.code, name=item.name, action=action,
            stage=stage_result.stage, confidence=stage_result.confidence,
            reasons=reasons, price=price, suggested_price=suggested,
        )

    @staticmethod
    def _suggested_position_price(klines: List[KlineData], price: float, action: str) -> float:
        """根据日K支撑压力位计算加减仓建议挂单价。

        加仓 → 加仓挂单价：下方最近支撑 + ATR/4 缓冲（不高于现价）
        减仓 → 减仓挂单价：上方最近压力 − ATR/4 缓冲（不低于现价）
        """
        try:
            sr = calc_support_resistance(klines, lookback=20, price=price)
        except Exception:
            sr = None

        atr = (sr.atr if sr and sr.atr else price * 0.01)
        if action == PositionSignal.ACTION_ADD:
            if sr and sr.support:
                suggested = sr.support + atr / 4
                return round(min(suggested, price), 3)
            return round(price * 0.99, 3)  # 无支撑，现价下方 1% 低吸
        # 减仓
        if sr and sr.resistance:
            suggested = sr.resistance - atr / 4
            return round(max(suggested, price), 3)
        return round(price * 1.01, 3)  # 无压力，现价上方 1% 高抛

    def _scan_position_signals(self) -> list[PositionSignal]:
        """扫描持仓的加减仓信号（只对持仓，节流扫描）"""
        now = time.time()
        if now - self._last_position_scan_ts < self._position_interval:
            return []
        self._last_position_scan_ts = now

        signals: list[PositionSignal] = []
        for item in self._watch_items:
            # 加减仓只针对持仓（无持仓谈不上加/减）
            if getattr(item, "type", "") != "持仓":
                continue
            quote = self._data_pool.get_quote(item.code)
            if quote is None:
                continue
            sig = self._evaluate_position_signal(item, quote)
            if sig:
                signals.append(sig)
        return signals

    def _prune_position_signals(self, max_age: float = 1800):
        """清理过期的加减仓信号缓存"""
        now = time.time()
        stale = [c for c, s in self._last_position_signals.items() if now - s.timestamp > max_age]
        for c in stale:
            del self._last_position_signals[c]

    def _handle_position_signals(self, signals: list[PositionSignal]):
        """统一处理一轮扫描的加减仓信号，去重后发一次通知"""
        self._prune_position_signals(max_age=1800)

        new_signals: list[PositionSignal] = []
        for sig in signals:
            last = self._last_position_signals.get(sig.code)
            # 同一标的一段时间内不重复推送同一动作（加/减仓方向不变视为同一信号）
            if last and last.action == sig.action and last.is_valid(max_age=900):
                log.debug(f"跳过重复加减仓信号: {sig.code} {sig.action}")
                continue
            self._last_position_signals[sig.code] = sig
            new_signals.append(sig)

        if not new_signals:
            return

        self._print_position_signals(new_signals)
        if self._position_push:
            self._push_position_signals(new_signals)

    def _print_position_signals(self, signals: list[PositionSignal]):
        """打印加减仓信号（醒目排版）"""
        now_str = datetime.now().strftime("%H:%M:%S")
        adds = [s for s in signals if s.action == PositionSignal.ACTION_ADD]
        reduces = [s for s in signals if s.action == PositionSignal.ACTION_REDUCE]

        print(f"\n{'='*75}")
        print(f"  🚨 加减仓信号汇总（{now_str}）[日K周期阶段]")
        print(f"{'='*75}")
        for s in adds + reduces:
            reasons = "；".join(s.reasons)
            side = "加仓" if s.action == PositionSignal.ACTION_ADD else "减仓"
            sugg = f"  建议{side}挂单价 {s.suggested_price:.2f}" if s.suggested_price > 0 else ""
            print(f"  {s.action_label}  {s.name}({s.code})  现价 {s.price:.2f}  "
                  f"阶段[{s.stage}] 置信{s.confidence_label}({s.confidence}%){sugg}")
            if reasons:
                print(f"      └─ {reasons}")
        print(f"{'='*75}\n")
        log.info(f"加减仓信号汇总: {len(adds)}加仓, {len(reduces)}减仓")

    def _push_position_signals(self, signals: list[PositionSignal]):
        """推送加减仓信号到微信（醒目标题 + markdown 正文）"""
        sendkey = os.environ.get("SCT_SENDKEY")
        if not sendkey:
            log.debug("未配置 SCT_SENDKEY，跳过加减仓推送")
            return

        adds = [s for s in signals if s.action == PositionSignal.ACTION_ADD]
        reduces = [s for s in signals if s.action == PositionSignal.ACTION_REDUCE]
        now_str = datetime.now().strftime("%m-%d %H:%M")

        parts = []
        if adds:
            parts.append(f"🟢加仓{len(adds)}")
        if reduces:
            parts.append(f"🔴减仓{len(reduces)}")
        title = f"🚨加减仓信号 {' '.join(parts)} | {now_str}"

        lines = ["# 🚨 加减仓信号（日K周期阶段）\n", f"扫描时间: {now_str}\n"]
        for label, group in (("🟢 加仓", adds), ("🔴 减仓", reduces)):
            if not group:
                continue
            lines.append(f"## {label}\n")
            for s in group:
                reasons = "；".join(s.reasons)
                side = "加仓" if s.action == PositionSignal.ACTION_ADD else "减仓"
                sugg = f"｜建议{side}挂单价 **{s.suggested_price:.2f}**" if s.suggested_price > 0 else ""
                lines.append(f"- {s.action_label} **{s.name}({s.code})**  现价 {s.price:.2f}｜"
                             f"阶段 **{s.stage}**｜置信 {s.confidence_label}({s.confidence}%){sugg}")
                if reasons:
                    lines.append(f"  > {reasons}")
            lines.append("")

        content = "\n".join(lines)

        try:
            url = f"{API_BASE}/{sendkey}.send"
            resp = serverchan_client.post(url, data={"title": title, "desp": content})
            if resp and resp.status_code == 200 and resp.json().get("code") == 0:
                log.info(f"加减仓信号推送成功: {len(signals)}个信号")
            else:
                log.warning("加减仓信号推送失败")
        except Exception as e:
            log.error(f"加减仓信号推送异常: {e}")

    @staticmethod
    def _calc_buy_sell_ratio(bid_vol: Optional[float], ask_vol: Optional[float]) -> Optional[float]:
        """计算外盘/内盘比值（>1 表示主动买入占优）"""
        if bid_vol and ask_vol and ask_vol > 0:
            return round(bid_vol / ask_vol, 2)
        return None

    @staticmethod
    def _calc_intraday_bias(quote: Quote) -> Optional[dict]:
        """计算日内位置与振幅"""
        if not (quote.high and quote.low and quote.price and quote.pre_close):
            return None
        day_range = quote.high - quote.low
        if day_range <= 0:
            return None
        position = (quote.price - quote.low) / day_range * 100
        amplitude = day_range / quote.pre_close * 100
        is_etf = quote.type and "ETF" in quote.type
        min_amp = 0.8 if is_etf else 1.5
        return {
            "position": round(position, 1),
            "amplitude": round(amplitude, 2),
            "amplitude_low": amplitude < min_amp,
            "below_open": quote.open is not None and quote.price < quote.open,
            "above_open": quote.open is not None and quote.price > quote.open,
        }

    @staticmethod
    def _find_nearby_strong_levels(
        price: float, sr, atr: float, is_buy: bool
    ) -> dict:
        """查找附近较强的支撑/压力位（多级分析）

        收集 swing/pivot/cluster 三类来源的关键位，
        合并邻近的，统计每个级别的共振次数和距离。

        Returns:
            {"levels": [(价格, 共振数, 距离%), ...], "strongest": 价格, "confluence_note": 说明}
        """
        result: dict = {"levels": [], "strongest": None, "confluence_note": ""}
        if not price or price <= 0 or atr <= 0:
            return result

        zone = atr * 0.5

        if is_buy:
            # 找支撑位（< 现价）
            candidates = []
            for lv in (sr.swing_supports or []):
                if lv and lv < price * 0.995:
                    candidates.append((lv, "swing"))
            for lv in (sr.pivot_supports or []):
                if lv and lv < price * 0.995:
                    candidates.append((lv, "pivot"))
            for lv in (sr.volume_clusters or []):
                if lv and lv < price * 0.995:
                    candidates.append((lv, "cluster"))
        else:
            # 找压力位（> 现价）
            candidates = []
            for lv in (sr.swing_resistances or []):
                if lv and lv > price * 1.005:
                    candidates.append((lv, "swing"))
            for lv in (sr.pivot_resistances or []):
                if lv and lv > price * 1.005:
                    candidates.append((lv, "pivot"))
            for lv in (sr.volume_clusters or []):
                if lv and lv > price * 1.005:
                    candidates.append((lv, "cluster"))

        if not candidates:
            return result

        # 合并邻近价位
        candidates.sort(key=lambda x: x[0], reverse=is_buy)
        merged: list[tuple[float, int, float]] = []  # (价格, 共振数, 距离%)
        seen = set()
        for i, (lv1, _) in enumerate(candidates):
            if i in seen:
                continue
            count = 1
            for j in range(i + 1, len(candidates)):
                if j in seen:
                    continue
                if abs(candidates[j][0] - lv1) <= zone:
                    count += 1
                    seen.add(j)
            dist = abs(lv1 - price) / price * 100
            if dist <= 10:  # 只保留 10% 以内的
                merged.append((round(lv1, 3), count, round(dist, 1)))

        if merged:
            merged.sort(key=lambda x: (x[1], -x[2]), reverse=True)  # 共振数优先
            result["levels"] = merged[:5]
            result["strongest"] = merged[0][0]
            strongest_count = merged[0][1]
            if strongest_count >= 3:
                result["confluence_note"] = f"{strongest_count}重共振(强)"
            elif strongest_count >= 2:
                result["confluence_note"] = f"{strongest_count}重共振(中)"
            else:
                result["confluence_note"] = "单一来源"

        return result

    def _check_buy_conditions(self, quote: Quote, tech: TechnicalSummary,
                              sr, price: float, risk_reward: float,
                              intrabar: dict) -> List[str]:
        """检查买入条件（5分钟级别指标 + 内外盘/委比资金信号 + 日内振幅）"""
        reasons = []

        # 条件1：接近强支撑位（多级分析）
        atr_val = sr.atr or price * 0.02
        sup_info = self._find_nearby_strong_levels(price, sr, atr_val, is_buy=True)
        if sup_info["strongest"] is not None:
            strong = sup_info["strongest"]
            dist = (price - strong) / price * 100
            note = sup_info["confluence_note"]
            reasons.append(f"接近强支撑{strong:.3f}({note}，距{dist:.1f}%，下方买盘托底)")
            # 显示其他级别的支撑
            extra = [f"{lv:.3f}({cnt}重)" for lv, cnt, d in sup_info["levels"][1:3]]
            if extra:
                reasons.append(f"多级支撑: {', '.join(extra)}")
        elif sr.support and price <= sr.support * 1.01:
            dist = (price - sr.support) / sr.support * 100
            reasons.append(f"接近支撑位{sr.support:.3f}(仅距{dist:.1f}%)")

        # 条件2：RSI偏低（超卖区域，反弹概率高）
        if tech.rsi and tech.rsi < 35:
            reasons.append(f"RSI={tech.rsi:.0f}(超卖区域，短期超跌有反弹需求)")

        # 条件3：KDJ超卖（K值低位，做空动能衰竭）
        if tech.kdj_k and tech.kdj_k < 25:
            reasons.append(f"KDJ超卖(K={tech.kdj_k:.0f}，做空动能衰竭)")

        # 条件4：量能萎缩（缩量下跌=抛压减轻）/ 地量见底
        if quote.volume_ratio and quote.volume_ratio < 0.6:
            if quote.volume_ratio <= 0.25:
                reasons.append(f"地量(量比{quote.volume_ratio:.2f}，底部信号强)")
            else:
                reasons.append(f"缩量(量比{quote.volume_ratio:.2f}，抛压减轻)")

        # 条件5：MA20多头趋势（顺势做多胜率高）
        if tech.ma_alignment in ("多头排列", "多头回调"):
            reasons.append(f"均线{tech.ma_alignment}(顺势做多，胜率偏高)")

        # 条件6：委比多头（买单挂单占优，市场看多意愿强）
        if quote.bid_ask_ratio and quote.bid_ask_ratio > 25:
            reasons.append(f"委比+{quote.bid_ask_ratio:.0f}%(挂单看多意愿强)")

        # 条件7：外盘>内盘（主动买入量远超主动卖出量）
        bs_ratio = self._calc_buy_sell_ratio(quote.bid_volume, quote.ask_volume)
        if bs_ratio and bs_ratio > 1.4:
            reasons.append(f"外盘/内盘={bs_ratio}(主动买入是卖出的{bs_ratio}倍，买方积极)")

        # 条件8：MA20乖离（现价低于MA20，均值回归做多）
        if tech.ma20 and price < tech.ma20:
            deviation = (price - tech.ma20) / tech.ma20 * 100
            if deviation <= -1.5:
                reasons.append(f"MA20乖离{deviation:.1f}%(远离均线，均值回归动力强)")

        # 条件9：MA20趋势向上（5分钟级别，MA20≈100分钟均线 > MA60≈5小时均线）
        if tech.ma20 and tech.ma60 and tech.ma20 > tech.ma60:
            reasons.append("MA20>MA60(日内均线偏多，回调做多胜率高)")

        # 条件10：筹码峰支撑（现价接近下方密集成交区 + 多级支撑参考）
        clusters = sr.volume_clusters or []
        nearby_below = [c for c in clusters if c < price and price <= c * 1.02]
        if nearby_below:
            if sup_info["strongest"]:
                cluster_near_sup = any(
                    abs(c - sup_info["strongest"]) <= atr_val * 0.5 for c in nearby_below
                )
                if cluster_near_sup:
                    reasons.append(f"筹码峰+多级支撑共振({max(nearby_below):.3f}，买盘集中)")
                else:
                    reasons.append(f"筹码峰支撑{max(nearby_below):.3f}(密集成交区支撑)")
            else:
                reasons.append(f"筹码峰支撑{max(nearby_below):.3f}(密集成交区支撑)")

        # 条件11：日内低位（现价接近今日最低点，盈亏比好）
        if intrabar["position"] <= 30:
            reasons.append(f"日内低位(位置{intrabar['position']:.0f}%，盈亏比优)")

        # 条件12：低于开盘（日内偏弱接近支撑，可能反弹）
        if intrabar.get("below_open") and intrabar["position"] <= 50:
            reasons.append("低于开盘(弱转强)")

        # 条件13：振幅充裕（波动空间大，做T利润高）
        if intrabar["amplitude"] >= 2.5:
            reasons.append(f"高振幅({intrabar['amplitude']:.1f}%)")

        # 条件14：资金流向方向（优先使用明细数据）
        ff = quote.fund_flow
        if ff and ff.is_valid:
            if ff.is_institution_driven and ff.super_large_net is not None and ff.super_large_net > 0:
                reasons.append(f"机构吸筹(超大单+{ff.super_large_net/1e4:.0f}万)")
            elif ff.main_net is not None and ff.main_net > 0 and quote.amount and quote.amount > 0:
                if ff.main_net / quote.amount >= 0.05:
                    reasons.append(f"主力流入({ff.main_net/1e4:.0f}万)")
        elif quote.main_net_inflow is not None and quote.amount and quote.amount > 0:
            if quote.main_net_inflow > 0 and quote.main_net_inflow / quote.amount >= 0.05:
                reasons.append(f"主力流入({quote.main_net_inflow/1e4:.0f}万)")

        # 条件15：向下跳空回补（日内均值回归机会）
        if tech.has_gap and tech.gap_type == "向下跳空" and not tech.gap_filled_pct >= 100:
            reasons.append(f"跳空回补({tech.gap_detail})")

        # ---- 背离加分：接近支撑 + 外盘>内盘 = 隐藏吸筹 ----
        has_near_support = (sup_info["strongest"] is not None) or (sr.support and price <= sr.support * 1.01)
        has_divergence = has_near_support and bs_ratio and bs_ratio > 1.2
        if has_divergence and len(reasons) < 2:
            # 只要有"接近支撑"+外盘占优(>1.0)，即使其他条件不满足也触发
            reasons.append("⭐内盘背离(主动买)")
            return reasons  # 背离信号直接触发

        # 条件14/15：MA60乖离极值（中期顶/底）
        if tech.ma60 and price and tech.ma60 > 0:
            dev = (price - tech.ma60) / tech.ma60 * 100
            if dev <= -15:  # 买入侧：中期底部
                reasons.append(f"MA60乖离{dev:.0f}%(中期底部)")
            elif dev >= 20:  # 卖出侧：中期顶部
                reasons.append(f"MA60乖离+{dev:.0f}%(中期顶部)")
        # 条件15/16：BB挤压变盘
        if tech.bb_width and tech.bb_width < 5.0:
            if "下轨" in tech.bb_signal:
                reasons.append(f"BB挤压(带宽{tech.bb_width:.1f}%,下轨)")
            elif "上轨" in tech.bb_signal:
                reasons.append(f"BB挤压(带宽{tech.bb_width:.1f}%,上轨)")

        # RR过滤（RR越高越需更多信号，避免高RR假象）
        if risk_reward >= 2.0:
            min_conditions = 3
        elif risk_reward >= 1.5:
            min_conditions = 4
        elif risk_reward >= 1.2:
            min_conditions = 5
        else:
            min_conditions = 5  # RR差需强证据
        return reasons if len(reasons) >= min_conditions else []

    def _check_sell_conditions(self, quote: Quote, tech: TechnicalSummary,
                               sr, price: float, risk_reward: float,
                               intrabar: dict) -> List[str]:
        """检查卖出条件（5分钟级别指标 + 内外盘/委比资金信号 + 日内振幅）"""
        reasons = []

        # 条件1：接近强压力位（多级分析）
        atr_val2 = sr.atr or price * 0.02
        res_info = self._find_nearby_strong_levels(price, sr, atr_val2, is_buy=False)
        if res_info["strongest"] is not None:
            strong = res_info["strongest"]
            dist = (strong - price) / price * 100
            note = res_info["confluence_note"]
            reasons.append(f"接近强压力{strong:.3f}({note}，距{dist:.1f}%，上方阻力大)")
            extra = [f"{lv:.3f}({cnt}重)" for lv, cnt, d in res_info["levels"][1:3]]
            if extra:
                reasons.append(f"多级压力: {', '.join(extra)}")
        elif sr.resistance and price >= sr.resistance * 0.97:
            dist = (sr.resistance - price) / price * 100
            reasons.append(f"接近压力位{sr.resistance:.3f}(仅距{dist:.1f}%)")

        # 条件2：RSI偏高（超买区域，回调风险）
        if tech.rsi and tech.rsi > 65:
            reasons.append(f"RSI={tech.rsi:.0f}(偏高区域，短期超买有回调压力)")

        # 条件3：KDJ超买（K值高位，做多动能衰竭）
        if tech.kdj_k and tech.kdj_k > 75:
            reasons.append(f"KDJ超买(K={tech.kdj_k:.0f}，做多动能衰竭)")

        # 条件4：量能放大（高位放量可能是出货）/ 天量见顶
        if quote.volume_ratio and quote.volume_ratio > 1.3:
            if quote.volume_ratio >= 3.0:
                reasons.append(f"天量(量比{quote.volume_ratio:.1f}，顶部信号强)")
            else:
                reasons.append(f"放量(量比{quote.volume_ratio:.2f}，高位放量需警惕出货)")

        # 条件5：MA20偏空（顺势做空或观望）
        if tech.ma_alignment in ("空头排列", "空头反弹", "缠绕"):
            reasons.append(f"均线{tech.ma_alignment}(趋势偏空，不宜做多)")

        # 条件6：委比空头（卖单挂单占优，市场看空意愿强）
        if quote.bid_ask_ratio and quote.bid_ask_ratio < -25:
            reasons.append(f"委比{quote.bid_ask_ratio:.0f}%(挂单看空意愿强)")

        # 条件7：内盘>外盘（主动卖出量远超主动买入量）
        bs_ratio = self._calc_buy_sell_ratio(quote.bid_volume, quote.ask_volume)
        if bs_ratio and bs_ratio < 1 / 1.4:  # 等价于 ask/bid > 1.4
            reasons.append(f"内盘/外盘={1/bs_ratio:.1f}(主动卖出是买入的{1/bs_ratio:.1f}倍，卖方主导)")

        # 条件8：MA20乖离（现价高于MA20，均值回归压力）
        if tech.ma20 and price > tech.ma20:
            deviation = (price - tech.ma20) / tech.ma20 * 100
            if deviation >= 1.5:
                reasons.append(f"MA20乖离+{deviation:.1f}%(远离均线，均值回归压力大)")

        # 条件9：MA20趋势向下（5分钟级别，MA20≈100分钟均线 < MA60≈5小时均线）
        if tech.ma20 and tech.ma60 and tech.ma20 < tech.ma60:
            reasons.append("MA20<MA60(日内均线偏空)")

        # 条件10：筹码峰压力（现价接近上方密集成交区 + 多级压力参考）
        clusters = sr.volume_clusters or []
        nearby_above = [c for c in clusters if c > price and c * 0.98 <= price]
        if nearby_above:
            # 检查是否与多级压力共振
            if res_info["strongest"]:
                cluster_near_strongest = any(
                    abs(c - res_info["strongest"]) <= atr_val2 * 0.5 for c in nearby_above
                )
                if cluster_near_strongest:
                    reasons.append(f"筹码峰+多级压力共振({min(nearby_above):.3f}，抛压集中)")
                else:
                    reasons.append(f"筹码峰压力{min(nearby_above):.3f}(密集成交区套牢盘)")
            else:
                reasons.append(f"筹码峰压力{min(nearby_above):.3f}(密集成交区套牢盘)")

        # 条件11：日内高位（现价接近今日最高点）
        if intrabar["position"] >= 70:
            reasons.append(f"日内高位({intrabar['position']:.0f}%)")

        # 条件12：高于开盘（日内偏强接近压力，可能回落）
        if intrabar.get("above_open") and intrabar["position"] >= 50:
            reasons.append("高于开盘(强转弱)")

        # 条件13：振幅充裕（波动空间大，做T利润高）
        if intrabar["amplitude"] >= 2.0:
            reasons.append(f"高振幅({intrabar['amplitude']:.1f}%)")

        # 条件14：资金流向方向（优先使用明细数据）
        ff = quote.fund_flow
        if ff and ff.is_valid:
            if ff.is_distribution and ff.super_large_net is not None and ff.super_large_net < 0:
                reasons.append(f"机构出逃(超大单{ff.super_large_net/1e4:.0f}万)")
            elif ff.main_net is not None and ff.main_net < 0 and quote.amount and quote.amount > 0:
                if abs(ff.main_net) / quote.amount >= 0.05:
                    reasons.append(f"主力流出({abs(ff.main_net)/1e4:.0f}万)")
        elif quote.main_net_inflow is not None and quote.amount and quote.amount > 0:
            if quote.main_net_inflow < 0 and abs(quote.main_net_inflow) / quote.amount >= 0.05:
                reasons.append(f"主力流出({abs(quote.main_net_inflow)/1e4:.0f}万)")

        # 条件15：向上跳空回补（日内均值回归机会）
        if tech.has_gap and tech.gap_type == "向上跳空" and not tech.gap_filled_pct >= 100:
            reasons.append(f"跳空回补({tech.gap_detail})")

        # ---- 背离加分：接近压力 + 内盘>外盘 = 隐藏出货 ----
        has_near_resistance = (res_info["strongest"] is not None) or (sr.resistance and price >= sr.resistance * 0.97)
        has_divergence = has_near_resistance and bs_ratio and bs_ratio < 1.0
        if has_divergence and len(reasons) < 2:
            reasons.append("⭐外盘背离(主动卖)")
            return reasons  # 背离信号直接触发

        # 条件14/15：MA60乖离极值（中期顶/底）
        if tech.ma60 and price and tech.ma60 > 0:
            dev = (price - tech.ma60) / tech.ma60 * 100
            if dev <= -15:  # 买入侧：中期底部
                reasons.append(f"MA60乖离{dev:.0f}%(中期底部)")
            elif dev >= 20:  # 卖出侧：中期顶部
                reasons.append(f"MA60乖离+{dev:.0f}%(中期顶部)")
        # 条件15/16：BB挤压变盘
        if tech.bb_width and tech.bb_width < 5.0:
            if "下轨" in tech.bb_signal:
                reasons.append(f"BB挤压(带宽{tech.bb_width:.1f}%,下轨)")
            elif "上轨" in tech.bb_signal:
                reasons.append(f"BB挤压(带宽{tech.bb_width:.1f}%,上轨)")

        # RR过滤（RR越高越需更多信号，避免高RR假象）
        if risk_reward >= 2.0:
            min_conditions = 3
        elif risk_reward >= 1.5:
            min_conditions = 4
        elif risk_reward >= 1.2:
            min_conditions = 5
        else:
            min_conditions = 5  # RR差需强证据
        return reasons if len(reasons) >= min_conditions else []

    def _handle_signals(self, signals: list[T0Signal]):
        """统一处理一轮扫描的所有信号，去重后只发一次通知"""
        # 定期清理过期信号
        self._prune_stale_signals(max_age=600)

        new_signals: list[T0Signal] = []
        for signal in signals:
            last_signal = self._last_signals.get(signal.code)
            if (last_signal
                and last_signal.signal_type == signal.signal_type
                and last_signal.is_valid(max_age=120)
                and abs(signal.price - last_signal.price) / last_signal.price < 0.005):
                log.debug(f"跳过重复信号: {signal.code} {signal.signal_type} "
                          f"(上次@{last_signal.price:.3f}, 本次@{signal.price:.3f})")
                continue
            self._last_signals[signal.code] = signal
            new_signals.append(signal)

        if not new_signals:
            return

        self._print_signals(new_signals)

        if self._enable_push:
            self._push_signals(new_signals)

    def _prune_stale_signals(self, max_age: float = 600):
        """清理过期的信号缓存"""
        now = time.time()
        stale_codes = [
            code for code, sig in self._last_signals.items()
            if now - sig.timestamp > max_age
        ]
        for code in stale_codes:
            del self._last_signals[code]
        if stale_codes:
            log.debug(f"清理了 {len(stale_codes)} 个过期做T信号缓存")

    @staticmethod
    def _format_signal_table(signals: list[T0Signal], signal_type: str, label: str) -> str:
        """将信号列表格式化为表格字符串"""
        filtered = [s for s in signals if s.signal_type == signal_type]
        if not filtered:
            return ""

        name_w = max(len(f"{s.name}({s.code})") for s in filtered) + 2
        price_w = 8
        suggest_w = 8
        ref_w = 10
        rr_w = 8
        reason_w = 48

        sep = f"  {'─' * (name_w + price_w + suggest_w + ref_w + rr_w + reason_w + 11)}"

        rows = [sep, f"  {label}"]
        header = (f"  │ {'标的':<{name_w-2}} │ {'现价':>{price_w-2}} │ "
                  f"{'挂单':>{suggest_w-2}} │ {'支撑/压力':>{ref_w-4}} │ "
                  f"{'RR':>{rr_w-2}} │ 触发条件")
        rows.append(header)
        rows.append(sep)

        for s in filtered:
            ref_label = "支撑" if signal_type == T0Signal.SIGNAL_BUY else "压力"
            ref_val = s.support if signal_type == T0Signal.SIGNAL_BUY else s.resistance
            suggest_val = s.buy_price if signal_type == T0Signal.SIGNAL_BUY else s.sell_price
            name_col = f"{s.name}({s.code})"
            rr_str = f"{s.risk_reward:.1f}x" if s.risk_reward > 0 else "--"
            if s.risk_reward >= 2.0:
                rr_str = f"⭐{rr_str}"
            elif s.risk_reward < 1.2 and s.risk_reward > 0:
                rr_str = f"⚠️{rr_str}"
            suggest_str = f"{suggest_val:.3f}" if suggest_val > 0 else "--"
            rows.append(
                f"  │ {name_col:<{name_w-2}} │ {s.price:>{price_w-2}.3f} │ "
                f"{suggest_str:>{suggest_w-2}} │ "
                f"{ref_label}{ref_val:>{ref_w-4}.2f} │ "
                f"{rr_str:>{rr_w-2}} │ {s.reason}"
            )
        rows.append(sep)
        return "\n".join(rows)

    def _print_signals(self, signals: list[T0Signal]):
        """统一打印所有信号（表格格式）"""
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"\n{'='*75}")
        print(f"  做T信号汇总（{now_str}）[5min K线]")
        print(f"{'='*75}")

        table = self._format_signal_table(signals, T0Signal.SIGNAL_BUY, "🟢 买入信号")
        if table:
            print(f"\n{table}")

        table = self._format_signal_table(signals, T0Signal.SIGNAL_SELL, "🔴 卖出信号")
        if table:
            print(f"\n{table}")

        # 汇总统计
        buys = [s for s in signals if s.signal_type == T0Signal.SIGNAL_BUY]
        sells = [s for s in signals if s.signal_type == T0Signal.SIGNAL_SELL]
        weak = [s for s in signals if s.is_weak]
        summary_parts = [f"{len(buys)}个买入, {len(sells)}个卖出"]
        if weak:
            summary_parts.append(f"{len(weak)}个RR偏低(⚠️)")
        print(f"  {' | '.join(summary_parts)}")
        print(f"{'='*75}\n")

        log.info(f"做T信号汇总: {len(buys)}个买入(5min), {len(sells)}个卖出(5min)")

    def _push_signals(self, signals: list[T0Signal]):
        """统一推送做T信号到微信"""
        sendkey = os.environ.get("SCT_SENDKEY")
        if not sendkey:
            log.debug("未配置 SCT_SENDKEY，跳过推送")
            return

        buys = [s for s in signals if s.signal_type == T0Signal.SIGNAL_BUY]
        sells = [s for s in signals if s.signal_type == T0Signal.SIGNAL_SELL]
        now_str = datetime.now().strftime("%m-%d %H:%M")

        parts = []
        if buys:
            parts.append(f"🟢买入{len(buys)}")
        if sells:
            parts.append(f"🔴卖出{len(sells)}")
        title = f"做T信号 {' '.join(parts)} | {now_str}"

        lines = [f"# 做T信号汇总（5min K线）\n", f"扫描时间: {now_str}\n"]

        if buys:
            lines.append("## 🟢 买入信号\n")
            lines.append("| 标的 | 现价 | 挂单买入 | 支撑位 | 盈亏比 | 触发条件 |")
            lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
            for s in buys:
                rr_str = f"{s.risk_reward:.1f}x ({s.rr_quality})"
                buy_str = f"{s.buy_price:.3f}" if s.buy_price > 0 else "--"
                lines.append(
                    f"| {s.name}({s.code}) | {s.price:.3f} | {buy_str} | "
                    f"{s.support:.3f} | {rr_str} | {s.reason} |")
            lines.append("")

        if sells:
            lines.append("## 🔴 卖出信号\n")
            lines.append("| 标的 | 现价 | 挂单卖出 | 压力位 | 盈亏比 | 触发条件 |")
            lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
            for s in sells:
                rr_str = f"{s.risk_reward:.1f}x ({s.rr_quality})"
                sell_str = f"{s.sell_price:.3f}" if s.sell_price > 0 else "--"
                lines.append(
                    f"| {s.name}({s.code}) | {s.price:.3f} | {sell_str} | "
                    f"{s.resistance:.2f} | {rr_str} | {s.reason} |")
            lines.append("")

        weak_signals = [s for s in signals if s.is_weak]
        if weak_signals:
            lines.append(f"> ⚠️ {len(weak_signals)} 个信号盈亏比偏低(<1.2x)，请谨慎操作\n")

        content = "\n".join(lines)

        try:
            url = f"{API_BASE}/{sendkey}.send"
            resp = serverchan_client.post(url, data={"title": title, "desp": content})

            if resp and resp.status_code == 200 and resp.json().get("code") == 0:
                log.info(f"做T信号推送成功: {len(signals)}个信号")
            else:
                log.warning("做T信号推送失败")

        except Exception as e:
            log.error(f"做T信号推送异常: {e}")
