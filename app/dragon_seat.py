"""
龙虎榜席位分析引擎 —— 席位分类、资金属性识别、行为追踪

在原始龙虎榜数据基础上进行席位级别的深度挖掘：
  1. 席位类型分类（机构/沪深股通/知名游资/量化/散户/未知）
  2. 持仓股龙虎榜联动预警
  3. 席位质量评分（机构主导性、游资抱团、散户接盘风险）
  4. 连续上榜追踪

核心API：
  - stock_lhb_detail_em(start_date, end_date)  → 汇总数据（已用）
  - stock_lhb_stock_detail_date_em(symbol)      → 单只个股席位明细
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.utils import log


# ============================================================
# 席位类型枚举
# ============================================================

class SeatType:
    INSTITUTION = "机构专用"
    HSGT = "沪深股通"
    HOT_MONEY = "知名游资"
    QUANT = "量化席位"
    RETAIL = "散户席位"
    UNKNOWN = "普通席位"


# ============================================================
# 知名游资席位名录（持续维护）
# ============================================================

# 知名游资营业部名称关键词（部分匹配即可）
HOT_MONEY_KEYWORDS = [
    # 顶级游资
    "华泰证券上海武定路",
    "华泰证券上海共和新路",
    "华泰证券上海澳门路",
    "华泰证券上海黄河路",
    "国泰君安上海江苏路",
    "国泰君安上海分公司",
    "国泰君安宁波彩虹北路",
    "中信证券上海分公司",
    "中信证券上海溧阳路",
    "中信证券上海淮海中路",
    "中信证券上海东方路",
    "中信证券北京望京",
    "中信证券北京金融大街",
    "中国银河证券上海杨浦区",
    "中国银河证券北京中关村大街",
    "中国银河证券绍兴",
    "中国银河证券杭州凤起路",
    "申万宏源上海闵行区",
    "申万宏源上海浦东新区",
    "光大证券上海世纪大道",
    "光大证券宁波解放南路",
    "光大证券深圳金田路",
    "海通证券上海建国西路",
    "海通证券上海天平路",
    "海通证券南京广州路",
    "东方证券上海浦东新区",
    "东方证券上海杨浦区",
    "国信证券上海北京东路",
    "国盛证券宁波桑田路",
    "兴业证券陕西分公司",
    "兴业证券深圳分公司",
    "财通证券杭州上塘路",
    "财通证券绍兴袍江",
    "招商证券深圳招商证券大厦",
    "中泰证券上海花园石桥路",
    "方正证券重庆金开大道",
    "浙商证券杭州杭大路",
    "华鑫证券上海分公司",
    "华鑫证券上海茅台路",
    "长江证券上海东明路",
    "东莞证券北京分公司",
    "平安证券深圳深南大道",
    "华宝证券上海东大名路",
    "广发证券上海东方路",
    "国联证券上海邯郸路",
    "国金证券上海互联网证券分公司",
    "国海证券上海世纪大道",
    # 知名短线席位
    "东亚前海证券上海分公司",
    "上海证券苏州中心广场",
    "中信建投证券杭州庆春路",
    "中信建投证券北京东城分公司",
    "天风证券深圳分公司",
    "甬兴证券杭州分公司",
    "华林证券上海分公司",
    "国信证券深圳泰然九路",
]

# 东财拉萨营业部关键词（散户集中营）
RETAIL_LHASA_KEYWORDS = [
    "东方财富证券拉萨",
    "东方财富证券山南",
]

# 量化席位检测标准
QUANT_DETECTION_MIN_STOCKS = 3  # 同日出现 >= N 只不同的上榜股票
QUANT_DETECTION_RATIO_TOLERANCE = 0.3  # 买卖金额比在 ±30% 以内


# ============================================================
# 席位级数据模型
# ============================================================

@dataclass
class SeatDetail:
    """单个席位在单只股票上的买卖明细"""
    seat_name: str = ""
    seat_type: str = SeatType.UNKNOWN
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    net_amount: float = 0.0
    buy_rank: int = 0       # 买入排名(1-5)
    sell_rank: int = 0      # 卖出排名(1-5)


@dataclass
class SeatAnalysis:
    """单只个股的席位分析结果"""
    code: str = ""
    name: str = ""
    change_pct: float = 0.0
    total_buy: float = 0.0
    total_sell: float = 0.0
    net_buy: float = 0.0
    total_trade: float = 0.0
    reason: str = ""
    seats: list[SeatDetail] = field(default_factory=list)

    # 聚合指标
    institution_net: float = 0.0      # 机构净买入
    hsgt_net: float = 0.0             # 北向净买入
    hot_money_net: float = 0.0        # 游资净买入
    retail_net: float = 0.0           # 散户（拉萨）净买入
    quant_seat_count: int = 0         # 被标记为量化的席位数

    @property
    def institution_ratio(self) -> float:
        """机构资金占比"""
        if self.total_trade > 0:
            return abs(self.institution_net) / self.total_trade
        return 0.0

    @property
    def is_institution_driven(self) -> bool:
        """机构主导（净买入占比 > 30%）"""
        return self.institution_net > 0 and self.institution_ratio > 0.3

    @property
    def is_retail_dominated(self) -> bool:
        """散户接盘（拉萨净买入 > 机构净买入 且 拉萨为净买入、机构为净卖出）"""
        return (self.retail_net > 0 and self.institution_net < 0
                and abs(self.retail_net) > abs(self.institution_net))

    @property
    def is_hot_money_clustering(self) -> bool:
        """游资抱团（≥2 个游资席位且同向）"""
        hm_seats = [s for s in self.seats if s.seat_type == SeatType.HOT_MONEY]
        if len(hm_seats) < 2:
            return False
        hm_buy = sum(s.buy_amount for s in hm_seats)
        hm_sell = sum(s.sell_amount for s in hm_seats)
        return (hm_buy > hm_sell * 1.5) or (hm_sell > hm_buy * 1.5)

    @property
    def risk_flags(self) -> list[str]:
        """风险标记列表"""
        flags = []
        if self.is_retail_dominated:
            flags.append("散户接盘")
        if self.quant_seat_count >= 2:
            flags.append("量化主导")
        if (self.retail_net > 0 and self.hot_money_net < 0
                and abs(self.retail_net) > abs(self.hot_money_net)):
            flags.append("游资出货散户接")
        if self.institution_net < -self.total_trade * 0.2:
            flags.append("机构大额出逃")
        return flags

    @property
    def quality_label(self) -> str:
        """信号质量标签"""
        if self.is_institution_driven and not self.is_retail_dominated:
            return "高质"
        if self.is_hot_money_clustering and not self.is_retail_dominated:
            return "活跃"
        if self.risk_flags:
            return "风险"
        return "普通"


# ============================================================
# 席位分类
# ============================================================

def classify_seat(seat_name: str) -> str:
    """根据席位名称判断席位类型

    Args:
        seat_name: 席位名称（如 "机构专用"、"华泰证券上海武定路"）

    Returns:
        SeatType 常量之一
    """
    name = seat_name.strip()

    # 1. 机构专用
    if name == "机构专用":
        return SeatType.INSTITUTION

    # 2. 沪深股通
    if "深股通" in name or "沪股通" in name:
        return SeatType.HSGT

    # 3. 散户（东财拉萨）
    for kw in RETAIL_LHASA_KEYWORDS:
        if kw in name:
            return SeatType.RETAIL

    # 4. 知名游资
    for kw in HOT_MONEY_KEYWORDS:
        if kw in name:
            return SeatType.HOT_MONEY

    return SeatType.UNKNOWN


def detect_quant_seats(code_seat_map: dict[str, list[str]]) -> list[str]:
    """检测量化席位（同日在多个标的上等额操作）

    Args:
        code_seat_map: {股票代码: [席位名称列表]}

    Returns:
        被识别为量化席位的席位名称列表
    """
    # 统计每个席位出现在多少只股票中
    seat_stock_count: dict[str, int] = {}
    for code, seats in code_seat_map.items():
        for seat_name in set(seats):  # 同只股票出现多次只算一次
            seat_stock_count[seat_name] = seat_stock_count.get(seat_name, 0) + 1

    # 出现在 >= N 只不同股票上的席位 → 疑似量化
    quant_candidates = [
        name for name, count in seat_stock_count.items()
        if count >= QUANT_DETECTION_MIN_STOCKS
    ]

    return quant_candidates


# ============================================================
# 龙虎榜历史持久化（连续上榜追踪）
# ============================================================

def _get_history_path():
    from pathlib import Path
    state_dir = Path(__file__).resolve().parent.parent / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "dragon_tiger_history.json"


def _load_history() -> dict[str, list]:
    """加载龙虎榜历史记录"""
    import json
    path = _get_history_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_history(records: list, max_days: int = 20):
    """保存龙虎榜历史记录（保留最近 N 个交易日）"""
    import json

    today = datetime.now().strftime("%Y-%m-%d")
    history = _load_history()

    # 添加今日记录
    history[today] = [
        {
            "code": r.code,
            "name": r.name,
            "net_buy": r.net_buy,
            "change_pct": r.change_pct,
            "total_trade": r.total_trade,
            "reason": r.reason,
        }
        for r in records
    ]

    # 只保留最近 max_days 个交易日
    keys = sorted(history.keys(), reverse=True)[:max_days]
    trimmed = {k: history[k] for k in keys}

    try:
        with open(_get_history_path(), "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"龙虎榜历史保存失败: {e}")


def detect_consecutive_listings(
    records: list,
) -> list[dict]:
    """检测连续上榜个股（连续2天以上出现在龙虎榜中）

    Returns:
        连续上榜信息列表：
        [{code, name, consecutive_days, history: [{date, net_buy, change_pct}]}, ...]
    """
    # 保存本次数据
    _save_history(records)

    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    history = _load_history()

    results = []
    for r in records:
        code = r.code
        # 查找历史记录中连续出现该代码的日期
        past_entries = []
        for date_str in sorted(history.keys(), reverse=True):
            if date_str == today:
                continue
            day_records = history.get(date_str, [])
            matched = next((d for d in day_records if d.get("code") == code), None)
            if matched:
                past_entries.append(matched)
            else:
                break  # 不连续了就停

        if not past_entries:
            continue

        last = past_entries[0]
        # 验证是连续的上个交易日（跳过周末）
        if last.get("date", "") != yesterday:
            # 允许跳过周末
            last_date = datetime.strptime(last.get("date", ""), "%Y-%m-%d")
            days_gap = (datetime.now() - last_date).days
            if days_gap > 3:  # 超过3天就不是连续了
                continue

        # 检测游资接力（净买入方向前后相反）
        prev_net = past_entries[0].get("net_buy", 0)
        is_relay = (prev_net > 0) != (r.net_buy > 0) if prev_net != 0 else False

        results.append({
            "code": code,
            "name": r.name,
            "consecutive_days": len(past_entries) + 1,
            "prev_entries": [
                {"date": e.get("date", ""), "net_buy": e.get("net_buy", 0),
                 "change_pct": e.get("change_pct", 0)}
                for e in past_entries[:3]
            ],
            "is_relay": is_relay,
            "relay_note": "🔄 游资接力（方向反转）" if is_relay else (
                "→ 同向加仓" if prev_net * (r.net_buy or 0) > 0 else ""),
        })

    if results:
        log.info(f"连续上榜: {len(results)}只 (含{sum(1 for r in results if r['is_relay'])}只方向转换)")

    return results

def fetch_seat_details(code: str) -> list[SeatDetail]:
    """获取单只股票的龙虎榜席位明细

    使用 AKShare stock_lhb_stock_detail_date_em(symbol) 获取席位级买卖数据。

    Args:
        code: 股票代码（如 "002594"）

    Returns:
        SeatDetail 列表
    """
    try:
        import akshare as ak
    except ImportError:
        return []

    try:
        df = ak.stock_lhb_stock_detail_date_em(symbol=str(code))
    except Exception as e:
        log.debug(f"席位明细获取失败 {code}: {e}")
        return []

    if df is None or df.empty:
        return []

    # 先收集所有席位名用于量化检测（需要跨股票对比，调用方负责）
    seats = []
    quant_names: list[str] = []  # 在同日所有股票间共享

    for _, row in df.iterrows():
        seat_name = str(row.get('营业部名称', row.get('席位名称', '')))
        if not seat_name:
            continue

        buy = _safe_float(row.get('买入金额', row.get('买入额')))
        sell = _safe_float(row.get('卖出金额', row.get('卖出额')))
        buy_rank = _safe_int(row.get('买入排名', 0))
        sell_rank = _safe_int(row.get('卖出排名', 0))

        seat_type = classify_seat(seat_name)
        net = (buy or 0) - (sell or 0)

        seats.append(SeatDetail(
            seat_name=seat_name,
            seat_type=seat_type,
            buy_amount=buy or 0,
            sell_amount=sell or 0,
            net_amount=net,
            buy_rank=buy_rank,
            sell_rank=sell_rank,
        ))

    return seats


# ============================================================
# 单股席位分析
# ============================================================

def analyze_seats(
    code: str,
    name: str,
    change_pct: float,
    total_buy: float,
    total_sell: float,
    net_buy: float,
    total_trade: float,
    reason: str,
    seats: list[SeatDetail],
    quant_seat_names: list[str],
) -> SeatAnalysis:
    """对单只股票的席位数据进行综合分析

    Args:
        quant_seat_names: 同日被检测为量化的席位名列表（跨股票共享）

    Returns:
        SeatAnalysis 对象
    """
    analysis = SeatAnalysis(
        code=code,
        name=name,
        change_pct=change_pct,
        total_buy=total_buy,
        total_sell=total_sell,
        net_buy=net_buy,
        total_trade=total_trade,
        reason=reason,
        seats=seats,
    )

    # 聚合各类席位资金
    for s in seats:
        # 标记量化席位
        if s.seat_name in quant_seat_names:
            analysis.quant_seat_count += 1
            continue

        if s.seat_type == SeatType.INSTITUTION:
            analysis.institution_net += s.net_amount
        elif s.seat_type == SeatType.HSGT:
            analysis.hsgt_net += s.net_amount
        elif s.seat_type == SeatType.HOT_MONEY:
            analysis.hot_money_net += s.net_amount
        elif s.seat_type == SeatType.RETAIL:
            analysis.retail_net += s.net_amount

    return analysis


# ============================================================
# 批量分析入口
# ============================================================

def analyze_dragon_tiger_seats(
    records: list,
    max_seat_fetch: int = 30,
) -> list[SeatAnalysis]:
    """对当日全部龙虎榜个股进行席位级分析

    Args:
        records: DragonTigerRecord 列表（来自 fetch_dragon_tiger_list）
        max_seat_fetch: 最多拉取多少只个股的席位明细（控制API调用量）

    Returns:
        SeatAnalysis 列表，按质量排序
    """
    if not records:
        return []

    # 限制深度分析数量（活跃个股优先）
    target = sorted(records[:max_seat_fetch], key=lambda r: r.total_trade, reverse=True)
    log.info(f"席位分析: 对 {len(target)} 只龙虎榜活跃个股进行席位级扫描...")

    # 第1遍：收集所有席位名称（用于量化检测）
    all_seat_names: dict[str, list[str]] = {}
    analysis_map: dict[str, SeatAnalysis] = {}
    import time

    for i, record in enumerate(target):
        code = record.code
        seats = fetch_seat_details(code)
        if seats:
            all_seat_names[code] = [s.seat_name for s in seats]

        # 创建分析对象（先不标记量化）
        sa = SeatAnalysis(
            code=code,
            name=record.name,
            change_pct=record.change_pct or 0,
            total_buy=record.total_buy,
            total_sell=record.total_sell,
            net_buy=record.net_buy,
            total_trade=record.total_trade,
            reason=record.reason,
            seats=seats,
        )

        # 聚合初步资金
        for s in seats:
            if s.seat_type == SeatType.INSTITUTION:
                sa.institution_net += s.net_amount
            elif s.seat_type == SeatType.HSGT:
                sa.hsgt_net += s.net_amount
            elif s.seat_type == SeatType.HOT_MONEY:
                sa.hot_money_net += s.net_amount
            elif s.seat_type == SeatType.RETAIL:
                sa.retail_net += s.net_amount

        analysis_map[code] = sa

        if (i + 1) % 10 == 0:
            log.info(f"  席位扫描: {i+1}/{len(target)}")

        time.sleep(0.3)

    # 第2遍：检测量化席位
    quant_names = detect_quant_seats(all_seat_names)
    if quant_names:
        log.info(f"检测到 {len(quant_names)} 个量化席位: {', '.join(quant_names[:5])}...")

        # 重新标记受影响的个股
        for code, sa in analysis_map.items():
            quant_count = sum(1 for s in sa.seats if s.seat_name in quant_names)
            sa.quant_seat_count = quant_count

    # 排序：风险标记少的优先，机构主导的优先
    results = list(analysis_map.values())
    # 负分排后面（有风险标记的），正分排前面（机构主导的）
    def _sort_key(sa: SeatAnalysis) -> tuple:
        risk_penalty = len(sa.risk_flags) * -20
        inst_bonus = 10 if sa.is_institution_driven else 0
        hm_bonus = 5 if sa.is_hot_money_clustering else 0
        return (risk_penalty + inst_bonus + hm_bonus, sa.institution_net)

    results.sort(key=_sort_key, reverse=True)
    return results


# ============================================================
# 持仓联动检测
# ============================================================

def check_holdings_dragon_tiger(
    seat_analyses: list[SeatAnalysis],
    holding_codes: set[str],
) -> list[dict]:
    """检测持仓股是否上榜龙虎榜

    Args:
        seat_analyses: 席位分析结果列表
        holding_codes: 持仓股票代码集合

    Returns:
        预警列表，每项包含股票信息 + 席位分析 + 操作建议
    """
    alerts = []
    for sa in seat_analyses:
        if sa.code not in holding_codes:
            continue

        alert = {
            'code': sa.code,
            'name': sa.name,
            'change_pct': sa.change_pct,
            'net_buy': sa.net_buy,
            'reason': sa.reason,
            'institution_net': sa.institution_net,
            'hsgt_net': sa.hsgt_net,
            'hot_money_net': sa.hot_money_net,
            'retail_net': sa.retail_net,
            'institution_driven': sa.is_institution_driven,
            'retail_dominated': sa.is_retail_dominated,
            'hot_money_clustering': sa.is_hot_money_clustering,
            'risk_flags': sa.risk_flags,
            'quality': sa.quality_label,
            'top_seats': [
                f"{s.seat_name}({s.seat_type}, 净{s.net_amount/1e4:+.0f}万)"
                for s in sorted(sa.seats, key=lambda x: abs(x.net_amount), reverse=True)[:5]
            ],
        }

        # 生成操作建议
        suggestions = []
        if sa.is_retail_dominated:
            suggestions.append("⚠️ 散户接盘 — 如持有建议减仓，不宜追涨")
        if sa.is_institution_driven:
            suggestions.append("📈 机构主导买入 — 可持有观察，回踩加仓")
        if sa.is_hot_money_clustering and not sa.is_retail_dominated:
            suggestions.append("🐉 游资抱团 — 次日出货概率高，做好T+1止盈准备")
        if sa.institution_net < -sa.total_trade * 0.2:
            suggestions.append("🚨 机构大额出逃 — 建议减仓避险")
        alert['suggestions'] = suggestions

        alerts.append(alert)

    return alerts


# ============================================================
# 工具函数
# ============================================================

def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val, default=0):
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


# ============================================================
# 报告生成
# ============================================================

def generate_seat_report(analyses: list[SeatAnalysis]) -> str:
    """生成席位级龙虎榜深度分析报告（Markdown）

    Args:
        analyses: SeatAnalysis 列表（已排序）

    Returns:
        Markdown 格式报告
    """
    now = datetime.now()
    lines = [
        f"## 🐉 席位级龙虎榜深度分析（{now.strftime('%Y-%m-%d')}）",
        f"",
        f"**分析样本**: {len(analyses)} 只上榜个股",
        f"**席位分类**: 机构专用 | 沪深股通 | 知名游资 | 量化 | 散户(拉萨) | 普通",
        f"",
    ]

    # ---- 高质信号 ----
    quality = [a for a in analyses if a.quality_label == "高质"]
    if quality:
        lines.append("### 🟢 机构主导买入（高质信号）")
        lines.append("")
        lines.append("| 个股 | 涨跌幅 | 机构净买 | 北向净买 | 游资净买 | 席位详情 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
        for a in quality[:10]:
            inst = f"{a.institution_net/1e4:+.0f}万" if a.institution_net else "--"
            hsgt = f"{a.hsgt_net/1e4:+.0f}万" if a.hsgt_net else "--"
            hm = f"{a.hot_money_net/1e4:+.0f}万" if a.hot_money_net else "--"
            top = " | ".join([
                f"{s.seat_name}({s.seat_type})"
                for s in sorted(a.seats, key=lambda x: abs(x.net_amount), reverse=True)[:3]
            ])
            lines.append(f"| {a.name}({a.code}) | {a.change_pct:+.1f}% | {inst} | {hsgt} | {hm} | {top} |")
        lines.append("")

    # ---- 活跃信号 ----
    active = [a for a in analyses if a.quality_label == "活跃" and a not in quality]
    if active:
        lines.append("### 🟡 游资抱团（活跃信号）")
        lines.append("")
        lines.append("| 个股 | 涨跌幅 | 游资净买 | 机构净买 | 席位详情 |")
        lines.append("| --- | ---: | ---: | ---: | --- |")
        for a in active[:10]:
            hm = f"{a.hot_money_net/1e4:+.0f}万" if a.hot_money_net else "--"
            inst = f"{a.institution_net/1e4:+.0f}万" if a.institution_net else "--"
            hm_seats = [s for s in a.seats if s.seat_type == SeatType.HOT_MONEY]
            top = " | ".join([f"{s.seat_name}(净{s.net_amount/1e4:+.0f}万)" for s in hm_seats[:3]])
            lines.append(f"| {a.name}({a.code}) | {a.change_pct:+.1f}% | {hm} | {inst} | {top} |")
        lines.append("")

    # ---- 风险信号 ----
    risky = [a for a in analyses if a.risk_flags]
    if risky:
        lines.append("### 🔴 风险标记")
        lines.append("")
        for a in risky[:15]:
            flags = ", ".join(a.risk_flags)
            inst = f"机构{a.institution_net/1e4:+.0f}万" if a.institution_net else ""
            retail = f"散户{a.retail_net/1e4:+.0f}万" if a.retail_net else ""
            detail = f"{inst} {retail}".strip()
            lines.append(f"- **{a.name}**({a.code}) {a.change_pct:+.1f}% — {flags} ({detail})")
        lines.append("")

    # ---- 量化席位汇总 ----
    quant_stocks = [a for a in analyses if a.quant_seat_count > 0]
    if quant_stocks:
        lines.append(f"### ⚪ 量化参与 ({len(quant_stocks)}只)")
        lines.append("")
        lines.append(f"以下个股有量化席位参与（方向性信号需谨慎对待）：")
        for a in quant_stocks[:10]:
            lines.append(f"- {a.name}({a.code}) — {a.quant_seat_count}个量化席位")
        lines.append("")

    lines.append("---")
    lines.append(f"*席位分析由盯盘雷达自动生成 · {now.strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")
    lines.append("*风险提示：龙虎榜席位分析基于公开数据，席位分类依赖固有名录匹配。量化检测为模式推断，可能存在误判。操作决策请结合基本面判断。*")

    return "\n".join(lines)
