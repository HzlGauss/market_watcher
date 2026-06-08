"""
数据生产者线程模块

负责定时获取实时行情和K线数据，并更新到共享数据池。
"""
import threading
import time
import logging
from typing import Dict, List

from .data_pool import SharedDataPool, KLine
from .data_fetcher import fetch_tencent_data
from .technical import fetch_historical_kline
from .models import Quote, WatchItem


log = logging.getLogger(__name__)


class DataFetcherThread(threading.Thread):
    """
    数据生产者线程

    定时获取行情数据和K线数据，更新到共享数据池。
    """

    def __init__(self,
                 watch_items: List[WatchItem],
                 data_pool: SharedDataPool,
                 interval: int = 30):
        """
        初始化数据生产者线程

        Args:
            watch_items: 监控标的列表
            data_pool: 共享数据池
            interval: 刷新间隔（秒），默认30秒
        """
        super().__init__(daemon=True, name="DataFetcher")
        self._watch_items = watch_items
        self._data_pool = data_pool
        self._interval = interval
        self._running = False
        self._error_count = 0

    @property
    def running(self) -> bool:
        """返回线程是否正在运行"""
        return self._running

    def start(self):
        """启动线程"""
        log.info(f"数据生产者线程启动，刷新间隔: {self._interval}秒")
        self._running = True
        super().start()

    def stop(self):
        """停止线程"""
        log.info("数据生产者线程停止中...")
        self._running = False

    def run(self):
        """线程主循环"""
        while self._running:
            try:
                self._fetch_and_update()
            except Exception as e:
                self._error_count += 1
                log.error(f"数据获取失败 (第{self._error_count}次): {e}")

            # 等待下一次刷新
            if self._running:
                time.sleep(self._interval)

        log.info("数据生产者线程已停止")

    def _fetch_and_update(self):
        """获取数据并更新到数据池"""
        log.debug("开始获取市场数据...")

        # 获取实时行情
        quotes = self._fetch_quotes()

        if quotes:
            self._data_pool.update_quotes(quotes)
            log.debug(f"行情数据已更新，共 {len(quotes)} 个标的")
        else:
            log.warning("未获取到行情数据")

        # 获取K线数据（分批获取，避免超时）
        klines = self._fetch_klines()

        if klines:
            self._data_pool.update_klines(klines)
            log.debug(f"K线数据已更新，共 {len(klines)} 个标的")

        log.debug(f"数据更新完成，第 {self._data_pool.update_count} 次更新")

    def _fetch_quotes(self) -> Dict[str, Quote]:
        """获取实时行情数据"""
        try:
            result = fetch_tencent_data(self._watch_items)
            return {code: Quote(**data) for code, data in result.items()}
        except Exception as e:
            log.error(f"获取行情数据失败: {e}")
            return {}

    def _fetch_klines(self) -> Dict[str, List[KLine]]:
        """获取K线数据"""
        klines = {}

        for item in self._watch_items:
            try:
                klines_data = fetch_historical_kline(item.code, item.market, days=60, scale=60)
                if klines_data:
                    klines[item.code] = [
                        KLine(
                            date=k.get('day', ''),
                            open=float(k.get('open', 0)),
                            high=float(k.get('high', 0)),
                            low=float(k.get('low', 0)),
                            close=float(k.get('close', 0)),
                            volume=float(k.get('volume', 0))
                        )
                        for k in klines_data
                    ]
            except Exception as e:
                log.warning(f"获取 {item.code} K线数据失败: {e}")

        return klines
