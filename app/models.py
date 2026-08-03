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
class FundFlowDetail:
    """
    个股资金流向明细（东方财富逐笔分类）

    主力 = 超大单 + 大单，散户 ≈ 小单。

    Attributes:
        main_net: 主力净流入（元）= super_large_net + large_net
        main_pct: 主力净流入占成交额比例 (%)
        super_large_net: 超大单净流入（元），通常代表机构/国家队
        super_large_pct: 超大单净占比 (%)
        large_net: 大单净流入（元），通常代表游资/私募
        large_pct: 大单净占比 (%)
        medium_net: 中单净流入（元），游资/中型资金
        medium_pct: 中单净占比 (%)
        small_net: 小单净流入（元），散户行为
        small_pct: 小单净占比 (%)
    """
    main_net: Optional[float] = None
    main_pct: Optional[float] = None
    super_large_net: Optional[float] = None
    super_large_pct: Optional[float] = None
    large_net: Optional[float] = None
    large_pct: Optional[float] = None
    medium_net: Optional[float] = None
    medium_pct: Optional[float] = None
    small_net: Optional[float] = None
    small_pct: Optional[float] = None

    @property
    def is_valid(self) -> bool:
        """数据是否有效（至少有主力净流入数据）"""
        return self.main_net is not None

    @property
    def is_institution_driven(self) -> bool:
        """是否机构主导（超大单净买 + 散户净卖）"""
        if self.super_large_net is None or self.small_net is None:
            return False
        return self.super_large_net > 0 and self.small_net < 0

    @property
    def is_retail_driven(self) -> bool:
        """是否散户主导（小单净买为主，主力净卖或中性）"""
        if self.small_net is None or self.main_net is None:
            return False
        return self.small_net > 0 and self.main_net <= 0

    @property
    def is_distribution(self) -> bool:
        """是否主力出货散户接盘（跌或平盘时超大单出+散户接）"""
        if self.super_large_net is None or self.small_net is None:
            return False
        return self.super_large_net < 0 and self.small_net > 0


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
        pe_ratio: 动态市盈率
        pb_ratio: 市净率
        market_cap: 总市值（元）
        turnover_rate: 换手率 (%)
        volume_ratio: 量比
        main_net_inflow: 主力净流入（元），向后兼容，优先使用 fund_flow
        fund_flow: 资金流向明细（超大/大/中/小单）
        upper_limit: 涨停价
        lower_limit: 跌停价
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
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    market_cap: Optional[float] = None
    turnover_rate: Optional[float] = None
    volume_ratio: Optional[float] = None
    main_net_inflow: Optional[float] = None
    fund_flow: Optional[FundFlowDetail] = None
    bid_volume: Optional[float] = None  # 外盘（主动买入）
    ask_volume: Optional[float] = None  # 内盘（主动卖出）
    bid_ask_ratio: Optional[float] = None  # 委比
    upper_limit: Optional[float] = None
    lower_limit: Optional[float] = None


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
    north_flow: Optional["NorthFlowData"] = None
    market_breadth: Optional["MarketBreadth"] = None
    llm_result: Optional[str] = None


@dataclass
class MarketNews:
    """单条市场快讯"""
    time: str = ""
    title: str = ""
    category: str = ""
    content: str = ""
    url: str = ""


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


@dataclass
class MarketBreadth:
    """全市场广度数据

    从东方财富 API 获取，反映整个 A 股的涨跌分布、
    量能水平和极端情绪（涨跌停家数）。

    Attributes:
        up_count: 上涨家数
        down_count: 下跌家数
        flat_count: 平盘家数
        total_count: 总家数
        limit_up: 涨停家数
        limit_down: 跌停家数
        total_amount: 全市场成交额（亿元）
        total_volume: 全市场成交量（万手）
        index_name: 参考指数名称
        index_price: 参考指数点位
        index_change_pct: 参考指数涨跌幅
        main_net_inflow: 主力净流入（亿元）
        update_time: 数据更新时间
    """
    up_count: int = 0
    down_count: int = 0
    flat_count: int = 0
    total_count: int = 0
    limit_up: int = 0
    limit_down: int = 0
    total_amount: float = 0.0
    total_volume: float = 0.0
    index_name: str = ""
    index_price: float = 0.0
    index_change_pct: float = 0.0
    main_net_inflow: float = 0.0
    update_time: str = ""

    @property
    def up_ratio(self) -> float:
        """上涨比例 (0-1)"""
        if self.total_count <= 0:
            return 0.5
        return self.up_count / self.total_count

    @property
    def down_ratio(self) -> float:
        """下跌比例 (0-1)"""
        if self.total_count <= 0:
            return 0.5
        return self.down_count / self.total_count

    @property
    def breadth_label(self) -> str:
        """市场宽度标签

        根据涨跌比给出市场状态定性：
        - > 70% 上涨 → 普涨
        - 50-70% 上涨 → 偏多
        - 30-50% 上涨 → 偏空
        - < 30% 上涨 → 普跌
        """
        r = self.up_ratio
        if r >= 0.7:
            return "普涨"
        elif r >= 0.5:
            return "偏多"
        elif r >= 0.3:
            return "偏空"
        else:
            return "普跌"

    @property
    def limit_emotion(self) -> str:
        """涨跌停情绪标签

        涨停多、跌停少 → 亢奋
        涨停少、跌停多 → 恐慌
        两者都少 → 平淡
        两者都多 → 极端分化
        """
        if self.limit_up >= 80 and self.limit_down < 10:
            return "亢奋"
        elif self.limit_down >= 50 and self.limit_up < 20:
            return "恐慌"
        elif self.limit_up >= 50 and self.limit_down >= 30:
            return "分化加剧"
        elif self.limit_up < 30 and self.limit_down < 10:
            return "平淡"
        else:
            return "正常"

    @property
    def is_valid(self) -> bool:
        """数据是否有效（至少要有基本的涨跌统计）"""
        return self.total_count > 0

    @property
    def estimated_full_day_amount(self) -> float:
        """估算全天成交额（亿元），基于当前时段线性外推"""
        return _estimate_full_day_amount(self.total_amount)


def _estimate_full_day_amount(cumulative_amount: float, now: "datetime.datetime | None" = None) -> float:
    """估算全天成交额（亿元），基于当前时段线性外推

    盘中累计值是当日的，直接给 LLM 会导致每轮都判断"量能不足"。
    按已流逝交易时间比例外推到全天，收盘后直接返回实际值。

    A股交易时段: 9:30-11:30 (120min) + 13:00-15:00 (120min) = 240min
    """
    if cumulative_amount <= 0:
        return 0.0

    if now is None:
        from datetime import datetime
        now = datetime.now()

    # 收盘后 → 实际值
    if now.hour >= 15:
        return cumulative_amount

    # 计算已流逝的交易分钟数
    elapsed = _trading_minutes_elapsed(now)
    if elapsed <= 0:
        return cumulative_amount  # 开盘前不推算

    # 线性外推
    ratio = 240 / elapsed
    estimated = round(cumulative_amount * ratio, 1)
    return max(estimated, cumulative_amount)  # 不低于累计值


def _trading_minutes_elapsed(t: "datetime.datetime") -> int:
    """计算当日已流逝的A股交易分钟数"""
    h, m = t.hour, t.minute
    if h < 9 or (h == 9 and m < 30):
        return 0
    current = h * 60 + m
    morning_end = 11 * 60 + 30
    afternoon_start = 13 * 60
    close = 15 * 60

    if current >= close:
        return 240
    if current >= afternoon_start:
        return 120 + min(current - afternoon_start, 120)
    if current >= morning_end:
        return 120
    return max(current - (9 * 60 + 30), 1)


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
    swing_supports: list[float] = field(default_factory=list)
    swing_resistances: list[float] = field(default_factory=list)
    pivot_supports: list[float] = field(default_factory=list)
    pivot_resistances: list[float] = field(default_factory=list)
    volume_clusters: list[float] = field(default_factory=list)
    atr: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_width: Optional[float] = None
    bb_signal: str = ""
    obv: Optional[float] = None
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    ma_alignment: str = ""  # 多头排列 / 空头排列 / 缠绕 / 多头回调 / 空头反弹 / 数据不足
    ma_alignment_detail: str = ""
    has_gap: bool = False
    gap_type: str = ""      # "向上跳空" / "向下跳空" / ""
    gap_pct: float = 0.0
    gap_detail: str = ""
    gap_filled_pct: float = 0.0
    breakout_type: str = ""  # "突破近期高点" / "跌破近期低点" / ""
    breakout_detail: str = ""
    signals: list[str] = field(default_factory=list)


# TechSnapshot 是 TechnicalSummary 的别名，向后兼容
TechSnapshot = TechnicalSummary


def tech_snapshot_to_summary(snapshot: "TechSnapshot") -> "TechnicalSummary":
    """将 TechSnapshot 转为 TechnicalSummary（用于作为 prev_tech 传入策略引擎）

    由于 TechSnapshot 现在是 TechnicalSummary 的别名，直接返回即可。
    """
    return snapshot


@dataclass
class FundScanStatus:
    """单次扫描中某只基金的状态"""
    price: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    vol_ratio: Optional[float] = None  # 相对前日成交量倍率
    alerts: list[str] = field(default_factory=list)
    tech_signals: list[str] = field(default_factory=list)
    tech_snapshot: Optional[TechSnapshot] = None


@dataclass
class ScanRecord:
    """单次扫描记录"""
    scan_id: int = 0
    time: str = ""  # HH:MM格式
    timestamp: int = 0
    market_sentiment: dict = field(default_factory=dict)
    alerts_summary: dict = field(default_factory=dict)
    funds_status: dict = field(default_factory=dict)  # code -> FundScanStatus
    llm_analysis: str | None = None


# ============================================================
# 龙虎榜模型
# ============================================================

@dataclass
class DragonTigerSeat:
    """
    龙虎榜买卖席位

    Attributes:
        seat_name: 营业部名称（如"机构专用"、"深股通专用"、"华泰证券...")
        buy_amount: 买入金额（元）
        sell_amount: 卖出金额（元）
        net_amount: 净买入金额（元）
        is_institution: 是否机构专用席位
        is_hsgt: 是否沪深股通席位
    """
    seat_name: str = ""
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    net_amount: float = 0.0
    is_institution: bool = False
    is_hsgt: bool = False


@dataclass
class DragonTigerRecord:
    """
    单只个股龙虎榜数据

    Attributes:
        code: 股票代码
        name: 股票名称
        reason: 上榜原因
        change_pct: 涨跌幅(%)
        total_buy: 龙虎榜总买入额（元）
        total_sell: 龙虎榜总卖出额（元）
        net_buy: 龙虎榜净买入额（元）
        turnover_rate: 换手率(%)
        total_trade: 龙虎榜总成交额（元）
        industry: 所属行业
        sector: 所属地域板块
        buy_seats: 买入前5席位
        sell_seats: 卖出前5席位
        main_net_inflow: 主力净流入（元）
    """
    code: str = ""
    name: str = ""
    reason: str = ""
    change_pct: Optional[float] = None
    total_buy: float = 0.0
    total_sell: float = 0.0
    net_buy: float = 0.0
    turnover_rate: Optional[float] = None
    total_trade: float = 0.0
    industry: str = ""
    sector: str = ""
    buy_seats: list[DragonTigerSeat] = field(default_factory=list)
    sell_seats: list[DragonTigerSeat] = field(default_factory=list)
    main_net_inflow: Optional[float] = None

    @property
    def buy_sell_ratio(self) -> Optional[float]:
        """买卖金额比（>1 表示买方力量更强）"""
        if self.total_sell > 0:
            return round(self.total_buy / self.total_sell, 2)
        return None

    @property
    def institution_net(self) -> float:
        """机构席位净买入额"""
        return sum(s.net_amount for s in self.buy_seats + self.sell_seats if s.is_institution)

    @property
    def hsgt_net(self) -> float:
        """沪深股通席位净买入额"""
        return sum(s.net_amount for s in self.buy_seats + self.sell_seats if s.is_hsgt)


@dataclass
class DragonTigerSummary:
    """
    龙虎榜综合分析结果

    Attributes:
        date: 数据日期
        total_count: 上榜个股总数
        records: 完整龙虎榜记录列表
        institutional_focus: 机构资金重点关注个股（机构净买入>0 且排名靠前）
        institutional_risk: 机构资金出逃个股（机构净卖出较大）
        hot_money_track: 知名游资动向追踪
        sector_flow: 板块资金流向汇总
        overall_assessment: 整体研判结论
    """
    date: str = ""
    total_count: int = 0
    records: list[DragonTigerRecord] = field(default_factory=list)
    institutional_focus: list[dict] = field(default_factory=list)
    institutional_risk: list[dict] = field(default_factory=list)
    hot_money_track: list[dict] = field(default_factory=list)
    sector_flow: list[dict] = field(default_factory=list)
    overall_assessment: str = ""
    abnormal_patterns: list[dict] = field(default_factory=list)
    sector_divergence: list[dict] = field(default_factory=list)
    reason_summary: list[dict] = field(default_factory=list)
    industry_flow: list[dict] = field(default_factory=list)
    consecutive_listings: list[dict] = field(default_factory=list)
    tomorrow_watch: list[dict] = field(default_factory=list)
    total_net_buy: float = 0.0


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
