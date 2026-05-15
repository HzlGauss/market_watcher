"""
Investment Report Generator - Morning Brief, Midday Review, Evening Review
Generate actionable strategy reports from a senior investment expert perspective
"""

from __future__ import annotations
from datetime import datetime
from pathlib import Path

from app.models import WatchItem, Quote, Holding
from app.config import Config
from app.data_fetcher import fetch_quotes, NorthFlowFetcher
from app.analyzer import analyze, calc_market_sentiment
from app.utils import log
from app.http_client import serverchan_client
from app.llm_client import get_llm_client, SYSTEM_PROMPTS


# ============================================================
# Private Helpers
# ============================================================

def _get_unique_items(config: Config) -> list[WatchItem]:
    """Merge watchlist and holdings to get unique list of items for fetching"""
    watchlist = config.watch_items
    holdings = config.holdings

    # Use code as key to avoid duplicates
    unique_map = {item.code: item for item in watchlist}

    for h in holdings:
        if h.code not in unique_map:
            # Convert Holding to WatchItem for fetcher compatibility
            unique_map[h.code] = WatchItem(
                name=h.name,
                code=h.code,
                market=h.market,
                type="持仓股"
            )

    return list(unique_map.values())


def _holdings_summary(
    holdings: list[Holding],
    quotes: list[Quote],
) -> list[dict]:
    """
    Match holdings with real-time quotes, calculate P&L
    Returns for each holding: name, amount, cost, current price, P&L amount, P&L %
    """
    results = []
    total_cost = 0
    total_pnl = 0

    for h in holdings:
        quote = next((q for q in quotes if q.code == h.code), None)
        if quote and quote.price:
            cost = h.cost * h.amount
            current_value = quote.price * h.amount
            pnl = current_value - cost
            pnl_pct = (pnl / cost) * 100 if cost > 0 else 0

            results.append({
                "name": h.name,
                "code": h.code,
                "amount": h.amount,
                "cost": h.cost,
                "price": quote.price,
                "pnl": pnl,
                "pnl_pct": pnl_pct
            })
            total_cost += cost
            total_pnl += pnl

    return results, total_pnl, total_cost


# ============================================================
# Report Saving & Pushing
# ============================================================

def _save_report(content: str, title: str, report_dir: Path) -> Path:
    """Save report to markdown file"""
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"{title}_{timestamp}.md"
    filepath = report_dir / filename

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("---\n\n")
        f.write(content)

    return filepath


def _push_report(title: str, content: str, config: Config) -> bool:
    """Push report to WeChat via ServerChan"""
    if not config.push_enabled or not config.sct_sendkey:
        return False

    try:
        url = f"/{config.sct_sendkey}.send"
        resp = serverchan_client.post(
            url,
            data={"title": title, "desp": content},
            timeout=30
        )
        if resp is None:
            log.warning("Report push failed: No response")
            return False
        resp.raise_for_status()
        log.info("Report pushed successfully")
        return True
    except Exception as e:
        log.warning(f"Report push failed: {e}")
        return False


def _call_llm(prompt: str, config: Config) -> str | None:
    """Call LLM to generate analysis content"""
    if not config.llm_enabled or not config.deepseek_key:
        return None

    try:
        llm = get_llm_client(config)
        system_prompt = SYSTEM_PROMPTS.get("analyst", "")
        response = llm.chat(system_prompt, prompt, max_tokens=2000, timeout=120)
        return response
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        return None


# ============================================================
# Morning Brief 08:25
# ============================================================

def generate_morning_brief(config: Config) -> Path | None:
    """Morning Brief - Overnight market review + today's strategy"""
    report_cfg = config.report_cfg.get("Morning Brief", {})
    if not report_cfg.get("启用", False):
        return None

    log.info("Generating morning brief...")

    # 1. Get overnight market data
    from app.data_fetcher import fetch_global_markets
    global_data = fetch_global_markets()

    # 2. Get yesterday's A股 data (fetch unique items)
    all_items = _get_unique_items(config)
    quotes = fetch_quotes(all_items)

    # 3. Build prompt
    lines = [
        f"现在是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，A股即将开盘。"
        f"请生成一份完整的早盘策略报告。"
    ]

    # Overnight US market
    if global_data:
        lines.append("\n## 隔夜市场数据")
        for k, v in global_data.items():
            lines.append(f"- {k}: {v}")

    # Market sentiment from yesterday
    if quotes:
        watchlist_codes = {item.code for item in config.watch_items}
        watch_quotes = [q for q in quotes if q.code in watchlist_codes]
        sentiment = calc_market_sentiment(watch_quotes)
        lines.append(f"\n## 昨日A股情绪")
        lines.append(f"- 情绪评分: {sentiment.score}/100 ({sentiment.label})")

        # Summary of holdings yesterday
        holdings = config.holdings
        if holdings:
            h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
            if h_results:
                lines.append(f"\n## 当前持仓概况 (Holdings)")
                lines.append(f"- 总盈亏: {total_pnl:+,.2f} ({total_pnl/total_cost*100:+.2f}%)")
                for h in h_results[:5]: # Only show top 5 in brief
                    lines.append(f"  - {h['name']}: {h['pnl_pct']:+.2f}%")

    lines.append(f"""

请作为首席策略师，基于以上数据生成一份结构化的早盘简报（约500字）：

1️⃣ **隔夜盘面解析**: 美股、A50及港股的表现对今日A股开盘的影响
2️⃣ **今日走势研判**: 预计今日大盘的波动区间、关键压力位与支撑位
3️⃣ **行业机会点**: 哪些板块今天可能受到消息面或隔夜行情的提振？
4️⃣ **持仓应对策略**: 针对现有持仓，今日开盘后建议的操作策略
5️⃣ **风险预警**: 今日盘中需重点防范的风险点

要求：专业、简洁、有明确的操作导向。使用 Markdown 格式增强可读性。""")

    content = _call_llm("\n".join(lines), config)
    if not content:
        log.warning("Morning brief generation failed")
        return None

    # Save
    report_dir = Path(config.report_dir)
    filepath = _save_report(content, "Morning Brief", report_dir)

    # Push
    push_title = f"Morning Brief {datetime.now().strftime('%m-%d')}"
    _push_report(push_title, f"## Morning Strategy\n\n{content}", config)

    log.info(f"Morning brief generated: {filepath}")
    return filepath


# ============================================================
# Midday Review 11:35
# ============================================================

def generate_midday_review(config: Config, north_fetcher: NorthFlowFetcher) -> Path | None:
    """Midday Review - Morning review + afternoon prediction"""
    report_cfg = config.report_cfg.get("Midday Review", {})
    if not report_cfg.get("启用", False):
        return None

    log.info("Generating midday review...")

    # Fetch quotes for both watchlist and holdings
    all_items = _get_unique_items(config)
    quotes = fetch_quotes(all_items)

    if not quotes:
        log.warning("Midday review: No quote data")
        return None

    # Separate quotes for analysis
    watchlist_codes = {item.code for item in config.watch_items}
    watch_quotes = [q for q in quotes if q.code in watchlist_codes]

    _, stats = analyze(watch_quotes, {}, config)
    nf = north_fetcher.fetch()

    lines = [
        f"It's now {datetime.now().strftime('%H:%M')}, morning trading has ended."
        f"Please generate an A-share midday review report."
    ]

    # Section 1: Morning Session Overview
    lines.append(f"\n## 1. 午间行情概览")
    lines.append(f"- 市场情绪评分: {stats.sentiment.score}/100 ({stats.sentiment.label})")
    lines.append(f"- 涨/跌/平盘数量: {stats.up} / {stats.down} / {stats.flat}")
    if nf:
        lines.append(f"- 北向资金: {nf.total_net:+.2f}亿")

    # Section 2: Watchlist Performance
    sorted_q = sorted(watch_quotes, key=lambda q: (q.change_pct or 0), reverse=True)
    lines.append("\n## 2. 核心观察标的 (Watchlist)")
    lines.append("### 涨幅榜前5:")
    for q in sorted_q[:5]:
        if q.change_pct is not None:
            lines.append(f"- {q.name}: {q.change_pct:+.2f}%")

    lines.append("### 跌幅榜前5:")
    for q in sorted_q[-5:]:
        if q.change_pct is not None:
            lines.append(f"- {q.name}: {q.change_pct:+.2f}%")

    # Section 3: Personal Holdings Analysis
    holdings = config.holdings
    if holdings:
        h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
        if h_results:
            lines.append(f"\n## 3. 个人持仓表现 (Holdings)")
            lines.append(f"- 持仓总成本: {total_cost:,.2f} | 午间总盈亏: {total_pnl:+,.2f}")
            for h in h_results:
                color = "🟢" if h["pnl"] >= 0 else "🔴"
                lines.append(f"  {color} {h['name']}({h['code']}): {h['amount']}股 | 盈亏: {h['pnl']:+,.2f} ({h['pnl_pct']:+.2f}%)")

    lines.append(f"""

请作为资深策略分析师，基于以上午盘数据，生成一份结构清晰的午评报告（约500字）：

1️⃣ **上午盘面总结**: 描述上午走势的特征（如冲高回落、缩量震荡等），点出主要影响因素
2️⃣ **热点与异动**: 观察标的中哪些板块或个股表现突出或异常，分析其原因
3️⃣ **持仓午间扫描**: 针对个人持仓（Holdings），简述其上午的表现，是否出现风险信号
4️⃣ **下午走势预测**: 基于上午的情绪和资金流向，预测下午的可能走势
5️⃣ **午间操作建议**: 下午是否需要进行调仓（补仓/减仓），给出具体的触发条件

要求：
- 视角：专业、敏锐，重点在于"预测下午"和"给出建议"。
- 格式：使用 Markdown 增强可读性，区分"自选"和"持仓"。
- 语气：务实，不拖泥带水。""")

    content = _call_llm("\n".join(lines), config)
    if not content:
        return None

    report_dir = Path(config.report_dir)
    filepath = _save_report(content, "Midday Review", report_dir)

    push_title = f"Midday Review {datetime.now().strftime('%m-%d')}"
    _push_report(push_title, f"## Midday Update\n\n{content}", config)

    log.info(f"Midday review generated: {filepath}")
    return filepath


# ============================================================
# Evening Review 16:00
# ============================================================

def _analyze_capital_flow(quote: Quote) -> str:
    """
    分析个股资金流向，判断主力/散户行为

    判断依据：
    1. 量价关系：放量上涨倾向于主力入场，缩量上涨可能是散户行为
    2. 开盘表现：高开高走且放量可能是主力
    3. 日内位置：收盘接近高点且放量倾向于主力
    4. 振幅与波动：大幅波动且放量可能是主力博弈
    """
    if not quote.price or not quote.open or not quote.high or not quote.low or not quote.pre_close:
        return "数据不足"

    # 计算关键指标
    change_pct = quote.change_pct or 0
    amplitude = quote.amplitude or 0
    volume = quote.volume or 0

    # 日内位置百分比 (0-100)
    if quote.high > quote.low:
        position = ((quote.price - quote.low) / (quote.high - quote.low)) * 100
    else:
        position = 50

    # 开盘溢价
    if quote.open > quote.pre_close:
        gap_up = True
        gap_pct = ((quote.open - quote.pre_close) / quote.pre_close) * 100
    else:
        gap_up = False
        gap_pct = 0

    # 主力/散户判断逻辑
    signals = []

    # 放量上涨 = 主力入场信号
    if change_pct > 1.5 and amplitude > 3:
        signals.append("主力资金入场")

    # 缩量上涨 = 散户行为或锁仓
    elif change_pct > 1 and amplitude < 2:
        signals.append("散户推动或锁仓上涨")

    # 放量下跌 = 主力出逃
    elif change_pct < -1.5 and amplitude > 3:
        signals.append("主力资金出逃")

    # 缩量下跌 = 散户抛售或惜售
    elif change_pct < -1 and amplitude < 2:
        signals.append("散户抛售或惜售")

    # 高位收盘 + 放量 = 强势主力
    if position > 80 and change_pct > 1:
        if not signals:
            signals.append("强势资金主导")
        else:
            signals[0] = signals[0].replace("资金", "强势资金")

    # 低位收盘 + 放量 = 恐慌抛盘
    elif position < 20 and change_pct < -1:
        if not signals:
            signals.append("恐慌抛压")

    # 高开低走 = 主力出货
    if gap_up and gap_pct > 1 and change_pct < 0:
        signals.append("高开低走，疑似出货")

    # 尾盘拉升 = 主力做盘
    if position > 85 and change_pct > 0.5 and amplitude > 2:
        signals.append("尾盘拉升，主力做盘")

    if signals:
        return "; ".join(signals)
    else:
        return "资金面中性"


def generate_evening_review(config: Config, north_fetcher: NorthFlowFetcher) -> Path | None:
    """Evening Review - Full day summary + next day strategy"""
    report_cfg = config.report_cfg.get("Evening Review", {})
    if not report_cfg.get("启用", False):
        return None

    log.info("Generating evening review...")

    holdings = config.holdings
    if not holdings:
        log.warning("Evening review: No holdings configured")
        return None

    holding_items = [
        WatchItem(name=h.name, code=h.code, market=h.market, type="持仓股")
        for h in holdings
    ]
    quotes = fetch_quotes(holding_items)

    if not quotes:
        log.warning("Evening review: No quote data")
        return None

    nf = north_fetcher.fetch()

    lines = [
        f"Market closed. Today is {datetime.now().strftime('%Y-%m-%d %H:%M')}, "
        f"Please generate a holdings-focused daily review report."
    ]

    # Section 1: Market Background
    lines.append(f"\n## 1. 市场背景")
    if nf:
        lines.append(f"- 北向资金: {nf.total_net:+.2f}亿")
    else:
        lines.append("- 北向资金: 暂无数据")

    # Section 2: Personal Holdings Analysis with Capital Flow
    h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
    if h_results:
        lines.append(f"\n## 2. 个人持仓表现 (Holdings)")
        lines.append(f"- 持仓总成本: {total_cost:,.2f} | 今日总盈亏: {total_pnl:+,.2f}")

        holdings_with_analysis = []
        for h in h_results:
            quote = next((q for q in quotes if q.code == h["code"]), None)
            if quote:
                tech_info = []
                if quote.high and quote.low and quote.price:
                    range_percent = ((quote.price - quote.low) / (quote.high - quote.low)) * 100 if (quote.high - quote.low) > 0 else 50
                    if range_percent > 80:
                        tech_info.append("处于日内高位")
                    elif range_percent < 20:
                        tech_info.append("处于日内低位")
                    else:
                        tech_info.append("日内震荡")

                if quote.amplitude and quote.amplitude > 3:
                    tech_info.append("波动较大")
                if quote.change_pct:
                    if quote.change_pct > 2:
                        tech_info.append("走势强劲")
                    elif quote.change_pct < -2:
                        tech_info.append("走势疲软")

                # 新增：资金流向分析
                capital_flow = _analyze_capital_flow(quote)

                holdings_with_analysis.append({
                    **h,
                    "quote": quote,
                    "tech": ", ".join(tech_info),
                    "capital_flow": capital_flow
                })

        lines.append("\n### 2.1 持仓详情")
        for h in holdings_with_analysis:
            color = "🟢" if h["pnl"] >= 0 else "🔴"
            tech_note = f" [{h['tech']}]" if h.get("tech") else ""
            lines.append(f"  {color} {h['name']}({h['code']}): {h['amount']}股 | 盈亏: {h['pnl']:+,.2f} ({h['pnl_pct']:+.2f}%){tech_note}")

        # 新增：资金流向分析报告
        lines.append("\n### 2.2 资金流向分析（主力/散户）")
        for h in holdings_with_analysis:
            flow_color = "🔵" if "主力" in h["capital_flow"] else "⚪"
            lines.append(f"  {flow_color} {h['name']}({h['code']}): {h['capital_flow']}")

    lines.append(f"""

请作为资深投资专家，基于以上数据，生成一份只围绕"个人持仓"的晚报，总字数约700字：

1️⃣ **持仓全景综述**: 总结今日持仓整体盈亏、强弱分化和仓位状态
2️⃣ **重点持仓点评**: 挑出表现最强、最弱、最值得警惕的持仓分别点评
3️⃣ **资金流向分析**: 分析各持仓的资金属性（主力/散户主导），判断后市方向
4️⃣ **持仓技术诊断**: 结合日内位置、波动、涨跌强弱，判断每类持仓的技术状态
5️⃣ **调仓与风控建议**: 明确哪些适合继续持有、逢高减仓、观察或止损
6️⃣ **明日操作清单**: 用清单形式给出次日最值得执行的动作和关注点

要求：
- 视角：从持仓管理和实盘操作出发，给出具可操作性的建议。
- 格式：使用 Markdown 标题、列表、加粗和 Emoji 增强可读性。
- 内容：不要分析自选股，不要输出观察标的板块点评，所有结论都必须落在当前持仓上。
- 语气：专业且辛辣，不模棱两可。
- 重点关注资金流向分析部分，明确指出主力资金动向。""")

    content = _call_llm("\n".join(lines), config)
    if not content:
        return None

    report_dir = Path(config.report_dir)
    filepath = _save_report(content, "Evening Review", report_dir)

    push_title = f"Evening Review {datetime.now().strftime('%m-%d')}"
    _push_report(push_title, f"## Evening Summary\n\n{content}", config)

    log.info(f"Evening review generated: {filepath}")
    return filepath
