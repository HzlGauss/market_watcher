"""
龙虎榜数据抓取与分析模块 —— AKShare (东方财富数据源)

抓取每日龙虎榜数据，分析大资金动向：
1. 机构席位买卖方向 → 关注/风险个股
2. 沪深股通席位动向 → 外资偏好
3. 知名游资追踪 → 短线情绪指标
4. 板块资金汇总 → 板块轮动信号
5. 买卖力量对比 → 个股多空判断
"""

from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional

from app.models import DragonTigerRecord, DragonTigerSummary
from app.utils import log

# ============================================================
# 常量
# ============================================================

# 净买入额阈值（元）—— 超过此值视为重点关注
BUY_THRESHOLD = 10_000_000  # 1000万

# 净卖出额阈值（元）—— 超过此值视为风险
SELL_THRESHOLD = -10_000_000  # -1000万


# ============================================================
# 数据抓取
# ============================================================

def fetch_dragon_tiger_list(max_count: int = 30) -> list[DragonTigerRecord]:
    """获取每日龙虎榜上榜个股列表

    通过 AKShare 调用东方财富龙虎榜详情接口，
    获取当日全部上榜个股及其交易数据。

    Args:
        max_count: 最大返回条数

    Returns:
        DragonTigerRecord 列表
    """
    try:
        import akshare as ak
        import pandas as pd
    except ImportError:
        log.debug("AKShare 未安装，无法获取龙虎榜数据")
        return []

    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    try:
        df = ak.stock_lhb_detail_em(start_date=yesterday, end_date=today)
    except Exception as e:
        log.warning(f"龙虎榜数据获取失败: {e}")
        return []

    if df is None or df.empty:
        log.info("龙虎榜无数据（可能非交易日）")
        return []

    records = []
    for _, row in df.iterrows():
        try:
            record = DragonTigerRecord(
                code=str(row.get("代码", "")),
                name=str(row.get("名称", "")),
                change_pct=_parse_float(row.get("涨跌幅")),
                total_buy=float(row.get("龙虎榜买入额", 0) or 0),
                total_sell=float(row.get("龙虎榜卖出额", 0) or 0),
                net_buy=float(row.get("龙虎榜净买额", 0) or 0),
                total_trade=float(row.get("龙虎榜成交额", 0) or 0),
                turnover_rate=_parse_float(row.get("换手率")),
                reason=str(row.get("上榜原因", "")),
            )
            records.append(record)
        except Exception:
            continue

        if len(records) >= max_count:
            break

    log.info(f"龙虎榜数据获取成功: {len(records)} 条")
    return records


def _parse_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(val)
        return v
    except (ValueError, TypeError):
        return None


def _get_stock_industry_map(codes: list[str]) -> dict[str, str]:
    """批量获取个股所属行业

    通过 AKShare 获取A股实时行情，建立 code->industry 映射。

    Args:
        codes: 股票代码列表

    Returns:
        {code: industry} 映射字典
    """
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        code_map = {}
        for _, row in df.iterrows():
            code = str(row.get("代码", ""))
            if code in codes:
                code_map[code] = str(row.get("行业", ""))
        return code_map
    except Exception:
        return {}


# ============================================================
# 分析逻辑
# ============================================================

def _classify_capital_flow(records: list[DragonTigerRecord]) -> tuple[list[dict], list[dict]]:
    """从龙虎榜数据中识别资金流向

    基于龙虎榜净买入额 + 买卖金额比，综合判断资金态度。

    Returns:
        (focus_list, risk_list)
        focus_list: 值得关注的个股列表（资金净买入，按净额排序）
        risk_list: 有风险的个股列表（资金净卖出，按净额排序）
    """
    focus_list = []
    risk_list = []

    for record in records:
        net_buy = record.net_buy
        buy_sell_ratio = record.buy_sell_ratio

        item = {
            "code": record.code,
            "name": record.name,
            "net_buy": net_buy,
            "buy_sell_ratio": buy_sell_ratio,
            "change_pct": record.change_pct,
            "turnover_rate": record.turnover_rate,
            "total_trade": record.total_trade,
            "reason": record.reason,
        }

        if net_buy > BUY_THRESHOLD and (buy_sell_ratio or 0) > 1:
            focus_list.append(item)
        elif net_buy < SELL_THRESHOLD and (buy_sell_ratio or 1) < 0.8:
            risk_list.append(item)

    focus_list.sort(key=lambda x: x["net_buy"], reverse=True)
    risk_list.sort(key=lambda x: x["net_buy"])

    return focus_list, risk_list


def _track_hot_money(records: list[DragonTigerRecord]) -> list[dict]:
    """追踪短线资金（游资）动向

    基于换手率和涨跌幅判断游资行为：
    - 涨停 + 高换手 → 游资拉升
    - 跌停 + 高换手 → 游资出逃
    - 高换手 + 低净买入 → 游资对倒

    Returns:
        游资动向列表
    """
    tracking = []

    for record in records:
        if record.turnover_rate is None or record.change_pct is None:
            continue

        if record.turnover_rate > 15:
            if record.change_pct > 7:
                tracking.append({
                    "code": record.code,
                    "name": record.name,
                    "signal": "游资拉升",
                    "change_pct": record.change_pct,
                    "turnover_rate": record.turnover_rate,
                    "net_buy": record.net_buy,
                    "detail": f"涨停+高换手({record.turnover_rate:.1f}%)，游资主导拉升",
                })
            elif record.change_pct < -7:
                tracking.append({
                    "code": record.code,
                    "name": record.name,
                    "signal": "游资出逃",
                    "change_pct": record.change_pct,
                    "turnover_rate": record.turnover_rate,
                    "net_buy": record.net_buy,
                    "detail": f"跌停+高换手({record.turnover_rate:.1f}%)，游资出逃⚠️",
                })
            elif abs(record.net_buy) < record.total_trade * 0.05 and record.total_trade > 0:
                tracking.append({
                    "code": record.code,
                    "name": record.name,
                    "signal": "游资对倒",
                    "change_pct": record.change_pct,
                    "turnover_rate": record.turnover_rate,
                    "net_buy": record.net_buy,
                    "detail": f"高换手({record.turnover_rate:.1f}%)但净买入仅{record.net_buy/1e4:.0f}万，游资对倒",
                })

    return tracking


def _aggregate_by_reason(records: list[DragonTigerRecord]) -> list[dict]:
    """按上榜原因汇总

    分析不同类型上榜原因的分布，判断市场焦点。

    Returns:
        上榜原因汇总列表
    """
    reason_map: dict[str, dict] = {}

    for record in records:
        reason = record.reason or "未知"
        if reason not in reason_map:
            reason_map[reason] = {
                "reason": reason,
                "count": 0,
                "total_net_buy": 0.0,
                "stocks": [],
            }

        info = reason_map[reason]
        info["count"] += 1
        info["total_net_buy"] += record.net_buy
        info["stocks"].append(record.name)

    result = sorted(reason_map.values(), key=lambda x: x["count"], reverse=True)
    for info in result:
        info.pop("stocks", None)

    return result


def _aggregate_sector_flow(records: list[DragonTigerRecord]) -> list[dict]:
    """汇总板块资金流向

    从上榜原因中提取板块信息，汇总资金流入/流出情况。

    Returns:
        板块资金流向列表（按净流入排序）
    """
    sector_map: dict[str, dict] = {}

    for record in records:
        reason = record.reason or ""
        sector = _extract_sector_from_reason(reason)
        if not sector:
            sector = "其他"

        if sector not in sector_map:
            sector_map[sector] = {
                "industry": sector,
                "total_net_buy": 0.0,
                "total_trade": 0.0,
                "stock_count": 0,
                "positive_count": 0,
                "negative_count": 0,
            }

        info = sector_map[sector]
        info["total_net_buy"] += record.net_buy
        info["total_trade"] += record.total_trade
        info["stock_count"] += 1
        if record.net_buy >= 0:
            info["positive_count"] += 1
        else:
            info["negative_count"] += 1

    result = sorted(sector_map.values(), key=lambda x: x["total_net_buy"], reverse=True)
    return result


def _extract_sector_from_reason(reason: str) -> str:
    """从上榜原因中提取板块/行业关键词"""
    keywords = {
        "ST": "ST板块",
        "新股": "新股",
        "无价格涨跌幅限制": "新股",
        "连续三个交易日": "连板股",
    }
    for kw, sector in keywords.items():
        if kw in reason:
            return sector
    return "主板"


def analyze_dragon_tiger(records: list[DragonTigerRecord]) -> DragonTigerSummary:
    """综合分析龙虎榜数据

    对龙虎榜数据进行多维度分析，输出结构化的分析结果。

    Args:
        records: 龙虎榜记录列表

    Returns:
        DragonTigerSummary 综合分析结果
    """
    if not records:
        return DragonTigerSummary(
            date=datetime.now().strftime("%Y-%m-%d"),
            total_count=0,
            overall_assessment="今日无龙虎榜数据（可能非交易日）",
        )

    focus_list, risk_list = _classify_capital_flow(records)
    hot_money = _track_hot_money(records)
    sector_flow = _aggregate_sector_flow(records)
    reason_summary = _aggregate_by_reason(records)

    top_focus = focus_list[:8]
    top_risk = risk_list[:5]
    top_sectors = sector_flow[:6]

    assessment_parts = []

    if top_focus:
        focus_names = "、".join(f"{s['name']}" for s in top_focus[:3])
        top_net = top_focus[0]["net_buy"]
        net_str = f"{top_net/1e8:.2f}亿" if abs(top_net) >= 1e8 else f"{top_net/1e4:.0f}万"
        assessment_parts.append(f"资金净买入: {focus_names} 等, 最大净买{net_str}")

    if top_risk:
        risk_names = "、".join(s["name"] for s in top_risk[:3])
        top_net = top_risk[0]["net_buy"]
        net_str = f"{abs(top_net)/1e8:.2f}亿" if abs(top_net) >= 1e8 else f"{abs(top_net)/1e4:.0f}万"
        assessment_parts.append(f"资金净卖出: {risk_names} 等, 最大净卖{net_str}")

    if hot_money:
        lift_count = sum(1 for h in hot_money if h["signal"] == "游资拉升")
        escape_count = sum(1 for h in hot_money if h["signal"] == "游资出逃")
        if lift_count > escape_count:
            assessment_parts.append(f"短线情绪偏暖（游资拉升{lift_count}只 > 出逃{escape_count}只）")
        elif escape_count > lift_count:
            assessment_parts.append(f"短线情绪偏冷（游资出逃{escape_count}只 > 拉升{lift_count}只）")
        else:
            assessment_parts.append(f"游资活跃（拉升{lift_count}只, 出逃{escape_count}只）")

    if reason_summary:
        top_reason = reason_summary[0]
        assessment_parts.append(f"主要上榜类型: {top_reason['reason'][:20]} ({top_reason['count']}只)")

    overall = "；".join(assessment_parts) if assessment_parts else "龙虎榜资金面无明显方向性信号"

    summary = DragonTigerSummary(
        date=datetime.now().strftime("%Y-%m-%d"),
        total_count=len(records),
        records=records[:30],
        institutional_focus=top_focus,
        institutional_risk=top_risk,
        hot_money_track=hot_money[:10],
        sector_flow=top_sectors,
        overall_assessment=overall,
    )

    return summary


def format_dragon_tiger_report(summary: DragonTigerSummary) -> str:
    """将龙虎榜分析结果格式化为可读的报告文本

    Args:
        summary: 龙虎榜综合分析结果

    Returns:
        格式化的报告文本
    """
    lines = []
    lines.append(f"## 龙虎榜资金分析（{summary.date}）")
    lines.append(f"上榜个股: {summary.total_count} 只")
    lines.append("")

    if summary.overall_assessment:
        lines.append(f"**整体研判**: {summary.overall_assessment}")
        lines.append("")

    if summary.institutional_focus:
        lines.append("### 🟢 资金关注（龙虎榜净买入）")
        lines.append("| 个股 | 龙虎榜净买入 | 买卖比 | 涨跌幅 | 换手率 |")
        lines.append("|------|-------------|--------|--------|--------|")
        for s in summary.institutional_focus[:6]:
            net_str = _format_money(s["net_buy"])
            ratio_str = f"{s['buy_sell_ratio']:.2f}" if s['buy_sell_ratio'] else "--"
            chg_str = f"{s['change_pct']:+.2f}%" if s['change_pct'] else "--"
            tr_str = f"{s['turnover_rate']:.1f}%" if s['turnover_rate'] else "--"
            lines.append(f"| {s['name']}({s['code']}) | {net_str} | {ratio_str} | {chg_str} | {tr_str} |")
        lines.append("")

    if summary.institutional_risk:
        lines.append("### 🔴 资金流出（龙虎榜净卖出）")
        lines.append("| 个股 | 龙虎榜净买入 | 买卖比 | 涨跌幅 | 换手率 |")
        lines.append("|------|-------------|--------|--------|--------|")
        for s in summary.institutional_risk[:5]:
            net_str = _format_money(s["net_buy"])
            ratio_str = f"{s['buy_sell_ratio']:.2f}" if s['buy_sell_ratio'] else "--"
            chg_str = f"{s['change_pct']:+.2f}%" if s['change_pct'] else "--"
            tr_str = f"{s['turnover_rate']:.1f}%" if s['turnover_rate'] else "--"
            lines.append(f"| {s['name']}({s['code']}) | {net_str} | {ratio_str} | {chg_str} | {tr_str} |")
        lines.append("")

    if summary.hot_money_track:
        lines.append("### 🐉 游资动向（高换手个股）")
        for h in summary.hot_money_track[:6]:
            if h["signal"] == "游资拉升":
                emoji = "🟢"
            elif h["signal"] == "游资出逃":
                emoji = "🔴"
            else:
                emoji = "⚪"
            lines.append(f"  {emoji} {h['name']}({h['code']}): {h['detail']}")
        lines.append("")

    if summary.sector_flow:
        lines.append("### 📊 资金分类汇总")
        lines.append("| 分类 | 净买入合计 | 上榜个数 | 资金方向 |")
        lines.append("|------|-----------|---------|---------|")
        for s in summary.sector_flow:
            net_str = _format_money(s["total_net_buy"])
            direction = "🟢 净流入" if s["total_net_buy"] > 0 else "🔴 净流出"
            lines.append(f"| {s['industry']} | {net_str} | {s['stock_count']}只 | {direction} |")
        lines.append("")

    return "\n".join(lines)


def _format_money(amount: float) -> str:
    if abs(amount) >= 1e8:
        return f"{amount/1e8:.2f}亿"
    elif abs(amount) >= 1e4:
        return f"{amount/1e4:.0f}万"
    return f"{amount:.0f}元"


def build_llm_context(summary: DragonTigerSummary) -> str:
    """生成适用于 LLM prompt 的龙虎榜数据摘要

    Args:
        summary: 龙虎榜综合分析结果

    Returns:
        紧凑的文本摘要
    """
    if summary.total_count == 0:
        return ""

    lines = []
    lines.append("[龙虎榜资金分析]")

    if summary.overall_assessment:
        lines.append(f"  整体: {summary.overall_assessment}")

    if summary.institutional_focus:
        focus_short = []
        for s in summary.institutional_focus[:5]:
            focus_short.append(f"{s['name']}({_format_money(s['net_buy'])})")
        lines.append(f"  净买入前5: {' '.join(focus_short)}")

    if summary.institutional_risk:
        risk_short = []
        for s in summary.institutional_risk[:3]:
            risk_short.append(f"{s['name']}({_format_money(s['net_buy'])})")
        lines.append(f"  净卖出前3: {' '.join(risk_short)}")

    if summary.hot_money_track:
        hot_short = []
        for h in summary.hot_money_track[:4]:
            hot_short.append(f"{h['name']}({h['signal']})")
        lines.append(f"  游资异动: {' '.join(hot_short)}")

    if summary.sector_flow:
        sector_short = []
        for s in summary.sector_flow[:3]:
            direction = "+" if s["total_net_buy"] > 0 else ""
            sector_short.append(f"{s['industry']}({direction}{_format_money(s['total_net_buy'])})")
        lines.append(f"  板块: {' '.join(sector_short)}")

    return "\n".join(lines)
