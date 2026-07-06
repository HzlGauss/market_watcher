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
from .technical import calc_support_resistance, TechnicalSummary, get_technical_summary
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
                 risk_reward: float = 0.0):
        self.code = code
        self.name = name
        self.signal_type = signal_type
        self.reason = reason
        self.price = price
        self.support = support
        self.resistance = resistance
        self.risk_reward = risk_reward  # 盈亏比
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


class T0MonitorThread(threading.Thread):
    """
    做T监控线程

    定时扫描持仓标的，计算支撑压力位（基于5分钟K线），识别做T买入/卖出信号。
    """

    # 5分钟K线缓存刷新间隔（秒）
    KLINE_CACHE_TTL = 300

    def __init__(self,
                 watch_items: List[WatchItem],
                 data_pool: SharedDataPool,
                 interval: int = 30,
                 enable_sound: bool = True,
                 enable_push: bool = False):
        """
        初始化做T监控线程

        Args:
            watch_items: 监控标的列表
            data_pool: 共享数据池（用于行情价格）
            interval: 扫描间隔（秒），默认30秒
            enable_sound: 是否启用声音提示
            enable_push: 是否启用微信推送
        """
        super().__init__(daemon=True, name="T0Monitor")
        self._watch_items = watch_items
        self._data_pool = data_pool
        self._interval = interval
        self._enable_sound = enable_sound
        self._enable_push = enable_push
        self._running = False
        self._last_signals: Dict[str, T0Signal] = {}
        # 5分钟K线缓存：{code: (klines, fetch_time)}
        self._klines_cache: Dict[str, tuple[List[KlineData], float]] = {}

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

    def run(self):
        """线程主循环"""
        while self._running:
            try:
                self._scan()
            except Exception as e:
                log.error(f"做T监控扫描失败: {e}")

            # 等待下一次扫描
            if self._running:
                time.sleep(self._interval)

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
        if not self._data_pool.is_fresh(max_age=120):
            log.debug("数据池数据过期，跳过扫描")
            return

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

    def _evaluate_signal(self, item: WatchItem, quote: Quote,
                         t0_klines: List[KlineData]) -> Optional[T0Signal]:
        """评估单个标的的做T信号（基于5分钟K线）"""
        # 计算支撑压力位
        sr = calc_support_resistance(t0_klines, lookback=40)

        if sr.support is None or sr.resistance is None:
            return None

        # 计算技术指标（基于5分钟K线）
        tech = get_technical_summary(quote, t0_klines)

        # 获取当前价格
        price = quote.price or quote.pre_close or 0

        if price <= 0:
            return None

        # 计算盈亏比
        upside = sr.resistance - price   # 到压力的空间
        downside = price - sr.support    # 到支撑的空间
        if downside > 0:
            risk_reward = round(upside / downside, 2)
        else:
            risk_reward = 0.0  # 价格已跌破支撑

        # 区间宽度至少需要覆盖交易成本 + 有利润空间
        range_width = sr.resistance - sr.support
        min_width = price * 0.008  # 至少 0.8%（覆盖双向成本 ~0.15% + 利润空间）
        if range_width < min_width:
            return None

        # 判断买入信号
        buy_reasons = self._check_buy_conditions(quote, tech, sr, price, risk_reward)

        # 判断卖出信号
        sell_reasons = self._check_sell_conditions(quote, tech, sr, price, risk_reward)

        if buy_reasons:
            reason = "; ".join(buy_reasons)
            return T0Signal(
                code=item.code, name=item.name,
                signal_type=T0Signal.SIGNAL_BUY,
                reason=reason, price=price,
                support=sr.support, resistance=sr.resistance,
                risk_reward=risk_reward,
            )

        if sell_reasons:
            reason = "; ".join(sell_reasons)
            return T0Signal(
                code=item.code, name=item.name,
                signal_type=T0Signal.SIGNAL_SELL,
                reason=reason, price=price,
                support=sr.support, resistance=sr.resistance,
                risk_reward=risk_reward,
            )

        return None

    @staticmethod
    def _calc_buy_sell_ratio(bid_vol: Optional[float], ask_vol: Optional[float]) -> Optional[float]:
        """计算外盘/内盘比值（>1 表示主动买入占优）"""
        if bid_vol and ask_vol and ask_vol > 0:
            return round(bid_vol / ask_vol, 2)
        return None

    def _check_buy_conditions(self, quote: Quote, tech: TechnicalSummary,
                              sr, price: float, risk_reward: float) -> List[str]:
        """检查买入条件（5分钟级别指标 + 内外盘/委比资金信号）"""
        reasons = []

        # 条件1：股价接近支撑位（1%以内）
        near_support = price <= sr.support * 1.01
        if near_support:
            reasons.append(f"接近支撑({sr.support:.2f})")

        # 条件2：RSI偏低
        if tech.rsi and tech.rsi < 35:
            reasons.append(f"RSI偏低({tech.rsi:.0f})")

        # 条件3：KDJ超卖
        if tech.kdj_k and tech.kdj_k < 25:
            reasons.append(f"KDJ超卖(K={tech.kdj_k:.0f})")

        # 条件4：量能萎缩
        if quote.volume_ratio and quote.volume_ratio < 0.6:
            reasons.append(f"缩量(比{quote.volume_ratio:.2f})")

        # 条件5：MA20多头方向
        if tech.ma_alignment in ("多头排列", "多头回调"):
            reasons.append(f"趋势({tech.ma_alignment})")

        # 条件6：委比多头（买单挂单占优）
        if quote.bid_ask_ratio and quote.bid_ask_ratio > 25:
            reasons.append(f"委比+{quote.bid_ask_ratio:.0f}%")

        # 条件7：外盘占优（主动买入 > 主动卖出）
        bs_ratio = self._calc_buy_sell_ratio(quote.bid_volume, quote.ask_volume)
        if bs_ratio and bs_ratio > 1.4:
            reasons.append(f"外盘优势(x{bs_ratio})")

        # ---- 背离加分：接近支撑 + 外盘>内盘 = 隐藏吸筹 ----
        has_divergence = near_support and bs_ratio and bs_ratio > 1.0
        if has_divergence and len(reasons) < 2:
            # 只要有"接近支撑"+外盘占优(>1.0)，即使其他条件不满足也触发
            reasons.append("⭐内盘背离(主动买)")
            return reasons  # 背离信号直接触发

        # RR过滤
        min_conditions = 3 if risk_reward < 1.2 and risk_reward > 0 else 2
        return reasons if len(reasons) >= min_conditions else []

    def _check_sell_conditions(self, quote: Quote, tech: TechnicalSummary,
                               sr, price: float, risk_reward: float) -> List[str]:
        """检查卖出条件（5分钟级别指标 + 内外盘/委比资金信号）"""
        reasons = []

        # 条件1：股价接近压力位（1%以内）
        near_resistance = price >= sr.resistance * 0.99
        if near_resistance:
            reasons.append(f"接近压力({sr.resistance:.2f})")

        # 条件2：RSI偏高
        if tech.rsi and tech.rsi > 65:
            reasons.append(f"RSI偏高({tech.rsi:.0f})")

        # 条件3：KDJ超买
        if tech.kdj_k and tech.kdj_k > 75:
            reasons.append(f"KDJ超买(K={tech.kdj_k:.0f})")

        # 条件4：量能放大
        if quote.volume_ratio and quote.volume_ratio > 1.3:
            reasons.append(f"放量(比{quote.volume_ratio:.2f})")

        # 条件5：MA20空头方向
        if tech.ma_alignment in ("空头排列", "空头反弹"):
            reasons.append(f"趋势({tech.ma_alignment})")

        # 条件6：委比空头（卖单挂单占优）
        if quote.bid_ask_ratio and quote.bid_ask_ratio < -25:
            reasons.append(f"委比{quote.bid_ask_ratio:.0f}%")

        # 条件7：内盘占优（主动卖出 > 主动买入）
        bs_ratio = self._calc_buy_sell_ratio(quote.bid_volume, quote.ask_volume)
        if bs_ratio and bs_ratio < 1 / 1.4:  # 等价于 ask/bid > 1.4
            reasons.append(f"内盘优势(x{1/bs_ratio:.1f})")

        # ---- 背离加分：接近压力 + 内盘>外盘 = 隐藏出货 ----
        has_divergence = near_resistance and bs_ratio and bs_ratio < 1.0
        if has_divergence and len(reasons) < 2:
            reasons.append("⭐外盘背离(主动卖)")
            return reasons  # 背离信号直接触发

        # RR过滤
        min_conditions = 3 if risk_reward < 1.2 and risk_reward > 0 else 2
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
                          f"(上次@{last_signal.price:.2f}, 本次@{signal.price:.2f})")
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
        price_w = 10
        ref_w = 10
        rr_w = 8
        reason_w = 56

        sep = f"  {'─' * (name_w + price_w + ref_w + rr_w + reason_w + 9)}"

        rows = [sep, f"  {label}"]
        header = (f"  │ {'标的':<{name_w-2}} │ {'当前价':>{price_w-2}} │ "
                  f"{'支撑/压力':>{ref_w-4}} │ {'盈亏比':>{rr_w-2}} │ 触发条件")
        rows.append(header)
        rows.append(sep)

        for s in filtered:
            ref_label = "支撑" if signal_type == T0Signal.SIGNAL_BUY else "压力"
            ref_val = s.support if signal_type == T0Signal.SIGNAL_BUY else s.resistance
            name_col = f"{s.name}({s.code})"
            rr_str = f"{s.risk_reward:.1f}x" if s.risk_reward > 0 else "--"
            if s.risk_reward >= 2.0:
                rr_str = f"⭐{rr_str}"
            elif s.risk_reward < 1.2 and s.risk_reward > 0:
                rr_str = f"⚠️{rr_str}"
            rows.append(
                f"  │ {name_col:<{name_w-2}} │ {s.price:>{price_w-2}.2f} │ "
                f"{ref_label}{ref_val:>{ref_w-4}.2f} │ {rr_str:>{rr_w-2}} │ {s.reason}"
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
            lines.append("| 标的 | 当前价 | 支撑位 | 盈亏比 | 触发条件 |")
            lines.append("| --- | ---: | ---: | ---: | --- |")
            for s in buys:
                rr_str = f"{s.risk_reward:.1f}x ({s.rr_quality})"
                lines.append(
                    f"| {s.name}({s.code}) | {s.price:.2f} | {s.support:.2f} "
                    f"| {rr_str} | {s.reason} |")
            lines.append("")

        if sells:
            lines.append("## 🔴 卖出信号\n")
            lines.append("| 标的 | 当前价 | 压力位 | 盈亏比 | 触发条件 |")
            lines.append("| --- | ---: | ---: | ---: | --- |")
            for s in sells:
                rr_str = f"{s.risk_reward:.1f}x ({s.rr_quality})"
                lines.append(
                    f"| {s.name}({s.code}) | {s.price:.2f} | {s.resistance:.2f} "
                    f"| {rr_str} | {s.reason} |")
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
