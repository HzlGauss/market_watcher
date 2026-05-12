"""
推送通知 —— Server酱 微信推送
"""

from __future__ import annotations
import os
from datetime import datetime

from app.models import Alert, AnalysisStats
from app.config import Config
from app.utils import log
from app.http_client import serverchan_client

API_BASE = "https://sctapi.ftqq.com"


def push_alert(
    alerts: list[Alert],
    stats: AnalysisStats,
    config: Config,
    llm_result: str | None = None,
) -> bool:
    """推送异动提醒到微信（Server酱）"""
    if not config.push_enabled:
        return False
    if config.push_trigger == "仅异动时" and stats.alert_count == 0:
        return False

    sendkey = os.environ.get("SCT_SENDKEY")
    if not sendkey:
        return False

    # 构建标题
    s = stats.sentiment
    now_str = datetime.now().strftime("%m-%d %H:%M")
    title = f"📈 盯盘提醒 | {s.label} {stats.up}涨{stats.down}跌"

    # 构建内容（Markdown）
    lines = [
        f"## 📊 市场概览",
        f"- **情绪**: {s.score}/100 {s.label}",
        f"- **涨跌**: {stats.up}涨 / {stats.down}跌 / {stats.flat}平",
        f"- **异常**: {stats.alert_count} 条",
        "",
    ]

    if alerts:
        lines.append("## 🔔 异动详情")
        for a in alerts[:5]:
            lines.append(f"- **{a.name}**: {' | '.join(a.messages)}")
        if len(alerts) > 5:
            lines.append(f"- ...还有 {len(alerts) - 5} 条")
        lines.append("")

    if llm_result and config.push_include_llm:
        lines.append("## 🤖 AI研判")
        lines.append(llm_result.strip())
        lines.append("")

    lines.append("---")
    lines.append(f"*{now_str} · 15分钟后自动更新*")

    content = "\n".join(lines)

    url = f"{API_BASE}/{sendkey}.send"
    resp = serverchan_client.post(url, data={"title": title, "desp": content})

    if resp is None:
        log.warning("推送失败")
        return False

    try:
        return resp.status_code == 200 and resp.json().get("code") == 0
    except Exception as e:
        log.warning(f"推送解析失败: {e}")
        return False
