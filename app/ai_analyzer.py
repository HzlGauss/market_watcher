"""
AI分析 —— 调用 DeepSeek API 生成盘面研判
"""

from __future__ import annotations
from datetime import datetime

from typing import Dict

from app.models import Quote, Alert, AnalysisStats, INDEX_TYPE, TechnicalSummary
from app.config import Config
from app.utils import log
from app.llm_client import get_llm_client, SYSTEM_PROMPTS


def _build_prompt(
    quotes: list[Quote],
    alerts: list[Alert],
    stats: AnalysisStats,
    tech_summaries: Dict[str, TechnicalSummary] | None = None,
) -> str:
    """构建大模型分析用的 Prompt"""
    s = stats.sentiment
    lines = [
        "请基于以下实时盯盘数据，给出专业、简洁的盘面研判。",
        "",
        f"## 市场状态",
        f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 监控标的: {stats.total} 只",
        f"- 涨跌分布: 涨{stats.up} / 跌{stats.down} / 平{stats.flat}",
        f"- 上涨家数占比: {stats.up/(stats.up+stats.down+stats.flat)*100:.0f}%",
        f"- 情绪评分: {s.score}/100 ({s.label})",
    ]

    # 成交量分析
    total_volume = sum(q.volume for q in quotes if q.volume is not None and q.volume > 0)
    advance_volume = sum(q.volume for q in quotes if q.volume and q.volume > 0 and q.change_pct and q.change_pct > 0)
    if total_volume > 0:
        lines.append(f"- 上涨量比: {advance_volume/total_volume*100:.0f}%（上涨标的成交量/总成交量）")

    # 指数数据
    indices = [q for q in quotes if q.type == INDEX_TYPE]
    if indices:
        lines.append("")
        lines.append("## 关键指数")
        for idx in indices:
            cp = f"{idx.change_pct:+.2f}%" if idx.change_pct is not None else "--"
            lines.append(f"- {idx.name}({idx.code}): {cp}")

    # 板块表现
    sectors: dict[str, list[float]] = {}
    for q in quotes:
        if q.type not in sectors:
            sectors[q.type] = []
        if q.change_pct is not None:
            sectors[q.type].append(q.change_pct)

    lines.append("")
    lines.append("## 板块表现")
    for st, pcts in sorted(sectors.items()):
        if pcts:
            avg = sum(pcts) / len(pcts)
            lines.append(f"- {st}: {len(pcts)}只, 均值 {avg:+.2f}%")

    # 异动
    if alerts:
        lines.append("")
        lines.append("## 异动提醒")
        for a in alerts:
            lines.append(f"- {a.name}({a.code}): {' | '.join(a.messages)}")

    # 组合策略信号（从异动中提取）
    strategy_signals = []
    for a in alerts:
        for msg in a.messages:
            if any(tag in msg for tag in [
                "[趋势启动]", "[逃顶组合]", "[震荡套利]", "[双翼齐飞]",
                "[低位放量启动]", "[高位放量滞警]", "[缩量洗盘]",
                "[放量突破确认]", "[地量地价反转]",
            ]):
                strategy_signals.append(f"- {a.name}({a.code}): {msg}")

    if strategy_signals:
        lines.append("")
        lines.append("## ⭐ 组合策略信号（多指标共振，高优先级）")
        for sig in strategy_signals:
            lines.append(sig)

    # 详细行情
    lines.append("")
    lines.append("## 全部标的涨跌幅")
    sorted_q = sorted(quotes, key=lambda x: (x.change_pct or 0), reverse=True)
    for q in sorted_q:
        cp = f"{q.change_pct:+.2f}%" if q.change_pct is not None else "--"
        lines.append(f"- {q.name}({q.code}): {cp}")

    # 技术指标
    if tech_summaries:
        lines.append("")
        lines.append("## 持仓技术指标")
        for q in quotes:
            tech = tech_summaries.get(q.code)
            if tech:
                parts = [f"- {q.name}({q.code}):"]
                if tech.rsi is not None:
                    parts.append(f"RSI:{tech.rsi:.1f}({tech.rsi_signal})")
                if tech.macd_dif is not None:
                    parts.append(f"MACD:{tech.macd_signal}")
                if tech.kdj_k is not None:
                    parts.append(f"KDJ:{tech.kdj_signal}")
                if tech.support is not None:
                    support_parts = [f"主支撑:{tech.support:.3f}"]
                    if hasattr(tech, 'swing_supports') and tech.swing_supports:
                        support_parts.append(f"摆动支撑:{tech.swing_supports[0]:.3f}")
                    if hasattr(tech, 'pivot_supports') and tech.pivot_supports:
                        support_parts.append(f"枢轴支撑:{tech.pivot_supports[0]:.3f}")
                    parts.append(";".join(support_parts))
                if tech.resistance is not None:
                    resistance_parts = [f"主压力:{tech.resistance:.3f}"]
                    if hasattr(tech, 'swing_resistances') and tech.swing_resistances:
                        resistance_parts.append(f"摆动压力:{tech.swing_resistances[0]:.3f}")
                    if hasattr(tech, 'pivot_resistances') and tech.pivot_resistances:
                        resistance_parts.append(f"枢轴压力:{tech.pivot_resistances[0]:.3f}")
                    parts.append(";".join(resistance_parts))
                if tech.bb_signal:
                    parts.append(f"布林带:{tech.bb_signal}")
                if tech.obv is not None:
                    parts.append(f"OBV:{tech.obv:.0f}")
                lines.append(" ".join(parts))

    lines.extend([
        "",
        "请按以下框架分析，输出控制在 300 字以内：",
        "",
        "### 第一步：盘面定性（1 句）",
        "用一个词或短语定性当前盘面（如：缩量震荡、放量上攻、冲高回落等），然后一句话说明核心矛盾。",
        "",
        "### 第二步：量价验证",
        "- 当前上涨是否有量能支撑？上涨量比是否大于 50%？",
        "- 如果缩量上涨，需标注\"缩量上涨，持续性存疑\"",
        "- 如果放量下跌，需标注\"抛压较重，谨慎\"",
        "",
        "### 第三步：组合策略信号解读（如有）",
        "如果存在组合策略信号，这是多指标共振的高胜率信号，需要重点解读：",
        "- 趋势启动：MACD+RSI+KDJ共振做多，可信度高",
        "- 逃顶组合：价格新高+顶背离+超买+死叉，立即减仓",
        "- 震荡套利：布林带边界+RSI极值+KDJ交叉，适合短线",
        "- 双翼齐飞：KDJ+RSI低位共振+放量，底部反弹信号",
        "- 低位放量启动：低位盘整后放量上涨+OBV资金入场，趋势反转信号",
        "- 高位放量滞警：高位放量但滞涨+RSI超买+OBV资金离场，主力出货风险",
        "- 缩量洗盘：上涨趋势中缩量回调+未破支撑，洗盘结束可加仓",
        "- 放量突破确认：放量突破布林中轨+OBV资金入场，突破有效确认",
        "- 地量地价反转：长期下跌后地量+RSI超卖+KDJ超卖，底部反转信号",
        "",
        "### 第四步：异动解读",
        "对警报标的逐一说明：是技术性回调、资金驱动还是基本面因素？",
        "",
        "### 第五步：关键技术位判断",
        "结合 RSI（超买/超卖）、MACD（金叉/死叉）、KDJ 的位置，给出交叉验证结论：",
        "- 多个指标是否指向同一方向？（如 RSI 超买 + KDJ 高位死叉 = 短期回调压力大）",
        "- 指标是否出现背离？（如价格新高但 RSI 未创新高 = 顶背离风险）",
        "",
        "### 置信度标注",
        "对以上判断标注置信度：[高] 数据充分且指标一致 / [中] 数据尚可但指标有分歧 / [低] 数据不足或方向不明",
        "",
        "注意: 如果数据不足以得出明确结论，请明确说\"数据不足\"，不要强行分析。",
    ])

    return "\n".join(lines)


def analyze(
    quotes: list[Quote],
    alerts: list[Alert],
    stats: AnalysisStats,
    config: Config,
    tech_summaries: Dict[str, TechnicalSummary] | None = None,
) -> str | None:
    """调用 DeepSeek 大模型进行智能分析"""
    if not config.llm_enabled:
        return None

    # 触发时机控制
    if config.llm_trigger == "仅异动时" and stats.alert_count == 0:
        return None

    llm = get_llm_client(config)
    if not llm.enabled:
        return None

    prompt = _build_prompt(quotes, alerts, stats, tech_summaries)
    result = llm.chat(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPTS["analyst"],
        max_tokens=800,
        temperature=0.3,
    )

    if result is None:
        log.warning("AI分析请求失败")

    return result
