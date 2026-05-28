"""
Investment Report Generator - Morning Brief, Midday Review, Evening Review
Generate actionable strategy reports from a senior investment expert perspective
"""

from __future__ import annotations
from datetime import datetime
from pathlib import Path

from app.models import WatchItem, Quote, Holding
from app.config import Config
from app.data_fetcher import fetch_quotes, fetch_quotes_rich
from app.analyzer import analyze, calc_market_sentiment
from app.utils import log
from app.http_client import serverchan_client
from app.llm_client import get_llm_client, SYSTEM_PROMPTS
from app.dragon_tiger import (
    fetch_dragon_tiger_list,
    analyze_dragon_tiger,
    format_dragon_tiger_report,
    build_llm_context,
)


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
) -> tuple[list[dict], float, float]:
    """
    Match holdings with real-time quotes, calculate P&L
    Returns: (results, total_pnl, total_cost)
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


def _get_holdings_strategy_signals(
    holdings: list[Holding],
    quotes: list[Quote],
) -> list[dict]:
    """获取持仓的组合策略信号

    返回每个持仓的组合策略信号列表。
    网络异常时返回空列表。
    """
    from app.technical import (
        fetch_historical_kline,
        get_technical_summary,
    )
    from app.strategy import evaluate_all_strategies, calc_macd_dif_series
    from app.models import TechSnapshot, tech_snapshot_to_summary
    from app.analyzer import _load_scan_history
    from concurrent.futures import ThreadPoolExecutor

    results: list[dict] = []
    quotes_map = {q.code: q for q in quotes}

    # 加载扫描历史，获取前一次技术快照
    scan_history = _load_scan_history()
    prev_tech_map: dict[str, "TechnicalSummary"] = {}
    if scan_history:
        last_record = scan_history[-1]
        for code, status in last_record.funds_status.items():
            if status.tech_snapshot:
                prev_tech_map[code] = tech_snapshot_to_summary(status.tech_snapshot)

    def _eval_one(h: Holding) -> dict | None:
        quote = quotes_map.get(h.code)
        if not quote:
            return None

        klines = fetch_historical_kline(h.code, h.market, days=60)
        if not klines:
            return None

        tech = get_technical_summary(quote, klines)
        prev_tech = prev_tech_map.get(h.code)
        closes = [k.close for k in klines if k.close is not None]
        dif_vals = calc_macd_dif_series(closes) if closes else None

        signals = evaluate_all_strategies(tech, prev_tech, quote, klines, dif_vals, closes)
        triggering = [s for s in signals if s.is_triggering]

        # 显示所有持仓状态，即使没有触发信号
        if triggering:
            return {
                "name": h.name,
                "code": h.code,
                "signals": [s.to_alert_text() for s in triggering],
            }
        else:
            # 没有触发信号时显示"无信号"
            return {
                "name": h.name,
                "code": h.code,
                "signals": ["⚪ [无信号] 当前无满足条件的策略"],
            }

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_eval_one, h): h.code for h in holdings}
        for future in futures:
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                log.warning(f"组合策略评估失败 [{futures[future]}]: {e}")

    return results


def _get_holdings_tech_analysis(
    holdings: list[Holding],
    quotes: list[Quote],
) -> list[dict]:
    """获取持仓的技术分析数据（支撑/压力位、量价关系、技术指标）

    返回每个持仓的技术分析字典列表。
    网络异常时返回空列表。
    """
    from app.technical import (
        fetch_historical_kline,
        calc_support_resistance,
        analyze_volume_price,
        calc_rsi,
        calc_macd,
        calc_kdj,
        calc_obv,
        rsi_signal,
    )
    from concurrent.futures import ThreadPoolExecutor

    results: list[dict] = []

    def _fetch_one(h: Holding) -> dict | None:
        quote = next((q for q in quotes if q.code == h.code), None)
        if not quote:
            return None

        # 获取 60 日 K 线（MACD 需要至少 35 天，RSI 需要 15 天，60 天留足余量）
        klines = fetch_historical_kline(h.code, h.market, days=60)
        if not klines:
            return None

        # 支撑/压力位
        sr = calc_support_resistance(klines)

        # 量价关系
        vol_price = analyze_volume_price(quote, klines)

        # 技术指标
        closes = [k.close for k in klines if k.close is not None]
        highs = [k.high for k in klines if k.high is not None]
        lows = [k.low for k in klines if k.low is not None]

        rsi_val = calc_rsi(closes)
        macd = calc_macd(closes)
        kdj = calc_kdj(highs, lows, closes)
        obv = calc_obv(klines)

        # 综合支撑/压力位描述
        support_parts = []
        if sr.support:
            support_parts.append(f"主支撑:{sr.support:.3f}")
        if sr.swing_supports:
            support_parts.append(f"摆动支撑:{','.join(f'{s:.3f}' for s in sr.swing_supports[:2])}")
        if sr.pivot_supports:
            support_parts.append(f"枢轴支撑:{sr.pivot_supports[0]:.3f}")

        resistance_parts = []
        if sr.resistance:
            resistance_parts.append(f"主压力:{sr.resistance:.3f}")
        if sr.swing_resistances:
            resistance_parts.append(f"摆动压力:{','.join(f'{r:.3f}' for r in sr.swing_resistances[:2])}")
        if sr.pivot_resistances:
            resistance_parts.append(f"枢轴压力:{sr.pivot_resistances[0]:.3f}")

        return {
            "name": h.name,
            "code": h.code,
            "price": quote.price,
            "change_pct": quote.change_pct,
            "support": sr.support,
            "resistance": sr.resistance,
            "support_desc": ";".join(support_parts) if support_parts else "数据不足",
            "resistance_desc": ";".join(resistance_parts) if resistance_parts else "数据不足",
            "atr": sr.atr,
            "vol_price": vol_price,
            "rsi": rsi_val,
            "rsi_signal": rsi_signal(rsi_val) if rsi_val else "数据不足",
            "macd_signal": macd.signal,
            "macd_dif": macd.dif,
            "kdj_signal": kdj.signal,
            "kdj_k": kdj.k,
            "obv": obv.obv,
            "obv_signal": obv.signal,
            "volume": quote.volume,
            "turnover": quote.turnover_rate,
            "volume_clusters": sr.volume_clusters,
        }

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_fetch_one, h): h.code for h in holdings}
        for future in futures:
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                log.warning(f"技术分析获取失败 [{futures[future]}]: {e}")

    return results


# ============================================================
# Report Saving & Pushing
# ============================================================

SEPARATOR = "═" * 43


def _build_report(data_section: str, llm_content: str | None) -> str:
    """拼接数据区 + 分析区为完整报告"""
    if llm_content:
        return f"{data_section}\n\n{SEPARATOR}\n\n## AI 分析\n\n{llm_content}"
    return data_section


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


def _call_llm(prompt: str, config: Config, role: str = "analyst", temperature: float = 0.3, max_tokens: int = 2000) -> str | None:
    """Call LLM to generate analysis content

    Args:
        prompt: User prompt
        config: Config object
        role: System prompt role key (analyst/morning_brief/midday_review/evening_review)
        temperature: Temperature parameter (0.3 default, 0.4 for morning_brief)
        max_tokens: Max tokens for response
    """
    if not config.llm_enabled or not config.deepseek_key:
        return None

    try:
        llm = get_llm_client(config)
        system_prompt = SYSTEM_PROMPTS.get(role, "")
        response = llm.chat(prompt, system_prompt=system_prompt, max_tokens=max_tokens, temperature=temperature, timeout=120)
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

    # 1. Fetch data
    from app.data_fetcher import fetch_global_markets, fetch_market_news
    global_data = fetch_global_markets()
    morning_news = fetch_market_news(start_hour=0, end_hour=9, max_count=15)
    all_items = _get_unique_items(config)
    quotes = fetch_quotes_rich(all_items)

    # 2. Build data section (shown in report)
    data_lines = []

    if global_data:
        data_lines.append("## 一、隔夜市场（截至北京时间 05:00，仅供参考）")
        for k, v in global_data.items():
            data_lines.append(f"- {k}: {v}")

    if morning_news:
        data_lines.append("\n## 二、早间要闻（供参考）")
        for n in morning_news:
            cat = f" [{n.category}]" if n.category else ""
            data_lines.append(f"- [{n.time}]{cat} {n.title}")

    if quotes:
        # 使用全部标的（持仓+自选）计算情绪评分
        all_quotes = [q for q in quotes if q.change_pct is not None]
        sentiment = calc_market_sentiment(all_quotes)
        data_lines.append(f"\n## 三、昨日A股收盘数据")
        data_lines.append(f"- 情绪评分: {sentiment.score}/100 ({sentiment.label})")

        index_quotes = [q for q in quotes if q.type == "指数"]
        for idx in index_quotes:
            close = f"{idx.price:.2f}" if idx.price else "--"
            chg = f"{idx.change_pct:+.2f}%" if idx.change_pct is not None else "--"
            data_lines.append(f"- {idx.name}: {close} ({chg})")

        holdings = config.holdings
        if holdings:
            h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
            if h_results:
                data_lines.append(f"\n## 四、持仓概况（昨收，开盘前参考）")
                for h in h_results[:5]:
                    data_lines.append(f"  - {h['name']}（昨收）")

            # 组合策略信号展示
            strat_sigs = _get_holdings_strategy_signals(holdings, quotes)
            if strat_sigs:
                data_lines.append(f"\n## 五、⭐ 组合策略信号（多指标共振）")
                for s in strat_sigs:
                    for sig_text in s['signals']:
                        data_lines.append(f"  - {s['name']}: {sig_text}")

    data_section = "\n".join(data_lines) if data_lines else "暂无数据"

    # 4. Fetch technical analysis for holdings (for LLM prompt)
    holdings = config.holdings
    tech_data = _get_holdings_tech_analysis(holdings, quotes) if holdings and quotes else []

    # 4.5. Fetch combination strategy signals for holdings
    strategy_signals = _get_holdings_strategy_signals(holdings, quotes) if holdings and quotes else []

    # 5. Build LLM prompt (compact data format)
    llm_lines = [
        f"现在是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，A股即将开盘。"
        f"请生成一份完整的早盘策略报告。"
    ]

    if global_data:
        llm_lines.append("\n[隔夜市场]")
        for k, v in global_data.items():
            llm_lines.append(f"  {k}: {v}")

    if morning_news:
        llm_lines.append("\n[早间要闻]")
        for n in morning_news:
            llm_lines.append(f"  [{n.time}] {n.title}")

    if quotes:
        # 使用全部标的（持仓+自选）计算情绪评分
        all_quotes_llm = [q for q in quotes if q.change_pct is not None]
        sentiment = calc_market_sentiment(all_quotes_llm)
        llm_lines.append(f"\n[昨日A股] 情绪: {sentiment.score}/100 ({sentiment.label})")

        index_quotes = [q for q in quotes if q.type == "指数"]
        for idx in index_quotes:
            close = f"{idx.price:.2f}" if idx.price else "--"
            chg = f"{idx.change_pct:+.2f}%" if idx.change_pct is not None else "--"
            llm_lines.append(f"  {idx.name}: {close} ({chg})")

        h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes) if holdings else ([], 0, 0)
        if h_results:
            llm_lines.append(f"\n[持仓]")
            for h in h_results[:5]:
                llm_lines.append(f"  {h['name']}")

    if tech_data:
        llm_lines.append("\n[持仓技术分析]")
        for t in tech_data:
            parts = [f"  {t['name']}:"]
            if t.get('support_desc'):
                parts.append(t['support_desc'])
            if t.get('resistance_desc'):
                parts.append(t['resistance_desc'])
            if t.get('volume_clusters'):
                clusters = t['volume_clusters']
                parts.append(f"成交密集区:{','.join(f'{c:.3f}' for c in clusters[:2])}")
            parts.append(f"量价:{t['vol_price']}")
            if t['rsi']:
                parts.append(f"RSI:{t['rsi']:.1f}({t['rsi_signal']})")
            parts.append(f"MACD:{t['macd_signal']}")
            parts.append(f"KDJ:{t['kdj_signal']}")
            if t.get('obv') is not None:
                parts.append(f"OBV:{t['obv']:.0f}({t['obv_signal']})")
            llm_lines.append(" ".join(parts))

    if strategy_signals:
        llm_lines.append("\n[⭐ 组合策略信号（多指标共振，高优先级）]")
        for s in strategy_signals:
            for sig_text in s['signals']:
                llm_lines.append(f"  {s['name']}: {sig_text}")

    llm_lines.append(f"""

请按以下结构生成早盘简报（约 600 字，关键判断标注置信度）：

### 一、隔夜传导判断（置信度：[高/中/低]）
- 隔夜美股/A50/港股/汇率的综合影响是正面、中性还是负面？
- 推导逻辑：外盘涨跌 → 对应的 A 股传导链条是什么？

### 二、今日走势推演
给出两种情景及概率：
- **基准情景（60%+）**：认为最可能的走势，给出波动区间参考
- **风险情景（20-30%）**：如果什么超预期事件发生会导致走势偏离预期
- **关键观察点**：开盘后前 30 分钟的量价特征

### 三、持仓应对预案（表格形式）
| 持仓 | 技术状态 | 若高开 | 若平开 | 若低开 | 止损位 |
|------|---------|--------|--------|--------|--------|
| ... | ... | ... | ... | ... | ... |

### 四、风险预警
今日最需要关注的 1-2 个风险点。

要求：每一个判断都必须标注置信度。如果数据不足以支撑判断，如实说"数据不足"。使用 Markdown 格式增强可读性。

---
**置信度标注规则**：对于每一个判断性结论，请在括号中标注你的确定程度。
- [高]：数据充分、指标一致、历史模式明确
- [中]：数据尚可但存在分歧信号
- [低]：数据不足或逻辑链条不完整
**不确定性处理**：如果数据不足以支持判断，请直接输出"数据不足"而非强行给出结论。""")

    # 6. Call LLM
    llm_content = _call_llm("\n".join(llm_lines), config, role="morning_brief", temperature=0.4, max_tokens=2000)
    if not llm_content:
        log.warning("Morning brief: LLM generation failed")

    # 7. Build complete report
    report = _build_report(data_section, llm_content)

    # 8. Save & push
    report_dir = Path(config.report_dir)
    filepath = _save_report(report, "Morning Brief", report_dir)

    push_title = f"Morning Brief {datetime.now().strftime('%m-%d')}"
    _push_report(push_title, report, config)

    log.info(f"Morning brief generated: {filepath}")
    return filepath


# ============================================================
# Midday Review 11:35
# ============================================================

def generate_midday_review(config: Config) -> Path | None:
    """Midday Review - Morning review + afternoon prediction"""
    report_cfg = config.report_cfg.get("Midday Review", {})
    if not report_cfg.get("启用", False):
        return None

    log.info("Generating midday review...")

    # 1. Fetch data
    all_items = _get_unique_items(config)
    quotes = fetch_quotes_rich(all_items)

    if not quotes:
        log.warning("Midday review: No quote data")
        return None

    # 使用全部标的（持仓+自选）进行统计和排行
    all_quotes = [q for q in quotes if q.change_pct is not None]
    _, stats = analyze(all_quotes, {}, config)
    from app.data_fetcher import fetch_market_news
    morning_news = fetch_market_news(start_hour=9, end_hour=12, max_count=10)

    # 2. Build data section
    data_lines = []

    if morning_news:
        data_lines.append("## 一、上午快讯（供参考）")
        for n in morning_news:
            cat = f" [{n.category}]" if n.category else ""
            data_lines.append(f"- [{n.time}]{cat} {n.title}")

    data_lines.append(f"\n## 二、行情概览")
    data_lines.append(f"- 情绪评分: {stats.sentiment.score}/100 ({stats.sentiment.label})")
    data_lines.append(f"- 涨/跌/平: {stats.up} / {stats.down} / {stats.flat}")

    sorted_q = sorted(all_quotes, key=lambda q: q.change_pct, reverse=True)
    data_lines.append("\n## 三、全部标的排行")

    # 涨幅前5：取涨幅最大的5个
    data_lines.append("### 涨幅前5:")
    for q in sorted_q[:5]:
        detail = f"- {q.name}: {q.change_pct:+.2f}%"
        if q.pe_ratio is not None:
            detail += f" | PE:{q.pe_ratio:.1f}"
        if q.market_cap is not None:
            detail += f" | 市值:{q.market_cap/1e4:.0f}亿"
        if q.turnover_rate is not None:
            detail += f" | 换手:{q.turnover_rate:.2f}%"
        data_lines.append(detail)

    # 跌幅前5：取跌幅最大的5个（即涨幅最小的5个）
    data_lines.append("### 跌幅前5:")
    for q in sorted_q[-5:]:
        detail = f"- {q.name}: {q.change_pct:+.2f}%"
        if q.pe_ratio is not None:
            detail += f" | PE:{q.pe_ratio:.1f}"
        if q.market_cap is not None:
            detail += f" | 市值:{q.market_cap/1e4:.0f}亿"
        if q.turnover_rate is not None:
            detail += f" | 换手:{q.turnover_rate:.2f}%"
        data_lines.append(detail)

    holdings = config.holdings
    if holdings:
        h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
        if h_results:
            data_lines.append(f"\n## 四、持仓午间扫描")
            for h in h_results:
                data_lines.append(f"  - {h['name']}({h['code']}): {h['amount']}股")

    # Technical analysis for holdings
    tech_data = _get_holdings_tech_analysis(holdings, quotes) if holdings else []
    strategy_signals = _get_holdings_strategy_signals(holdings, quotes) if holdings else []
    if tech_data:
        data_lines.append("\n## 五、持仓技术分析")
        data_lines.append("")
        data_lines.append("| 标的 | 现价 | 涨跌幅 | 支撑位详情 | 压力位详情 | 成交密集区 | ATR | 量价关系 | RSI | MACD | KDJ | OBV | 成交量 | 换手率 |")
        data_lines.append("|------|------|--------|------------|------------|------------|-----|----------|-----|------|-----|-----|--------|--------|")
        for t in tech_data:
            price = f"{t['price']:.3f}" if t.get('price') else "--"
            chg = f"{t['change_pct']:+.2f}%" if t.get('change_pct') is not None else "--"
            sup = t.get('support_desc', '--')
            res = t.get('resistance_desc', '--')
            clusters = t.get('volume_clusters')
            cluster_str = f"{','.join(f'{c:.3f}' for c in clusters[:2])}" if clusters else "--"
            atr = f"{t['atr']:.3f}" if t['atr'] else "--"
            rsi = f"{t['rsi']:.1f}({t['rsi_signal']})" if t['rsi'] else "--"
            macd = f"DIF:{t['macd_dif']:.4f}({t['macd_signal']})" if t['macd_dif'] is not None else "--"
            kdj = f"K:{t['kdj_k']:.1f}({t['kdj_signal']})" if t['kdj_k'] is not None else "--"
            obv_val = f"{t['obv']:.0f}({t['obv_signal']})" if t.get('obv') is not None else "--"
            vol = f"{t['volume']/10000:.0f}万" if t.get('volume') and t['volume'] > 0 else "--"
            tr = f"{t['turnover']:.2f}%" if t.get('turnover') else "--"
            data_lines.append(f"| {t['name']} | {price} | {chg} | {sup} | {res} | {cluster_str} | {atr} | {t['vol_price']} | {rsi} | {macd} | {kdj} | {obv_val} | {vol} | {tr} |")

    if strategy_signals:
        data_lines.append(f"\n## 六、⭐ 组合策略信号（多指标共振，下午操作参考）")
        for s in strategy_signals:
            for sig_text in s['signals']:
                data_lines.append(f"  - {s['name']}: {sig_text}")

    data_section = "\n".join(data_lines) if data_lines else "暂无数据"

    # 3. Build LLM prompt (compact)
    llm_lines = [
        f"现在是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，上午交易结束。"
        f"请生成一份A股午评报告。"
    ]

    if morning_news:
        llm_lines.append("\n[上午快讯]")
        for n in morning_news:
            llm_lines.append(f"  [{n.time}] {n.title}")

    llm_lines.append(f"\n[行情] 情绪: {stats.sentiment.score}/100 ({stats.sentiment.label}), 涨跌平: {stats.up}/{stats.down}/{stats.flat}")

    llm_lines.append("\n[自选-涨幅前5]")
    for q in sorted_q[:5]:
        if q.change_pct is not None:
            detail = f"  {q.name}: {q.change_pct:+.2f}%"
            if q.pe_ratio is not None:
                detail += f" PE:{q.pe_ratio:.1f}"
            if q.market_cap is not None:
                detail += f" 市值:{q.market_cap/1e4:.0f}亿"
            if q.turnover_rate is not None:
                detail += f" 换手:{q.turnover_rate:.2f}%"
            llm_lines.append(detail)

    llm_lines.append("[自选-跌幅前5]")
    for q in sorted_q[-5:]:
        if q.change_pct is not None:
            detail = f"  {q.name}: {q.change_pct:+.2f}%"
            if q.pe_ratio is not None:
                detail += f" PE:{q.pe_ratio:.1f}"
            if q.market_cap is not None:
                detail += f" 市值:{q.market_cap/1e4:.0f}亿"
            if q.turnover_rate is not None:
                detail += f" 换手:{q.turnover_rate:.2f}%"
            llm_lines.append(detail)

    if holdings:
        h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
        if h_results:
            llm_lines.append(f"\n[持仓]")
            for h in h_results:
                llm_lines.append(f"  {h['name']}")

    if tech_data:
        llm_lines.append("\n[技术分析]")
        for t in tech_data:
            parts = [f"  {t['name']}:"]
            if t.get('support_desc'):
                parts.append(t['support_desc'])
            if t.get('resistance_desc'):
                parts.append(t['resistance_desc'])
            if t.get('volume_clusters'):
                clusters = t['volume_clusters']
                parts.append(f"成交密集区:{','.join(f'{c:.3f}' for c in clusters[:2])}")
            parts.append(f"量价:{t['vol_price']}")
            if t['rsi']:
                parts.append(f"RSI:{t['rsi']:.1f}({t['rsi_signal']})")
            parts.append(f"MACD:{t['macd_signal']}")
            parts.append(f"KDJ:{t['kdj_signal']}")
            if t.get('obv') is not None:
                parts.append(f"OBV:{t['obv']:.0f}({t['obv_signal']})")
            llm_lines.append(" ".join(parts))

    if strategy_signals:
        llm_lines.append("\n[⭐ 组合策略信号（多指标共振，高优先级）]")
        for s in strategy_signals:
            for sig_text in s['signals']:
                llm_lines.append(f"  {s['name']}: {sig_text}")

    llm_lines.append(f"""

请作为资深策略分析师，基于以上午盘数据，生成一份结构清晰的午评报告（约500字）：

1️⃣ **上午盘面总结**: 描述上午走势的特征（如冲高回落、缩量震荡等），点出主要影响因素
2️⃣ **热点与异动**: 观察标的中哪些板块或个股表现突出或异常，结合换手率和市值判断市场风格
3️⃣ **持仓午间扫描**: 针对个人持仓（Holdings），简述其上午的表现，是否出现风险信号
4️⃣ **下午走势预测**: 基于上午的情绪和资金流向，预测下午的可能走势
5️⃣ **午间操作建议**: 下午是否需要进行调仓（补仓/减仓），给出具体的触发条件

要求：
- 视角：专业、敏锐，重点在于"预测下午"和"给出建议"。
- 格式：使用 Markdown 增强可读性，区分"自选"和"持仓"。
- 语气：务实，不拖泥带水。

---
**置信度标注规则**：对于每一个判断性结论，请在括号中标注你的确定程度。
- [高]：数据充分、指标一致、历史模式明确
- [中]：数据尚可但存在分歧信号
- [低]：数据不足或逻辑链条不完整
**不确定性处理**：如果数据不足以支持判断，请直接输出"数据不足"而非强行给出结论。""")

    # 4. Call LLM
    llm_content = _call_llm("\n".join(llm_lines), config, role="midday_review", temperature=0.3, max_tokens=1500)
    if not llm_content:
        log.warning("Midday review: LLM generation failed")

    # 5. Build & save
    report = _build_report(data_section, llm_content)
    report_dir = Path(config.report_dir)
    filepath = _save_report(report, "Midday Review", report_dir)

    push_title = f"Midday Review {datetime.now().strftime('%m-%d')}"
    _push_report(push_title, report, config)

    log.info(f"Midday review generated: {filepath}")
    return filepath


# ============================================================
# Evening Review 16:00
# ============================================================

def _simple_attribution(h_results: list[dict], market_return: float | None) -> dict:
    """简单的持仓归因：将持仓收益拆解为 β（市场）和 α（选股）部分

    Args:
        h_results: _holdings_summary 的返回结果列表
        market_return: 大盘当日涨跌幅（小数，如 0.02 表示 2%），为 None 时跳过归因

    Returns:
        包含 beta_pnl, alpha_pnl, total_pnl 的字典
    """
    total_pnl = sum(h["pnl"] for h in h_results)
    total_cost = sum(h["cost"] * h["amount"] for h in h_results)

    if market_return is not None and total_cost > 0:
        beta_pnl = total_cost * market_return
        alpha_pnl = total_pnl - beta_pnl
        return {
            "beta_pnl": round(beta_pnl, 2),
            "alpha_pnl": round(alpha_pnl, 2),
            "total_pnl": round(total_pnl, 2),
        }
    return {
        "beta_pnl": None,
        "alpha_pnl": None,
        "total_pnl": round(total_pnl, 2),
    }


def _analyze_capital_flow(quote: Quote, prev_volume: float | None = None) -> str:
    """
    分析个股资金流向，判断主力/散户行为

    判断依据：
    1. 真实成交量倍率：当日成交量 / 前日成交量（核心指标）
    2. 量价关系：放量上涨倾向于主力入场，缩量上涨可能是散户行为
    3. 开盘表现：高开高走且放量可能是主力
    4. 日内位置：收盘接近高点且放量倾向于主力
    5. 振幅与波动：大幅波动且放量可能是主力博弈

    Args:
        quote: 实时行情数据
        prev_volume: 前一日成交量（用于计算倍率）
    """
    from app.technical import estimate_full_day_volume

    if not quote.price or not quote.open or not quote.high or not quote.low or not quote.pre_close:
        return "数据不足"

    change_pct = quote.change_pct or 0
    amplitude = quote.amplitude or 0
    # 估算全天量，避免午盘半日量导致量比失真
    volume = estimate_full_day_volume(quote) or 0
    qtype = quote.type or ""

    is_etf = "ETF" in qtype
    is_index = "指数" in qtype

    # 涨跌幅阈值（按标的类型调整）
    if is_index:
        pct_high = 0.5
    elif is_etf:
        pct_high = 1.0
    else:
        pct_high = 2.0

    # 计算成交量倍率（核心判断指标）
    vol_ratio = None
    if prev_volume and prev_volume > 0 and volume > 0:
        vol_ratio = volume / prev_volume

    # 放量/缩量判断阈值
    VOL_EXPANSION_THRESHOLD = 1.5   # 成交量是前日的1.5倍以上为放量
    VOL_SHRINK_THRESHOLD = 0.6      # 成交量不到前日的60%为缩量

    # 日内位置百分比 (0-100)
    if quote.high > quote.low:
        position = ((quote.price - quote.low) / (quote.high - quote.low)) * 100
    else:
        position = 50

    # 开盘溢价
    gap_up = quote.open > quote.pre_close
    gap_pct = ((quote.open - quote.pre_close) / quote.pre_close * 100) if gap_up else 0

    signals = []

    # ---- 基于真实成交量的判断（优先）----
    if vol_ratio is not None:
        # 放量上涨 = 主力入场信号
        if change_pct > pct_high and vol_ratio >= VOL_EXPANSION_THRESHOLD:
            signals.append(f"放量上涨（{vol_ratio:.1f}倍），主力入场")

        # 缩量上涨 = 散户行为或锁仓
        elif change_pct > pct_high * 0.5 and vol_ratio <= VOL_SHRINK_THRESHOLD:
            signals.append(f"缩量上涨（{vol_ratio:.1f}倍），买盘不强")

        # 放量下跌 = 主力出逃
        elif change_pct < -pct_high and vol_ratio >= VOL_EXPANSION_THRESHOLD:
            signals.append(f"放量下跌（{vol_ratio:.1f}倍），主力出逃⚠️")

        # 缩量下跌 = 散户抛售或惜售
        elif change_pct < -pct_high * 0.5 and vol_ratio <= VOL_SHRINK_THRESHOLD:
            signals.append(f"缩量下跌（{vol_ratio:.1f}倍），抛压减弱")

        # 平盘放量
        elif abs(change_pct) <= pct_high * 0.3 and vol_ratio >= VOL_EXPANSION_THRESHOLD:
            signals.append(f"平盘放量（{vol_ratio:.1f}倍），资金博弈")

    # ---- 没有历史成交量时，使用振幅作为辅助判断 ----
    elif vol_ratio is None:
        amp_high = 1.0 if is_index else (1.5 if is_etf else 4.0)
        amp_low = 0.3 if is_index else (0.6 if is_etf else 1.5)

        if change_pct > pct_high and amplitude > amp_high:
            signals.append("振幅较大上涨，疑似放量（无历史数据）")
        elif change_pct > pct_high * 0.7 and amplitude < amp_low:
            signals.append("振幅较小上涨，疑似缩量（无历史数据）")
        elif change_pct < -pct_high and amplitude > amp_high:
            signals.append("振幅较大下跌，疑似放量（无历史数据）")
        elif change_pct < -pct_high * 0.7 and amplitude < amp_low:
            signals.append("振幅较小下跌，疑似缩量（无历史数据）")

    # ---- 辅助判断因素 ----

    # 高位收盘 + 放量 = 强势主力
    if position > 80 and change_pct > pct_high * 0.7:
        if signals and "放量" in signals[0]:
            signals[0] = signals[0].replace("主力", "强势主力")
        elif not signals:
            signals.append("强势资金主导（收盘接近日内高点）")

    # 低位收盘 + 放量 = 恐慌抛盘
    elif position < 20 and change_pct < -pct_high * 0.7:
        if signals and "放量" in signals[0]:
            signals[0] = signals[0].replace("出逃", "恐慌出逃")
        elif not signals:
            signals.append("恐慌抛压（收盘接近日内低点）")

    # 高开低走 = 主力出货
    if gap_up and gap_pct > pct_high * 0.7 and change_pct < 0:
        signals.append("高开低走，疑似出货")

    # 尾盘拉升 = 主力做盘
    tail_amp = 0.6 if is_etf else 2.0
    if position > 85 and change_pct > pct_high * 0.5 and amplitude > tail_amp:
        signals.append("尾盘拉升，主力做盘")

    return "; ".join(signals) if signals else "资金面中性"


def generate_evening_review(config: Config) -> Path | None:
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
    quotes = fetch_quotes_rich(holding_items)

    if not quotes:
        log.warning("Evening review: No quote data")
        return None

    # 加载历史成交量数据
    prev_state = {}
    try:
        from pathlib import Path
        state_path = Path(__file__).resolve().parent.parent / "state" / "monitor_state.json"
        if state_path.exists():
            import json
            with open(state_path, "r", encoding="utf-8") as f:
                prev_state = json.load(f)
    except Exception as e:
        log.warning(f"加载历史状态失败: {e}")

    # 获取大盘当日涨跌幅，用于 β/α 归因
    market_return = None
    try:
        from app.config import Config as _Cfg
        # 用沪深300作为大盘基准
        market_items = [WatchItem(name="沪深300", code="000300", market="sh", type="指数")]
        market_quotes = fetch_quotes_rich(market_items)
        if market_quotes and market_quotes[0].change_pct is not None:
            market_return = market_quotes[0].change_pct / 100  # 转为小数
    except Exception:
        pass

    from app.data_fetcher import fetch_market_news
    day_news = fetch_market_news(start_hour=9, end_hour=16, max_count=10)

    # 获取龙虎榜数据
    dragon_tiger_summary = None
    dragon_tiger_report = ""
    dragon_tiger_llm = ""
    try:
        dt_records = fetch_dragon_tiger_list(max_count=30)
        if dt_records:
            dragon_tiger_summary = analyze_dragon_tiger(dt_records)
            dragon_tiger_report = format_dragon_tiger_report(dragon_tiger_summary)
            dragon_tiger_llm = build_llm_context(dragon_tiger_summary)
            log.info(f"龙虎榜数据分析完成: {len(dt_records)} 只个股")
    except Exception as e:
        log.warning(f"龙虎榜数据获取/分析失败: {e}")

    # 2. Build data section
    data_lines = []

    if day_news:
        data_lines.append("## 一、盘中快讯（供参考）")
        for n in day_news:
            cat = f" [{n.category}]" if n.category else ""
            data_lines.append(f"- [{n.time}]{cat} {n.title}")

    if dragon_tiger_report:
        data_lines.append(f"\n## 二、龙虎榜资金分析")
        data_lines.append(f"\n{dragon_tiger_report}")

    data_lines.append(f"\n## 三、市场背景")

    h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
    if h_results:
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

                # 获取前一日成交量
                prev_volume = prev_state.get(h["code"], {}).get("volume")
                capital_flow = _analyze_capital_flow(quote, prev_volume)
                holdings_with_analysis.append({
                    **h,
                    "quote": quote,
                    "tech": ", ".join(tech_info),
                    "capital_flow": capital_flow,
                })

        data_lines.append(f"\n## 四、持仓表现")

        data_lines.append("\n### 3.1 持仓详情")
        for h in holdings_with_analysis:
            quote = h.get("quote")
            parts = [f"  - {h['name']}({h['code']}):"]
            # 收盘价
            if quote and quote.price:
                parts.append(f"收盘{quote.price:.3f}")
            # 涨跌幅
            if quote and quote.change_pct is not None:
                parts.append(f"涨跌{quote.change_pct:+.2f}%")
            # 量比（使用估算全天量，避免午盘半日量失真）
            if quote and quote.volume and quote.volume > 0:
                from app.technical import estimate_full_day_volume
                est_vol = estimate_full_day_volume(quote)
                if est_vol and est_vol > 0:
                    # 用报告生成时间估算全天量，并取估算全天量与均量的比
                    prev_vol = prev_state.get(h["code"], {}).get("volume")
                    # 若 prev_vol 存在且为今早半日量（>0），用它估算昨日全天量
                    if prev_vol and prev_vol > 0:
                        prev_est = prev_vol * 2  # 假设上午量≈下午量估算昨日全天量
                        ratio = est_vol / prev_est if prev_est > 0 else 1.0
                        parts.append(f"量比{ratio:.2f}")
            # 主力净流入
            capital = h.get("capital_flow", "")
            if capital and capital != "数据不足":
                parts.append(f"资金{capital}")
            # 技术状态
            tech_note = f" [{h['tech']}]" if h.get("tech") else ""
            parts.append(tech_note)
            data_lines.append(" ".join(parts))

        has_rich = any(h.get("quote", {}).pe_ratio is not None for h in holdings_with_analysis)
        if has_rich:
            data_lines.append("\n### 3.2 估值与市值")
            for h in holdings_with_analysis:
                q = h.get("quote")
                if q and (q.pe_ratio is not None or q.market_cap is not None or q.turnover_rate is not None):
                    parts = []
                    if q.pe_ratio is not None:
                        parts.append(f"PE:{q.pe_ratio:.1f}")
                    if q.pb_ratio is not None:
                        parts.append(f"PB:{q.pb_ratio:.2f}")
                    if q.market_cap is not None:
                        parts.append(f"市值:{q.market_cap/1e4:.0f}亿")
                    if q.turnover_rate is not None:
                        parts.append(f"换手:{q.turnover_rate:.2f}%")
                    data_lines.append(f"  - {h['name']}({h['code']}): {', '.join(parts)}")

        data_lines.append("\n### 3.3 资金流向（主力/散户）")
        for h in holdings_with_analysis:
            flow_color = "🔵" if "主力" in h["capital_flow"] else "⚪"
            data_lines.append(f"  {flow_color} {h['name']}({h['code']}): {h['capital_flow']}")

    # Technical analysis for holdings
    tech_data_evening = _get_holdings_tech_analysis(holdings, quotes)
    strategy_signals_evening = _get_holdings_strategy_signals(holdings, quotes)
    if tech_data_evening:
        data_lines.append("\n## 五、持仓技术分析")
        data_lines.append("")
        data_lines.append("| 标的 | 现价 | 涨跌幅 | 支撑位详情 | 压力位详情 | 成交密集区 | ATR | 量价关系 | RSI | MACD | KDJ | OBV | 成交量 | 换手率 |")
        data_lines.append("|------|------|--------|------------|------------|------------|-----|----------|-----|------|-----|-----|--------|--------|")
        for t in tech_data_evening:
            price = f"{t['price']:.3f}" if t.get('price') else "--"
            chg = f"{t['change_pct']:+.2f}%" if t.get('change_pct') is not None else "--"
            sup = t.get('support_desc', '--')
            res = t.get('resistance_desc', '--')
            clusters = t.get('volume_clusters')
            cluster_str = f"{','.join(f'{c:.3f}' for c in clusters[:2])}" if clusters else "--"
            atr = f"{t['atr']:.3f}" if t['atr'] else "--"
            rsi = f"{t['rsi']:.1f}({t['rsi_signal']})" if t['rsi'] else "--"
            macd = f"DIF:{t['macd_dif']:.4f}({t['macd_signal']})" if t['macd_dif'] is not None else "--"
            kdj = f"K:{t['kdj_k']:.1f}({t['kdj_signal']})" if t['kdj_k'] is not None else "--"
            obv_val = f"{t['obv']:.0f}({t['obv_signal']})" if t.get('obv') is not None else "--"
            vol = f"{t['volume']/10000:.0f}万" if t.get('volume') and t['volume'] > 0 else "--"
            tr = f"{t['turnover']:.2f}%" if t.get('turnover') else "--"
            data_lines.append(f"| {t['name']} | {price} | {chg} | {sup} | {res} | {cluster_str} | {atr} | {t['vol_price']} | {rsi} | {macd} | {kdj} | {obv_val} | {vol} | {tr} |")

    if strategy_signals_evening:
        data_lines.append(f"\n## 六、⭐ 组合策略信号（多指标共振，明日操作参考）")
        for s in strategy_signals_evening:
            for sig_text in s['signals']:
                data_lines.append(f"  - {s['name']}: {sig_text}")

    data_section = "\n".join(data_lines) if data_lines else "暂无数据"

    # 3. Build LLM prompt (compact)
    llm_lines = [
        f"今日收盘。时间是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，"
        f"请生成一份围绕个人持仓的晚报。"
    ]

    if day_news:
        llm_lines.append("\n[盘中快讯]")
        for n in day_news:
            llm_lines.append(f"  [{n.time}] {n.title}")

    if h_results:
        attr = _simple_attribution(h_results, market_return)
        llm_lines.append(f"\n[持仓]")
    else:
        attr = {"beta_pnl": None, "alpha_pnl": None, "total_pnl": 0}
        llm_lines.append(f"\n[持仓]")
    for h in h_results:
        quote = next((q for q in quotes if q.code == h["code"]), None)
        info = f"  {h['name']}"
        if quote:
            flow = _analyze_capital_flow(quote, prev_state.get(h["code"], {}).get("volume"))
            info += f" [{flow}]"
            if quote.pe_ratio is not None:
                info += f" PE:{quote.pe_ratio:.1f}"
            if quote.market_cap is not None:
                info += f" 市值:{quote.market_cap/1e4:.0f}亿"
            if quote.turnover_rate is not None:
                info += f" 换手:{quote.turnover_rate:.2f}%"
            if quote.change_pct and abs(quote.change_pct) > 2:
                info += f" {'走势强劲' if quote.change_pct > 0 else '走势疲软'}"
        llm_lines.append(info)

    if tech_data_evening:
        llm_lines.append("\n[技术分析]")
        for t in tech_data_evening:
            parts = [f"  {t['name']}:"]
            if t.get('support_desc'):
                parts.append(t['support_desc'])
            if t.get('resistance_desc'):
                parts.append(t['resistance_desc'])
            if t.get('volume_clusters'):
                clusters = t['volume_clusters']
                parts.append(f"成交密集区:{','.join(f'{c:.3f}' for c in clusters[:2])}")
            parts.append(f"量价:{t['vol_price']}")
            if t['rsi']:
                parts.append(f"RSI:{t['rsi']:.1f}({t['rsi_signal']})")
            parts.append(f"MACD:{t['macd_signal']}")
            parts.append(f"KDJ:{t['kdj_signal']}")
            if t.get('obv') is not None:
                parts.append(f"OBV:{t['obv']:.0f}({t['obv_signal']})")
            llm_lines.append(" ".join(parts))

    if strategy_signals_evening:
        llm_lines.append("\n[⭐ 组合策略信号（多指标共振，高优先级）]")
        for s in strategy_signals_evening:
            for sig_text in s['signals']:
                llm_lines.append(f"  {s['name']}: {sig_text}")

    if dragon_tiger_llm:
        llm_lines.append(f"\n{dragon_tiger_llm}")

    llm_lines.append(f"""

请按以下结构生成晚报（约 700 字）：

### 一、龙虎榜资金动向（简要）
- 大资金整体取向：偏多/偏空/分歧
- 如果龙虎榜数据中有与持仓同板块/同行业的个股出现异常资金流动，请重点提醒
- 市场短线情绪判断（游资活跃度）

### 二、持仓全景分析
- 强弱分化：哪只最强/最弱，差距原因是什么？
- 技术面综合评估：整体持仓的技术状态如何？

### 三、重点持仓深度点评（按重要性排序，不超过 3 只）
对每只持仓输出：
- **技术状态**：价在均线什么位置？RSI/MACD/KDJ 是否同向？
- **资金行为**：主力主导还是散户主导？有无异常放量/缩量？
- **估值水平**：结合 PE/PB 判断贵贱（标注置信度）
- **核心判断**：一句话结论（如："短期超买，明日有回调需求"）

### 四、明日多情景预案

| 情景 | 触发条件 | 应对动作 | 置信度 |
|------|---------|---------|--------|
| 乐观 | 大盘高开+放量 | ... | 中 |
| 基准 | 平开震荡 | ... | 高 |
| 悲观 | 低开+放量下跌 | ... | 中 |

### 五、风控红线
明日每只持仓的硬止损位和硬止盈位（具体价格）。

要求：必须使用条件格式（if-then），标注置信度。不输出与持仓无关的市场分析。

---
**置信度标注规则**：对于每一个判断性结论，请在括号中标注你的确定程度。
- [高]：数据充分、指标一致、历史模式明确
- [中]：数据尚可但存在分歧信号
- [低]：数据不足或逻辑链条不完整
**不确定性处理**：如果数据不足以支持判断，请直接输出"数据不足"而非强行给出结论。""")

    # 4. Call LLM
    llm_content = _call_llm("\n".join(llm_lines), config, role="evening_review", temperature=0.3, max_tokens=2000)
    if not llm_content:
        log.warning("Evening review: LLM generation failed")

    # 5. Build & save
    report = _build_report(data_section, llm_content)
    report_dir = Path(config.report_dir)
    filepath = _save_report(report, "Evening Review", report_dir)

    push_title = f"Evening Review {datetime.now().strftime('%m-%d')}"
    _push_report(push_title, report, config)

    log.info(f"Evening review generated: {filepath}")
    return filepath
