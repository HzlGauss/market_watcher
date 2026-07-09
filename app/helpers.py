"""
辅助函数模块 —— 通用业务逻辑辅助函数

包含数据验证、转换、计算等可复用的业务逻辑函数。
"""

from __future__ import annotations
from typing import Optional
from datetime import datetime

from app.models import Quote, WatchItem, Holding, VALID_MARKETS
from app.utils import log


def _detect_market(code: str, provided_market: str = "") -> str:
    """根据代码特征自动识别市场 (SH/SZ/HK)"""
    market = str(provided_market).strip().upper()
    if market in VALID_MARKETS:
        return market

    # 自动识别逻辑
    if len(code) == 5:
        return "HK"
    
    # 深圳代码特征：00, 30, 15, 16, 18
    if code.startswith(("00", "30", "15", "16", "18")):
        return "SZ"
    
    # 上海代码特征：60, 68, 51, 58
    if code.startswith(("60", "68", "51", "58")):
        return "SH"
        
    return "SH"  # 默认上海


def validate_watch_item(item: dict, source: str = "unknown") -> Optional[WatchItem]:
    """
    验证并创建 WatchItem

    Args:
        item: 包含 watch item 数据的字典
        source: 数据来源描述

    Returns:
        验证通过的 WatchItem，验证失败返回 None
    """
    try:
        # 安全获取并转换为字符串
        code = str(item.get("code", "")).strip()
        if not code:
            log.warning(f"Skipping item from {source}: code is empty")
            return None

        name = str(item.get("name", "")).strip()
        market = _detect_market(code, item.get("market", ""))
        item_type = str(item.get("type", "宽基 ETF")).strip()

        return WatchItem(
            name=name,
            code=code,
            market=market,
            type=item_type,
        )
    except Exception as e:
        log.error(f"Failed to validate watch item: {e}")
        return None


def validate_holding(item: dict, source: str = "unknown") -> Optional[Holding]:
    """
    验证并创建 Holding

    数据质量检查：
    - code 为空 → 跳过
    - amount = 0 → 跳过（观察标的，非真实持仓）
    - cost <= 0 → 警告但仍然创建（可能只是未填成本）
    - market 为空 → 自动检测

    Args:
        item: 包含持仓数据的字典
        source: 数据来源描述

    Returns:
        验证通过的 Holding，验证失败返回 None
    """
    try:
        # 安全获取并转换为字符串
        code = str(item.get("code", "")).strip()
        if not code:
            log.warning(f"Skipping holding from {source}: code is empty")
            return None

        name = str(item.get("name", "")).strip()
        if not name:
            log.warning(f"Skipping holding from {source}: name is empty (code={code})")
            return None

        # 市场自动检测（CSV 中 market 字段可能为空或 null）
        raw_market = str(item.get("market", "")).strip()
        market = _detect_market(code, raw_market)

        amount = _safe_positive_int(item.get("amount", 0))
        cost = _safe_positive_float(item.get("cost", 0.0))

        # 标记异常成本
        if cost <= 0:
            log.debug(f"Holding {name}({code}): cost={cost} (P&L unavailable)")

        # 标记市场为空的情况
        if not raw_market:
            log.debug(f"Holding {name}({code}) market auto-detected as {market}")

        return Holding(
            name=name,
            code=code,
            market=market,
            amount=amount,
            cost=cost,
        )
    except Exception as e:
        log.error(f"Failed to validate holding: {e}")
        return None


def _safe_positive_int(value: any, default: int = 0) -> int:
    """安全转换为正整数"""
    try:
        result = int(float(value))
        return max(0, result)
    except (ValueError, TypeError):
        return default


def _safe_positive_float(value: any, default: float = 0.0) -> float:
    """安全转换为正浮点数"""
    try:
        result = float(value)
        return max(0.0, result)
    except (ValueError, TypeError):
        return default


def calculate_profit(current_price: float, cost: float, amount: int) -> tuple[float, float]:
    """
    计算持仓盈亏

    Args:
        current_price: 当前价格
        cost: 成本价
        amount: 持仓数量

    Returns:
        (盈亏金额，盈亏比例) 的元组
    """
    if cost <= 0 or amount <= 0:
        return 0.0, 0.0

    profit_amount = (current_price - cost) * amount
    profit_pct = ((current_price - cost) / cost) * 100

    return profit_amount, profit_pct


def parse_time(time_str: str) -> tuple[int, int]:
    """
    解析时间字符串为小时和分钟

    Args:
        time_str: 时间字符串，格式为 "HH:MM"

    Returns:
        (hour, minute) 元组

    Raises:
        ValueError: 时间格式错误
    """
    try:
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            raise ValueError("Invalid time format")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Hour or minute out of range")
        return hour, minute
    except Exception as e:
        raise ValueError(f"Failed to parse time '{time_str}': {e}")


def time_to_minutes(time_str: str) -> int:
    """
    将时间字符串转换为从 0 点开始的分钟数

    Args:
        time_str: 时间字符串，格式为 "HH:MM"

    Returns:
        分钟数
    """
    hour, minute = parse_time(time_str)
    return hour * 60 + minute


def is_in_time_range(current: datetime, start: str, end: str) -> bool:
    """
    判断当前时间是否在指定时间范围内

    Args:
        current: 当前时间
        start: 开始时间 ("HH:MM")
        end: 结束时间 ("HH:MM")

    Returns:
        是否在时间范围内
    """
    try:
        current_minutes = current.hour * 60 + current.minute
        start_minutes = time_to_minutes(start)
        end_minutes = time_to_minutes(end)
        return start_minutes <= current_minutes <= end_minutes
    except Exception as e:
        log.error(f"Error checking time range: {e}")
        return False


def is_trading_time(current: datetime, sessions: dict) -> tuple[bool, str]:
    """
    判断当前时间是否在交易时间内

    Args:
        current: 当前时间
        sessions: 交易时段配置

    Returns:
        (是否在交易时间内，原因描述) 元组
    """
    # 检查是否是周末
    weekday = current.weekday()
    if weekday >= 5:
        return False, "Weekend"

    # 检查是否在交易时段内
    morning = sessions.get("上午", ["09:30", "11:30"])
    afternoon = sessions.get("下午", ["13:00", "15:00"])

    try:
        am_start = time_to_minutes(morning[0])
        am_end = time_to_minutes(morning[1])
        pm_start = time_to_minutes(afternoon[0])
        pm_end = time_to_minutes(afternoon[1])

        current_minutes = current.hour * 60 + current.minute

        is_in = (am_start <= current_minutes <= am_end) or \
                (pm_start <= current_minutes <= pm_end)

        reason = "In trading hours" if is_in else "Non-trading hours"
        return is_in, reason
    except Exception as e:
        log.error(f"Error checking trading time: {e}")
        return False, "Error checking trading time"


def format_quote_summary(quotes: list[Quote]) -> str:
    """
    格式化行情摘要

    Args:
        quotes: 行情数据列表

    Returns:
        格式化的摘要字符串
    """
    if not quotes:
        return "No data"

    lines = []
    for quote in quotes[:5]:  # 只显示前 5 个
        change = f"{quote.change_pct:+.2f}%" if quote.change_pct is not None else "--"
        lines.append(f"{quote.name}: {change}")

    if len(quotes) > 5:
        lines.append(f"... and {len(quotes) - 5} more")

    return " | ".join(lines)
