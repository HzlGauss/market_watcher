"""
AI分析 —— 调用 DeepSeek API 生成盘面研判
"""

from __future__ import annotations
from datetime import datetime

from app.models import Quote, Alert, AnalysisStats, INDEX_TYPE
from app.config import Config
from app.utils import log
from app.llm_client import get_llm_client, SYSTEM_PROMPTS


def _build_prompt(
    quotes: list[Quote],
    alerts: list[Alert],
    stats: AnalysisStats,
) -> str:
    """构建大模型分析用的 Prompt"""
    s = stats.sentiment
    lines = [
        "你是一位专业的A股市场实时分析师。请基于以下实时盯盘数据，给出专业、简洁的盘面研判。",
        "",
        f"## 市场状态",
        f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 监控标的: {stats.total} 只",
        f"- 涨跌分布: 涨{stats.up} / 跌{stats.down} / 平{stats.flat}",
        f"- 情绪评分: {s.score}/100 ({s.label})",
    ]

    # 指数数据
    indices = [q for q in quotes if q.type == INDEX_TYPE]
    if indices:
        lines.append("")
        lines.append("## 关键指数")
        for idx in indices:
            cp = f"{idx.change_pct:+.2f}%" if idx.change_pct is not None else "--"
            lines.append(f"- {idx.name}({idx.code}): {cp}")

    # 北向资金
    nf = stats.north_flow
    if nf:
        lines.append("")
        lines.append(f"## 北向资金: {nf.get('total_net', 0):+.0f}亿")

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

    # 详细行情
    lines.append("")
    lines.append("## 全部标的涨跌幅")
    sorted_q = sorted(quotes, key=lambda x: (x.change_pct or 0), reverse=True)
    for q in sorted_q:
        cp = f"{q.change_pct:+.2f}%" if q.change_pct is not None else "--"
        lines.append(f"- {q.name}({q.code}): {cp}")

    lines.extend([
        "",
        "请从以下3个方面分析，总字数不超过200字：",
        "1. 盘面特征: 当前市场的主要特征和资金动向",
        "2. 异动解读: 异动标的原因分析和后续关注点",
        "3. 操作参考: 针对当前盘面的参考建议",
        "",
        "注意: 语言简洁专业，基于数据说话，不做无依据预测。",
    ])

    return "\n".join(lines)


def analyze(
    quotes: list[Quote],
    alerts: list[Alert],
    stats: AnalysisStats,
    config: Config,
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

    prompt = _build_prompt(quotes, alerts, stats)
    result = llm.chat(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPTS["analyst"],
        max_tokens=500,
        temperature=0.3,
    )

    if result is None:
        log.warning("AI分析请求失败")

    return result
