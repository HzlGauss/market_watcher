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
    PURPLE = "\033[95m"
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


def _format_flow_signal(q: Quote) -> str:
    """生成资金信号标签（紧凑，用于控制台表格）"""
    ff = q.fund_flow
    if ff and ff.is_valid and ff.main_net is not None:
        # 有资金明细时精准判断
        if ff.is_institution_driven:
            return f"{Color.CYAN}机构吸筹{Color.RESET}"
        elif ff.is_distribution:
            return f"{Color.PURPLE}机构出货{Color.RESET}"
        elif ff.is_retail_driven:
            return f"{Color.YELLOW}散户推升{Color.RESET}"
        elif ff.main_net > 0 and q.amount and q.amount > 0 and ff.main_net / q.amount >= 0.05:
            return f"{Color.RED}主力流入{Color.RESET}"
        elif ff.main_net < 0 and q.amount and q.amount > 0 and abs(ff.main_net) / q.amount >= 0.05:
            return f"{Color.GREEN}主力流出{Color.RESET}"
        else:
            return "  ·中性  "
    # 没资金明细时用 main_net_inflow
    if q.main_net_inflow is not None and q.amount and q.amount > 0:
        pct = q.main_net_inflow / q.amount
        if pct >= 0.1:
            return f"{Color.RED}主力流入{Color.RESET}"
        elif pct <= -0.1:
            return f"{Color.GREEN}主力流出{Color.RESET}"
        elif pct >= 0.05:
            return "  偏多  "
        elif pct <= -0.05:
            return "  偏空  "
    return f"{Color.DIM}  ----  {Color.RESET}"


def _format_compact_flow(value: Optional[float]) -> str:
    """紧凑格式化金额（控制台表格用）"""
    if value is None:
        return f"{Color.DIM}  ---  {Color.RESET}"
    abs_v = abs(value)
    sign = "+" if value >= 0 else "-"
    if abs_v >= 1e8:
        return f"{sign}{abs_v/1e8:.2f}亿"
    elif abs_v >= 1e6:
        return f"{sign}{abs_v/1e6:.0f}万"
    elif abs_v >= 1e4:
        return f"{sign}{abs_v/1e4:.1f}万"
    else:
        return f"{sign}{abs_v:.0f}元"


def print_quotes_table(quotes: list[Quote]) -> None:
    """打印实时行情表格"""
    if not quotes:
        print(f"{Color.YELLOW}  暂无数据{Color.RESET}")
        return

    header = (
        f"{'代码':>8} {'名称':<12} {'最新价':>8} {'均价':>8} "
        f"{'涨跌幅':>8} {'主力净流入':>10} {'资金信号':<8} "
        f"{'委比':>6} {'量比':>6} {'成交量':>10} {'换手率':>8} {'振幅':>7}"
    )
    print(f"\n{Color.CYAN}{Color.BOLD}{header}{Color.RESET}")
    print(f"{Color.DIM}{'-' * 110}{Color.RESET}")

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

        # 量比
        if q.volume_ratio is not None:
            vr_str = f"{q.volume_ratio:.2f}"
            if q.volume_ratio >= 1.5:
                vr_str = f"{Color.RED}{vr_str}{Color.RESET}"
            elif q.volume_ratio <= 0.5:
                vr_str = f"{Color.GREEN}{vr_str}{Color.RESET}"
        else:
            vr_str = f"{Color.DIM}--{Color.RESET}"

        # 主力净流入（优先用 fund_flow.main_net，回退到 main_net_inflow）
        flow_val: Optional[float] = None
        if q.fund_flow is not None and q.fund_flow.main_net is not None:
            flow_val = q.fund_flow.main_net
        elif q.main_net_inflow is not None:
            flow_val = q.main_net_inflow

        if flow_val is not None:
            flow_str = _format_compact_flow(flow_val)
            if flow_val > 0:
                flow_str = f"{Color.RED}{flow_str}{Color.RESET}"
            elif flow_val < 0:
                flow_str = f"{Color.GREEN}{flow_str}{Color.RESET}"
        else:
            flow_str = f"{Color.DIM}  ---  {Color.RESET}"

        # 资金信号
        sig_str = _format_flow_signal(q)

        # 委比
        if q.bid_ask_ratio is not None:
            bar_str = f"{q.bid_ask_ratio:+.1f}%"
            if q.bid_ask_ratio > 30:
                bar_str = f"{Color.RED}{bar_str}{Color.RESET}"
            elif q.bid_ask_ratio < -30:
                bar_str = f"{Color.GREEN}{bar_str}{Color.RESET}"
        else:
            bar_str = f"{Color.DIM}--{Color.RESET}"

        # 换手率
        if q.turnover_rate is not None:
            tr_str = f"{q.turnover_rate:.2f}%"
            if q.turnover_rate >= 10:
                tr_str = f"{Color.YELLOW}{tr_str}{Color.RESET}"
        else:
            tr_str = f"{Color.DIM}--{Color.RESET}"

        # 均价（黄线）：现价与均价对比着色
        if q.avg_price is not None and q.avg_price > 0:
            avg_str = f"{q.avg_price:.3f}"
            if q.price and q.price > q.avg_price * 1.01:
                avg_str = f"{Color.RED}{avg_str}{Color.RESET}"  # 现价高于均价=偏强
            elif q.price and q.price < q.avg_price * 0.99:
                avg_str = f"{Color.GREEN}{avg_str}{Color.RESET}"  # 现价低于均价=偏弱
        else:
            avg_str = f"{Color.DIM}--{Color.RESET}"

        line = (
            f"{q.code:>8} {q.name:<12} {price:>8} {avg_str:>8} {cp:>8} "
            f"{flow_str:>10} {sig_str:<8} "
            f"{bar_str:>6} {vr_str:>6} {vol_str:>10} {tr_str:>8} {amp_str:>7}"
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

    # 全市场广度
    if stats.market_breadth and stats.market_breadth.is_valid:
        b = stats.market_breadth
        breadth_label = b.breadth_label
        b_color = Color.RED if breadth_label in ("普涨", "偏多") else (
            Color.GREEN if breadth_label == "普跌" else Color.YELLOW)
        emotion = b.limit_emotion
        # 成交额显示
        amount_str = f"{b.total_amount:.0f}亿" if b.total_amount >= 10000 else f"{b.total_amount:.0f}亿"
        if b.total_amount >= 12000:
            amount_str = f"{Color.RED}{amount_str}{Color.RESET}"  # 放量
        elif b.total_amount < 6000:
            amount_str = f"{Color.GREEN}{amount_str}{Color.RESET}"  # 缩量
        print(f"{Color.CYAN}🏛️ 全市场:{Color.RESET}   "
              f"{b_color}{breadth_label}{Color.RESET} | "
              f"{Color.RED}涨{b.up_count}{Color.RESET}/"
              f"{Color.GREEN}跌{b.down_count}{Color.RESET}/"
              f"平{b.flat_count} | "
              f"涨停 {Color.RED}{b.limit_up}{Color.RESET} | "
              f"跌停 {Color.GREEN}{b.limit_down}{Color.RESET} | "
              f"成交 {amount_str} | "
              f"{b.index_name} {Color.CYAN}{b.index_change_pct:+.2f}%{Color.RESET} | "
              f"情绪: {emotion}")

    # 涨跌分布（自选标的）
    print(f"{Color.CYAN}📊 自选分布:{Color.RESET}   "
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


def print_key_levels(tech_summaries: dict, quotes: list[Quote]) -> None:
    """打印关键支撑/压力位

    Args:
        tech_summaries: 技术摘要字典
        quotes: 行情列表
    """
    if not tech_summaries:
        print(f"\n{Color.BOLD}{Color.CYAN}═══ 关键价位 ═══{Color.RESET}")
        print(f"  {Color.DIM}暂无关键位数据{Color.RESET}")
        print()
        return

    quote_map = {q.code: q for q in quotes}
    has_data = False

    print(f"\n{Color.BOLD}{Color.CYAN}═══ 关键价位 ═══{Color.RESET}")
    for code, tech in tech_summaries.items():
        quote = quote_map.get(code)
        if not quote or quote.price is None:
            continue

        parts = [f"{Color.BOLD}{quote.name}({code}){Color.RESET}"]
        current = quote.price

        # 枢轴点（日内最重要）
        if tech.pivot_supports and tech.pivot_resistances:
            p_str = f"枢轴:S1={tech.pivot_supports[0]:.3f} "
            if len(tech.pivot_supports) > 1:
                p_str += f"S2={tech.pivot_supports[1]:.3f} "
            p_str += f"P={(tech.pivot_supports[0] + tech.pivot_resistances[0])/2:.3f} "
            p_str += f"R1={tech.pivot_resistances[0]:.3f}"
            if len(tech.pivot_resistances) > 1:
                p_str += f" R2={tech.pivot_resistances[1]:.3f}"
            parts.append(p_str)

        # 主支撑/压力位
        if tech.support and tech.resistance:
            parts.append(f"| 支撑:{tech.support:.3f} 压力:{tech.resistance:.3f}")

            # 标注当前价格位置
            if current <= tech.support * 1.01:
                parts.append(f"{Color.GREEN}←跌破支撑{Color.RESET}")
            elif current >= tech.resistance * 0.99:
                parts.append(f"{Color.RED}←突破压力{Color.RESET}")

        # 均线排列状态
        if tech.ma_alignment and tech.ma_alignment != "数据不足":
            ma_color = Color.GREEN if tech.ma_alignment in ("多头排列", "多头回调") else (
                Color.RED if tech.ma_alignment in ("空头排列", "空头反弹") else Color.YELLOW)
            parts.append(f"均线:{ma_color}{tech.ma_alignment}{Color.RESET}")

        if len(parts) > 1:
            print(f"  {' | '.join(parts)}")
            has_data = True

    if not has_data:
        print(f"  {Color.DIM}暂无关键位数据{Color.RESET}")
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

        # 全市场广度
        if stats.market_breadth and stats.market_breadth.is_valid:
            b = stats.market_breadth
            f.write("## 🏛️ 全市场广度\n\n")
            f.write(f"- 上涨: {b.up_count} | 下跌: {b.down_count} | 平盘: {b.flat_count} | 共: {b.total_count} 只\n")
            f.write(f"- 涨跌比: {b.up_count}:{b.down_count} — {b.breadth_label}\n")
            f.write(f"- 涨停: {b.limit_up} | 跌停: {b.limit_down} — {b.limit_emotion}\n")
            f.write(f"- 成交额: {b.total_amount:.0f}亿 | 主力净流入: {b.main_net_inflow:+.1f}亿\n")
            f.write(f"- {b.index_name}: {b.index_price:.2f} ({b.index_change_pct:+.2f}%)\n")
            f.write(f"- 更新时间: {b.update_time}\n")
            f.write(f"\n")
            f.write("## 📊 市场概览\n\n")
        else:
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
