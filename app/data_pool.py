"""
共享数据池模块

提供线程安全的数据共享机制，用于在多个线程之间共享行情数据和K线数据。
"""
import threading
import time
from typing import Dict, Optional, List

from .models import Quote


class KLine:
    """K线数据"""
    def __init__(self, date: str, open: float, high: float, low: float, close: float, volume: float):
        self.date = date
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class SharedDataPool:
    """
    线程安全的共享数据池
    
    数据生产者线程负责更新数据，多个消费者线程可以安全地读取数据。
    """
    
    def __init__(self):
        self._lock = threading.RLock()  # 可重入锁
        self._quotes: Dict[str, Quote] = {}
        self._klines: Dict[str, List[KLine]] = {}
        self._last_update = 0
        self._update_count = 0
    
    @property
    def last_update(self) -> float:
        """返回最后更新时间戳"""
        return self._last_update
    
    @property
    def update_count(self) -> int:
        """返回更新次数"""
        return self._update_count
    
    def update_quotes(self, quotes: Dict[str, Quote]):
        """更新行情数据"""
        with self._lock:
            self._quotes = quotes
            self._last_update = time.time()
            self._update_count += 1
    
    def update_klines(self, klines: Dict[str, List[KLine]]):
        """更新K线数据"""
        with self._lock:
            self._klines = klines
    
    def get_quote(self, code: str) -> Optional[Quote]:
        """获取指定代码的行情数据"""
        with self._lock:
            return self._quotes.get(code)
    
    def get_all_quotes(self) -> Dict[str, Quote]:
        """获取所有行情数据（返回副本）"""
        with self._lock:
            return dict(self._quotes)
    
    def get_klines(self, code: str) -> Optional[List[KLine]]:
        """获取指定代码的K线数据"""
        with self._lock:
            return self._klines.get(code)
    
    def is_fresh(self, max_age: float = 60) -> bool:
        """判断数据是否新鲜（默认60秒内更新）"""
        return time.time() - self._last_update < max_age
    
    def clear(self):
        """清空数据池"""
        with self._lock:
            self._quotes = {}
            self._klines = {}
            self._last_update = 0
