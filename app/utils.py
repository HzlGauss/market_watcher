"""
工具函数 —— 日志、路径、环境变量等通用功能

提供项目通用的辅助函数，包括日志记录、路径处理、数据格式化等。
"""

from __future__ import annotations
import logging
import os
import sys
from pathlib import Path
from typing import Optional


# ============================================================
# 日志系统
# ============================================================

def setup_logger(
    name: str = "watcher",
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
) -> logging.Logger:
    """
    统一日志配置，替换 print
    
    Args:
        name: 日志记录器名称
        level: 日志级别
        log_file: 日志文件路径（可选）
    
    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # 避免重复配置

    logger.setLevel(level)
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(console_handler)
    
    # 文件处理器（可选）
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(file_handler)
    
    return logger


# 全局日志实例
log = setup_logger()


# ============================================================
# 路径 & 环境变量
# ============================================================

def load_env(base_dir: Path) -> bool:
    """
    从项目根目录的 .env 文件加载环境变量
    
    Args:
        base_dir: 项目根目录
    
    Returns:
        是否成功加载
    """
    env_path = base_dir / ".env"
    if not env_path.exists():
        log.warning(f".env file not found: {env_path}")
        return False
    
    try:
        with open(str(env_path), "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    try:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip()
                    except Exception as e:
                        log.error(f"Failed to parse line {line_num} in .env: {e}")
        log.info(f"Loaded environment variables from {env_path}")
        return True
    except Exception as e:
        log.error(f"Failed to load .env file: {e}")
        return False


def ensure_dirs(*dirs: Path) -> None:
    """
    确保多个目录存在
    
    Args:
        *dirs: 目录路径列表
    """
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.error(f"Failed to create directory {d}: {e}")
            raise


# ============================================================
# 格式辅助
# ============================================================

def format_volume(vol: float | None) -> str:
    """
    格式化成交量：自动转换为 万/亿 单位
    
    Args:
        vol: 成交量
    
    Returns:
        格式化后的成交量字符串
    """
    if vol is None:
        return "--"
    if vol >= 100_000_000:
        return f"{vol / 100_000_000:.2f}亿"
    if vol >= 10_000:
        return f"{vol / 10_000:.1f}万"
    return f"{vol:.0f}"


def format_amount(amt: float | None) -> str:
    """
    格式化成交额：自动转换为 万/亿 单位
    
    Args:
        amt: 成交额
    
    Returns:
        格式化后的成交额字符串
    """
    if amt is None:
        return "--"
    if amt >= 100_000_000:
        return f"{amt / 100_000_000:.2f}亿"
    if amt >= 10_000:
        return f"{amt / 10_000:.0f}万"
    return f"{amt:.0f}"


def format_percentage(value: float | None, decimals: int = 2) -> str:
    """
    格式化百分比
    
    Args:
        value: 百分比值
        decimals: 小数位数
    
    Returns:
        格式化后的百分比字符串
    """
    if value is None:
        return "--"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def format_number(value: float | None, decimals: int = 2) -> str:
    """
    格式化数字
    
    Args:
        value: 数值
        decimals: 小数位数
    
    Returns:
        格式化后的数字字符串
    """
    if value is None:
        return "--"
    return f"{value:.{decimals}f}"


def safe_float(value: any, default: float = 0.0) -> float:
    """
    安全转换为浮点数
    
    Args:
        value: 任意类型的值
        default: 转换失败时的默认值
    
    Returns:
        转换后的浮点数
    """
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value: any, default: int = 0) -> int:
    """
    安全转换为整数
    
    Args:
        value: 任意类型的值
        default: 转换失败时的默认值
    
    Returns:
        转换后的整数
    """
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default
