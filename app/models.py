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
    industry: str = ""  # 所属行业板块


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
    industry: str = ""  # 所属行业板块


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

    @property
    def is_mid_capital_active(self) -> bool:
        """中单活跃（游资/私募/大户主导，中单净流入占比 > 50%）"""
        if self.medium_net is None or self.main_net is None:
            return False
        total = abs(self.medium_net) + abs(self.main_net)
        if total == 0:
            return False
        return abs(self.medium_net) / total > 0.5

    @property
    def is_institution_absorbing(self) -> bool:
        """机构吸筹深化（超大单净流入，且中单+小单都净流出）"""
        if self.super_large_net is None or self.medium_net is None or self.small_net is None:
            return False
        return (self.super_large_net > 0
                and self.medium_net < 0
                and self.small_net < 0)

    @property
    def flow_structure(self) -> str:
        """资金结构标签"""
        if not self.is_valid:
            return "无数据"
        if self.is_institution_absorbing:
            return "机构主导(中小资金出逃)"
        if self.is_institution_driven:
            return "机构主导"
        if self.is_distribution:
            return "机构出货"
        if self.is_mid_capital_active:
            return "游资活跃"
        if self.is_retail_driven:
            return "散户主导"
        if self.main_net and self.main_net > 0:
            return "主力偏多"
        if self.main_net and self.main_net < 0:
            return "主力偏空"
        return "均衡"

    @property
    def total_net(self) -> Optional[float]:
        """总体净流入（元）= 超大单 + 大单 + 中单 + 小单（全部订单类型）

        仅当四档分类都齐全时才返回合计值；新浪兜底数据缺少大/中/小单，
        此时返回 None（无法得出可信的总体净流入）。
        """
        if None in (self.super_large_net, self.large_net, self.medium_net, self.small_net):
            return None
        return (self.super_large_net + self.large_net
                + self.medium_net + self.small_net)

    @property
    def total_pct(self) -> Optional[float]:
        """总体净流入占成交额比例（%）= 各档净占比之和

        仅当四档净占比都齐全时才返回；新浪兜底数据无占比字段，返回 None。
        """
        if None in (self.super_large_pct, self.large_pct, self.medium_pct, self.small_pct):
            return None
        return (self.super_large_pct + self.large_pct
                + self.medium_pct + self.small_pct)


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
    avg_price: Optional[float] = None  # 分时均价 = 成交额/成交量（日内VWAP）
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
    industry: str = ""  # 所属行业板块（从东方财富行业分类获取）


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
    priority: bool = False  # 是否包含高优先级资金流提醒（转向/背离），推送与展示时排最前

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
class MarginData:
    """两融数据（融资融券余额，替代已停止披露的北向资金）

    Attributes:
        financing_balance: 融资余额（亿元）
        financing_net_buy: 融资净买入（亿元，当日）
        securities_lending_balance: 融券余额（亿元）
        total_balance: 两融总余额（亿元）
        date: 数据日期
    """
    financing_balance: float = 0.0
    financing_net_buy: float = 0.0
    securities_lending_balance: float = 0.0
    total_balance: float = 0.0
    date: str = ""

    @property
    def financing_change_direction(self) -> str:
        """融资净买入方向"""
        if self.financing_net_buy > 0:
            return "融资加仓"
        elif self.financing_net_buy < 0:
            return "融资减仓"
        return "持平"


@dataclass
class StockMarginData:
    """个股两融明细（融资融券，日频，T+1 披露）

    与 MarginData（全市场汇总）不同，本模型是逐标的的两融数据，
    用于判断杠杆资金对单只股票的加/减仓。

    Attributes:
        code: 证券代码（6 位）
        name: 证券简称
        financing_balance: 融资余额（元）
        financing_net_buy: 融资净买入（元，最新日余额 - 前一日余额）
        securities_lending_balance: 融券余额（元）
        securities_lending_volume: 融券余量（股）
        date: 数据日期
    """
    code: str = ""
    name: str = ""
    financing_balance: float = 0.0
    financing_net_buy: float = 0.0
    securities_lending_balance: float = 0.0
    securities_lending_volume: float = 0.0
    date: str = ""

    @property
    def financing_change_direction(self) -> str:
        """融资净买入方向"""
        if self.financing_net_buy > 0:
            return "融资加仓"
        elif self.financing_net_buy < 0:
            return "融资减仓"
        return "持平"


@dataclass
class SectorBoard:
    """行业板块实时数据

    Attributes:
        code: 板块代码（如 BK0477）
        name: 板块名称（如 "半导体"）
        change_pct: 板块涨跌幅 (%)
        amount: 成交额（元）
        leader_stock: 领涨股名称
        leader_change_pct: 领涨股涨跌幅
        main_net_inflow: 板块主力净流入（元）
        stock_count: 板块成分股数量
    """
    code: str = ""
    name: str = ""
    change_pct: Optional[float] = None
    amount: Optional[float] = None
    leader_stock: str = ""
    leader_change_pct: Optional[float] = None
    main_net_inflow: Optional[float] = None
    stock_count: int = 0

    @property
    def change_direction(self) -> str:
        """涨跌方向"""
        if self.change_pct is None:
            return "平"
        return "涨" if self.change_pct > 0 else ("跌" if self.change_pct < 0 else "平")


@dataclass
class SectorFundFlow:
    """板块资金流排名快照（东财数据中心-资金流向-板块资金流）

    Attributes:
        name: 板块名称（如 "半导体"）
        change_pct: 涨跌幅（%）
        main_net: 主力净流入（元）
        main_pct: 主力净流入净占比（%）
        top_stock: 主力净流入最大股名称
    """
    name: str = ""
    change_pct: Optional[float] = None
    main_net: Optional[float] = None
    main_pct: Optional[float] = None
    top_stock: str = ""


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

    A股交易时段: 9:30-11:30 (120min) + 13:00-15:00 (120min) + 盘后15:00-15:30 (30min) = 270min
    """
    if cumulative_amount <= 0:
        return 0.0

    if now is None:
        from datetime import datetime
        now = datetime.now()

    # 收盘后（15:30 盘后结束）→ 实际值
    if now.hour >= 16 or (now.hour == 15 and now.minute >= 30):
        return cumulative_amount

    # 计算已流逝的交易分钟数
    elapsed = _trading_minutes_elapsed(now)
    if elapsed <= 0:
        return cumulative_amount  # 开盘前不推算

    # 线性外推
    ratio = 270 / elapsed
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
    close = 15 * 60 + 30  # 盘后交易到 15:30

    if current >= close:
        return 270
    if current >= afternoon_start:
        return 120 + min(current - afternoon_start, 150)
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
    obv_signal: str = ""  # OBV 信号
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
    # 关键位动态行为分析
    has_resistance_rejection: bool = False
    resistance_rejection_detail: str = ""
    has_support_confirmation: bool = False
    support_confirmation_detail: str = ""
    has_support_breakdown: bool = False
    support_breakdown_detail: str = ""
    has_breakout_retest: bool = False
    breakout_retest_detail: str = ""
    support_strength: str = ""       # "强" / "中" / "弱"
    resistance_strength: str = ""    # "强" / "中" / "弱"
    strength_summary: str = ""
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
# 智能选股模型
# ============================================================

@dataclass
class FundFlowDaily:
    """
    单日主力资金流快照（多日序列的一环）

    Attributes:
        date: 日期（YYYY-MM-DD）
        main_net: 主力净流入（元）= 超大单 + 大单
        large_net: 大单净流入（元），通常代表游资/私募
        super_large_net: 超大单净流入（元），通常代表机构/国家队
        main_pct: 主力净流入占成交额比例 (%)
    """
    date: str = ""
    main_net: Optional[float] = None
    large_net: Optional[float] = None
    super_large_net: Optional[float] = None
    main_pct: Optional[float] = None


@dataclass
class AccumulationScore:
    """
    综合评分结果（0-100）= 持续低吸子分(50%) + 估值分位子分(50%)

    持续低吸维度：主力连续净流入但股价横盘/微跌（背离），筹码在低位悄悄集中
    估值分位维度：主力净流入强度（占流通市值）+ PE-TTM 历史百分位（越低越便宜）

    Attributes:
        code: 股票代码
        name: 股票名称
        score: 综合评分（0-100）
        label: 评级标签（强吸筹+低估值 / 吸筹+估值适中 / 中性 / 偏弱 / 出货 / 无数据）
        inflow_days: 窗口内主力净流入天数
        consecutive_days: 最近连续主力净流入天数
        total_net: 窗口累计主力净流入额（元），缺省回退当日口径
        price_change_10d: 窗口股价涨跌幅（%），用于算背离度
        divergence: 背离度（0-1），资金净流入 + 股价滞涨 → 越高越背离
        large_ratio_rising: 大单+超大单占比近 5 日是否较前 5 日上升（机构/大户吸筹）
        inflow_strength_pct: 主力净流入占流通市值比例（%）
        valuation_status: 估值状态（估值较低/适中/较高，妙想分类）
        valuation_percentile: PE-TTM 历史百分位（0-100，越小越便宜）
        notes: 判定说明（供报告/LLM 引用）
    """
    code: str = ""
    name: str = ""
    score: float = 0.0
    label: str = "中性"
    inflow_days: int = 0
    consecutive_days: int = 0
    total_net: Optional[float] = None
    price_change_10d: Optional[float] = None
    divergence: Optional[float] = None
    large_ratio_rising: bool = False
    inflow_strength_pct: Optional[float] = None
    valuation_status: str = ""
    valuation_percentile: Optional[float] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ScreeningCondition:
    """
    LLM 生成的妙想选股条件（对应一个热点板块）

    Attributes:
        sector: 热点板块名（LLM 归一化后的最终板块名）
        condition: 妙想可执行的自然语言条件，带板块限定，方向=资金流入+低估值
        intent: 策略意图说明
        risk_note: 风险提示
    """
    sector: str = ""
    condition: str = ""
    intent: str = ""
    risk_note: str = ""


@dataclass
class ScreeningCandidate:
    """
    智能选股候选（妙想筛出 + 资金流入+估值分位评分）

    Attributes:
        code: 股票代码
        name: 股票名称
        market: 市场标识（SH/SZ）
        price: 最新价（妙想返回，可能缺失）
        change_pct: 涨跌幅（%）
        last_price: 最新收盘价（K线末尾，供 LLM 引用，避免臆造价格）
        ma20: 20日均线
        support: 主要支撑位
        resistance: 主要压力位
        flow_days: 妙想选股自带的多日主力净额序列（省去东财 daykline 取数）
        main_net: 主力净流入额（元）
        circulation_value: 流通市值（元），用于算净流入强度
        valuation_status: 估值状态（估值较低/适中/较高）
        valuation_percentile: PE-TTM 历史百分位（0-100，越小越便宜）
        industry: 东财行业总分类（黑名单硬过滤用）
        concept: 概念题材
        hot_sectors: 命中的热点板块
        hit_conditions: 命中的妙想条件文本
        resonance: 命中条件数（共振加分）
        accumulation: 综合评分结果（持续低吸 + 估值分位）
        tech_signals: 技术面信号（RSI/MACD/均线等）
        blacklisted: 是否被黑名单过滤
        rank: 最终排名
        grade: 关注分级（强关注/关注/风险/剔除）
    """
    code: str = ""
    name: str = ""
    market: str = ""
    price: Optional[float] = None
    change_pct: Optional[float] = None
    last_price: Optional[float] = None
    ma20: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    flow_days: list[FundFlowDaily] = field(default_factory=list)
    main_net: Optional[float] = None
    circulation_value: Optional[float] = None
    valuation_status: str = ""
    valuation_percentile: Optional[float] = None
    industry: str = ""
    concept: str = ""
    hot_sectors: list[str] = field(default_factory=list)
    hit_conditions: list[str] = field(default_factory=list)
    resonance: int = 0
    accumulation: Optional[AccumulationScore] = None
    tech_signals: list[str] = field(default_factory=list)
    blacklisted: bool = False
    rank: int = 0
    grade: str = ""


@dataclass
class ScreeningReport:
    """
    智能选股最终报告

    Attributes:
        date: 报告日期
        hot_sectors: 选中的板块名（已排除黑名单）
        accumulating_sectors: 资金潜伏板块（东财板块资金流排名，含净流入/涨幅）
        conditions: 选股条件列表
        candidates: 候选列表（已按综合分排序、过滤黑名单）
        llm_analysis: LLM 综合排序解读
        degraded: 是否降级（妙想/LLM 失败回退技术面筛选）
        error: 错误信息（若有）
    """
    date: str = ""
    hot_sectors: list[str] = field(default_factory=list)
    accumulating_sectors: list[SectorFundFlow] = field(default_factory=list)
    conditions: list[ScreeningCondition] = field(default_factory=list)
    candidates: list[ScreeningCandidate] = field(default_factory=list)
    llm_analysis: str = ""
    degraded: bool = False
    error: str = ""


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
