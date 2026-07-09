"""
A 股市场实时监控和分析系统

核心功能:
- 实时行情获取和分析
- 智能异动检测和预警
- 大模型 AI 研判
- 微信推送通知
- 投资报告生成

版本：2.0.0
"""

__version__ = "2.0.0"
__author__ = "Your Name"
__email__ = "your.email@example.com"

# 导出核心类和函数，方便外部使用
from app.models import (
    WatchItem,
    Holding,
    Quote,
    Alert,
    SentimentResult,
    AnalysisStats,
    NorthFlowData,
    MarketBreadth,
    INDEX_TYPE,
    MARKET_PREFIX,
)

from app.config import Config, ConfigValidationError

from app.utils import (
    log,
    setup_logger,
    load_env,
    ensure_dirs,
    format_volume,
    format_amount,
    format_percentage,
    format_number,
    safe_float,
    safe_int,
)

__all__ = [
    # 版本信息
    "__version__",
    "__author__",
    "__email__",
    
    # 数据模型
    "WatchItem",
    "Holding",
    "Quote",
    "Alert",
    "SentimentResult",
    "AnalysisStats",
    "NorthFlowData",
    "MarketBreadth",
    "INDEX_TYPE",
    "MARKET_PREFIX",
    
    # 配置
    "Config",
    "ConfigValidationError",
    
    # 工具函数
    "log",
    "setup_logger",
    "load_env",
    "ensure_dirs",
    "format_volume",
    "format_amount",
    "format_percentage",
    "format_number",
    "safe_float",
    "safe_int",
]
