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


def _format_flow_breakdown_for_llm(quotes: list[Quote]) -> str:
    """汇总所有标的的资金结构，生成紧凑的 LLM 提示文本"""
    total_sl = 0.0  # 超大单
    total_lg = 0.0  # 大单
    total_md = 0.0  # 中单
    total_sm = 0.0  # 小单
    inst_count = 0   # 机构主导标的数
    retail_count = 0 # 散户主导标的数
    dist_count = 0   # 出货标的数
    has_data = False

    for q in quotes:
        ff = q.fund_flow
        if ff:
            if ff.super_large_net is not None:
                total_sl += ff.super_large_net
                has_data = True
            if ff.large_net is not None:
                total_lg += ff.large_net
            if ff.medium_net is not None:
                total_md += ff.medium_net
            if ff.small_net is not None:
                total_sm += ff.small_net
            if ff.is_institution_driven:
                inst_count += 1
            if ff.is_distribution:
                dist_count += 1
            if ff.is_retail_driven:
                retail_count += 1

    if not has_data:
        return "暂无资金结构数据"

    parts = []
    if abs(total_sl) >= 1e8:
        parts.append(f"超大单{total_sl/1e8:+.2f}亿")
    elif abs(total_sl) >= 1e6:
        parts.append(f"超大单{total_sl/1e4:+.0f}万")
    if abs(total_sm) >= 1e8:
        parts.append(f"散户{total_sm/1e8:+.2f}亿")
    elif abs(total_sm) >= 1e6:
        parts.append(f"散户{total_sm/1e4:+.0f}万")

    if inst_count > 0:
        parts.append(f"{inst_count}只机构主导")
    if dist_count > 0:
        parts.append(f"{dist_count}只疑似出货")
    if retail_count > 0:
        parts.append(f"{retail_count}只散户推升")

    return " | ".join(parts) if parts else "资金结构均衡"


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
    ]

    # 全市场广度（优先，LLM据此判断大盘环境）
    if stats.market_breadth and stats.market_breadth.is_valid:
        b = stats.market_breadth
        lines.extend([
            "## 🏛️ 全市场广度",
            f"- 涨跌分布: {b.up_count}涨 / {b.down_count}跌 / {b.flat_count}平 (共{b.total_count}只)",
            f"- 涨跌比: {b.up_ratio:.0%} → {b.breadth_label}",
            f"- 涨停: {b.limit_up} | 跌停: {b.limit_down} → 情绪: {b.limit_emotion}",
            f"- 全市场成交(累计): {b.total_amount:.0f}亿"
            f" | 估算全天: ~{b.estimated_full_day_amount:.0f}亿"
            f" | 主力净流入: {b.main_net_inflow:+.1f}亿",
            f"- 资金结构(超大/大/中/小): "
            f"{_format_flow_breakdown_for_llm(quotes)}",
            f"- 参考指数: {b.index_name} {b.index_change_pct:+.2f}% ({b.index_price:.2f})",
            f"- 数据时间: {b.update_time}",
            "",
        ])
    else:
        # 兜底：用监控标的数据估算市场状态
        lines.extend([
            "## 🏛️ 市场概况（基于监控标的）",
            f"- 说明: 全市场数据暂不可用，以下为自选标的统计",
            f"- 涨跌分布: 涨{stats.up} / 跌{stats.down} / 平{stats.flat} (共{stats.total}只)",
            f"- 情绪评分: {s.score}/100 ({s.label})",
            f"- 资金结构(超大/大/中/小): {_format_flow_breakdown_for_llm(quotes)}",
            "",
        ])

    lines.extend([
        f"## 持仓概况",
        f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 监控标的: {stats.total} 只",
        f"- 涨跌分布: 涨{stats.up} / 跌{stats.down} / 平{stats.flat}",
        f"- 情绪评分: {s.score}/100 ({s.label})",
    ])

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
                "[均线多头回踩]", "[均线空头反弹]", "[均线金叉]", "[均线死叉]",
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
                if tech.ma_alignment and tech.ma_alignment != "数据不足":
                    ma_info = f"均线:{tech.ma_alignment}"
                    # 价格与各均线位置
                    ma_parts = []
                    for ma_name, ma_val in [("MA5", tech.ma5), ("MA10", tech.ma10), ("MA20", tech.ma20), ("MA60", tech.ma60)]:
                        if ma_val is not None and q.price and ma_val > 0:
                            dev = (q.price - ma_val) / ma_val * 100
                            direction = "↑" if dev > 0 else "↓"
                            ma_parts.append(f"{ma_name}{direction}{abs(dev):.1f}%")
                    if ma_parts:
                        ma_info += "(" + ",".join(ma_parts) + ")"
                    parts.append(ma_info)
                lines.append(" ".join(parts))

    lines.extend([
        "",
        "请按以下框架分析，输出控制在 500 字以内。**每个判断必须包含推理链条：结论→直接原因(指标+数值)→深层逻辑→风险提示。**",
        "",
        "### 第一步：盘面定性（1-2 句）",
        "对照全市场广度数据（涨跌比/涨跌停/成交额），用一个词或短语定性当前盘面"
        "（如：放量普涨、缩量震荡、二八分化、恐慌杀跌等），然后一句话说明核心矛盾和推导逻辑。",
        "格式示例：\"缩量震荡[高] → 成交额仅6800亿(5日均量8500亿)且涨跌比接近1:1 → 市场缺乏方向，观望情绪浓 → 若放量突破万亿则方向选择\"",
        "",
        "### 第二步：量价验证",
        "对全市场成交额和自选标的的放量/缩量状态进行交叉验证：",
        "- 全市场成交额是否充沛？与近5日均量对比如何？",
        "- 自选标的涨跌是否与全市场方向一致？偏离度如何？",
        "- 上涨标的是否有量能支撑（量比>1.5）？无量上涨需标注风险",
        "- 下跌标的是否放量（量比>1.5）？放量下跌需判断是恐慌还是出货",
        "",
        "### 第三步：组合策略信号解读（如有）",
        "如果存在组合策略信号，这是多指标共振的高胜率信号，必须重点解读其共振逻辑和可信度：",
        "- 趋势启动：MACD+RSI+KDJ共振做多，可信度高",
        "- 逃顶组合：价格新高+顶背离+超买+死叉，立即减仓",
        "- 震荡套利：布林带边界+RSI极值+KDJ交叉，适合短线",
        "- 双翼齐飞：KDJ+RSI低位共振+放量，底部反弹信号",
        "- 低位放量启动：低位盘整后放量上涨+OBV资金入场，趋势反转信号",
        "- 高位放量滞警：高位放量但滞涨+RSI超买+OBV资金离场，主力出货风险",
        "- 缩量洗盘：上涨趋势中缩量回调+未破支撑，洗盘结束可加仓",
        "- 放量突破确认：放量突破布林中轨+OBV资金入场，突破有效确认",
        "- 地量地价反转：长期下跌后地量+RSI超卖+KDJ超卖，底部反转信号",
        "- 均线多头回踩：多头排列+价格回踩MA20+缩量，均线支撑买入点，顺势低吸",
        "- 均线空头反弹：空头排列+价格反弹至MA20+无量，均线压力卖出点，借反弹减仓",
        "- 均线金叉：MA5上穿MA10，配合MA20方向判断趋势转多可信度",
        "- 均线死叉：MA5下穿MA10，配合MA20方向判断趋势转空可信度",
        "**对每个触发的策略信号，必须解释：哪些具体指标在什么数值上共振？共振强度如何？历史回测胜率参考？**",
        "",
        "### 第四步：异动解读（含推理链）",
        "对警报标的逐一说明，每条必须包含推理链：",
        "格式：\"{标的}：{结论}[置信度] → {直接触发的指标及数值} → {深层原因} → {若条件变化则判断失效}\"",
        "示例：\"芯片ETF：短期回调[中] → RSI=72(超买)+量比0.6(缩量上涨) → 获利盘兑现，买盘不济 → 若放量突破前高则判断失效\"",
        "",
        "### 第五步：关键技术位判断",
        "结合 RSI、MACD、KDJ、支撑/压力位、分时均价(黄线)的位置，给出交叉验证结论：",
        "- 多个指标是否指向同一方向？数值如何？（如 RSI=68超买 + KDJ-K=85高位死叉 + 距压力位仅1.5% = 三重阻力，短期回调概率高）",
        "- 指标是否出现背离？（如价格新高但 RSI 未创新高 = 顶背离风险；价格新低但 OBV 未新低 = 底背离蓄力）",
        "- 分时均价(黄线)与现价关系？现价高于均价X%说明日内强势，低于均价X%说明日内弱势",
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
