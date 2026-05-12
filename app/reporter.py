"""
Investment Report Generator - Morning Brief, Midday Review, Evening Review
Generate actionable strategy reports from a senior investment expert perspective
"""

from __future__ import annotations
import re
from datetime import datetime, timedelta
from pathlib import Path

from app.models import Quote, Alert, AnalysisStats, SentimentResult, Holding
from app.config import Config
from app.data_fetcher import fetch_quotes, NorthFlowFetcher
from app.analyzer import analyze, calc_market_sentiment
from app.utils import log
from app.http_client import sina_client, serverchan_client
from app.llm_client import get_llm_client, SYSTEM_PROMPTS


# ============================================================
# Holdings P&L Calculation
# ============================================================

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
            data={"text": title, "desp": content},
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
        response = llm.chat(system_prompt, prompt, max_tokens=2000)
        return response.strip()
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

    # 2. Get yesterday's A股 data (also fetch once during non-trading hours)
    quotes = fetch_quotes(config.watch_items)

    # 3. Build prompt
    lines = [
        f"现在是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，A股即将开盘。"
        f"请生成一份完整的早盘策略报告。"
    ]

    # Overnight US market
    if global_data:
        lines.append("\n## 隔夜市场")
        lines.append(f"- 道琼斯: {global_data.get('道琼斯', '')}")
        lines.append(f"- 纳斯达克: {global_data.get('纳斯达克', '')}")
        lines.append(f"- 标普500: {global_data.get('标普500', '')}")
        lines.append(f"- A50期货: {global_data.get('A50期货', '')}")
        lines.append(f"- 恒生指数: {global_data.get('恒生指数', '')}")
        lines.append(f"- 汇率: {global_data.get('汇率', '')}")

    # Market sentiment from yesterday
    if quotes:
        sentiment = calc_market_sentiment(quotes)
        lines.append(f"\n## 昨日情绪")
        lines.append(f"- 情绪评分: {sentiment.score}/100 ({sentiment.label})")

    lines.append(f"""

Please provide analysis from the following 5 aspects, within 300 words:

1️⃣ **Overnight Review**: Key movements in US stocks, A50 futures, and Hong Kong market
2️⃣ **A-share Strategy**: Today's expected trend, key support/resistance levels
3️⃣ **Hot Sectors**: Which sectors are likely to perform well today?
4️⃣ **Position Impact**: Based on your holdings, what's the operation suggestion for today?
5️⃣ **Risk Reminders**: What risks need attention today?

Requirements: Professional and practical, data-supported, actionable for ordinary investors.""")

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

    # Get morning intraday data
    items = config.watch_items
    quotes = fetch_quotes(items)

    if not quotes:
        log.warning("Midday review: No quote data")
        return None

    # Analysis
    alerts, stats = analyze(quotes, {}, config)

    # Northbound funds
    nf = north_fetcher.fetch()

    lines = [
        f"It's now {datetime.now().strftime('%H:%M')}, morning trading has ended."
        f"Please generate an A-share midday review report."
    ]

    # Morning data
    lines.append(f"\n## Morning Session Data")
    lines.append(f"- Sentiment Score: {stats.sentiment.score}/100 ({stats.sentiment.label})")
    lines.append(f"- Up/Down/Flat: {stats.up} / {stats.down} / {stats.flat}")
    lines.append(f"- Alerts: {stats.alert_count}")

    if nf:
        lines.append(f"- Northbound Funds: {nf.total_net:+.0f}亿")

    # Morning rankings
    sorted_q = sorted(quotes, key=lambda q: (q.change_pct or 0), reverse=True)
    lines.append("\n## Top 5 Gainers")
    for q in sorted_q[:5]:
        if q.change_pct is not None:
            lines.append(f"- {q.name}: {q.change_pct:+.2f}%")

    lines.append("\n## Top 5 Losers")
    for q in sorted_q[-5:]:
        if q.change_pct is not None:
            lines.append(f"- {q.name}: {q.change_pct:+.2f}%")

    if alerts:
        lines.append("\n## Morning Alerts")
        for a in alerts[:5]:
            lines.append(f"- {a.name}: {' | '.join(a.messages)}")

    # Holdings analysis with technical patterns
    holdings = config.holdings
    if holdings:
        h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
        if h_results:
            lines.append(f"\n## Your Holdings Morning Performance")
            lines.append(f"- Cost: {total_cost:,.0f} | P&L: {total_pnl:+,.0f}")

            # Find matching quotes for technical analysis
            holdings_with_tech = []
            for h in h_results:
                quote = next((q for q in quotes if q.code == h["code"]), None)
                if quote:
                    # Technical analysis
                    tech_info = []
                    if quote.high and quote.low and quote.price:
                        # Price position within day range
                        range_percent = ((quote.price - quote.low) / (quote.high - quote.low)) * 100 if (quote.high - quote.low) > 0 else 50
                        if range_percent > 80:
                            tech_info.append("near high")
                        elif range_percent < 20:
                            tech_info.append("near low")
                        else:
                            tech_info.append("mid range")

                    if quote.amplitude:
                        if quote.amplitude > 3:
                            tech_info.append("high volatility")
                        elif quote.amplitude < 1:
                            tech_info.append("low volatility")

                    holdings_with_tech.append({**h, "quote": quote, "tech": ", ".join(tech_info)})

            for h in holdings_with_tech:
                color = "🟢" if h["pnl"] >= 0 else "🔴"
                tech_note = f" [{h['tech']}]" if h.get("tech") else ""
                lines.append(f"  {color} {h['name']}: {h['amount']} shares "
                             f"| P&L {h['pnl']:+,.0f} ({h['pnl_pct']:+.2f}%){tech_note}")

    lines.append(f"""

Please provide analysis from the following 6 aspects, within 300 words:

1️⃣ **Morning Review**: Morning trend characteristics, major player movements
2️⃣ **Sector Rotation**: Which sectors are strong/weak, any style shift signals?
3️⃣ **Fund Flow**: Combining northbound funds and price-volume, judge capital sentiment
4️⃣ **Holdings Technical Analysis**: Analyze each holding's technical pattern (price position, volatility, support/resistance levels)
5️⃣ **Position Impact**: Based on your holdings P&L and technical patterns, give afternoon operation suggestions
6️⃣ **Afternoon Prediction**: Likely afternoon trend direction, what to watch for

Requirements: Concise, clear views, suitable for quick intraday reading.""")

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

def generate_evening_review(config: Config, north_fetcher: NorthFlowFetcher) -> Path | None:
    """Evening Review - Full day summary + next day strategy"""
    report_cfg = config.report_cfg.get("Evening Review", {})
    if not report_cfg.get("启用", False):
        return None

    log.info("Generating evening review...")

    items = config.watch_items
    quotes = fetch_quotes(items)

    if not quotes:
        log.warning("Evening review: No quote data")
        return None

    alerts, stats = analyze(quotes, {}, config)
    nf = north_fetcher.fetch()

    lines = [
        f"Market closed. Today is {datetime.now().strftime('%Y-%m-%d %H:%M')}, "
        f"Please generate a complete A-share closing analysis report."
    ]

    # Full day data
    lines.append(f"\n## Today's Market Data")
    lines.append(f"- Sentiment Score: {stats.sentiment.score}/100 ({stats.sentiment.label})")
    lines.append(f"- Up/Down/Flat: {stats.up} / {stats.down} / {stats.flat}")
    lines.append(f"- Alerts: {stats.alert_count}")

    if nf:
        lines.append(f"- Northbound Funds: {nf.total_net:+.0f}亿")

    # Full day rankings
    sorted_q = sorted(quotes, key=lambda q: (q.change_pct or 0), reverse=True)
    lines.append("\n## Today's Top Gainers")
    for q in sorted_q[:5]:
        if q.change_pct is not None:
            lines.append(f"- {q.name}: {q.change_pct:+.2f}%")

    lines.append("\n## Today's Top Losers")
    for q in sorted_q[-5:]:
        if q.change_pct is not None:
            lines.append(f"- {q.name}: {q.change_pct:+.2f}%")

    if alerts:
        lines.append("\n## Today's Alerts")
        for a in alerts[:5]:
            lines.append(f"- {a.name}: {' | '.join(a.messages)}")

    # Holdings analysis with technical patterns
    holdings = config.holdings
    if holdings:
        h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
        if h_results:
            lines.append(f"\n## Your Holdings Today's Performance")
            lines.append(f"- Cost: {total_cost:,.0f} | Total P&L: {total_pnl:+,.0f}")

            # Find matching quotes for technical analysis
            holdings_with_tech = []
            for h in h_results:
                quote = next((q for q in quotes if q.code == h["code"]), None)
                if quote:
                    # Technical analysis
                    tech_info = []
                    if quote.high and quote.low and quote.price:
                        # Price position within day range
                        range_percent = ((quote.price - quote.low) / (quote.high - quote.low)) * 100 if (quote.high - quote.low) > 0 else 50
                        if range_percent > 80:
                            tech_info.append("near high")
                        elif range_percent < 20:
                            tech_info.append("near low")
                        else:
                            tech_info.append("mid range")

                    if quote.amplitude:
                        if quote.amplitude > 3:
                            tech_info.append("high volatility")
                        elif quote.amplitude < 1:
                            tech_info.append("low volatility")

                    # Check for price patterns
                    if quote.change_pct:
                        if quote.change_pct > 2:
                            tech_info.append("strong up")
                        elif quote.change_pct < -2:
                            tech_info.append("strong down")

                    holdings_with_tech.append({**h, "quote": quote, "tech": ", ".join(tech_info)})

            for h in holdings_with_tech:
                color = "🟢" if h["pnl"] >= 0 else "🔴"
                tech_note = f" [{h['tech']}]" if h.get("tech") else ""
                lines.append(f"  {color} {h['name']}: {h['amount']} shares "
                             f"| P&L {h['pnl']:+,.0f} ({h['pnl_pct']:+.2f}%){tech_note}")

    lines.append(f"""

Please provide analysis from the following 7 aspects, within 500 words:

1️⃣ **Full Day Summary**: Today's trend characteristics, key turning points and capital sentiment
2️⃣ **Sector Analysis**: Strongest and weakest sectors, sustainability
3️⃣ **Sentiment Analysis**: Combining sentiment score and northbound funds, judge market temperature
4️⃣ **Holdings Technical Analysis**: Analyze each holding's technical pattern (price position in day range, volatility, trend strength, support/resistance levels)
5️⃣ **Position Analysis**: Based on your holdings' today performance and technical patterns, how to handle tomorrow
6️⃣ **Risks & Opportunities**: ⚠️ **Special attention to cyclical sectors** (steel, coal, non-ferrous, chemical, building materials, etc.)
   Their current valuation levels, inventory cycle position,
   Any policy catalysts or suppression factors, short-term operation suggestions
7️⃣ **Tomorrow's Strategy**: What to focus on tomorrow, how to adjust position

Requirements: From a retail investor perspective, provide actionable tomorrow operation reference.""")

    content = _call_llm("\n".join(lines), config)
    if not content:
        return None

    report_dir = Path(config.report_dir)
    filepath = _save_report(content, "Evening Review", report_dir)

    push_title = f"Evening Review {datetime.now().strftime('%m-%d')}"
    _push_report(push_title, f"## Evening Summary\n\n{content}", config)

    log.info(f"Evening review generated: {filepath}")
    return filepath
