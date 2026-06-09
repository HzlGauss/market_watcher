"""
做T监控线程模块

负责监控持仓标的的支撑压力位，识别做T买入/卖出信号。
"""
import threading
import time
import logging
import os
from datetime import datetime
from typing import List, Optional, Dict

from .data_pool import SharedDataPool, KLine
from .models import Quote, WatchItem
from .technical import calc_support_resistance, TechnicalSummary, get_technical_summary
from .http_client import serverchan_client

log = logging.getLogger(__name__)


class T0Signal:
    """做T信号"""
    SIGNAL_BUY = 'buy'
    SIGNAL_SELL = 'sell'
    SIGNAL_NONE = 'none'

    def __init__(self, code: str, name: str, signal_type: str, reason: str,
                 price: float, support: float, resistance: float):
        self.code = code
        self.name = name
        self.signal_type = signal_type
        self.reason = reason
        self.price = price
        self.support = support
        self.resistance = resistance
        self.timestamp = time.time()

    def is_valid(self, max_age: float = 300) -> bool:
        """判断信号是否有效（默认5分钟内）"""
        return time.time() - self.timestamp < max_age

    def __repr__(self):
        return f"T0Signal(code={self.code}, type={self.signal_type}, reason={self.reason})"


class T0MonitorThread(threading.Thread):
    """
    做T监控线程

    定时扫描持仓标的，计算支撑压力位，识别做T买入/卖出信号。
    """

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
            data_pool: 共享数据池
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
        self._last_signals: Dict[str, T0Signal] = {}  # 记录最近信号，避免重复提示

    @property
    def running(self) -> bool:
        """返回线程是否正在运行"""
        return self._running

    def start(self):
        """启动线程"""
        log.info(f"做T监控线程启动，扫描间隔: {self._interval}秒")
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

    def _scan(self):
        """扫描所有标的，识别做T信号"""
        if not self._data_pool.is_fresh(max_age=120):
            log.debug("数据池数据过期，跳过扫描")
            return

        log.debug("开始做T信号扫描...")

        new_signals: list[T0Signal] = []
        for item in self._watch_items:
            quote = self._data_pool.get_quote(item.code)
            klines = self._data_pool.get_klines(item.code)

            if quote is None or klines is None:
                continue

            signal = self._evaluate_signal(item, quote, klines)

            if signal and signal.signal_type != T0Signal.SIGNAL_NONE:
                # 检查是否重复信号（5分钟内相同类型的信号不重复提示）
                last_signal = self._last_signals.get(signal.code)
                if last_signal and last_signal.signal_type == signal.signal_type and last_signal.is_valid():
                    log.debug(f"跳过重复信号: {signal.code} {signal.signal_type}")
                    continue
                # 更新最后信号记录
                self._last_signals[signal.code] = signal
                new_signals.append(signal)

        if new_signals:
            self._print_signals_batch(new_signals)
            if self._enable_push:
                for signal in new_signals:
                    self._push_signal(signal)

    def _evaluate_signal(self, item: WatchItem, quote: Quote, klines: List[KLine]) -> Optional[T0Signal]:
        """评估单个标的的做T信号"""
        # 计算支撑压力位
        sr = calc_support_resistance(klines)

        if sr.support is None or sr.resistance is None:
            return None

        # 计算技术指标
        tech = get_technical_summary(quote, klines)

        # 获取当前价格
        price = quote.price or quote.last_close or 0

        if price <= 0:
            return None

        # 计算区间宽度
        range_width = sr.resistance - sr.support

        # 区间宽度至少需要覆盖交易成本（假设千分之五）
        if range_width < price * 0.005:
            return None

        # 判断买入信号
        buy_reasons = self._check_buy_conditions(quote, tech, sr, price)

        # 判断卖出信号
        sell_reasons = self._check_sell_conditions(quote, tech, sr, price)

        if buy_reasons:
            reason = "; ".join(buy_reasons)
            return T0Signal(
                code=item.code,
                name=item.name,
                signal_type=T0Signal.SIGNAL_BUY,
                reason=reason,
                price=price,
                support=sr.support,
                resistance=sr.resistance
            )

        if sell_reasons:
            reason = "; ".join(sell_reasons)
            return T0Signal(
                code=item.code,
                name=item.name,
                signal_type=T0Signal.SIGNAL_SELL,
                reason=reason,
                price=price,
                support=sr.support,
                resistance=sr.resistance
            )

        return None

    def _check_buy_conditions(self, quote: Quote, tech: TechnicalSummary,
                              sr, price: float) -> List[str]:
        """检查买入条件"""
        reasons = []

        # 条件1：股价接近支撑位（1%以内）
        support_range = sr.support * 1.01
        if price <= support_range:
            reasons.append(f"股价接近支撑位({sr.support:.2f})")

        # 条件2：RSI超卖
        if tech.rsi and tech.rsi < 30:
            reasons.append(f"RSI超卖({tech.rsi:.1f})")

        # 条件3：KDJ超卖
        if tech.kdj_k and tech.kdj_k < 20:
            reasons.append("KDJ超卖")

        # 条件4：量能萎缩（量比<0.6）
        if quote.volume_ratio and quote.volume_ratio < 0.6:
            reasons.append(f"量能萎缩(量比{quote.volume_ratio:.2f})")

        # 需要至少2个条件满足
        return reasons if len(reasons) >= 2 else []

    def _check_sell_conditions(self, quote: Quote, tech: TechnicalSummary,
                               sr, price: float) -> List[str]:
        """检查卖出条件"""
        reasons = []

        # 条件1：股价接近压力位（1%以内）
        resistance_range = sr.resistance * 0.99
        if price >= resistance_range:
            reasons.append(f"股价接近压力位({sr.resistance:.2f})")

        # 条件2：RSI超买
        if tech.rsi and tech.rsi > 70:
            reasons.append(f"RSI超买({tech.rsi:.1f})")

        # 条件3：KDJ超买
        if tech.kdj_k and tech.kdj_k > 80:
            reasons.append("KDJ超买")

        # 条件4：量能放大（量比>1.5）
        if quote.volume_ratio and quote.volume_ratio > 1.5:
            reasons.append(f"量能放大(量比{quote.volume_ratio:.2f})")

        # 需要至少2个条件满足
        return reasons if len(reasons) >= 2 else []

    def _print_signals_batch(self, signals: list[T0Signal]):
        """批量打印做T信号汇总（表格形式）"""
        buy_signals = [s for s in signals if s.signal_type == T0Signal.SIGNAL_BUY]
        sell_signals = [s for s in signals if s.signal_type == T0Signal.SIGNAL_SELL]

        # 找最长名称用于列宽
        max_name_len = max((len(f"{s.name}({s.code})") for s in signals), default=0)
        name_width = max(max_name_len + 2, 16)
        reason_width = 42

        sep = "─" * (name_width + reason_width + 40)

        print(f"\n  📊 做T信号汇总")
        print(f"  {sep}")

        # 表头
        type_col_width = 8
        price_col_width = 10
        sr_col_width = 18
        header = (
            f"  │ {'类型':<{type_col_width}}"
            f"│ {'标的':<{name_width}}"
            f"│ {'当前价':>{price_col_width}}"
            f"│ {'支撑/压力':^{sr_col_width}}"
            f"│ {'触发条件':<{reason_width}}│"
        )
        print(header)
        print(f"  │{'─' * (type_col_width + name_width + price_col_width + sr_col_width + reason_width + 6)}│")

        for s in signals:
            icon = "🟢 买入" if s.signal_type == T0Signal.SIGNAL_BUY else "🔴 卖出"
            name = f"{s.name}({s.code})"
            price_str = f"{s.price:.3f}"
            sr_str = f"{s.support:.3f}/{s.resistance:.3f}"
            # 截断过长的触发条件
            reason = s.reason if len(s.reason) <= reason_width else s.reason[:reason_width - 3] + "..."

            row = (
                f"  │ {icon:<{type_col_width}}"
                f"│ {name:<{name_width}}"
                f"│ {price_str:>{price_col_width}}"
                f"│ {sr_str:^{sr_col_width}}"
                f"│ {reason:<{reason_width}}│"
            )
            print(row)

        print(f"  {sep}")
        print(f"  建议: 买入→目标压力位附近卖出  |  卖出→回落支撑位附近买回\n")

        for s in signals:
            signal_text = "买入信号" if s.signal_type == T0Signal.SIGNAL_BUY else "卖出信号"
            log.info(f"做T信号: {s.name}({s.code}) - {signal_text}: {s.reason}")

    def _play_sound(self, signal_type: str):
        """播放提示音"""
        try:
            import winsound
            # 买入信号：高频短音
            # 卖出信号：低频长音
            if signal_type == T0Signal.SIGNAL_BUY:
                winsound.Beep(1000, 300)
                winsound.Beep(1200, 300)
            else:
                winsound.Beep(500, 500)
                winsound.Beep(400, 500)
        except Exception as e:
            log.debug(f"播放提示音失败: {e}")

    def _push_signal(self, signal: T0Signal):
        """推送做T信号到微信（Server酱）"""
        sendkey = os.environ.get("SCT_SENDKEY")
        if not sendkey:
            log.debug("未配置 SCT_SENDKEY，跳过推送")
            return

        try:
            # 构建标题
            signal_icon = "🟢" if signal.signal_type == T0Signal.SIGNAL_BUY else "🔴"
            signal_text = "买入信号" if signal.signal_type == T0Signal.SIGNAL_BUY else "卖出信号"
            now_str = datetime.now().strftime("%m-%d %H:%M")
            title = f"{signal_icon} 做T信号 | {signal.name}({signal.code})"

            # 构建内容
            content = f"""## {signal_icon} {signal_text}

**标的**: {signal.name}({signal.code})

**触发条件**: {signal.reason}

**当前价**: {signal.price:.2f}
**支撑位**: {signal.support:.2f}
**压力位**: {signal.resistance:.2f}

**建议**: {'可考虑买入做T，目标压力位附近卖出' if signal.signal_type == T0Signal.SIGNAL_BUY else '可考虑卖出做T，回落支撑位附近买回'}

---
*{now_str}*"""

            url = f"/{sendkey}.send"
            resp = serverchan_client.post(url, data={"title": title, "desp": content})

            if resp and resp.status_code == 200 and resp.json().get("code") == 0:
                log.info(f"做T信号推送成功: {signal.name}")
            else:
                log.warning(f"做T信号推送失败")

        except Exception as e:
            log.error(f"做T信号推送异常: {e}")
