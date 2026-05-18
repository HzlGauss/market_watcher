"""
输出呈现 —— 控制台彩色输出 + Markdown 简报
"""

from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path

from app.models import Quote, Alert, AnalysisStats, INDEX_TYPE
from app.utils import log, format_volume, format_amount

# ============================================================
# ANSI 颜色常量
# ============================================================

class Color:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


# ============================================================
# 控制台输出
# ============================================================

def print_header() -> None:
    """打印扫描头部"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 70}")
    print(f"{Color.BOLD}{Color.CYAN}  📈 盯盘雷达  {now}{Color.RESET}")
    print(f"{'=' * 70}")


def print_quotes_table(quotes: list[Quote]) -> None:
    """打印实时行情表格"""
    if not quotes:
        print(f"{Color.YELLOW}  暂无数据{Color.RESET}")
        return

    header = (
        f"{'代码':>8} {'名称':<12} {'最新价':>8} "
        f"{'涨跌幅':>8} {'涨跌额':>8} {'成交量':>10} "
        f"{'成交额':>12} {'振幅':>7}"
    )
    print(f"\n{Color.CYAN}{Color.BOLD}{header}{Color.RESET}")
    print(f"{Color.DIM}{'-' * 70}{Color.RESET}")

    for q in quotes:
        price = f"{q.price:.3f}" if q.price is not None else f"{Color.DIM}--{Color.RESET}"

        if q.change_pct is not None:
            cp = f"{q.change_pct:+.2f}%"
            ca = f"{q.change_amt:+.3f}" if q.change_amt is not None else "--"
            if q.change_pct > 0:
                cp = f"{Color.RED}{cp}{Color.RESET}"
                ca = f"{Color.RED}{ca}{Color.RESET}"
            elif q.change_pct < 0:
                cp = f"{Color.GREEN}{cp}{Color.RESET}"
                ca = f"{Color.GREEN}{ca}{Color.RESET}"
        else:
            cp = f"{Color.DIM}--{Color.RESET}"
            ca = f"{Color.DIM}--{Color.RESET}"

        vol_str = format_volume(q.volume)
        amt_str = format_amount(q.amount)

        amp_str = f"{q.amplitude:.2f}%" if q.amplitude is not None else f"{Color.DIM}--{Color.RESET}"
        if q.amplitude is not None and q.amplitude >= 5:
            amp_str = f"{Color.YELLOW}{amp_str}{Color.RESET}"

        line = (
            f"{q.code:>8} {q.name:<12} {price:>8} {cp:>8} {ca:>8} "
            f"{vol_str:>10} {amt_str:>12} {amp_str:>7}"
        )
        print(f"  {line}")


def print_sentiment(stats: AnalysisStats) -> None:
    """打印市场情绪和资金流向"""
    s = stats.sentiment
    if not s:
        return

    score = s.score
    mood_color = Color.RED if score >= 60 else (Color.GREEN if score <= 30 else Color.YELLOW)
    print(f"\n{Color.CYAN}🌡️ 市场情绪:{Color.RESET}   {mood_color}{s.label}{Color.RESET}  (评分{score})  {s.detail}")

    # 动态阈值
    if stats.dynamic_enabled:
        t = stats.thresholds
        bt = stats.base_thresholds
        up_adj = t.get("涨幅预警", 0) - bt.get("涨幅预警", 4)
        down_adj = t.get("跌幅预警", 0) - bt.get("跌幅预警", -3)
        up_str = f"{t['涨幅预警']:+.1f}%({up_adj:+.1f})" if abs(up_adj) > 0.01 else f"{bt['涨幅预警']:+.1f}%"
        down_str = f"{t['跌幅预警']:+.1f}%({down_adj:+.1f})" if abs(down_adj) > 0.01 else f"{bt['跌幅预警']:+.1f}%"
        print(f"{Color.DIM}⚙️ 动态阈值: 涨幅预警 {up_str} | 跌幅预警 {down_str}{Color.RESET}")

    # 涨跌分布
    print(f"{Color.CYAN}📊 涨跌分布:{Color.RESET}   "
          f"{Color.RED}涨{stats.up}{Color.RESET} | "
          f"{Color.GREEN}跌{stats.down}{Color.RESET} | "
          f"平{stats.flat} | 共{stats.total}只")
    if stats.alert_count > 0:
        print(f"{Color.YELLOW}🔔 异动提醒: {stats.alert_count} 条{Color.RESET}")
    else:
        print(f"{Color.DIM}✅ 暂无异常{Color.RESET}")


def print_alerts(alerts: list[Alert]) -> None:
    """打印异动详情"""
    if not alerts:
        return
    print(f"\n{Color.BOLD}{Color.YELLOW}═══ 异动提醒详情 ═══{Color.RESET}")
    for a in alerts:
        msg = " | ".join(a.messages)
        print(f"  {Color.BOLD}{a.name}({a.code}){Color.RESET}  →  {msg}")
    print()


def print_llm_result(result: str | None) -> None:
    """打印AI研判结果"""
    if not result:
        return
    print(f"\n{Color.BOLD}{Color.CYAN}🤖 AI研判:{Color.RESET}")
    for line in result.strip().split("\n"):
        print(f"  {line}")
    print()


def print_tail(interval: int = 15) -> None:
    """打印尾部信息"""
    next_time = (datetime.now() + timedelta(minutes=interval)).strftime("%H:%M")
    print(f"{Color.DIM}⏰ 下次扫描: {next_time}  |  按 Ctrl+C 停止{Color.RESET}")
    print(f"{Color.DIM}{'=' * 70}{Color.RESET}\n")


# ============================================================
# Markdown 简报
# ============================================================

def save_brief(
    quotes: list[Quote],
    alerts: list[Alert],
    stats: AnalysisStats,
    brief_dir: Path,
    llm_config: dict | None = None,
) -> Path:
    """生成 Markdown 格式盯盘简报"""
    now = datetime.now()
    filename = f"monitoring_brief_{now.strftime('%Y-%m-%d_%H%M')}.md"
    filepath = brief_dir / filename

    s = stats.sentiment
    t = stats.thresholds

    with open(str(filepath), "w", encoding="utf-8") as f:
        f.write(f"# 📈 Monitoring Brief  {now.strftime('%Y-%m-%d %H:%M')}\n\n")

        # 市场情绪
        f.write("## 🌡️ 市场情绪\n\n")
        f.write(f"- **情绪评分**: {s.score}/100 — {s.label}\n")
        f.write(f"- **市场描述**: {s.detail}\n")
        if stats.dynamic_enabled:
            bt = stats.base_thresholds
            up_adj = t.get("涨幅预警", 0) - bt.get("涨幅预警", 4)
            down_adj = t.get("跌幅预警", 0) - bt.get("跌幅预警", -3)
            f.write(f"- **⚙️ 动态阈值已启用**\n")
            f.write(f"  - 涨幅预警: {t.get('涨幅预警', '--'):+.1f}%")
            if abs(up_adj) > 0.01:
                f.write(f" (较基准{'上调' if up_adj > 0 else '下调'}{abs(up_adj):.1f}%)")
            f.write("\n")
            f.write(f"  - 跌幅预警: {t.get('跌幅预警', '--'):+.1f}%")
            if abs(down_adj) > 0.01:
                f.write(f" (较基准{'上调' if down_adj > 0 else '下调'}{abs(down_adj):.1f}%)")
            f.write("\n")
        f.write("\n")

        # 市场概览
        f.write("## 📊 市场概览\n\n")
        f.write(f"- 上涨: {stats.up} | 下跌: {stats.down} | 平盘: {stats.flat} | 共监控: {stats.total} 只\n")
        f.write(f"- 涨跌比: {stats.up}:{stats.down}\n")
        if stats.alert_count > 0:
            f.write(f"- 🔔 异动提醒: {stats.alert_count} 条\n\n")
        else:
            f.write(f"- ✅ 暂无异常\n\n")

        # 异动详情
        if alerts:
            f.write("## 🔔 异动提醒\n\n")
            f.write("| 名称 | 代码 | 异动 |\n")
            f.write("|------|------|------|\n")
            for a in alerts:
                msg = " | ".join(a.messages)
                f.write(f"| {a.name} | {a.code} | {msg} |\n")
            f.write("\n")

        # 全部行情
        f.write("## 📋 全部行情\n\n")
        f.write("| 排名 | 代码 | 名称 | 最新价 | 涨跌幅 | 涨跌额 | 成交量 | 成交额 | 振幅 |\n")
        f.write("|------|------|------|--------|--------|--------|--------|--------|------|\n")

        sorted_q = sorted(quotes, key=lambda x: (x.change_pct or 0), reverse=True)
        for rank, q in enumerate(sorted_q, 1):
            cp = f"{q.change_pct:+.2f}%" if q.change_pct is not None else "--"
            price = f"{q.price:.3f}" if q.price is not None else "--"
            ca = f"{q.change_amt:+.3f}" if q.change_amt is not None else "--"
            vol = format_volume(q.volume)
            amt = format_amount(q.amount)
            amp = f"{q.amplitude:.2f}%" if q.amplitude is not None else "--"
            f.write(f"| {rank} | {q.code} | {q.name} | {price} | {cp} | {ca} | {vol} | {amt} | {amp} |\n")

        f.write(f"\n\n---\n*下次扫描: {datetime.now() + timedelta(minutes=15)}*")

    # AI研判（追加）
    llm = stats.llm_result
    if llm and llm_config and llm_config.get("启用", False):
        with open(str(filepath), "r", encoding="utf-8") as f:
            content = f.read()
        with open(str(filepath), "w", encoding="utf-8") as f:
            f.write(content + f"\n\n## 🤖 AI研判\n\n{llm.strip()}\n")

    return filepath
