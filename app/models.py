"""
数据模型 —— 所有核心数据结构

使用 dataclass 替代字典，配合类型注解，让代码自文档化。
所有数据模型都应该是不可变的（frozen=True），除非明确需要可变性。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Literal


# ============================================================
# 原始数据模型
# ============================================================

@dataclass
class WatchItem:
    """
    配置文件中的单个盯盘标的
    
    Attributes:
        name: 标的名称
        code: 股票代码
        market: 市场标识 (SH: 上海，SZ: 深圳，HK: 香港)
        type: 标的类型 (宽基 ETF/行业 ETF/港股 ETF/指数)
    """
    name: str = ""
    code: str = ""
    market: Literal["SH", "SZ", "HK"] = "SH"
    type: str = "宽基 ETF"


@dataclass
class Holding:
    """
    用户持仓信息
    
    Attributes:
        name: 持仓名称
        code: 股票代码
        market: 市场标识
        amount: 持仓数量（股/份）
        cost: 持仓成本价
    """
    name: str = ""
    code: str = ""
    market: Literal["SH", "SZ", "HK"] = "SH"
    amount: int = 0
    cost: float = 0.0


@dataclass
class Quote:
    """
    单个标的的实时行情快照
    
    Attributes:
        code: 股票代码
        name: 标的名称
        type: 标的类型
        price: 当前价格
        change_pct: 涨跌幅 (%)
        change_amt: 涨跌额
        pre_close: 昨收价
        open: 开盘价
        high: 最高价
        low: 最低价
        volume: 成交量
        amount: 成交额
        amplitude: 振幅 (%)
    """
    code: str = ""
    name: str = ""
    type: str = "其他"
    price: Optional[float] = None
    change_pct: Optional[float] = None
    change_amt: Optional[float] = None
    pre_close: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[float] = None
    amount: Optional[float] = None
    amplitude: Optional[float] = None


@dataclass
class Alert:
    """
    单条异动提醒
    
    Attributes:
        code: 股票代码
        name: 标的名称
        messages: 异动消息列表
    """
    code: str = ""
    name: str = ""
    messages: list[str] = field(default_factory=list)
    
    def add_message(self, message: str) -> None:
        """添加异动消息"""
        self.messages.append(message)
    
    def has_messages(self) -> bool:
        """检查是否有异动消息"""
        return len(self.messages) > 0


# ============================================================
# 分析结果模型
# ============================================================

@dataclass
class SentimentResult:
    """
    市场情绪评估结果
    
    Attributes:
        score: 情绪评分 (0-100)
        label: 情绪标签
        detail: 详细描述
        up_ratio: 上涨比例
        median_pct: 中位数涨跌幅
    """
    score: int = 50
    label: str = "未知"
    detail: str = ""
    up_ratio: float = 0.0
    median_pct: float = 0.0


@dataclass
class AnalysisStats:
    """
    单次扫描的统计结果
    
    Attributes:
        total: 总标的数
        up: 上涨标的数
        down: 下跌标的数
        flat: 平盘标的数
        alert_count: 异动数量
        sentiment: 市场情绪评估
        thresholds: 当前阈值
        base_thresholds: 基础阈值
        dynamic_enabled: 是否启用动态阈值
        north_flow: 北向资金数据
        llm_result: LLM 分析结果
    """
    total: int = 0
    up: int = 0
    down: int = 0
    flat: int = 0
    alert_count: int = 0
    sentiment: Optional[SentimentResult] = None
    thresholds: dict = field(default_factory=dict)
    base_thresholds: dict = field(default_factory=dict)
    dynamic_enabled: bool = False
    north_flow: Optional[dict] = None
    llm_result: Optional[str] = None


@dataclass
class NorthFlowData:
    """
    北向资金数据
    
    Attributes:
        hk2sh_net: 沪股通净流入 (亿元)
        hk2sz_net: 深股通净流入 (亿元)
        total_net: 总净流入 (亿元)
        hk2sh_quota: 沪股通额度使用率
        hk2sz_quota: 深股通额度使用率
        date: 数据日期
    """
    hk2sh_net: float = 0.0
    hk2sz_net: float = 0.0
    total_net: float = 0.0
    hk2sh_quota: float = 0.0
    hk2sz_quota: float = 0.0
    date: str = ""
    
    @property
    def is_significant(self) -> bool:
        """判断北向资金是否显著 (净流入/流出超过 50 亿)"""
        return abs(self.total_net) > 50.0


# ============================================================
# K线与技术分析模型
# ============================================================

@dataclass
class KlineData:
    """单日K线数据"""
    date: str = ""
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None


@dataclass
class TechnicalSummary:
    """技术指标汇总"""
    rsi: Optional[float] = None
    rsi_signal: str = ""
    macd_dif: Optional[float] = None
    macd_dea: Optional[float] = None
    macd_histogram: Optional[float] = None
    macd_signal: str = ""
    kdj_k: Optional[float] = None
    kdj_d: Optional[float] = None
    kdj_j: Optional[float] = None
    kdj_signal: str = ""
    support: Optional[float] = None
    resistance: Optional[float] = None
    atr: Optional[float] = None
    signals: list[str] = field(default_factory=list)


# ============================================================
# 常量
# ============================================================

# 市场前缀映射（新浪财经用）
MARKET_PREFIX: dict[str, str] = {
    "SH": "sh",
    "SZ": "sz",
    "HK": "hk",
}

# 指数类型标记（不触发报警）
INDEX_TYPE = "指数"

# 有效的市场标识
VALID_MARKETS = frozenset(["SH", "SZ", "HK"])

# 有效的标的类型
VALID_TYPES = frozenset(["宽基 ETF", "行业 ETF", "港股 ETF", "指数", "其他"])
