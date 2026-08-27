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
    _format_money,
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


def _append_mx_analysis(data_lines: list[str], config: Config, items, quotes) -> None:
    """妙想分析：标的消息面 + 体检 + 评级事件（持仓+自选全量，就地追加到 data_lines）

    Args:
        data_lines: 报告数据行列表（就地追加）
        config: 配置对象
        items: 标的列表（Holding/WatchItem，需含 name/code）
        quotes: 行情列表（用于异动优先排序）
    """
    from app.miaoxiang import (
        fetch_holdings_news,
        fetch_holdings_fundamental,
        fetch_holdings_events,
    )

    mx_news = fetch_holdings_news(config, items, quotes)
    if mx_news:
        data_lines.append("\n## 📌 标的消息面（妙想逐个检索）")
        data_lines.append(mx_news)

    mx_fund = fetch_holdings_fundamental(config, items)
    if mx_fund:
        data_lines.append("\n## 📊 标的体检（资金面+筹码+基本面）")
        data_lines.append(mx_fund)

    mx_events = fetch_holdings_events(config, items)
    if mx_events:
        data_lines.append("\n## 🚨 标的评级与事件监控")
        data_lines.append(mx_events)


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


def _format_nearby_levels(
    sr, price: float, atr: Optional[float]
) -> tuple[str, str]:
    """提取附近较强的支撑位和压力位列表

    Returns:
        (support_list_str, resistance_list_str) 格式化的字符串
    """
    if not price or price <= 0:
        return "", ""

    atr_val = atr if atr and atr > 0 else price * 0.02
    zone = atr_val * 0.5  # 合并阈值

    def _collect_and_merge(levels: list[float], above: bool) -> list[tuple[float, str]]:
        """收集并合并邻近价位，标注来源"""
        if not levels:
            return []
        # 筛选方向上合理的价位
        if above:
            candidates = [lv for lv in levels if lv and lv > price * 1.005]
        else:
            candidates = [lv for lv in levels if lv and lv < price * 0.995]
        if not candidates:
            return []
        # 合并邻近的
        candidates.sort(reverse=not above)
        merged: list[tuple[float, str]] = []
        used = set()
        for i, lv1 in enumerate(candidates):
            if i in used:
                continue
            group = [lv1]
            for j in range(i + 1, len(candidates)):
                if j in used:
                    continue
                if abs(candidates[j] - lv1) <= zone:
                    group.append(candidates[j])
                    used.add(j)
            merged.append((round(sum(group) / len(group), 3),
                           f"{len(group)}重" if len(group) > 1 else ""))
        return merged

    # 收集各类支撑/压力
    all_sups = (sr.swing_supports or []) + (sr.pivot_supports or []) + (sr.volume_clusters or [])
    all_res = (sr.swing_resistances or []) + (sr.pivot_resistances or []) + (sr.volume_clusters or [])

    sup_list = _collect_and_merge(all_sups, above=False)
    res_list = _collect_and_merge(all_res, above=True)

    # 只保留距离在合理范围内的（< 15%）
    sup_str = ", ".join(
        f"{lv:.3f}{'(' + tag + ')' if tag else ''}(距{(price-lv)/price*100:.1f}%)"
        for lv, tag in sup_list[:5] if (price - lv) / price < 0.15
    ) if sup_list else ""
    res_str = ", ".join(
        f"{lv:.3f}{'(' + tag + ')' if tag else ''}(距{(lv-price)/price*100:.1f}%)"
        for lv, tag in res_list[:5] if (lv - price) / price < 0.15
    ) if res_list else ""

    return sup_str, res_str


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
        calc_ma_alignment,
        calc_bollinger,
        get_technical_summary,
        calc_composite_score,
        detect_market_regime,
        detect_box_regime,
        MarketRegime,
        rsi_signal,
        detect_gap,
        check_key_level_breakout,
        analyze_key_level_behavior,
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

        # 跳空缺口 & 关键位突破
        gap = detect_gap(klines, quote.price or 0, quote.open or 0)
        breakout = check_key_level_breakout(klines, quote.price or 0, period=20)

        # 关键位动态行为分析
        key_level = analyze_key_level_behavior(
            klines,
            quote.price or 0,
            support=sr.support,
            resistance=sr.resistance,
            atr=sr.atr,
            swing_supports=sr.swing_supports,
            swing_resistances=sr.swing_resistances,
            pivot_supports=sr.pivot_supports,
            pivot_resistances=sr.pivot_resistances,
            volume_clusters=sr.volume_clusters,
        )

        # 附近较强的支撑/压力位
        nearby_sups, nearby_res = _format_nearby_levels(sr, quote.price or 0, sr.atr)

        # 技术指标
        closes = [k.close for k in klines if k.close is not None]
        highs = [k.high for k in klines if k.high is not None]
        lows = [k.low for k in klines if k.low is not None]

        rsi_val = calc_rsi(closes)
        macd = calc_macd(closes)
        kdj = calc_kdj(highs, lows, closes)
        obv = calc_obv(klines)
        ma = calc_ma_alignment(klines)
        bb = calc_bollinger(closes)

        # 箱体震荡 + 网格适用性诊断（代码确定性判定）
        box = detect_box_regime(klines, quote.price or 0)

        # 复合评分 + 市场状态（独立 try，不影响其他数据返回）
        try:
            tech_summary = get_technical_summary(quote, klines)
            flow_pct_r = None
            if quote.main_net_inflow and quote.amount and quote.amount > 0:
                flow_pct_r = quote.main_net_inflow / quote.amount * 100
            composite = calc_composite_score(tech_summary, quote.price or 0, flow_pct=flow_pct_r)
            regime = detect_market_regime(tech_summary, quote.price or 0, sr.atr)
        except Exception as exc:
            composite = {"score": 0, "label": "", "signals": [], "breakdown": {}}
            regime = MarketRegime(regime="未知", suggestion="", confidence="低")
            from app.utils import log
            log.warning(f"复合评分计算失败 [{h.code}]: {exc}")
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

        # 拥挤度检测
        crowd_score = 0
        if quote.change_pct and abs(quote.change_pct) > 3:
            crowd_score += 1
        if quote.volume_ratio and quote.volume_ratio >= 2.5:
            crowd_score += 1
        if quote.main_net_inflow and quote.amount and quote.amount > 0:
            if abs(quote.main_net_inflow) / quote.amount * 100 >= 25:
                crowd_score += 2
        if quote.turnover_rate and quote.turnover_rate >= 10:
            crowd_score += 1
        if quote.avg_price and quote.price and quote.avg_price > 0:
            if abs(quote.price - quote.avg_price) / quote.avg_price * 100 > 4:
                crowd_score += 1
        crowd_label = f"🚨 极高拥挤(×{crowd_score})" if crowd_score >= 4 else (f"⚠️ 高拥挤(×{crowd_score})" if crowd_score >= 3 else "")

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
            "ma_alignment": ma.alignment,
            "ma_alignment_detail": ma.detail,
            "bb_upper": bb.upper,
            "bb_middle": bb.middle,
            "bb_lower": bb.lower,
            "bb_width": bb.width,
            "bb_signal": bb.signal,
            "volume": quote.volume,
            "turnover": quote.turnover_rate,
            "volume_ratio": quote.volume_ratio,
            "avg_price": quote.avg_price,
            "volume_clusters": sr.volume_clusters,
            # 跳空缺口 & 关键位突破
            "has_gap": gap.has_gap,
            "gap_type": gap.gap_type,
            "gap_pct": gap.gap_pct,
            "gap_detail": gap.detail,
            "gap_filled_pct": gap.filled_pct,
            "breakout_type": breakout.breakout_type,
            "breakout_detail": breakout.detail,
            # 关键位动态行为
            "has_resistance_rejection": key_level.has_resistance_rejection,
            "resistance_rejection_detail": key_level.resistance_rejection_detail,
            "has_support_confirmation": key_level.has_support_confirmation,
            "support_confirmation_detail": key_level.support_confirmation_detail,
            "has_support_breakdown": key_level.has_support_breakdown,
            "support_breakdown_detail": key_level.support_breakdown_detail,
            "has_breakout_retest": key_level.has_breakout_retest,
            "breakout_retest_detail": key_level.breakout_retest_detail,
            "support_strength": key_level.support_strength,
            "resistance_strength": key_level.resistance_strength,
            "strength_summary": key_level.strength_summary,
            # 附近较强的支撑/压力位
            "nearby_supports": nearby_sups,
            "nearby_resistances": nearby_res,
            "composite_score": composite["score"],
            "composite_label": composite["label"],
            "composite_signals": composite["signals"],
            "market_regime": regime.regime,
            "regime_suggestion": regime.suggestion,
            "crowd_score": crowd_score,
            "crowd_label": crowd_label,
            # 箱体震荡 + 网格适用性（代码确定性判定）
            "box_regime": box.regime,
            "box_score": box.score,
            "box_reasons": box.reasons or [],
            "box_lower": box.lower,
            "box_upper": box.upper,
            "box_pos_pct": box.pos_pct,
            "avg_amp": box.avg_amp,
            "is_box": box.is_box,
            "grid_verdict": box.grid_verdict,
            "grid_verdict_reason": box.grid_verdict_reason,
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


def _format_box_grid_section(tech_data: list[dict]) -> tuple[str, str]:
    """箱体震荡 + 网格判定数据表（代码确定性判定）

    Returns:
        (markdown, llm紧凑文本)；两者均为 "" 表示无有效数据。
    """
    rows = [t for t in tech_data if t.get("box_regime") and t["box_regime"] != "数据不足"]
    if not rows:
        return "", ""

    lines = ["## 📦 箱体震荡与网格判定（代码判定）", ""]
    lines.append("| 标的 | 箱体震荡 | 箱体区间 | 现价位置 | 日均振幅 | 代码判定(网格) |")
    lines.append("|------|---------|---------|---------|---------|---------------|")

    llm_parts = []
    verdict_icon = {"开启": "🟢", "观望": "🟡", "关闭": "🔴"}
    for t in rows:
        name = t.get("name") or t.get("code", "")
        code = t.get("code", "")
        regime = t.get("box_regime", "")
        score = t.get("box_score", 0)
        lo = t.get("box_lower")
        hi = t.get("box_upper")
        pos = t.get("box_pos_pct")
        amp = t.get("avg_amp")
        verdict = t.get("grid_verdict", "")
        reason = t.get("grid_verdict_reason", "")

        box_range = f"{lo:.3f}~{hi:.3f}" if lo is not None and hi is not None else "--"
        pos_s = f"{pos:.0f}%" if pos is not None else "--"
        amp_s = f"{amp:.2f}%" if amp is not None else "--"
        icon = verdict_icon.get(verdict, "⚪")
        verdict_s = f"{icon} {verdict}" + (f"（{reason}）" if reason else "")

        lines.append(
            f"| {name}({code}) | {regime} | {box_range} | {pos_s} | {amp_s} | {verdict_s} |"
        )
        llm_parts.append(
            f"{name}: 箱体={regime}(评分{score}) 区间[{box_range}] 位置{pos_s} "
            f"日均振幅{amp_s} → 代码判定:{verdict}"
        )

    return "\n".join(lines), "  " + "\n  ".join(llm_parts)


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

    # Server酱 desp 内容长度限制（约32KB），晚报最长，需截断避免推送失败
    MAX_BYTES = 28000  # 留出余量
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_BYTES:
        log.warning(f"报告内容过长({len(content_bytes)}字节)，截断到{MAX_BYTES}字节后推送")
        # 优先保留开头（数据区）和结尾（AI分析摘要），截掉中间
        head = content_bytes[: MAX_BYTES // 2].decode("utf-8", errors="ignore")
        tail = content_bytes[-MAX_BYTES // 2:].decode("utf-8", errors="ignore")
        content = head + "\n\n...[中间内容已截断]...\n\n" + tail

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


def _call_llm(prompt: str, config: Config, role: str = "analyst", temperature: float = 0.3, max_tokens: int = 2000, timeout: int = 120) -> str | None:
    """Call LLM to generate analysis content

    Args:
        prompt: User prompt
        config: Config object
        role: System prompt role key (analyst/morning_brief/midday_review/evening_review)
        temperature: Temperature parameter (0.3 default, 0.4 for morning_brief)
        max_tokens: Max tokens for response
        timeout: Request timeout in seconds
    """
    if not config.llm_enabled or not config.deepseek_key:
        return None

    try:
        llm = get_llm_client(config)
        system_prompt = SYSTEM_PROMPTS.get(role, "")
        response = llm.chat(prompt, system_prompt=system_prompt, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
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
    # 妙想优先获取早间要闻，新浪兜底
    from app.miaoxiang import fetch_news_for_report
    mx_morning_news = fetch_news_for_report(config, "今日A股早间要闻 政策 利好 风险")
    morning_news = fetch_market_news(start_hour=0, end_hour=9, max_count=15)
    all_items = _get_unique_items(config)
    quotes = fetch_quotes_rich(all_items)

    # 2. Build data section (shown in report)
    # 加载前日收盘缓存（晚报运行时保存的）
    import json
    morning_cache = {}
    cache_path = Path(__file__).resolve().parent.parent / "state" / "morning_cache.json"
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                morning_cache = json.load(f)
            if morning_cache.get("_date") != datetime.now().strftime("%Y-%m-%d"):
                morning_cache = {}  # 过期缓存
        except Exception:
            morning_cache = {}

    data_lines = []

    if global_data:
        data_lines.append("## 一、隔夜市场（截至北京时间 05:00，仅供参考）")
        for k, v in global_data.items():
            data_lines.append(f"- {k}: {v}")

    if mx_morning_news:
        data_lines.append("\n## 二、早间要闻（妙想）")
        data_lines.append(mx_morning_news)
    elif morning_news:
        data_lines.append("\n## 二、早间要闻（供参考）")
        for n in morning_news:
            cat = f" [{n.category}]" if n.category else ""
            data_lines.append(f"- [{n.time}]{cat} {n.title}")

    if quotes:
        # 使用全部标的（持仓+自选）计算情绪评分
        all_quotes = [q for q in quotes if q.change_pct is not None]
        from app.data_fetcher import fetch_market_breadth, enrich_quotes_with_industry, fetch_sector_boards, fetch_major_indices
        sentiment = calc_market_sentiment(all_quotes, breadth=fetch_market_breadth())

        # 大盘及行业板块（昨收参考）
        enrich_quotes_with_industry(quotes)
        sector_boards_mb = fetch_sector_boards()
        major_indices_mb = fetch_major_indices()
        sector_md_mb, sector_llm_mb = _format_market_sector_section(quotes, major_indices_mb, sector_boards_mb)
        if sector_md_mb:
            data_lines.append(f"\n## 三、📈 大盘及行业板块（昨收参考）")
            data_lines.append(f"- 情绪评分: {sentiment.score}/100 ({sentiment.label})")
            data_lines.append(sector_md_mb)

        holdings = config.holdings
        if holdings:
            h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
            if h_results:
                data_lines.append(f"\n## 四、持仓概况（昨收，开盘前参考）")
                for h in h_results[:5]:
                    data_lines.append(f"  - {h['name']}（昨收）")
            elif morning_cache.get("holdings"):
                # 从缓存加载昨收数据
                cached_holdings = morning_cache["holdings"]
                data_lines.append(f"\n## 四、持仓概况（昨收，来自缓存）")
                for h in cached_holdings[:5]:
                    chg = f"({h['change_pct']:+.2f}%)" if h.get("change_pct") is not None else ""
                    data_lines.append(f"  - {h['name']}: 收盘{h['price']:.3f}{chg}")
                # 缓存中取更多数据显示
                for h in cached_holdings[5:]:
                    chg = f"({h['change_pct']:+.2f}%)" if h.get("change_pct") is not None else ""
                    data_lines.append(f"  - {h['name']}: 收盘{h['price']:.3f}{chg}")

    # Fetch technical analysis and strategy signals (shared by data section + LLM)
    tech_data = _get_holdings_tech_analysis(holdings, quotes) if holdings and quotes else []
    strategy_signals = _get_holdings_strategy_signals(holdings, quotes) if holdings and quotes else []

    # 自选标的技术分析（早报 quotes 已含自选，复用同一份行情）
    watch_tech = _get_holdings_tech_analysis(
        [Holding(name=w.name, code=w.code, market=w.market, amount=0, cost=0.0)
         for w in config.watch_items], quotes
    ) if config.watch_items and quotes else []
    box_md, box_llm = _format_box_grid_section(tech_data + watch_tech)

    if strategy_signals:
        data_lines.append(f"\n## 五、⭐ 组合策略信号（多指标共振）")
        for s in strategy_signals:
            for sig_text in s['signals']:
                data_lines.append(f"  - {s['name']}: {sig_text}")

    # 主力资金动向（昨日参考）
    from app.miaoxiang import fetch_etf_fund_flow
    etf_flow_map = fetch_etf_fund_flow(config, all_items)
    fund_md, fund_llm = _format_fund_flow_section(quotes, label="自选", etf_flow_map=etf_flow_map)
    if not fund_md and morning_cache.get("fund_flow"):
        # 从缓存加载资金流数据
        cached_flow = morning_cache["fund_flow"]
        if cached_flow:
            fund_llm_parts = ["[自选主力资金(缓存)]"]
            data_lines.append(f"\n## 六、💰 主力资金动向（昨收，来自缓存）")
            data_lines.append("| 标的 | 涨跌幅 | 主力净流入占比 | 资金结构 |")
            data_lines.append("|------|--------|--------------|---------|")
            for f in cached_flow[:10]:
                chg = f"{f['change_pct']:+.2f}%" if f.get("change_pct") is not None else "--"
                fp = f"{f['flow_pct']:+.1f}%" if f.get("flow_pct") is not None else "--"
                label = f.get("flow_label", "--")
                data_lines.append(f"| {f['name']}({f['code']}) | {chg} | {fp} | {label} |")
            data_lines.append("")
            fund_llm = "\n".join(fund_llm_parts) if fund_llm_parts else ""
    if fund_md:
        data_lines.append(f"\n## 六、💰 主力资金动向（昨收参考）")
        data_lines.append(fund_md)

    # 仓位操作建议摘要（带技术理由）
    pos_md, pos_llm = _format_position_summary(strategy_signals if strategy_signals else [], tech_data if tech_data else [])
    if pos_md:
        data_lines.append(f"\n## 七、📊 仓位操作建议")
        data_lines.append(pos_md)

    # 箱体震荡与网格判定（代码判定，覆盖自选+持仓）
    if box_md:
        data_lines.append("\n" + box_md)

    # 妙想分析：消息面 + 体检 + 评级事件（持仓+自选全量）
    _append_mx_analysis(data_lines, config, all_items, quotes)

    data_section = "\n".join(data_lines) if data_lines else "暂无数据"

    # 5. Build LLM prompt (compact data format)
    llm_lines = [
        f"现在是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，A股即将开盘。"
        f"请生成一份完整的早盘策略报告。"
    ]

    if global_data:
        llm_lines.append("\n[隔夜市场]")
        for k, v in global_data.items():
            llm_lines.append(f"  {k}: {v}")

    if mx_morning_news:
        llm_lines.append("\n[早间要闻（妙想）]")
        llm_lines.append(mx_morning_news[:1500])
    elif morning_news:
        llm_lines.append("\n[早间要闻]")
        for n in morning_news:
            llm_lines.append(f"  [{n.time}] {n.title}")

    if quotes:
        # 使用全部标的（持仓+自选）计算情绪评分
        all_quotes_llm = [q for q in quotes if q.change_pct is not None]
        from app.data_fetcher import fetch_market_breadth
        sentiment = calc_market_sentiment(all_quotes_llm, breadth=fetch_market_breadth())
        llm_lines.append(f"\n[昨日A股] 情绪: {sentiment.score}/100 ({sentiment.label})")

        index_quotes = [q for q in quotes if q.type == "指数"]
        for idx in index_quotes:
            close = f"{idx.price:.2f}" if idx.price else "--"
            chg = f"{idx.change_pct:+.2f}%" if idx.change_pct is not None else "--"
            llm_lines.append(f"  {idx.name}: {close} ({chg})")

        # 给指数计算支撑压力位（用于波动区间参考）
        if index_quotes:
            from app.technical import fetch_historical_kline, calc_support_resistance
            llm_lines.append("\n[指数支撑压力]")
            for idx in index_quotes[:3]:  # 最多显示3个主要指数
                klines = fetch_historical_kline(idx.code, idx.market, days=60)
                if klines:
                    sr = calc_support_resistance(klines)
                    support = f"{sr.support:.2f}" if sr.support else "--"
                    resistance = f"{sr.resistance:.2f}" if sr.resistance else "--"
                    llm_lines.append(f"  {idx.name}: 支撑={support} 压力={resistance}")
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
            vr_v = t.get("volume_ratio")
            if vr_v is not None and vr_v > 0:
                parts.append(f"量比{vr_v:.1f}")
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

    if pos_llm:
        llm_lines.append("\n[📊 仓位建议摘要]")
        llm_lines.append(pos_llm)

    if sector_llm_mb:
        llm_lines.append("\n[📈 大盘及行业板块（昨收）]")
        llm_lines.append(sector_llm_mb)

    if fund_llm:
        llm_lines.append(f"\n{fund_llm}")

    # 附近关键位（紧凑格式）
    nearby_mb = []
    for t in (tech_data or []):
        sups = t.get("nearby_supports", "")
        ress = t.get("nearby_resistances", "")
        if sups or ress:
            nearby_mb.append(f"{t['name']}: 支撑[{sups}] 压力[{ress}]")
    if nearby_mb:
        llm_lines.append("\n[📌 附近关键位] " + "; ".join(nearby_mb[:8]))

    # 关键位动态行为
    key_level_md, key_level_llm = _format_key_level_behavior_section(tech_data)
    if key_level_llm:
        llm_lines.append(f"\n[关键位动态行为（昨日收盘）]")
        llm_lines.append(key_level_llm)

    if box_llm:
        llm_lines.append("\n[📦 箱体与网格（代码预计算）]")
        llm_lines.append(box_llm)

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

### 五、网格交易建议（逐标的：代码判定 vs 你的判定）
结合[箱体与网格]数据，对每个标的给出你自己的「网格开启/观望/关闭」判定，并说明与代码判定是否一致及理由（一句话）。

要求：每一个判断都必须标注置信度。如果数据不足以支撑判断，如实说"数据不足"。使用 Markdown 格式增强可读性。

---
**置信度标注规则**：对于每一个判断性结论，请在括号中标注你的确定程度。
- [高]：数据充分、指标一致、历史模式明确
- [中]：数据尚可但存在分歧信号
- [低]：数据不足或逻辑链条不完整
**不确定性处理**：如果数据不足以支持判断，请直接输出"数据不足"而非强行给出结论。
**关键位动态解读**：受压回落→上方压力沉重注意减仓；支撑确认→回调可低吸；跌破支撑→注意止损减仓；突破回踩确认→突破有效可适当加仓；位级强度→强级别更可信。""")

    # 6. Call LLM
    llm_content = _call_llm("\n".join(llm_lines), config, role="morning_brief", temperature=0.4, max_tokens=8000, timeout=300)
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
    from app.data_fetcher import fetch_market_breadth
    _, stats = analyze(all_quotes, {}, config, market_breadth=fetch_market_breadth())
    from app.data_fetcher import fetch_market_news
    # 妙想优先获取上午快讯，新浪兜底
    from app.miaoxiang import fetch_news_for_report
    mx_midday_news = fetch_news_for_report(config, "上午A股盘面 热点板块 异动 原因")
    morning_news = fetch_market_news(start_hour=9, end_hour=12, max_count=10)

    # 2. Build data section
    data_lines = []

    if mx_midday_news:
        data_lines.append("## 一、上午快讯（妙想）")
        data_lines.append(mx_midday_news)
    elif morning_news:
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

    # 大盘及行业板块（午间实时）
    from app.data_fetcher import enrich_quotes_with_industry, fetch_sector_boards, fetch_major_indices
    enrich_quotes_with_industry(quotes)
    sector_boards_mid = fetch_sector_boards()
    major_indices_mid = fetch_major_indices()
    sector_md_mid, sector_llm_mid = _format_market_sector_section(quotes, major_indices_mid, sector_boards_mid)
    if sector_md_mid:
        data_lines.append(f"\n## 四、📈 大盘及行业板块（午间实时）")
        data_lines.append(sector_md_mid)

    holdings = config.holdings
    if holdings:
        h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
        if h_results:
            data_lines.append(f"\n## 五、持仓午间扫描")
            for h in h_results:
                data_lines.append(f"  - {h['name']}({h['code']}): {h['amount']}股")

    # Technical analysis for holdings
    tech_data = _get_holdings_tech_analysis(holdings, quotes) if holdings else []
    strategy_signals = _get_holdings_strategy_signals(holdings, quotes) if holdings else []

    # 自选标的技术分析（午评 quotes 已含自选，复用同一份行情）
    watch_tech = _get_holdings_tech_analysis(
        [Holding(name=w.name, code=w.code, market=w.market, amount=0, cost=0.0)
         for w in config.watch_items], quotes
    ) if config.watch_items and quotes else []
    box_md, box_llm = _format_box_grid_section(tech_data + watch_tech)
    if tech_data:
        data_lines.append("\n## 六、持仓技术分析")
        data_lines.append("")
        data_lines.append("| 标的 | 现价 | 均价 | 涨跌幅 | 量比 | 量价 | 布林(上/中/下) | RSI | MACD | KDJ | OBV | 成交量 | 换手率 |")
        data_lines.append("|------|------|------|--------|------|------|--------------|-----|------|-----|-----|--------|--------|")
        for t in tech_data:
            price = f"{t['price']:.3f}" if t.get('price') else "--"
            avg_p_val = t.get('avg_price')
            avg_str = f"{avg_p_val:.3f}" if avg_p_val else "--"
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
            vr_val = t.get('volume_ratio')
            vr_str = f"{vr_val:.1f}" if vr_val is not None and vr_val > 0 else "--"
            # 布林三轨
            bb_u = t.get('bb_upper')
            bb_m = t.get('bb_middle')
            bb_l = t.get('bb_lower')
            bb_str = f"{bb_u:.3f}/{bb_m:.3f}/{bb_l:.3f}" if bb_u and bb_m and bb_l else "--"
            data_lines.append(f"| {t['name']} | {price} | {avg_str} | {chg} | {vr_str} | {t['vol_price']} | {bb_str} | {rsi} | {macd} | {kdj} | {obv_val} | {vol} | {tr} |")

    if strategy_signals:
        data_lines.append(f"\n## 七、⭐ 组合策略信号（多指标共振，下午操作参考）")
        for s in strategy_signals:
            for sig_text in s['signals']:
                data_lines.append(f"  - {s['name']}: {sig_text}")

    # 仓位建议摘要
    pos_md_mid, pos_llm_mid2 = _format_position_summary(strategy_signals if strategy_signals else [], tech_data if tech_data else [])
    if pos_md_mid:
        data_lines.append(f"\n## 八、📊 仓位操作建议")
        data_lines.append(pos_md_mid)

    # 主力资金流向
    from app.miaoxiang import fetch_etf_fund_flow
    etf_flow_map_mid = fetch_etf_fund_flow(config, all_items)
    fund_md_mid, fund_llm_mid = _format_fund_flow_section(quotes, label="自选", etf_flow_map=etf_flow_map_mid)
    if fund_md_mid:
        data_lines.append(f"\n## 九、💰 主力资金动向")
        data_lines.append(fund_md_mid)

    # 量能分析（午间，使用量比作为近似对比）
    vol_mid_md, vol_mid_llm = _format_volume_section(quotes, holdings, {})
    if vol_mid_md:
        data_lines.append(f"\n## 十、📊 量能分析（半日数据）")
        data_lines.append(vol_mid_md)

    # 关键位动态行为
    key_level_mid_md, key_level_mid_llm = _format_key_level_behavior_section(tech_data)
    if key_level_mid_md:
        data_lines.append(f"\n## 十一、🎯 关键位动态行为")
        data_lines.append(key_level_mid_md)

    # 盘中复盘（上午半日）
    intraday_mid_md, intraday_mid_llm = _format_intraday_evolution(holdings)
    if intraday_mid_md:
        data_lines.append(f"\n## 十二、📈 盘中复盘（上午）")
        data_lines.append(intraday_mid_md)

    # 箱体震荡与网格判定（代码判定，覆盖自选+持仓）
    if box_md:
        data_lines.append("\n" + box_md)

    # 妙想分析：消息面 + 体检 + 评级事件（持仓+自选全量）
    _append_mx_analysis(data_lines, config, all_items, quotes)

    data_section = "\n".join(data_lines) if data_lines else "暂无数据"

    # 3. Build LLM prompt (compact)
    llm_lines = [
        f"现在是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，上午交易结束。"
        f"请生成一份A股午评报告。"
    ]

    if mx_midday_news:
        llm_lines.append("\n[上午快讯（妙想）]")
        llm_lines.append(mx_midday_news[:1500])
    elif morning_news:
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
            vr_v = t.get("volume_ratio")
            if vr_v is not None and vr_v > 0:
                parts.append(f"量比{vr_v:.1f}")
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

    # 仓位建议摘要
    pos_llm_mid3 = _format_position_summary(strategy_signals if strategy_signals else [], tech_data if tech_data else [])[1]
    if pos_llm_mid3:
        llm_lines.append("\n[📊 仓位建议摘要]")
        llm_lines.append(pos_llm_mid3)

    if fund_llm_mid:
        llm_lines.append(f"\n{fund_llm_mid}")

    if vol_mid_llm:
        llm_lines.append(f"\n{vol_mid_llm}")

    if intraday_mid_llm:
        llm_lines.append(f"\n[盘中复盘] {intraday_mid_llm}")

    if sector_llm_mid:
        llm_lines.append("\n[📈 大盘及行业板块（午间实时）]")
        llm_lines.append(sector_llm_mid)

    # 附近关键位
    nearby_mid = []
    for t in (tech_data or []):
        sups = t.get("nearby_supports", "")
        ress = t.get("nearby_resistances", "")
        if sups or ress:
            nearby_mid.append(f"{t['name']}: 支撑[{sups}] 压力[{ress}]")
    if nearby_mid:
        llm_lines.append("\n[📌 附近关键位] " + "; ".join(nearby_mid[:8]))

    if key_level_mid_llm:
        llm_lines.append(f"\n[关键位动态行为（上午盘中）]")
        llm_lines.append(key_level_mid_llm)

    if box_llm:
        llm_lines.append("\n[📦 箱体与网格（代码预计算）]")
        llm_lines.append(box_llm)

    llm_lines.append(f"""

请作为资深策略分析师，基于以上午盘数据，生成一份结构清晰的午评报告（约500字）：

1️⃣ **上午盘面总结**: 描述上午走势的特征（如冲高回落、缩量震荡等），点出主要影响因素
2️⃣ **热点与异动**: 观察标的中哪些板块或个股表现突出或异常，结合换手率和市值判断市场风格
3️⃣ **持仓午间扫描**: 针对个人持仓（Holdings），简述其上午的表现，是否出现风险信号
4️⃣ **下午走势预测**: 基于上午的情绪和资金流向，预测下午的可能走势
5️⃣ **午间操作建议**: 下午是否需要进行调仓（补仓/减仓），给出具体的触发条件

6️⃣ **网格交易建议（逐标的：代码判定 vs 你的判定）**: 结合[箱体与网格]数据，对每个标的给出你自己的「网格开启/观望/关闭」判定，并说明与代码判定是否一致及理由（一句话）

要求：
- 视角：专业、敏锐，重点在于"预测下午"和"给出建议"。
- 格式：使用 Markdown 增强可读性，区分"自选"和"持仓"。
- 语气：务实，不拖泥带水。

---
**置信度标注规则**：对于每一个判断性结论，请在括号中标注你的确定程度。
- [高]：数据充分、指标一致、历史模式明确
- [中]：数据尚可但存在分歧信号
- [低]：数据不足或逻辑链条不完整
**不确定性处理**：如果数据不足以支持判断，请直接输出"数据不足"而非强行给出结论。
**关键位动态解读**：受压回落→上方压力沉重注意减仓；支撑确认→回调可低吸；跌破支撑→注意止损减仓；突破回踩确认→突破有效可适当加仓；位级强度→强级别更可信。""")

    # 4. Call LLM
    llm_content = _call_llm("\n".join(llm_lines), config, role="midday_review", temperature=0.3, max_tokens=8000, timeout=300)
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

FUND_FLOW_DISPLAY_LIMIT = 15  # 主力资金动向表格最多展示的标的数（按主力净流入占比截断）


def _format_fund_flow_section(quotes: list[Quote], label: str = "自选标的", etf_flow_map: dict | None = None) -> tuple[str, str]:
    """生成主力资金流向摘要，返回 (数据区Markdown, LLM紧凑文本)

    汇总主力净流入/流出情况，标注重点关注标的。
    包含超大单/大单/中单/小单资金结构。
    对 ETF 额外展示净申购额（申赎口径，来自妙想 etf_flow_map）。
    """
    has_flow = [q for q in quotes if q.main_net_inflow is not None and q.amount and q.amount > 0]
    if not has_flow:
        return "", ""

    def _etf_sub_str(q: Quote) -> str:
        """ETF 净申购额列（非 ETF 或无数据返回 --）"""
        if etf_flow_map:
            info = etf_flow_map.get(q.code)
            if info and info.get("net_subscribe") is not None:
                v = info["net_subscribe"]
                if abs(v) >= 1e8:
                    return f"{v/1e8:+.2f}亿"
                return f"{v/1e4:+.0f}万"
        return "--"

    # 按主力净流入占比排序
    scored = []
    total_inflow = 0.0
    total_super_large = 0.0
    total_large = 0.0
    total_medium = 0.0
    total_small = 0.0
    total_overall = 0.0
    has_detail = False
    has_overall = False
    for q in has_flow:
        pct = q.main_net_inflow / q.amount * 100  # type: ignore[operator]
        total_inflow += q.main_net_inflow  # type: ignore[operator]
        ff = q.fund_flow
        if ff:
            if ff.super_large_net is not None:
                total_super_large += ff.super_large_net
                has_detail = True
            if ff.large_net is not None:
                total_large += ff.large_net
            if ff.medium_net is not None:
                total_medium += ff.medium_net
            if ff.small_net is not None:
                total_small += ff.small_net
            if ff.total_net is not None:
                total_overall += ff.total_net
                has_overall = True
        scored.append((q, pct))
    scored.sort(key=lambda x: x[1], reverse=True)

    # ---- Markdown 数据区 ----
    md_lines = [f"### 💰 {label}主力资金动向"]
    md_lines.append("")

    if has_detail:
        md_lines.append("| 标的 | 涨跌幅 | 主力净流入 | 占比 | 超大单 | 大单 | 中单 | 散户(小单) | 总体 | 净申购额 | 信号 |")
        md_lines.append("|------|--------|-----------|------|--------|------|------|-----------|------|------|------|")

        for q, pct in scored[:FUND_FLOW_DISPLAY_LIMIT]:
            chg = f"{q.change_pct:+.2f}%" if q.change_pct is not None else "--"
            inflow_str = _format_money(q.main_net_inflow)  # type: ignore[arg-type]
            ff = q.fund_flow
            if ff:
                sl_str = _format_money(ff.super_large_net) if ff.super_large_net is not None else "--"
                lg_str = _format_money(ff.large_net) if ff.large_net is not None else "--"
                md_str = _format_money(ff.medium_net) if ff.medium_net is not None else "--"
                sm_str = _format_money(ff.small_net) if ff.small_net is not None else "--"
                ov_str = _format_money(ff.total_net) if ff.total_net is not None else "--"
                # 信号判断（使用增强的资金结构标签）
                sig = ff.flow_structure if ff.is_valid else ("⚪ 中性" if abs(pct) < 5 else ("🟢 流入" if pct > 0 else "🟠 流出"))
                # 映射到带图标的标签
                sig_map = {
                    "机构主导(中小资金出逃)": "🔵🔥 深度吸筹",
                    "机构主导": "🔵 机构吸筹",
                    "机构出货": "🔴 机构出货",
                    "游资活跃": "🟣 游资活跃",
                    "散户主导": "🟡 散户主导",
                    "主力偏多": "🟢 主力流入",
                    "主力偏空": "🔴 主力流出",
                    "均衡": "⚪ 均衡",
                }
                sig = sig_map.get(sig, sig)
                # 附加拆单信号
                from app.analyzer import detect_split_order
                split = detect_split_order(ff, q.amount or 0, q.change_pct)
                if split:
                    sig = f"{sig} {split}"
            else:
                sl_str = lg_str = md_str = sm_str = ov_str = "--"
                sig = "⚪ 中性" if abs(pct) < 5 else ("🟢 流入" if pct > 0 else "🟠 流出")
            sub_str = _etf_sub_str(q)
            md_lines.append(
                f"| {q.name}({q.code}) | {chg} | {inflow_str} | {pct:.1f}% "
                f"| {sl_str} | {lg_str} | {md_str} | {sm_str} | {ov_str} | {sub_str} | {sig} |"
            )
    else:
        # 无明细数据时沿用旧格式
        md_lines.append("| 标的 | 涨跌幅 | 主力净流入 | 占成交额 | 净申购额 | 信号 |")
        md_lines.append("|------|--------|-----------|---------|------|------|")
        for q, pct in scored[:FUND_FLOW_DISPLAY_LIMIT]:
            chg = f"{q.change_pct:+.2f}%" if q.change_pct is not None else "--"
            inflow_str = _format_money(q.main_net_inflow)  # type: ignore[arg-type]
            if pct >= 15:
                sig = "🔵 大幅流入"
            elif pct >= 5:
                sig = "🟢 流入"
            elif pct <= -10:
                sig = "🔴 大幅流出"
            elif pct <= -5:
                sig = "🟠 流出"
            else:
                sig = "⚪ 中性"
            sub_str = _etf_sub_str(q)
            md_lines.append(f"| {q.name}({q.code}) | {chg} | {inflow_str} | {pct:.1f}% | {sub_str} | {sig} |")

    # 合计汇总
    if abs(total_inflow) >= 1e8:
        direction = "净流入" if total_inflow > 0 else "净流出"
        summary = f"> 合计主力资金: **{direction} {abs(total_inflow)/1e8:.2f}亿**"
        if has_detail:
            summary += f" | 超大单: {total_super_large/1e8:+.2f}亿 | 大单: {total_large/1e8:+.2f}亿"
            summary += f" | 中单: {total_medium/1e8:+.2f}亿 | 散户: {total_small/1e8:+.2f}亿"
        if has_overall:
            summary += f" | 总体: {total_overall/1e8:+.2f}亿"
        md_lines.append(f"\n{summary}")
    elif abs(total_inflow) >= 1e6:
        direction = "净流入" if total_inflow > 0 else "净流出"
        summary = f"> 合计主力资金: **{direction} {abs(total_inflow)/1e4:.0f}万**"
        if has_detail:
            summary += f" | 超大单: {total_super_large/1e4:+.0f}万"
            summary += f" | 散户: {total_small/1e4:+.0f}万"
        if has_overall:
            summary += f" | 总体: {total_overall/1e4:+.0f}万"
        md_lines.append(f"\n{summary}")

    md = "\n".join(md_lines) + "\n"

    # ---- LLM 紧凑文本 ----
    top_in = scored[:3]
    top_out = sorted(scored, key=lambda x: x[1])[:3]
    llm_parts = [f"[{label}主力资金]"]
    if abs(total_inflow) >= 1e8:
        direction = "净流入" if total_inflow > 0 else "净流出"
        llm_parts.append(f"合计{direction}{abs(total_inflow)/1e8:.2f}亿")
    if has_detail:
        llm_parts.append(f"超大单{total_super_large/1e8:+.2f}亿/中单{total_medium/1e8:+.2f}亿/散户{total_small/1e8:+.2f}亿")
        # 统计资金结构分布
        inst_count = sum(1 for q, _ in scored if q.fund_flow and q.fund_flow.is_institution_absorbing)
        dist_count = sum(1 for q, _ in scored if q.fund_flow and q.fund_flow.is_distribution)
        mid_count = sum(1 for q, _ in scored if q.fund_flow and q.fund_flow.is_mid_capital_active)
        if inst_count or dist_count or mid_count:
            structs = []
            if inst_count:
                structs.append(f"{inst_count}只深度吸筹")
            if mid_count:
                structs.append(f"{mid_count}只游资活跃")
            if dist_count:
                structs.append(f"{dist_count}只疑似出货")
            llm_parts.append("结构: " + ", ".join(structs))
    top_in_str = " ".join(f"{q.name}({pct:.0f}%)" for q, pct in top_in)
    top_out_str = " ".join(f"{q.name}({pct:.0f}%)" for q, pct in top_out)
    llm_parts.append(f"流入前3: {top_in_str}")
    llm_parts.append(f"流出前3: {top_out_str}")

    return md, "\n".join(llm_parts)


def _format_intraday_evolution(holdings: list[Holding]) -> tuple[str, str]:
    """从盯盘落盘的日内序列生成盘中复盘，返回 (Markdown, LLM紧凑文本)

    数据源：
    - state/intraday_series.json: 每标的的价格/资金流/量比时间序列
    - state/scan_history.json: 市场情绪演变
    """
    import json
    from pathlib import Path as _P

    state_dir = _P(__file__).resolve().parent.parent / "state"
    series_path = state_dir / "intraday_series.json"
    history_path = state_dir / "scan_history.json"

    md_lines = []
    llm_parts = []

    # 1. 市场情绪演变
    if history_path.exists():
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                hist = json.load(f)
            if hist:
                md_lines.append("### 盘中情绪演变")
                md_lines.append("| 时间 | 情绪评分 | 涨/跌/平 | 告警数 |")
                md_lines.append("|------|---------|---------|--------|")
                for h in hist[-8:]:
                    sent = h.get("market_sentiment", {})
                    score = sent.get("score", "--")
                    label = sent.get("label", "")
                    alerts_sum = h.get("alerts_summary", {})
                    if isinstance(alerts_sum, dict):
                        alert_n = alerts_sum.get("total_alerts", alerts_sum.get("critical_alerts", 0))
                    else:
                        alert_n = 0
                    t = h.get("time") or "--"
                    if t == "--":
                        # 时间字段空时从 timestamp 提取
                        ts = h.get("timestamp", "")
                        if isinstance(ts, (int, float)):
                            from datetime import datetime as _dt2
                            t = _dt2.fromtimestamp(ts).strftime("%H:%M")
                        elif isinstance(ts, str) and len(ts) >= 16:
                            t = ts[11:16]
                    md_lines.append(f"| {t} | {score}({label}) | -- | {alert_n} |")
                md_lines.append("")
                if hist:
                    first_score = hist[0].get("market_sentiment", {}).get("score", 50)
                    last_score = hist[-1].get("market_sentiment", {}).get("score", 50)
                    trend = "走强" if last_score > first_score else ("走弱" if last_score < first_score else "平稳")
                    llm_parts.append(f"[情绪演变] {first_score}→{last_score} ({trend})")
        except Exception:
            pass

    # 1.5 两融数据（替代已停止披露的北向资金）
    from app.data_fetcher import fetch_margin_data
    margin_data = fetch_margin_data()
    if margin_data and margin_data.financing_balance > 0:
        md_lines.append("### 两融数据（替代北向资金）")
        md_lines.append("| 指标 | 数值 |")
        md_lines.append("|------|------|")
        md_lines.append(f"| 融资余额 | {margin_data.financing_balance:.1f}亿 |")
        md_lines.append(f"| 融资净买入 | {margin_data.financing_net_buy:+.1f}亿 ({margin_data.financing_change_direction}) |")
        md_lines.append(f"| 融券余额 | {margin_data.securities_lending_balance:.1f}亿 |")
        md_lines.append(f"| 两融总余额 | {margin_data.total_balance:.1f}亿 |")
        md_lines.append(f"| 数据日期 | {margin_data.date} |")
        md_lines.append("")
        llm_parts.append(
            f"[两融] 融资余额{margin_data.financing_balance:.0f}亿,"
            f"净买入{margin_data.financing_net_buy:+.1f}亿({margin_data.financing_change_direction})"
        )

    # 2. 持仓分时特征（价格/资金流轨迹）
    if series_path.exists():
        try:
            with open(series_path, "r", encoding="utf-8") as f:
                series = json.load(f)
            stocks = series.get("stocks", {})
            holding_codes = {h.code for h in holdings}
            md_lines.append("### 持仓分时特征")
            md_lines.append("| 标的 | 首价 | 最高 | 最低 | 尾价 | 资金流(万) | 分时形态 |")
            md_lines.append("|------|------|------|------|------|-----------|---------|")
            intraday_llm = []
            for code, data in stocks.items():
                if code not in holding_codes:
                    continue
                tl = data.get("timeline", [])
                if len(tl) < 2:
                    continue
                prices = [p.get("price") for p in tl if p.get("price")]
                flows = [p.get("fund_flow", 0) for p in tl if p.get("fund_flow") is not None]
                if not prices:
                    continue
                first_p, last_p = prices[0], prices[-1]
                high_p, low_p = max(prices), min(prices)
                flow_sum = sum(flows) if flows else 0
                # 分时形态判断（涨幅 + 收盘在日内振幅中的位置）
                change_pct = (last_p - first_p) / first_p * 100 if first_p > 0 else 0
                if high_p > low_p:
                    close_pos = (last_p - low_p) / (high_p - low_p) * 100  # 0=最低,100=最高
                else:
                    close_pos = 50

                if abs(change_pct) < 0.3:  # 涨跌幅太小，本质是窄幅震荡
                    shape = "窄幅震荡"
                elif change_pct > 0 and close_pos >= 75:  # 明显上涨且收盘在高位
                    shape = "单边上涨"
                elif change_pct < 0 and close_pos <= 25:  # 明显下跌且收盘在低位
                    shape = "单边下跌"
                elif change_pct > 0 and close_pos < 50:  # 涨了但收盘在中低位（冲高回落）
                    shape = "冲高回落"
                elif change_pct < 0 and close_pos > 50:  # 跌了但收盘在中高位（探底回升）
                    shape = "探底回升"
                else:
                    shape = "震荡"
                md_lines.append(
                    f"| {data.get('name', code)}({code}) | {first_p:.3f} | {high_p:.3f} | {low_p:.3f} "
                    f"| {last_p:.3f} | {flow_sum:+.0f} | {shape} |"
                )
                intraday_llm.append(f"{data.get('name', code)}({shape},资金{flow_sum:+.0f}万)")
            md_lines.append("")
            if intraday_llm:
                llm_parts.append(f"[分时特征] {'; '.join(intraday_llm[:6])}")
        except Exception:
            pass

    if not md_lines:
        return "", ""
    return "\n".join(md_lines) + "\n", "\n".join(llm_parts)


def _format_composite_scoring(tech_data: list[dict]) -> tuple[str, str]:
    """生成多信号共振评分摘要，返回 (Markdown, LLM紧凑文本)"""
    if not tech_data:
        return "", ""
    scores = [(t["name"], t.get("composite_score", 0), t.get("composite_label", ""),
               t.get("market_regime", ""), t.get("composite_signals", []),
               t.get("crowd_label", ""))
              for t in tech_data if t.get("composite_score")]
    if not scores:
        return "", ""
    scores.sort(key=lambda x: x[1], reverse=True)
    regimes = [s[3] for s in scores if s[3]]
    dominant_regime = max(set(regimes), key=regimes.count) if regimes else "未知"
    avg_score = round(sum(s[1] for s in scores) / len(scores))
    regime_map = {"趋势上涨": "🟢", "趋势下跌": "🔴", "震荡偏多": "🟡", "震荡偏空": "🟡", "窄幅震荡": "⚪", "震荡": "⚪"}
    regime_emoji = regime_map.get(dominant_regime, "⚪")

    md_lines = []
    md_lines.append(f"### 📊 多信号共振评分")
    md_lines.append(f"**市场状态**: {regime_emoji} {dominant_regime} | **平均评分**: {avg_score}/100")
    md_lines.append("")
    md_lines.append("| 标的 | 评分 | 标签 | 状态 | 拥挤度 | 关键信号 |")
    md_lines.append("|------|------|------|------|--------|---------|")
    for name, score, label, regime, signals, crowd in scores[:10]:
        sig_str = ", ".join(signals[:3]) if signals else "--"
        crowd_str = crowd if crowd else "—"
        md_lines.append(f"| {name} | {score} | {label} | {regime} | {crowd_str} | {sig_str} |")
    md_lines.append("")

    # 高拥挤度标的汇总
    high_crowd = [(name, label, crowd) for name, _, _, _, _, crowd in scores if crowd]
    if high_crowd:
        md_lines.append(f"**⚠️ 拥挤度预警**: {'; '.join(f'{name}:{crowd}' for name, _, crowd in high_crowd)}")
        md_lines.append("")

    llm_parts = [f"[多信号评分] {regime_emoji}{dominant_regime} 均分{avg_score} | " +
                 " ".join(f"{name}({score})" for name, score, _, _, _, _ in scores[:5])]
    if high_crowd:
        llm_parts.append(f"[拥挤度] {'; '.join(f'{name}:{crowd}' for name, _, crowd in high_crowd)}")
    return "\n".join(md_lines) + "\n", "\n".join(llm_parts)


def _format_gap_breakout_section(tech_data: list[dict]) -> tuple[str, str]:
    """生成跳空缺口/关键位突破摘要，返回 (数据区Markdown, LLM紧凑文本)"""
    gaps = [t for t in tech_data if t.get("has_gap")]
    breakouts = [t for t in tech_data if t.get("breakout_type")]

    if not gaps and not breakouts:
        return "", ""

    md_lines = []
    llm_parts = []

    if gaps:
        md_lines.append("#### 🔲 跳空缺口")
        md_lines.append("| 标的 | 类型 | 幅度 | 缺口区间 | 回补 |")
        md_lines.append("|------|------|-----|---------|------|")
        for t in gaps:
            name = t["name"]
            code = t["code"]
            gp = t.get("gap_pct", 0)
            gd = t.get("gap_detail", "")
            filled = f"{t.get('gap_filled_pct', 0):.0f}%"
            if t.get("gap_filled_pct", 0) >= 100:
                filled = "✅ 已回补"
            # Parse gap range from detail
            md_lines.append(f"| {name}({code}) | {t.get('gap_type', '')} | {gp:+.1f}% | {gd[:40]} | {filled} |")
        md_lines.append("")
        gap_list = [f"{t['name']}({t.get('gap_pct',0):+.1f}%,回补{t.get('gap_filled_pct',0):.0f}%)" for t in gaps[:3]]
        llm_parts.append(f"[跳空] {' '.join(gap_list)}")

    if breakouts:
        md_lines.append("#### 🎯 关键位突破")
        md_lines.append("")
        for t in breakouts:
            md_lines.append(f"- **{t['name']}**({t['code']}): {t.get('breakout_detail', '')}")
        md_lines.append("")
        bo_list = [f"{t['name']}({t.get('breakout_type','')})" for t in breakouts[:3]]
        llm_parts.append(f"[突破] {' '.join(bo_list)}")

    return "\n".join(md_lines) + "\n", "\n".join(llm_parts)


def _format_key_level_behavior_section(tech_data: list[dict]) -> tuple[str, str]:
    """生成关键位动态行为摘要，返回 (数据区Markdown, LLM紧凑文本)

    检测：受压回落、支撑确认、跌破支撑、突破回踩、关键位强度。
    """
    rejections = [t for t in tech_data if t.get("has_resistance_rejection")]
    confirmations = [t for t in tech_data if t.get("has_support_confirmation")]
    breakdowns = [t for t in tech_data if t.get("has_support_breakdown")]
    retests = [t for t in tech_data if t.get("has_breakout_retest")]
    strong_items = [
        t for t in tech_data
        if t.get("support_strength") in ("强", "中") or t.get("resistance_strength") in ("强", "中")
    ]

    if not any([rejections, confirmations, breakdowns, retests, strong_items]):
        return "", ""

    md_lines = []
    llm_parts = []

    if rejections:
        md_lines.append("#### 🔴 压力位受阻回落")
        for t in rejections:
            md_lines.append(f"- **{t['name']}**: {t.get('resistance_rejection_detail', '')}")
        md_lines.append("")
        llm_parts.append(
            f"[受压回落] {'; '.join(t['name'] + '(' + t.get('resistance_rejection_detail', '')[:30] + ')' for t in rejections[:3])}"
        )

    if confirmations:
        md_lines.append("#### 🟢 支撑位有效确认")
        for t in confirmations:
            md_lines.append(f"- **{t['name']}**: {t.get('support_confirmation_detail', '')}")
        md_lines.append("")
        llm_parts.append(
            f"[支撑确认] {'; '.join(t['name'] + '(' + t.get('support_confirmation_detail', '')[:30] + ')' for t in confirmations[:3])}"
        )

    if breakdowns:
        md_lines.append("#### 🚨 跌破支撑位")
        for t in breakdowns:
            md_lines.append(f"- **{t['name']}**: {t.get('support_breakdown_detail', '')}")
        md_lines.append("")
        llm_parts.append(
            f"[跌破支撑] {'; '.join(t['name'] + '(' + t.get('support_breakdown_detail', '')[:30] + ')' for t in breakdowns[:3])}"
        )

    if retests:
        md_lines.append("#### ✅ 突破后回踩确认")
        for t in retests:
            md_lines.append(f"- **{t['name']}**: {t.get('breakout_retest_detail', '')}")
        md_lines.append("")
        llm_parts.append(
            f"[回踩确认] {'; '.join(t['name'] + '(' + t.get('breakout_retest_detail', '')[:30] + ')' for t in retests[:3])}"
        )

    if strong_items:
        md_lines.append("#### 📊 支撑/压力位强度")
        md_lines.append("| 标的 | 支撑强度 | 压力强度 | 综合 |")
        md_lines.append("|------|---------|---------|------|")
        for t in strong_items:
            md_lines.append(
                f"| {t['name']} | {t.get('support_strength', '--')} | "
                f"{t.get('resistance_strength', '--')} | {t.get('strength_summary', '--')} |"
            )
        md_lines.append("")

    return "\n".join(md_lines) + "\n", "\n".join(llm_parts)


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

    优先使用东方财富真实资金流向明细（FundFlowDetail），
    没有时回退到量价关系的启发式判断。

    Args:
        quote: 实时行情数据
        prev_volume: 前一日成交量（用于计算倍率，仅启发式回退时使用）
    """
    from app.technical import estimate_full_day_volume

    if not quote.price:
        return "数据不足"

    change_pct = quote.change_pct or 0

    # ================================================================
    # 优先：使用东方财富真实资金流向明细
    # ================================================================
    ff = quote.fund_flow
    if ff and ff.is_valid and ff.main_net is not None:
        parts = []
        # 主力净流入概述
        if ff.main_net >= 1e8:
            parts.append(f"主力净流入{ff.main_net/1e8:+.2f}亿")
        elif abs(ff.main_net) >= 1e6:
            parts.append(f"主力净流入{ff.main_net/1e4:+.0f}万")
        elif abs(ff.main_net) >= 1e4:
            parts.append(f"主力净流入{ff.main_net/1e4:+.1f}万")
        else:
            parts.append(f"主力净流入{ff.main_net/1e4:+.2f}万")

        # 总体净流入（超大+大+中+小）
        if ff.total_net is not None and abs(ff.total_net) >= 1e6:
            parts.append(f"总体{ff.total_net/1e8:+.2f}亿")

        # 资金结构
        if ff.is_institution_driven:
            parts.append("机构吸筹(超大单买+散户卖)")
        elif ff.is_distribution:
            parts.append("机构出货(超大单卖+散户接)⚠️")
        elif ff.is_retail_driven:
            parts.append("散户主导(小单推升)⚠️")
        elif ff.main_net > 0:
            parts.append("主力偏多")
        elif ff.main_net < 0:
            parts.append("主力偏空")

        # 补充量价信息
        if ff.super_large_net is not None and abs(ff.super_large_net) >= 5e7:
            parts.append(f"超大单{ff.super_large_net/1e8:+.2f}亿")
        if ff.small_net is not None and abs(ff.small_net) >= 5e7:
            parts.append(f"散户{ff.small_net/1e8:+.2f}亿")

        return "; ".join(parts)

    # ================================================================
    # 回退：量价关系启发式判断
    # ================================================================
    if not quote.open or not quote.high or not quote.low or not quote.pre_close:
        return "数据不足"

    amplitude = quote.amplitude or 0
    volume = estimate_full_day_volume(quote) or 0
    qtype = quote.type or ""

    is_etf = "ETF" in qtype
    is_index = "指数" in qtype

    if is_index:
        pct_high = 0.5
    elif is_etf:
        pct_high = 1.0
    else:
        pct_high = 2.0

    vol_ratio = None
    if prev_volume and prev_volume > 0 and volume > 0:
        vol_ratio = volume / prev_volume
    # 回退：使用腾讯 API 的量比（今日量 vs 近5日均量）
    if vol_ratio is None and quote.volume_ratio and quote.volume_ratio > 0:
        vol_ratio = quote.volume_ratio

    VOL_EXPANSION_THRESHOLD = 1.5
    VOL_SHRINK_THRESHOLD = 0.6

    if quote.high > quote.low:
        position = ((quote.price - quote.low) / (quote.high - quote.low)) * 100
    else:
        position = 50

    gap_up = quote.open > quote.pre_close
    gap_pct = ((quote.open - quote.pre_close) / quote.pre_close * 100) if gap_up else 0

    signals = []

    if vol_ratio is not None:
        if change_pct > pct_high and vol_ratio >= VOL_EXPANSION_THRESHOLD:
            signals.append(f"放量上涨（{vol_ratio:.1f}倍），主力入场")
        elif change_pct > pct_high * 0.5 and vol_ratio <= VOL_SHRINK_THRESHOLD:
            signals.append(f"缩量上涨（{vol_ratio:.1f}倍），买盘不强")
        elif change_pct < -pct_high and vol_ratio >= VOL_EXPANSION_THRESHOLD:
            signals.append(f"放量下跌（{vol_ratio:.1f}倍），主力出逃⚠️")
        elif change_pct < -pct_high * 0.5 and vol_ratio <= VOL_SHRINK_THRESHOLD:
            signals.append(f"缩量下跌（{vol_ratio:.1f}倍），抛压减弱")
        elif abs(change_pct) <= pct_high * 0.3 and vol_ratio >= VOL_EXPANSION_THRESHOLD:
            signals.append(f"平盘放量（{vol_ratio:.1f}倍），资金博弈")
    else:
        amp_high = 1.0 if is_index else (1.5 if is_etf else 4.0)
        amp_low = 0.3 if is_index else (0.6 if is_etf else 1.5)
        if change_pct > pct_high and amplitude > amp_high:
            signals.append("振幅较大上涨，疑似放量")
        elif change_pct > pct_high * 0.7 and amplitude < amp_low:
            signals.append("振幅较小上涨，疑似缩量")
        elif change_pct < -pct_high and amplitude > amp_high:
            signals.append("振幅较大下跌，疑似放量")
        elif change_pct < -pct_high * 0.7 and amplitude < amp_low:
            signals.append("振幅较小下跌，疑似缩量")

    if position > 80 and change_pct > pct_high * 0.7:
        if signals and "放量" in signals[0]:
            signals[0] = signals[0].replace("主力", "强势主力")
        elif not signals:
            signals.append("强势资金主导（收盘接近日内高点）")
    elif position < 20 and change_pct < -pct_high * 0.7:
        if signals and "放量" in signals[0]:
            signals[0] = signals[0].replace("出逃", "恐慌出逃")
        elif not signals:
            signals.append("恐慌抛压（收盘接近日内低点）")

    if gap_up and gap_pct > pct_high * 0.7 and change_pct < 0:
        signals.append("高开低走，疑似出货")

    tail_amp = 0.6 if is_etf else 2.0
    if position > 85 and change_pct > pct_high * 0.5 and amplitude > tail_amp:
        signals.append("尾盘拉升，主力做盘")

    return "; ".join(signals) if signals else "资金面中性"


# ============================================================
# 仓位建议摘要（早报/午评用，基于策略信号聚合）
# ============================================================


def _format_position_summary(
    strategy_signals: list[dict],
    tech_data: list[dict],
) -> tuple[str, str]:
    """基于策略信号和技术数据，生成仓位操作建议摘要

    返回 (Markdown数据区, LLM紧凑文本)，每条建议附带技术理由。
    """
    if not strategy_signals and not tech_data:
        return "", ""

    tech_map = {t["code"]: t for t in tech_data} if tech_data else {}

    def _build_reasons(tech: dict) -> str:
        """从技术数据中提取详细理由（含趋势判断和具体数值）"""
        parts: list[str] = []
        rsi = tech.get("rsi")
        rsi_sig = tech.get("rsi_signal", "")
        macd_sig = tech.get("macd_signal", "")
        kdj_sig = tech.get("kdj_signal", "")
        vol_price = tech.get("vol_price", "")
        obv_sig = tech.get("obv_signal", "")
        ma_align = tech.get("ma_alignment", "")

        # 趋势背景 + 价格与均线位置
        if ma_align and ma_align not in ("数据不足", "缠绕"):
            parts.append(f"均线{ma_align}")
        price = tech.get("price")
        for ma_name, ma_key, label in [
            ("MA5", "ma5", "MA5"), ("MA10", "ma10", "MA10"),
            ("MA20", "ma20", "MA20"), ("MA60", "ma60", "MA60"),
        ]:
            ma_val = tech.get(ma_key)
            if ma_val and price and ma_val > 0 and price > 0:
                dev = (price - ma_val) / ma_val * 100
                if abs(dev) >= 1.0:
                    direction = "站上" if dev > 0 else "跌破"
                    parts.append(f"{direction}{label}{abs(dev):.1f}%")

        # RSI 详细
        if rsi is not None and rsi_sig:
            level = "低位" if rsi <= 35 else ("高位" if rsi >= 65 else "中位")
            parts.append(f"RSI={rsi:.0f}({rsi_sig},{level})")

        # MACD
        if macd_sig:
            dif_val = tech.get("macd_dif")
            if dif_val is not None:
                direction = "向上" if dif_val > 0 else "向下"
                parts.append(f"MACD{direction}({macd_sig})")
            else:
                parts.append(f"MACD{macd_sig}")

        # KDJ
        if kdj_sig:
            k_val = tech.get("kdj_k")
            if k_val is not None:
                parts.append(f"KDJ-K={k_val:.0f}({kdj_sig})")
            else:
                parts.append(f"KDJ{kdj_sig}")

        # 量价 + 量比
        if vol_price and "数据不足" not in vol_price:
            parts.append(vol_price.split("（")[0])
        vr = tech.get("volume_ratio")
        if vr is not None and vr > 0:
            if vr >= 2.0:
                parts.append(f"量比={vr:.1f}(大幅放量)")
            elif vr >= 1.5:
                parts.append(f"量比={vr:.1f}(放量)")
            elif vr <= 0.5:
                parts.append(f"量比={vr:.1f}(缩量)")

        # OBV 资金流
        if obv_sig and obv_sig not in ("中性", "数据不足"):
            parts.append(f"OBV:{obv_sig}")

        # 关键位动态
        if tech.get("has_resistance_rejection"):
            parts.append("⚠️受阻于压力位")
        if tech.get("has_support_confirmation"):
            parts.append("✅支撑位有效确认")
        if tech.get("has_support_breakdown"):
            parts.append("🚨跌破支撑位")
        if tech.get("has_breakout_retest"):
            parts.append("✅突破后回踩站稳")
        if tech.get("has_support_breakdown"):
            parts.append("跌破支撑")
        if tech.get("has_breakout_retest"):
            parts.append("突破回踩")
        support = tech.get("support")
        resistance = tech.get("resistance")
        price = tech.get("price")
        if support and price and price > 0:
            parts.append(f"距支撑{(price-support)/price*100:.1f}%")
        if resistance and price and price > 0:
            parts.append(f"距压力{(resistance-price)/price*100:.1f}%")
        if tech.get("support_strength") in ("强", "中"):
            parts.append(f"支撑{tech['support_strength']}")
        if tech.get("resistance_strength") in ("强", "中"):
            parts.append(f"压力{tech['resistance_strength']}")
        # 均价（黄线）
        avg_p = tech.get("avg_price")
        if avg_p and price and avg_p > 0:
            dev = (price - avg_p) / avg_p * 100
            direction = "高于" if dev > 0 else "低于"
            parts.append(f"均价{avg_p:.3f}({direction}{abs(dev):.1f}%)")
        return "; ".join(parts) if parts else ""

    buy_signals: list[dict] = []
    sell_signals: list[dict] = []
    hold_signals: list[dict] = []

    for s in strategy_signals:
        code = s.get("code", "")
        tech = tech_map.get(code, {})
        signals_text = " ".join(s.get("signals", []))
        reasons = _build_reasons(tech)

        is_buy = any(kw in signals_text for kw in [
            "🟢", "启动", "吸纳", "抄底", "回踩", "金叉",
            "底背离", "反弹", "反转", "放量突破", "建仓",
        ])
        is_sell = any(kw in signals_text for kw in [
            "🔴", "逃顶", "滞警", "减仓", "出货", "死叉",
            "顶背离", "无量反弹", "接盘",
        ])

        entry = {"name": s["name"], "code": code, "signals": s["signals"], "reasons": reasons}

        if is_sell and not is_buy:
            sell_signals.append(entry)
        elif is_buy and not is_sell:
            buy_signals.append(entry)
        else:
            hold_signals.append(entry)

    # 补充：有技术数据但无策略信号的标的，归入中性
    seen_codes = {s.get("code") for signals in [buy_signals, sell_signals, hold_signals] for s in signals}
    for code, tech in tech_map.items():
        if code in seen_codes:
            continue
        reasons = _build_reasons(tech)
        if reasons:
            entry = {"name": tech.get("name", code), "code": code, "signals": [], "reasons": reasons}
            hold_signals.append(entry)

    if not buy_signals and not sell_signals and not hold_signals:
        return "", ""

    md_lines = []
    llm_parts = []

    if buy_signals:
        md_lines.append("#### 🟢 偏多 / 可加仓")
        for b in buy_signals:
            sig_text = '; '.join(b['signals'][:2]) if b['signals'] else ""
            reason_text = f"  \n  *理由: {b['reasons']}*" if b['reasons'] else ""
            md_lines.append(f"- **{b['name']}**: {sig_text}{reason_text}")
        md_lines.append("")
        buy_items = [f"{b['name']}({b['reasons'][:50] if b['reasons'] else '信号共振'})" for b in buy_signals[:3]]
        llm_parts.append(f"[偏多] {' '.join(buy_items)}")

    if sell_signals:
        md_lines.append("#### 🔴 偏空 / 考虑减仓")
        for s in sell_signals:
            sig_text = '; '.join(s['signals'][:2]) if s['signals'] else ""
            reason_text = f"  \n  *理由: {s['reasons']}*" if s['reasons'] else ""
            md_lines.append(f"- **{s['name']}**: {sig_text}{reason_text}")
        md_lines.append("")
        sell_items = [f"{s['name']}({s['reasons'][:50] if s['reasons'] else '信号共振'})" for s in sell_signals[:3]]
        llm_parts.append(f"[偏空] {' '.join(sell_items)}")

    if hold_signals:
        md_lines.append("#### ⚪ 中性 / 持有观望")
        for h in hold_signals:
            sig_text = '; '.join(h['signals'][:2]) if h['signals'] else ""
            reason_text = f"  \n  *理由: {h['reasons']}*" if h['reasons'] else ""
            md_lines.append(f"- **{h['name']}**: {sig_text}{reason_text}")
        md_lines.append("")
        llm_parts.append(f"[中性] {' '.join(h['name'] for h in hold_signals[:3])}")

    return "\n".join(md_lines) + "\n", "\n".join(llm_parts)


# ============================================================
# 大盘及板块分析（早报/午评/晚报用）
# ============================================================


def _format_market_sector_section(
    quotes: list[Quote],
    major_indices: list[Quote],
    sector_boards: list,
) -> tuple[str, str]:
    """生成大盘及行业板块摘要，返回 (Markdown数据区, LLM紧凑文本)"""
    md_lines = []
    llm_parts = []

    # 大盘指数
    if major_indices:
        md_lines.append("### 大盘指数")
        md_lines.append("| 指数 | 最新 | 涨跌幅 | 量比 |")
        md_lines.append("|------|------|--------|------|")
        idx_names = []
        for idx in major_indices[:7]:
            price = f"{idx.price:.2f}" if idx.price else "--"
            chg = f"{idx.change_pct:+.2f}%" if idx.change_pct is not None else "--"
            vr = f"{idx.volume_ratio:.1f}" if idx.volume_ratio and idx.volume_ratio > 0 else "--"
            md_lines.append(f"| {idx.name} | {price} | {chg} | {vr} |")
            if idx.change_pct is not None:
                label = f"{idx.name}{idx.change_pct:+.1f}%"
                if idx.volume_ratio and idx.volume_ratio > 0:
                    label += f"(量比{idx.volume_ratio:.1f})"
                idx_names.append(label)
        md_lines.append("")
        if idx_names:
            llm_parts.append(f"[大盘] {' '.join(idx_names[:5])}")

    # 行业板块排名
    if sector_boards:
        top5 = [sb for sb in sector_boards[:5] if sb.change_pct is not None]
        bot5 = [sb for sb in sector_boards[-5:] if sb.change_pct is not None]
        md_lines.append("### 行业板块 Top5 / Bottom5")
        md_lines.append("| 排名 | 板块 | 涨跌幅 | 领涨股 |")
        md_lines.append("|------|------|--------|--------|")
        for i, sb in enumerate(top5):
            md_lines.append(f"| ▲{i+1} | {sb.name} | {sb.change_pct:+.2f}% | {sb.leader_stock} |")
        md_lines.append("| ... | ... | ... | ... |")
        for i, sb in enumerate(bot5):
            rank = len(sector_boards) - len(bot5) + i + 1
            md_lines.append(f"| ▼{rank} | {sb.name} | {sb.change_pct:+.2f}% | {sb.leader_stock} |")
        md_lines.append("")
        llm_parts.append(
            f"[板块Top3] {' '.join(f'{sb.name}{sb.change_pct:+.1f}%' for sb in top5[:3])}"
        )
        llm_parts.append(
            f"[板块Bot3] {' '.join(f'{sb.name}{sb.change_pct:+.1f}%' for sb in bot5[:3])}"
        )

    # 大盘指数技术面分析
    from app.data_fetcher import fetch_index_klines
    idx_codes = ["000001", "399001", "399006", "000688", "000300", "000905", "000852"]
    idx_klines_map = fetch_index_klines(idx_codes)
    idx_name_map = {"000001": "上证", "399001": "深证", "399006": "创业板", "000688": "科创50",
                    "000300": "沪深300", "000905": "中证500", "000852": "中证1000"}
    if idx_klines_map:
        from app.technical import get_technical_summary
        md_lines.append("### 大盘指数技术面")
        md_lines.append("| 指数 | 现价 | MA排列 | 距MA20 | RSI | MACD | 强支撑 | 强压力 | 状态 |")
        md_lines.append("|------|------|--------|--------|-----|------|--------|--------|------|")
        idx_llm = []
        for code, kls in idx_klines_map.items():
            if len(kls) < 20:
                continue
            quote = Quote(code=code, price=kls[-1].close)
            tech = get_technical_summary(quote, kls)
            name = idx_name_map.get(code, code)
            price = f"{kls[-1].close:.0f}" if kls[-1].close and kls[-1].close > 100 else f"{kls[-1].close:.2f}" if kls[-1].close else "--"
            ma = tech.ma_alignment or "--"
            # 距MA20
            ma20_str = "--"
            if tech.ma20 and kls[-1].close:
                dist = (kls[-1].close - tech.ma20) / tech.ma20 * 100
                ma20_str = f"{dist:+.1f}%"
            rsi_str = f"{tech.rsi:.0f}" if tech.rsi else "--"
            macd_str = tech.macd_signal or "--"
            # 强支撑/强压力（取主支撑/主压力，或摆动点）
            sup_str = f"{tech.support:.0f}" if tech.support and tech.support > 100 else (f"{tech.support:.2f}" if tech.support else "--")
            res_str = f"{tech.resistance:.0f}" if tech.resistance and tech.resistance > 100 else (f"{tech.resistance:.2f}" if tech.resistance else "--")
            from app.technical import detect_market_regime
            reg = detect_market_regime(tech, kls[-1].close or 0, tech.atr)
            status = reg.regime or "--"
            md_lines.append(f"| {name} | {price} | {ma} | {ma20_str} | {rsi_str} | {macd_str} | {sup_str} | {res_str} | {status} |")
            idx_llm.append(f"{name}({ma},{ma20_str},{rsi_str},{macd_str},{sup_str}/{res_str})")
        md_lines.append("")
        if idx_llm:
            llm_parts.append(f"[大盘技术] {' '.join(idx_llm)}")

    # 持仓板块归位
    holding_industries: dict[str, list[Quote]] = {}
    for q in quotes:
        ind = q.industry or ""
        if not ind:
            continue
        if ind not in holding_industries:
            holding_industries[ind] = []
        holding_industries[ind].append(q)

    # 板块涨跌映射（支持模糊匹配：ETF行业名 vs 东方财富板块名）
    sector_chg_map: dict[str, Optional[float]] = {}
    if sector_boards:
        board_names = {sb.name: sb.change_pct for sb in sector_boards}
        for ind in set(q.industry for q in quotes if q.industry):
            if not ind:
                continue
            # 精确匹配
            if ind in board_names:
                sector_chg_map[ind] = board_names[ind]
                continue
            # 模糊匹配：板块名包含行业名 或 行业名包含板块名
            for bname, bchg in board_names.items():
                if ind in bname or bname in ind:
                    sector_chg_map[ind] = bchg
                    break

    if holding_industries:
        md_lines.append("### 持仓板块归位")
        md_lines.append("| 板块 | 持仓标的 | 板块涨跌 | 个股涨跌 | 相对强弱 |")
        md_lines.append("|------|---------|---------|---------|---------|")
        for ind, group in sorted(holding_industries.items(), key=lambda x: len(x[1]), reverse=True):
            sector_chg = sector_chg_map.get(ind)
            for q in group[:3]:  # 每个板块最多显示3只
                chg_str = f"{q.change_pct:+.2f}%" if q.change_pct is not None else "--"
                if sector_chg is not None and q.change_pct is not None:
                    rs = q.change_pct - sector_chg
                    if rs > 1:
                        rs_str = f"领先{rs:+.1f}%"
                    elif rs < -1:
                        rs_str = f"落后{rs:+.1f}%"
                    else:
                        rs_str = "同步"
                else:
                    rs_str = "--"
                sec_str = f"{sector_chg:+.2f}%" if sector_chg is not None else "--"
                md_lines.append(f"| {ind} | {q.name}({q.code}) | {sec_str} | {chg_str} | {rs_str} |")
            if len(group) > 3:
                md_lines.append(f"| {ind} | ... 等{len(group)}只 | | | |")
        md_lines.append("")
        llm_parts.append(
            f"[持仓板块] {' '.join(f'{ind}({len(group)}只)' for ind, group in
             sorted(holding_industries.items(), key=lambda x: len(x[1]), reverse=True)[:5])}"
        )

    # 板块轮动（日环比）
    if sector_boards:
        from datetime import datetime as _dt
        from pathlib import Path as _Pth
        import json
        rot_path = _Pth(__file__).resolve().parent.parent / "state" / "sector_history.json"
        prev_sectors = {}
        if rot_path.exists():
            try:
                with open(rot_path, "r", encoding="utf-8") as f:
                    prev_data = json.load(f)
                today_str = _dt.now().strftime("%Y-%m-%d")
                if prev_data.get("_date") != today_str:
                    prev_sectors = {it["name"]: it["change_pct"] for it in prev_data.get("sectors", [])}
            except Exception:
                pass
        # 保存当日数据
        try:
            rot_path.parent.mkdir(parents=True, exist_ok=True)
            with open(rot_path, "w", encoding="utf-8") as f:
                json.dump({"_date": _dt.now().strftime("%Y-%m-%d"),
                           "sectors": [{"name": sb.name, "change_pct": sb.change_pct}
                                      for sb in sector_boards[:30]]}, f, ensure_ascii=False)
        except Exception:
            pass
        # 计算轮动
        if prev_sectors:
            rotations = []
            for sb in sector_boards[:15]:
                prev_chg = prev_sectors.get(sb.name)
                if prev_chg is not None and sb.change_pct is not None:
                    delta = sb.change_pct - prev_chg
                    if abs(delta) >= 1.0:
                        direction = "🔥加速" if delta > 0 else "💧减速"
                        rotations.append((sb.name, delta, direction))
            if rotations:
                rotations.sort(key=lambda x: abs(x[1]), reverse=True)
                md_lines.append("### 板块轮动(日环比)")
                md_lines.append("| 板块 | 今日 | 昨日 | 变化 | 方向 |")
                md_lines.append("|------|------|------|------|------|")
                for name, delta, direction in rotations[:8]:
                    sb_info = next((s for s in sector_boards if s.name == name), None)
                    today_str2 = f"{sb_info.change_pct:+.2f}%" if sb_info and sb_info.change_pct is not None else "--"
                    prev_str = f"{prev_sectors[name]:+.2f}%" if name in prev_sectors else "--"
                    md_lines.append(f"| {name} | {today_str2} | {prev_str} | {delta:+.1f}% | {direction} |")
                md_lines.append("")
                rot_items = [f"{n}({d:+.1f}%)" for n, d, _ in rotations[:5]]
                llm_parts.append(f"[板块轮动] {' '.join(rot_items)}")

    # 持仓 vs 板块一致性
    if holding_industries and sector_boards:
        md_lines.append("### 持仓 vs 板块一致性")
        md_lines.append("| 持仓 | 个股涨跌 | 所属板块 | 板块涨跌 | 偏离 | 一致性 |")
        md_lines.append("|------|---------|---------|---------|------|--------|")
        bench_parts = []
        for q in quotes:
            ind = q.industry or q.type or ""
            if not ind or q.change_pct is None:
                continue
            sector_chg = sector_chg_map.get(ind) if sector_boards else None
            if sector_chg is not None:
                dev_val = q.change_pct - sector_chg
                if abs(dev_val) >= 1.0:
                    same_dir = (q.change_pct > 0) == (sector_chg > 0)
                    consistency = "✅共振" if same_dir else "⚠️背离"
                    md_lines.append(f"| {q.name}({q.code}) | {q.change_pct:+.2f}% | {ind} | {sector_chg:+.2f}% | {dev_val:+.1f}% | {consistency} |")
                    if abs(dev_val) >= 2.0:
                        bench_parts.append(f"{q.name}({dev_val:+.1f}%{'+共振' if same_dir else '背离'})")
        if bench_parts:
            md_lines.append("")
            llm_parts.append(f"[持仓vs板块] {' '.join(bench_parts[:5])}")

    if not md_lines:
        return "", ""
    return "\n".join(md_lines) + "\n", "\n".join(llm_parts)


# ============================================================
# 量能分析（独立章节，成交量对比 + 量价关系）
# ============================================================

def _format_volume_section(
    quotes: list[Quote],
    holdings: list[Holding],
    prev_state: dict,
) -> tuple[str, str]:
    """生成量能分析章节，返回 (数据区Markdown, LLM紧凑文本)

    对每个持仓标的，对比今日成交量与前日成交量，判断量能变化。
    """
    from app.technical import estimate_full_day_volume

    if not quotes:
        return "", ""

    holding_codes = {h.code for h in holdings}
    quote_map = {q.code: q for q in quotes}

    rows: list[dict] = []
    for code in holding_codes:
        q = quote_map.get(code)
        if not q or not q.volume or q.volume <= 0:
            continue

        # 估算今日全天成交量
        today_vol = estimate_full_day_volume(q) or q.volume

        # 前日成交量（从 monitor_state 取）
        prev_vol = None
        ps = prev_state.get(code, {})
        if isinstance(ps, dict):
            prev_vol = ps.get("volume")
        # prev_state 也可能是数字（旧格式）
        elif isinstance(ps, (int, float)):
            prev_vol = float(ps)

        # 量比（优先用 prev_state 计算，回退到 quote.volume_ratio）
        vol_ratio = None
        vol_signal = ""
        if prev_vol and prev_vol > 0:
            vol_ratio = today_vol / prev_vol
        elif q.volume_ratio is not None and q.volume_ratio > 0:
            vol_ratio = q.volume_ratio

        if vol_ratio is not None:
            if vol_ratio >= 2.0:
                vol_signal = "🔴 大幅放量"
            elif vol_ratio >= 1.5:
                vol_signal = "🟠 放量"
            elif vol_ratio >= 0.8:
                vol_signal = "⚪ 持平"
            elif vol_ratio >= 0.5:
                vol_signal = "🔵 缩量"
            else:
                vol_signal = "🔷 大幅缩量"

        # 量价关系
        chg = q.change_pct or 0
        if vol_signal and chg != 0:
            if "放量" in vol_signal and chg > 0:
                vol_signal += "上涨"
            elif "放量" in vol_signal and chg < 0:
                vol_signal += "下跌⚠️"
            elif "缩量" in vol_signal and chg > 0:
                vol_signal += "上涨"
            elif "缩量" in vol_signal and chg < 0:
                vol_signal += "下跌"

        rows.append({
            "name": q.name,
            "code": q.code,
            "today_vol": today_vol,
            "prev_vol": prev_vol,
            "vol_ratio": vol_ratio,
            "vol_signal": vol_signal,
            "turnover": q.turnover_rate,
            "change_pct": chg,
        })

    if not rows:
        return "", ""

    # ---- Markdown 数据区 ----
    md_lines = ["### 📊 量能分析", ""]
    md_lines.append("| 标的 | 涨跌幅 | 今日量(估算) | 前日量 | 量比 | 换手率 | 量价信号 |")
    md_lines.append("|------|--------|-------------|--------|------|--------|----------|")

    llm_parts = ["[量能]"]
    alerts_for_llm = []

    for r in rows:
        chg = f"{r['change_pct']:+.2f}%" if r['change_pct'] is not None else "--"
        today_str = _format_volume_compact(r['today_vol'])
        prev_str = _format_volume_compact(r['prev_vol']) if r['prev_vol'] else "--"
        ratio_str = f"{r['vol_ratio']:.1f}x" if r['vol_ratio'] else "--"
        tr_str = f"{r['turnover']:.2f}%" if r['turnover'] else "--"
        sig = r['vol_signal'] or "--"

        md_lines.append(
            f"| {r['name']}({r['code']}) | {chg} | {today_str} "
            f"| {prev_str} | {ratio_str} | {tr_str} | {sig} |"
        )

        # LLM 紧凑文本
        if r['vol_signal']:
            alerts_for_llm.append(f"{r['name']} {r['vol_signal']}({ratio_str})")

    if alerts_for_llm:
        llm_parts.append("; ".join(alerts_for_llm[:5]))
    else:
        llm_parts.append("量能平稳")

    md = "\n".join(md_lines) + "\n"
    return md, "\n".join(llm_parts)


def _format_volume_compact(vol: float | None) -> str:
    """紧凑格式化成交量"""
    if vol is None:
        return "--"
    if vol >= 1e8:
        return f"{vol/1e8:.2f}亿"
    elif vol >= 1e4:
        return f"{vol/1e4:.0f}万"
    else:
        return f"{vol:.0f}"


# ============================================================
# 交易辅助数据预计算（代码算价格，LLM 做推理）
# ============================================================


def _compute_entry_suggestions(
    watchlist: list[WatchItem],
    holdings: list[Holding],
    quotes: list[Quote],
    tech_data: list[dict],
    dragon_tiger_summary: "DragonTigerSummary | None" = None,
) -> list[dict]:
    """为自选但未持有的标的生成建仓建议

    基于逃顶/抄底评分模型，但标签针对"建仓"场景调整。

    Args:
        watchlist: 自选标的列表
        holdings: 持仓列表（用于排除已持有的）
        quotes: 实时行情
        tech_data: _get_holdings_tech_analysis 的技术分析数据
        dragon_tiger_summary: 龙虎榜数据（可选）

    Returns:
        建仓建议列表，按优先级排序
    """
    holding_codes = {h.code for h in holdings}
    quote_map = {q.code: q for q in quotes}
    tech_map = {t["code"]: t for t in tech_data}

    # 龙虎榜索引
    dt_patterns: dict[str, list[str]] = {}
    if dragon_tiger_summary and dragon_tiger_summary.abnormal_patterns:
        for p in dragon_tiger_summary.abnormal_patterns:
            code = p.get("code", "")
            if code not in dt_patterns:
                dt_patterns[code] = []
            dt_patterns[code].append(p.get("pattern_type", ""))

    results: list[dict] = []

    for item in watchlist:
        code = item.code
        # 排除已持仓的
        if code in holding_codes:
            continue

        quote = quote_map.get(code)
        tech = tech_map.get(code)
        if not quote or not quote.price or not tech:
            continue

        price = quote.price
        support = tech.get("support")
        resistance = tech.get("resistance")
        atr = tech.get("atr")
        rsi = tech.get("rsi")
        rsi_sig = tech.get("rsi_signal", "")
        macd_sig = tech.get("macd_signal", "")
        kdj_sig = tech.get("kdj_signal", "")
        vol_price = tech.get("vol_price", "")
        obv_signal = tech.get("obv_signal", "")
        clusters = tech.get("volume_clusters", []) or []

        # ---- 建仓评分（偏重多方信号） ----
        entry_score = 0
        entry_reasons: list[str] = []
        risk_score = 0
        risk_reasons: list[str] = []

        # 主力资金因子
        ff_entry = quote.fund_flow
        if ff_entry and ff_entry.is_valid and quote.main_net_inflow and quote.amount and quote.amount > 0:
            flow_pct_e = quote.main_net_inflow / quote.amount * 100
            if flow_pct_e >= 10:
                entry_score += 3
                entry_reasons.append(f"主力流入{flow_pct_e:.0f}%")
            elif flow_pct_e >= 5:
                entry_score += 1
                entry_reasons.append(f"主力偏多({flow_pct_e:.0f}%)")
            if ff_entry.is_institution_absorbing:
                entry_score += 3
                entry_reasons.append("机构深度吸筹")
            elif ff_entry.is_institution_driven:
                entry_score += 2
                entry_reasons.append("机构主导")
            if ff_entry.is_distribution:
                risk_score += 3
                risk_reasons.append("机构出货")
            elif ff_entry.is_retail_driven:
                risk_score += 1
                risk_reasons.append("散户主导")
            if flow_pct_e <= -8:
                risk_score += 2
                risk_reasons.append(f"主力流出{abs(flow_pct_e):.0f}%")

        # 加分项
        if rsi is not None and rsi <= 30:
            entry_score += 3
            entry_reasons.append(f"RSI超卖({rsi:.0f})")
        elif rsi is not None and rsi <= 40:
            entry_score += 1
            entry_reasons.append(f"RSI偏弱({rsi:.0f})")

        if support and price <= support * 1.03:
            entry_score += 3
            entry_reasons.append(f"接近支撑{support:.3f}")

        if "金叉" in macd_sig or "底背离" in macd_sig:
            entry_score += 2
            entry_reasons.append(f"MACD{macd_sig}")
        elif "动能增强" in macd_sig:
            entry_score += 1
            entry_reasons.append(f"MACD{macd_sig}")

        if "超卖" in kdj_sig:
            entry_score += 1
            entry_reasons.append("KDJ超卖")
        if "金叉" in kdj_sig:
            entry_score += 1
            entry_reasons.append("KDJ金叉")

        # OBV 资金流入信号
        if obv_signal in ("资金加速流入", "资金转向流入", "底背离"):
            entry_score += 2
            entry_reasons.append(f"OBV{obv_signal}")

        if "放量上涨" in vol_price or "主力入场" in vol_price:
            entry_score += 2
            entry_reasons.append(vol_price)
        if "缩量下跌" in vol_price or "抛压减弱" in vol_price:
            entry_score += 1
            entry_reasons.append(vol_price)

        # 关键位动态
        if tech.get("has_support_confirmation"):
            entry_score += 2
            entry_reasons.append("支撑位有效确认")
        if tech.get("has_breakout_retest"):
            entry_score += 3
            entry_reasons.append("突破后回踩确认")

        # 龙虎榜
        dt_ptypes = dt_patterns.get(code, [])
        if "limit_down_accumulation" in dt_ptypes:
            entry_score += 3
            entry_reasons.append("龙虎榜跌停接筹")

        # 减分项（风险因素）
        if rsi is not None and rsi >= 70:
            risk_score += 3
            risk_reasons.append(f"RSI超买({rsi:.0f})")
        elif rsi is not None and rsi >= 60:
            risk_score += 1
            risk_reasons.append(f"RSI偏强({rsi:.0f})")

        if resistance and price >= resistance * 0.98:
            risk_score += 3
            risk_reasons.append(f"接近压力{resistance:.3f}")

        if "死叉" in macd_sig or "顶背离" in macd_sig:
            risk_score += 2
            risk_reasons.append(f"MACD{macd_sig}")

        if "超买" in kdj_sig:
            risk_score += 1
            risk_reasons.append("KDJ超买")

        # OBV 资金流出
        if obv_signal in ("资金加速流出", "资金转向流出", "顶背离⚠️"):
            risk_score += 2
            risk_reasons.append(f"OBV{obv_signal}")

        if tech.get("has_resistance_rejection"):
            risk_score += 2
            risk_reasons.append("压力位受阻回落")
        if tech.get("has_support_breakdown"):
            risk_score += 3
            risk_reasons.append("跌破支撑位")

        if "limit_up_distribution" in dt_ptypes:
            risk_score += 3
            risk_reasons.append("龙虎榜涨停出货")

        # ---- 建仓标签 ----
        net_score = entry_score - risk_score
        if entry_score >= 5 and risk_score <= 2:
            label = "🟢 强烈建议建仓"
            priority = net_score
        elif entry_score >= 3 and risk_score <= 3:
            label = "🟢 建议建仓"
            priority = net_score
        elif entry_score >= 2:
            label = "🟡 关注等待"
            priority = entry_score
        elif risk_score >= 5:
            label = "🔴 暂避"
            priority = -risk_score
        else:
            label = "⚪ 暂不建议"
            priority = 0

        # ---- 建仓参考价位 ----
        # 理想建仓价：支撑位附近
        entry_price = support if support and support < price else round(price * 0.97, 3)
        # 止损价：支撑位下方或 ATR×2
        if support:
            sl = round(support * 0.98, 3)
            sl_reason = f"跌破支撑{support:.3f}"
        else:
            atr_val = atr or price * 0.02
            sl = round(price - atr_val * 2, 3)
            sl_reason = f"2×ATR({atr_val:.3f})波动止损"

        results.append({
            "name": item.name,
            "code": code,
            "type": item.type,
            "price": price,
            "change_pct": quote.change_pct,
            "entry_score": entry_score,
            "entry_reasons": entry_reasons,
            "risk_score": risk_score,
            "risk_reasons": risk_reasons,
            "net_score": net_score,
            "label": label,
            "priority": priority,
            "entry_price": round(entry_price, 3),
            "stop_loss": sl,
            "stop_loss_reason": sl_reason,
            "support": support,
            "resistance": resistance,
        })

    results.sort(key=lambda x: x["priority"], reverse=True)
    return results


def _compute_trading_suggestions(
    holdings: list[Holding],
    quotes: list[Quote],
    tech_data: list[dict],
    dragon_tiger_summary: "DragonTigerSummary | None" = None,
) -> list[dict]:
    """为每只持仓预计算交易辅助数据：网格区间、逃顶/抄底信号

    所有价格点由代码精确计算，LLM 只负责解释和给出操作建议。

    Args:
        holdings: 持仓列表
        quotes: 实时行情列表
        tech_data: _get_holdings_tech_analysis 的返回结果
        dragon_tiger_summary: 龙虎榜综合分析结果（可选）

    Returns:
        每只持仓的交易辅助数据列表，按信号强度排序
    """
    results: list[dict] = []

    # 建立辅助索引
    quote_map = {q.code: q for q in quotes}
    tech_map = {t["code"]: t for t in tech_data}

    # 龙虎榜异常形态索引
    dt_patterns: dict[str, list[str]] = {}
    if dragon_tiger_summary and dragon_tiger_summary.abnormal_patterns:
        for p in dragon_tiger_summary.abnormal_patterns:
            code = p.get("code", "")
            if code not in dt_patterns:
                dt_patterns[code] = []
            dt_patterns[code].append(p.get("pattern_type", ""))

    for h in holdings:
        code = h.code
        quote = quote_map.get(code)
        tech = tech_map.get(code)
        if not quote or not tech or not quote.price:
            continue

        price = quote.price
        support = tech.get("support")
        resistance = tech.get("resistance")
        atr = tech.get("atr")
        clusters = tech.get("volume_clusters", []) or []
        rsi = tech.get("rsi")
        rsi_sig = tech.get("rsi_signal", "")
        macd_sig = tech.get("macd_signal", "")
        kdj_sig = tech.get("kdj_signal", "")
        vol_price = tech.get("vol_price", "")

        # ---- 1. 网格区间计算 ----
        # 收集所有有效的支撑和压力点
        all_supports: list[float] = []
        all_resistances: list[float] = []

        if support and price > 0:
            all_supports.append(support)
        if resistance and price > 0:
            all_resistances.append(resistance)

        # 成交密集区低于现价 = 支撑，高于现价 = 压力
        for c in clusters:
            if c < price * 0.99:
                all_supports.append(c)
            elif c > price * 1.01:
                all_resistances.append(c)

        all_supports.sort(reverse=True)   # 从高到低
        all_resistances.sort()            # 从低到高

        # 网格下沿 = 最近的主要支撑（不含太远的）
        grid_lower = all_supports[0] if all_supports else round(price * 0.95, 2)
        # 网格上沿 = 最近的主要压力
        grid_upper = all_resistances[0] if all_resistances else round(price * 1.05, 2)

        # 网格间距 = ATR * 0.5，最小 0.01（防止低股价精度不够）
        grid_step = round(max(atr or price * 0.01, 0.01) * 0.5, 3)
        if grid_step <= 0:
            grid_step = round(price * 0.005, 3)

        # 计算网格档位
        grid_levels: list[dict] = []
        if grid_upper > grid_lower and grid_step > 0:
            level_count = int((grid_upper - grid_lower) / grid_step)
            level_count = min(level_count, 30)  # 最多 30 档
            for i in range(level_count + 1):
                lvl_price = round(grid_lower + i * grid_step, 3)
                tag = ""
                if lvl_price <= price * 1.002 and lvl_price >= price * 0.998:
                    tag = "◀ 当前价"
                elif abs(lvl_price - support) < grid_step * 0.5 if support else False:
                    tag = "(支撑)"
                elif abs(lvl_price - resistance) < grid_step * 0.5 if resistance else False:
                    tag = "(压力)"
                grid_levels.append({"price": lvl_price, "tag": tag})

        # 当前价在网格中的位置（0-100%）
        if grid_upper > grid_lower:
            grid_position = round((price - grid_lower) / (grid_upper - grid_lower) * 100)
        else:
            grid_position = 50

        # ---- 2. 逃顶信号评分 ----
        escape_score = 0
        escape_reasons: list[str] = []

        # 主力资金因子
        ff_sug = quote.fund_flow
        if ff_sug and ff_sug.is_valid and quote.main_net_inflow and quote.amount and quote.amount > 0:
            flow_pct_sug = quote.main_net_inflow / quote.amount * 100
            if flow_pct_sug <= -15:
                escape_score += 3
                escape_reasons.append(f"主力大幅流出({flow_pct_sug:.0f}%)")
            elif flow_pct_sug <= -8:
                escape_score += 1
                escape_reasons.append(f"主力流出({flow_pct_sug:.0f}%)")
            if ff_sug.is_distribution:
                escape_score += 2
                escape_reasons.append("机构出货(散户接盘)")
            elif ff_sug.is_retail_driven:
                escape_score += 1
                escape_reasons.append("散户主导(主力缺位)")

        # RSI 超买：RSI >= 70 算超买
        if rsi is not None and rsi >= 70:
            escape_score += 2
            escape_reasons.append(f"RSI超买({rsi:.0f})")
        elif rsi is not None and rsi >= 60:
            escape_score += 1
            escape_reasons.append(f"RSI偏强({rsi:.0f})")

        # 接近压力位
        if resistance and price >= resistance * 0.98:
            escape_score += 2
            escape_reasons.append(f"接近压力{resistance:.3f}")

        # MACD 死叉或即将死叉
        if "死叉" in macd_sig or "顶背离" in macd_sig:
            escape_score += 2
            escape_reasons.append(f"MACD{macd_sig}")
        elif "动能减弱" in macd_sig or "可能见顶" in macd_sig:
            escape_score += 1
            escape_reasons.append(f"MACD{macd_sig}")

        # KDJ 超买
        if "超买" in kdj_sig:
            escape_score += 1
            escape_reasons.append(f"KDJ超买")

        # 量价背离：缩量上涨 / 放量滞涨
        if "缩量上涨" in vol_price:
            escape_score += 2
            escape_reasons.append("缩量上涨(量价背离)")
        elif "放量滞涨" in vol_price or "平盘放量" in vol_price:
            escape_score += 1
            escape_reasons.append(vol_price)

        # 龙虎榜出货信号
        dt_ptypes = dt_patterns.get(code, [])
        if "limit_up_distribution" in dt_ptypes:
            escape_score += 3
            escape_reasons.append("龙虎榜涨停板出货")
        if "wash_trade" in dt_ptypes:
            escape_score += 1
            escape_reasons.append("龙虎榜机构对倒")

        # 网格位置高
        if grid_position >= 80:
            escape_score += 1
            escape_reasons.append(f"网格高位({grid_position}%)")

        # 关键位动态信号
        if tech.get("has_resistance_rejection"):
            escape_score += 2
            escape_reasons.append("关键压力位受阻回落")
        if tech.get("has_support_breakdown"):
            escape_score += 3
            escape_reasons.append("跌破关键支撑位")

        # ---- 3. 抄底信号评分 ----
        dip_score = 0
        dip_reasons: list[str] = []

        # 主力资金因子（抄底侧）
        if ff_sug and ff_sug.is_valid and quote.main_net_inflow and quote.amount and quote.amount > 0:
            flow_pct_sug2 = quote.main_net_inflow / quote.amount * 100
            if flow_pct_sug2 >= 12:
                dip_score += 3
                dip_reasons.append(f"主力大幅流入({flow_pct_sug2:.0f}%)")
            elif flow_pct_sug2 >= 6:
                dip_score += 1
                dip_reasons.append(f"主力流入({flow_pct_sug2:.0f}%)")
            if ff_sug.is_institution_absorbing:
                dip_score += 3
                dip_reasons.append("机构深度吸筹(中小资金出逃)")
            elif ff_sug.is_institution_driven:
                dip_score += 2
                dip_reasons.append("机构吸筹(散户卖出)")

        # RSI 超卖
        if rsi is not None and rsi <= 30:
            dip_score += 2
            dip_reasons.append(f"RSI超卖({rsi:.0f})")
        elif rsi is not None and rsi <= 40:
            dip_score += 1
            dip_reasons.append(f"RSI偏弱({rsi:.0f})")

        # 接近支撑位
        if support and price <= support * 1.02:
            dip_score += 2
            dip_reasons.append(f"接近支撑{support:.3f}")

        # MACD 金叉或底背离
        if "金叉" in macd_sig or "底背离" in macd_sig:
            dip_score += 2
            dip_reasons.append(f"MACD{macd_sig}")
        elif "动能增强" in macd_sig:
            dip_score += 1
            dip_reasons.append(f"MACD{macd_sig}")

        # KDJ 超卖
        if "超卖" in kdj_sig:
            dip_score += 1
            dip_reasons.append(f"KDJ超卖")

        # 量价关系
        if "缩量下跌" in vol_price or "抛压减弱" in vol_price:
            dip_score += 1
            dip_reasons.append(vol_price)
        if "放量上涨" in vol_price or "主力入场" in vol_price:
            dip_score += 1
            dip_reasons.append(vol_price)

        # 龙虎榜接筹信号
        if "limit_down_accumulation" in dt_ptypes:
            dip_score += 3
            dip_reasons.append("龙虎榜跌停接筹")

        # 网格位置低
        if grid_position <= 20:
            dip_score += 1
            dip_reasons.append(f"网格低位({grid_position}%)")

        # 关键位动态信号
        if tech.get("has_support_confirmation"):
            dip_score += 2
            dip_reasons.append("关键支撑位有效确认")
        if tech.get("has_breakout_retest"):
            dip_score += 2
            dip_reasons.append("突破后回踩确认站稳")

        # ---- 4. 综合建议标签 ----
        suggestion = "观望"
        priority = 0
        # 窄幅震荡时降级建议（回测：此状态胜率45.6%，均值-0.32%）
        regime_penalty = ""
        if tech.get("market_regime") == "窄幅震荡":
            regime_penalty = "(窄幅震荡，信号可靠性降低)"

        if dip_score >= 5 and escape_score <= 1:
            suggestion = f"🟢 逢低吸纳{regime_penalty}"
            priority = dip_score
        elif dip_score >= 3:
            suggestion = f"🟡 关注抄底{regime_penalty}"
            priority = dip_score
        elif escape_score >= 5 and dip_score <= 1:
            suggestion = f"🚨 逃顶预警{regime_penalty}"
            priority = escape_score
        elif escape_score >= 3:
            suggestion = f"🔴 考虑减仓{regime_penalty}"
            priority = escape_score
        elif escape_score >= 2 and dip_score >= 2:
            suggestion = "⚪ 多空博弈"
            priority = 0

        # ---- 5. 止盈/止损价（代码计算 + 理由） ----
        stop_loss = 0.0
        stop_loss_reason = ""
        take_profit = 0.0
        take_profit_reason = ""

        # 止损价：找下方最近的有效支撑
        below_refs = [(s, "main_support") for s in [support] if s and s < price]
        below_refs += [(c, "volume_cluster") for c in clusters if c < price]

        if below_refs:
            below_refs.sort(key=lambda x: x[0], reverse=True)
            stop_loss = below_refs[0][0]
            ref_type = below_refs[0][1]
            if ref_type == "main_support":
                stop_loss_reason = f"主支撑位{stop_loss:.2f}，跌破则趋势走坏"
            else:
                stop_loss_reason = f"成交密集区{stop_loss:.2f}，跌破大量筹码被套"
        else:
            # 无有效支撑参照 → ATR × 2 止损
            atr_val = atr or price * 0.02
            stop_loss = round(price - atr_val * 2, 3)
            if stop_loss >= price:
                stop_loss = round(price * 0.95, 3)
            stop_loss_reason = f"无明确支撑位，基于2×ATR({atr_val:.3f})波动止损"

        # 止盈价：找上方最近的有效压力
        above_refs = [(r, "main_resistance") for r in [resistance] if r and r > price]
        above_refs += [(c, "volume_cluster") for c in clusters if c > price]

        if above_refs:
            above_refs.sort(key=lambda x: x[0])
            take_profit = above_refs[0][0]
            ref_type = above_refs[0][1]
            if ref_type == "main_resistance":
                take_profit_reason = f"主压力位{take_profit:.2f}，触及大概率受阻回落"
            else:
                take_profit_reason = f"成交密集区{take_profit:.2f}，大量套牢盘解套抛压"
        else:
            atr_val = atr or price * 0.02
            take_profit = round(price + atr_val * 2, 3)
            take_profit_reason = f"无明确压力位，基于2×ATR({atr_val:.3f})波动止盈"

        # 止损/止盈合理性检查
        if stop_loss >= price:
            stop_loss = round(price * 0.95, 3)
            stop_loss_reason = "无有效支撑，按现价-5%硬止损"
        if take_profit <= price:
            take_profit = round(price * 1.05, 3)
            take_profit_reason = "无有效压力，按现价+5%硬止盈"

        # ---- 5.5 仓位比例计算（ATR波动率 + Kelly公式） ----
        position_pct = 0.0
        position_reason = ""
        if "吸纳" in suggestion or "抄底" in suggestion:
            if stop_loss > 0 and price > stop_loss:
                risk_per_share = price - stop_loss
                risk_pct = risk_per_share / price * 100
                if risk_pct > 0.1:
                    raw_pct = 1.0 / risk_pct * 100
                    position_pct = round(max(3.0, min(25.0, raw_pct)), 1)
                    position_reason = f"止损{stop_loss:.3f}(幅度{risk_pct:.1f}%)，1%风险预算→仓位{position_pct:.1f}%"
                else:
                    position_pct = 5.0
                    position_reason = "止损幅度极小，保守建议仓位5%"
            else:
                position_pct = 5.0
                position_reason = "止损距现价太近，保守仓位5%"
            if "抄底" in suggestion:
                position_pct = round(position_pct * 0.5, 1)
                suggestion = f"🟡 关注抄底(试探仓位{position_pct:.0f}%)"
            else:
                # Kelly公式交叉验证
                kelly_pct = 0.0
                score_val = tech.get("composite_score", 0)
                if score_val >= 75:
                    prob = 0.56
                elif score_val >= 60:
                    prob = 0.52
                else:
                    prob = 0.50
                if take_profit > price and stop_loss > 0 and price > stop_loss:
                    b_ratio = (take_profit - price) / (price - stop_loss)
                    if b_ratio > 0:
                        kelly_raw = (prob * b_ratio - (1 - prob)) / b_ratio
                        kelly_pct = round(max(0, kelly_raw) * 100, 1)
                # 取 ATR 和 Kelly 中较保守的值
                if kelly_pct > 0 and kelly_pct < position_pct:
                    position_pct = kelly_pct
                    position_reason += f" (Kelly校准→{kelly_pct:.1f}%)"
                suggestion = f"🟢 逢低吸纳(建议仓位{position_pct:.0f}%)"

        results.append({
            "name": h.name,
            "code": code,
            "price": price,
            "change_pct": quote.change_pct,
            "grid_lower": grid_lower,
            "grid_upper": grid_upper,
            "grid_step": grid_step,
            "grid_levels": grid_levels,
            "grid_position": grid_position,
            "grid_level_count": len(grid_levels) - 1,
            "escape_score": escape_score,
            "escape_reasons": escape_reasons,
            "dip_score": dip_score,
            "dip_reasons": dip_reasons,
            "suggestion": suggestion,
            "priority": priority,
            "support": support,
            "resistance": resistance,
            "atr": atr,
            "rsi": rsi,
            "rsi_signal": rsi_sig,
            "macd_signal": macd_sig,
            "kdj_signal": kdj_sig,
            "vol_price": vol_price,
            "volume_clusters": clusters[:3],
            # 止盈/止损
            "stop_loss": round(stop_loss, 3),
            "stop_loss_reason": stop_loss_reason,
            "take_profit": round(take_profit, 3),
            "take_profit_reason": take_profit_reason,
            # 仓位建议
            "position_pct": position_pct,
            "position_reason": position_reason,
        })

    # 按优先级排序：需要行动的排前面
    results.sort(key=lambda x: (x["priority"], x["escape_score"] + x["dip_score"]), reverse=True)
    return results


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
    # 妙想优先获取盘中快讯，新浪兜底
    from app.miaoxiang import fetch_news_for_report
    mx_day_news = fetch_news_for_report(config, "今日A股收盘 重要新闻 政策 影响")
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

    if mx_day_news:
        data_lines.append("## 一、盘中快讯（妙想）")
        data_lines.append(mx_day_news)
    elif day_news:
        data_lines.append("## 一、盘中快讯（供参考）")
        for n in day_news:
            cat = f" [{n.category}]" if n.category else ""
            data_lines.append(f"- [{n.time}]{cat} {n.title}")

    if dragon_tiger_report:
        data_lines.append(f"\n## 二、龙虎榜资金分析")
        data_lines.append(f"\n{dragon_tiger_report}")

    # 大盘及行业板块
    from app.data_fetcher import enrich_quotes_with_industry, fetch_sector_boards, fetch_major_indices
    enrich_quotes_with_industry(quotes)
    sector_boards_ev = fetch_sector_boards()
    major_indices_ev = fetch_major_indices()
    sector_md, sector_llm = _format_market_sector_section(quotes, major_indices_ev, sector_boards_ev)
    if sector_md:
        data_lines.append(f"\n## 三、📈 大盘及行业板块")
        data_lines.append(sector_md)

    # 市场背景（全市场广度数据）
    from app.data_fetcher import fetch_market_breadth
    breadth = fetch_market_breadth()
    if breadth and breadth.is_valid:
        data_lines.append(f"\n## 四、全市场背景")
        data_lines.append(f"- 涨跌: {breadth.up_count}涨/{breadth.down_count}跌/{breadth.flat_count}平 ({breadth.breadth_label})")
        data_lines.append(f"- 涨停{breadth.limit_up}只/跌停{breadth.limit_down}只 ({breadth.limit_emotion})")
        if breadth.total_amount > 0:
            data_lines.append(f"- 成交额: {breadth.total_amount:.0f}亿 (估算全天{breadth.estimated_full_day_amount:.0f}亿)")
        if breadth.main_net_inflow != 0:
            direction = "净流入" if breadth.main_net_inflow > 0 else "净流出"
            data_lines.append(f"- 全市场主力: {direction}{abs(breadth.main_net_inflow):.1f}亿")
    else:
        data_lines.append(f"\n## 四、市场背景")
        data_lines.append("- 全市场广度数据暂不可用")

    h_results, total_pnl, total_cost = _holdings_summary(holdings, quotes)
    vol_llm = ""  # 量能分析 LLM 紧凑文本（在 h_results 块中填充）
    intraday_llm = ""  # 盘中复盘 LLM 紧凑文本
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

        data_lines.append(f"\n## 五、持仓表现")

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

        # 量能分析章节
        vol_md, vol_llm = _format_volume_section(quotes, holdings, prev_state)
        if vol_md:
            data_lines.append(f"\n### 3.4 量能分析")
            data_lines.append(vol_md)

        # 盘中复盘（盯盘落盘的日内序列）
        intraday_md, intraday_llm = _format_intraday_evolution(holdings)
        if intraday_md:
            data_lines.append(f"\n### 3.5 盘中复盘")
            data_lines.append(intraday_md)

    # Technical analysis for holdings
    tech_data_evening = _get_holdings_tech_analysis(holdings, quotes)
    strategy_signals_evening = _get_holdings_strategy_signals(holdings, quotes)

    # 多信号共振评分
    score_md, score_llm = _format_composite_scoring(tech_data_evening)
    if score_md:
        data_lines.append(f"\n## 六、📊 多信号共振评分")
        data_lines.append(score_md)

    if tech_data_evening:
        data_lines.append("\n## 七、持仓技术分析")
        data_lines.append("")
        data_lines.append("| 标的 | 现价 | 均价 | 涨跌幅 | 量比 | 量价 | 布林(上/中/下) | RSI | MACD | KDJ | OBV | 成交量 | 换手率 |")
        data_lines.append("|------|------|------|--------|------|------|--------------|-----|------|-----|-----|--------|--------|")
        for t in tech_data_evening:
            price = f"{t['price']:.3f}" if t.get('price') else "--"
            avg_p_val = t.get('avg_price')
            avg_str = f"{avg_p_val:.3f}" if avg_p_val else "--"
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
            vr_val = t.get('volume_ratio')
            vr_str = f"{vr_val:.1f}" if vr_val is not None and vr_val > 0 else "--"
            bb_u = t.get('bb_upper')
            bb_m = t.get('bb_middle')
            bb_l = t.get('bb_lower')
            bb_str = f"{bb_u:.3f}/{bb_m:.3f}/{bb_l:.3f}" if bb_u and bb_m and bb_l else "--"
            data_lines.append(f"| {t['name']} | {price} | {avg_str} | {chg} | {vr_str} | {t['vol_price']} | {bb_str} | {rsi} | {macd} | {kdj} | {obv_val} | {vol} | {tr} |")

    if strategy_signals_evening:
        data_lines.append(f"\n## 八、⭐ 组合策略信号（多指标共振，明日操作参考）")
        for s in strategy_signals_evening:
            for sig_text in s['signals']:
                data_lines.append(f"  - {s['name']}: {sig_text}")

    # 7. Pre-compute trading suggestions (grid / escape / dip)
    # 跳空缺口 & 关键位突破
    gap_bo_md, gap_bo_llm = _format_gap_breakout_section(tech_data_evening)
    if gap_bo_md:
        data_lines.append(f"\n## 九、📊 缺口与突破")
        data_lines.append(gap_bo_md)

    # 附近较强的支撑/压力位全景
    nearby_items = [(t["name"], t["code"], t.get("nearby_supports", ""), t.get("nearby_resistances", ""),
                     t.get("price"), t.get("support"), t.get("resistance"), t.get("atr"))
                    for t in tech_data_evening
                    if t.get("nearby_supports") or t.get("nearby_resistances")]
    if nearby_items:
        data_lines.append(f"\n## 十、📌 附近关键位全景")
        data_lines.append("")
        data_lines.append("| 标的 | 现价 | 较强支撑(距现价) | 较强压力(距现价) |")
        data_lines.append("|------|------|------------------|------------------|")
        for name, code, sups, ress, pr, _, _, _ in nearby_items:
            p_str = f"{pr:.3f}" if pr else "--"
            data_lines.append(f"| {name}({code}) | {p_str} | {sups or '无'} | {ress or '无'} |")
        data_lines.append("")

    # 关键位动态行为
    key_level_ev_md, key_level_ev_llm = _format_key_level_behavior_section(tech_data_evening)
    if key_level_ev_md:
        data_lines.append(f"\n## 十一、🎯 关键位动态行为")
        data_lines.append(key_level_ev_md)

    trade_suggestions = _compute_trading_suggestions(
        holdings, quotes, tech_data_evening, dragon_tiger_summary
    )
    if trade_suggestions:
        data_lines.append(f"\n## 十二、🎯 交易辅助数据（网格挂单 / 逃顶 / 抄底）")
        data_lines.append("")
        data_lines.append(f"*价格点由代码计算，建议由 AI 解读*")
        data_lines.append("")

        for ts in trade_suggestions:
            name = ts["name"]
            code = ts["code"]
            price = ts["price"]
            chg = f"{ts['change_pct']:+.2f}%" if ts['change_pct'] is not None else "--"
            grid_l = ts["grid_lower"]
            grid_u = ts["grid_upper"]
            step = ts["grid_step"]
            pos = ts["grid_position"]
            suggestion = ts["suggestion"]

            data_lines.append(f"### {name}({code}) — {suggestion}")
            data_lines.append(
                f"现价 **{price:.3f}** ({chg}) | 网格区间 **{grid_l:.3f} ~ {grid_u:.3f}** | "
                f"间距 **{step:.3f}** (半ATR) | 档位 **{pos}%**处"
            )

            # Escape / dip scores
            e_reasons = " + ".join(ts["escape_reasons"]) if ts["escape_reasons"] else "无"
            d_reasons = " + ".join(ts["dip_reasons"]) if ts["dip_reasons"] else "无"
            data_lines.append(f"- 🔴 逃顶信号({ts['escape_score']}分): {e_reasons}")
            data_lines.append(f"- 🟢 抄底信号({ts['dip_score']}分): {d_reasons}")

            # 止盈/止损
            sl = ts.get("stop_loss", 0)
            tp = ts.get("take_profit", 0)
            sl_reason = ts.get("stop_loss_reason", "")
            tp_reason = ts.get("take_profit_reason", "")
            if sl > 0:
                data_lines.append(f"- 🛑 止损: **{sl:.3f}**（{sl_reason}）")
            if tp > 0:
                data_lines.append(f"- 🎯 止盈: **{tp:.3f}**（{tp_reason}）")
            # 仓位建议
            pos_pct = ts.get("position_pct", 0)
            pos_reason = ts.get("position_reason", "")
            if pos_pct > 0:
                data_lines.append(f"- 📊 仓位建议: **{pos_pct:.0f}%**（{pos_reason}）")

            # Grid levels compact table
            levels = ts["grid_levels"]
            if levels and len(levels) <= 15:
                data_lines.append(f"- 网格挂单位:")
                mid = len(levels) // 2
                half_levels = []
                for i, lv in enumerate(levels):
                    mark = f" **→ {lv['price']:.3f} {lv['tag']}**" if lv["tag"] else f" {lv['price']:.3f}"
                    half_levels.append(mark)
                    if i >= mid and len(half_levels) >= 8:
                        break
                data_lines.append("  " + " |".join(half_levels))
            elif levels:
                # Too many levels, show key ones only
                key_indices = set()
                for i in range(0, len(levels), max(1, len(levels) // 8)):
                    key_indices.add(i)
                # Add current price level
                for i, lv in enumerate(levels):
                    if lv["tag"]:
                        key_indices.add(i)
                key_levels = [levels[i] for i in sorted(key_indices)]
                data_lines.append(f"- 网格挂单({len(levels)}档，仅显示关键位):")
                data_lines.append("  " + " | ".join(
                    f"{lv['price']:.3f}{' ← ' + lv['tag'] if lv['tag'] else ''}" for lv in key_levels
                ))
            data_lines.append("")

    # 自选标的行情（提前 fetch，供资金流全量展示与建仓机会共用）
    watchlist = config.watch_items
    watch_quotes: list[Quote] = []
    if watchlist:
        _wl_items = [
            WatchItem(name=w.name, code=w.code, market=w.market, type=w.type)
            for w in watchlist
        ]
        watch_quotes = fetch_quotes_rich(_wl_items) or []

    # 主力资金流向（持仓+自选全量）
    from app.miaoxiang import fetch_etf_fund_flow
    etf_flow_map_ev = fetch_etf_fund_flow(config, _get_unique_items(config))
    _held_codes = {q.code for q in quotes}
    fund_quotes_ev = list(quotes) + [q for q in watch_quotes if q.code not in _held_codes]
    fund_md_ev, fund_llm_ev = _format_fund_flow_section(fund_quotes_ev, label="全部标的", etf_flow_map=etf_flow_map_ev)
    if fund_md_ev:
        data_lines.append(f"\n## 十三、💰 主力资金动向（全天）")
        data_lines.append(fund_md_ev)

    # 自选标的建仓机会分析
    entry_suggestions = []  # type: list[dict]
    if watchlist:
        watch_tech = _get_holdings_tech_analysis(
            [Holding(name=w.name, code=w.code, market=w.market, amount=0, cost=0.0)
             for w in watchlist], watch_quotes
        ) if watch_quotes else []
        entry_suggestions = _compute_entry_suggestions(
            watchlist, holdings, watch_quotes, watch_tech, dragon_tiger_summary
        )
        if entry_suggestions:
            data_lines.append(f"\n## 十四、🔍 自选标的建仓机会")
            data_lines.append("")
            data_lines.append("| 标的 | 类型 | 现价 | 涨跌幅 | 建仓评分 | 风险评分 | 建议 | 理想建仓价 | 止损 |")
            data_lines.append("|------|------|------|--------|---------|---------|------|-----------|------|")
            for es in entry_suggestions:
                chg = f"{es['change_pct']:+.2f}%" if es['change_pct'] is not None else "--"
                sl_val = f"{es['stop_loss']:.3f}" if es['stop_loss'] > 0 else "--"
                data_lines.append(
                    f"| {es['name']}({es['code']}) | {es['type']} | {es['price']:.3f} | {chg} | "
                    f"{es['entry_score']}({', '.join(es['entry_reasons']) if es['entry_reasons'] else '--'}) | "
                    f"{es['risk_score']}({', '.join(es['risk_reasons']) if es['risk_reasons'] else '--'}) | "
                    f"{es['label']} | {es['entry_price']:.3f} | {sl_val} |"
                )
            data_lines.append("")

    # 箱体震荡与网格判定（代码判定，覆盖自选+持仓）
    box_md, box_llm = _format_box_grid_section(
        tech_data_evening + (watch_tech if watchlist else [])
    )
    if box_md:
        data_lines.append("\n" + box_md)

    # 个股两融数据（融资融券，逐标的：持仓 + 自选，仅普通 A 股）
    from app.data_fetcher import fetch_stock_margin_detail
    from app.helpers import is_a_share_stock
    stock_margin = fetch_stock_margin_detail()
    margin_targets = []  # [(name, code, StockMarginData)]
    margin_llm = []
    seen_codes = set()
    for h in list(holdings) + [
        Holding(name=w.name, code=w.code, market=w.market, amount=0, cost=0.0)
        for w in watchlist
    ]:
        if h.code in seen_codes:
            continue
        seen_codes.add(h.code)
        if not is_a_share_stock(h.code, h.market):
            continue
        md = stock_margin.get(h.code)
        if md is None:
            continue
        margin_targets.append((h.name or md.name, h.code, md))

    if margin_targets:
        data_lines.append(f"\n## 十五、💰 个股两融（融资融券，T+1 披露）")
        data_lines.append("")
        data_lines.append("| 标的 | 融资余额 | 融资净买入 | 融券余额 | 数据日期 |")
        data_lines.append("|------|---------|-----------|---------|---------|")
        for name, code, md in margin_targets:
            data_lines.append(
                f"| {name}({code}) | {md.financing_balance/1e8:.2f}亿 | "
                f"{md.financing_net_buy/1e8:+.2f}亿 ({md.financing_change_direction}) | "
                f"{md.securities_lending_balance/1e8:.2f}亿 | {md.date} |"
            )
            margin_llm.append(
                f"{name}: 融资余额{md.financing_balance/1e8:.2f}亿,"
                f"净买入{md.financing_net_buy/1e8:+.2f}亿({md.financing_change_direction})"
            )
        data_lines.append("")
        data_lines.append("*注：仅普通 A 股为两融标的，ETF/基金/港股无个股两融数据；融资净买入为最新日相对前一日的余额变化。*")
        data_lines.append("")

    # 妙想增强：标的消息 + 智能选股 + 标的体检 + 评级/事件（持仓+自选全量）
    from app.miaoxiang import (
        fetch_stock_screen_for_report,
        fetch_holdings_news,
        fetch_holdings_fundamental,
        fetch_holdings_events,
    )

    mx_items = _get_unique_items(config)

    mx_holdings_news = fetch_holdings_news(config, mx_items, fund_quotes_ev)
    if mx_holdings_news:
        data_lines.append(f"\n## 十六、📌 标的消息面（妙想逐个检索）")
        data_lines.append(mx_holdings_news)

    mx_screen = fetch_stock_screen_for_report(config, "今日涨幅超过3%且主力资金净流入的股票")
    if mx_screen:
        data_lines.append(f"\n## 十七、🧠 妙想智能选股（建仓参考）")
        data_lines.append(mx_screen)

    mx_fundamental = fetch_holdings_fundamental(config, mx_items)
    if mx_fundamental:
        data_lines.append(f"\n## 十八、📊 标的体检（资金面+筹码+基本面）")
        data_lines.append(mx_fundamental)

    mx_events = fetch_holdings_events(config, mx_items)
    if mx_events:
        data_lines.append(f"\n## 十九、🚨 标的评级与事件监控")
        data_lines.append(mx_events)

    data_section = "\n".join(data_lines) if data_lines else "暂无数据"

    # 3. Build LLM prompt (compact)
    llm_lines = [
        f"今日收盘。时间是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，"
        f"请生成一份围绕个人持仓的晚报。"
    ]

    # 妙想消息面注入 LLM
    if mx_holdings_news:
        llm_lines.append("\n[标的消息面（妙想）]")
        llm_lines.append(mx_holdings_news[:2500])
    if mx_screen:
        llm_lines.append("\n[妙想智能选股]")
        llm_lines.append(mx_screen[:1000])
    if mx_fundamental:
        llm_lines.append("\n[标的体检（资金面+筹码+基本面）]")
        llm_lines.append(mx_fundamental[:2500])
    if mx_events:
        llm_lines.append("\n[标的评级与事件（减持/增持/回购/解禁/评级）]")
        llm_lines.append(mx_events[:2000])

    if mx_day_news:
        llm_lines.append("\n[盘中快讯（妙想）]")
        llm_lines.append(mx_day_news[:1500])
    elif day_news:
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
            vr_v = t.get("volume_ratio")
            if vr_v is not None and vr_v > 0:
                parts.append(f"量比{vr_v:.1f}")
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

    if fund_llm_ev:
        llm_lines.append(f"\n{fund_llm_ev}")

    if margin_llm:
        llm_lines.append("\n[💰 个股两融（融资融券，杠杆资金动向）]")
        llm_lines.append("  " + "; ".join(margin_llm))

    if vol_llm:
        llm_lines.append(f"\n{vol_llm}")

    if intraday_llm:
        llm_lines.append(f"\n[盘中复盘] {intraday_llm}")

    if gap_bo_llm:
        llm_lines.append(f"\n{gap_bo_llm}")

    if key_level_ev_llm:
        llm_lines.append(f"\n[关键位动态行为（全天收盘）]")
        llm_lines.append(key_level_ev_llm)

    # 4.5 交易辅助数据（代码预计算，LLM 解读）
    if trade_suggestions:
        llm_lines.append("\n[🎯 交易辅助数据（代码预计算，请据此给出操作建议）]")
        for ts in trade_suggestions:
            llm_lines.append(
                f"  {ts['name']}({ts['code']}): "
                f"现价{ts['price']:.3f} | "
                f"网格{ts['grid_lower']:.3f}~{ts['grid_upper']:.3f}(间距{ts['grid_step']:.3f}) | "
                f"当前在{ts['grid_position']}%位置"
            )
            llm_lines.append(
                f"    逃顶({ts['escape_score']}分): {', '.join(ts['escape_reasons']) if ts['escape_reasons'] else '无信号'}"
            )
            llm_lines.append(
                f"    抄底({ts['dip_score']}分): {', '.join(ts['dip_reasons']) if ts['dip_reasons'] else '无信号'}"
            )
            llm_lines.append(f"    综合: {ts['suggestion']}")
            sl = ts.get("stop_loss", 0)
            tp = ts.get("take_profit", 0)
            if sl > 0:
                llm_lines.append(f"    止损: {sl:.3f}（{ts.get('stop_loss_reason', '')}）")
            if tp > 0:
                llm_lines.append(f"    止盈: {tp:.3f}（{ts.get('take_profit_reason', '')}）")
            pp = ts.get("position_pct", 0)
            if pp > 0:
                llm_lines.append(f"    建议仓位: {pp:.0f}%（{ts.get('position_reason', '')}）")

    # 建仓机会（自选标的）
    if entry_suggestions:
        llm_lines.append("\n[🔍 自选标的建仓机会（代码预计算）]")
        for es in entry_suggestions[:8]:
            llm_lines.append(
                f"  {es['name']}({es['code']}): "
                f"建仓{es['entry_score']}分/{' '.join(es['entry_reasons']) if es['entry_reasons'] else '--'} | "
                f"风险{es['risk_score']}分/{' '.join(es['risk_reasons']) if es['risk_reasons'] else '--'} | "
                f"→ {es['label']} | 理想建仓价{es['entry_price']:.3f}"
            )

    # 大盘及行业板块
    if sector_llm:
        llm_lines.append("\n[📈 大盘及行业板块]")
        llm_lines.append(sector_llm)

    # 多信号评分
    if score_llm:
        llm_lines.append("\n[📊 多信号评分] " + score_llm)

    # 附近关键位（紧凑格式）
    nearby_parts = []
    for t in tech_data_evening:
        sups = t.get("nearby_supports", "")
        ress = t.get("nearby_resistances", "")
        if sups or ress:
            nearby_parts.append(f"{t['name']}: 支撑[{sups}] 压力[{ress}]")
    if nearby_parts:
        llm_lines.append("\n[📌 附近关键位] " + "; ".join(nearby_parts[:8]))

    if box_llm:
        llm_lines.append("\n[📦 箱体与网格（代码预计算）]")
        llm_lines.append(box_llm)

    llm_lines.append(f"""

请按以下结构生成晚报（约 800 字）：

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
- **资金行为**：主力主导还是散户主导？有无异常放量/缩量？结合[个股两融]的融资净买入判断杠杆资金是否在加/减仓。
- **估值水平**：结合 PE/PB 判断贵贱（标注置信度）
- **核心判断**：一句话结论（如："短期超买，明日有回调需求"）

### 四、🔧 交易操作建议（每只持仓，核心板块）
⚠️ 所有价格点已由代码预计算，你不需要自己算价格，直接基于提供的数据给出建议。

对每只持仓，按以下格式输出：

**{{持仓名}}** [当前建议: 参考上方预计算综合标签]
- 逃顶：是否有逃顶信号？触发什么条件应该减仓？减到多少仓位？置信度[高/中/低]
- 抄底：是否有抄底信号？什么价位可以试探性建仓/加仓？仓位多少？置信度[高/中/低]
- 网格：按照预计算的网格区间和间距，给出买卖挂单建议（买单挂在支撑位下方，卖单挂在压力位上方）
- 止损/止盈：硬止损位（跌破即走），硬止盈位（触及即减仓）
- 基本面/事件：结合[标的体检]的净利润增速/机构持股变化，及[标的评级与事件]的减持/回购/评级变化，判断是否需要因基本面恶化而减仓/清仓，或因增持/回购/评级上调而加仓

### 五、🔍 自选标的建仓机会
根据预计算的建仓评分，分析自选标的的建仓机会。请对评分最高的 2-3 只给出：
- **建仓理由**：为什么现在值得关注？
- **建仓策略**：是一次性建仓还是分批？建议在什么价位区间介入？
- **风控提醒**：主要风险是什么？什么情况下应该放弃建仓计划？

### 六、明日多情景预案

| 情景 | 触发条件 | 应对动作 | 置信度 |
|------|---------|---------|--------|
| 乐观 | 大盘高开+放量 | ... | 中 |
| 基准 | 平开震荡 | ... | 高 |
| 悲观 | 低开+放量下跌 | ... | 中 |

### 七、风控红线
明日每只持仓的硬止损位和硬止盈位（具体价格，可直接引用预计算数据中的支撑/压力位）。

### 八、网格交易建议（逐标的：代码判定 vs 你的判定）
结合[箱体与网格]数据，对每个标的给出你自己的「网格开启/观望/关闭」判定，并说明与代码判定是否一致及理由（一句话）。

要求：必须使用条件格式（if-then），标注置信度。交易建议必须给出具体的仓位比例和触发价格，不写"适当减仓"这种模糊表述。

**推理深度要求**：每条买卖建议必须写明推理链条，格式为：
`结论 → 直接原因(技术指标+具体数值) → 深层逻辑(为什么这个指标此时重要) → 风险提示(什么情况下判断失效)`
示例："建议减仓至20%[高] → RSI=78超买+接近压力位1.35仅2%→ 双重阻力叠加，历史回测该组合回调概率>70% → 若放量突破1.35则止损上移"

---
**置信度标注规则**：对于每一个判断性结论，请在括号中标注你的确定程度。
- [高]：数据充分、指标一致、历史模式明确
- [中]：数据尚可但存在分歧信号
- [低]：数据不足或逻辑链条不完整
**不确定性处理**：如果数据不足以支持判断，请直接输出"数据不足"而非强行给出结论。
**关键位动态解读**：受压回落→上方压力沉重注意减仓；支撑确认→回调可低吸；跌破支撑→注意止损减仓；突破回踩确认→突破有效可适当加仓；位级强度→强级别更可信。""")

    # 4. Call LLM
    llm_content = _call_llm("\n".join(llm_lines), config, role="evening_review", temperature=0.3, max_tokens=8000, timeout=300)
    if not llm_content:
        log.warning("Evening review: LLM generation failed")

    # 5. Build & save
    report = _build_report(data_section, llm_content)
    report_dir = Path(config.report_dir)
    filepath = _save_report(report, "Evening Review", report_dir)

    push_title = f"Evening Review {datetime.now().strftime('%m-%d')}"
    _push_report(push_title, report, config)

    # 缓存收盘数据供次日早报使用
    _save_morning_cache(quotes, holdings, tech_data_evening, sector_boards_ev, major_indices_ev)

    log.info(f"Evening review generated: {filepath}")
    return filepath


# ============================================================
# Weekly Review（周报）
# ============================================================

WEEKLY_WINDOW_DAYS = 5  # 周报统计口径：最近 5 个交易日
WEEKLY_BENCHMARK_CODE = "510500"  # 中证500ETF，作为超额收益基准


def _weekly_return_pct(klines: list) -> float | None:
    """近 5 个交易日涨跌幅（%）"""
    from app.technical import calc_period_returns
    returns = calc_period_returns(klines, [(WEEKLY_WINDOW_DAYS, "近5日")])
    if returns and returns[0].return_pct is not None:
        return returns[0].return_pct
    return None


def _build_weekly_item(item: WatchItem, quote: Quote | None, benchmark_return: float | None) -> dict | None:
    """计算单个标的的周度数据，返回 dict 或 None（K线/行情缺失）"""
    from app.technical import (
        fetch_historical_kline,
        get_technical_summary,
        calc_support_resistance,
        calc_composite_score,
        detect_market_regime,
        MarketRegime,
    )

    klines = fetch_historical_kline(item.code, item.market, days=60)
    if not klines:
        return None

    week_return = _weekly_return_pct(klines)
    excess = None
    if week_return is not None and benchmark_return is not None:
        excess = round(week_return - benchmark_return, 2)

    sr = calc_support_resistance(klines)

    tech = None
    if quote is not None:
        try:
            tech = get_technical_summary(quote, klines)
        except Exception:
            tech = None

    composite = {"score": 0, "label": "", "signals": [], "breakdown": {}}
    regime = MarketRegime()
    if tech is not None and quote is not None:
        try:
            flow_pct = None
            if quote.main_net_inflow and quote.amount and quote.amount > 0:
                flow_pct = quote.main_net_inflow / quote.amount * 100
            composite = calc_composite_score(tech, quote.price or 0, flow_pct=flow_pct)
            regime = detect_market_regime(tech, quote.price or 0, sr.atr)
        except Exception:
            pass

    return {
        "name": item.name,
        "code": item.code,
        "type": item.type,
        "price": quote.price if quote else None,
        "week_return": week_return,
        "excess_return": excess,
        "support": sr.support,
        "resistance": sr.resistance,
        "ma_alignment": tech.ma_alignment if tech else "数据不足",
        "rsi": tech.rsi if tech else None,
        "rsi_signal": tech.rsi_signal if tech else "数据不足",
        "macd_signal": tech.macd_signal if tech else "数据不足",
        "kdj_signal": tech.kdj_signal if tech else "数据不足",
        "obv_signal": tech.obv_signal if tech else "数据不足",
        "composite_score": composite["score"],
        "composite_label": composite["label"],
        "composite_signals": composite.get("signals", []),
        "market_regime": regime.regime,
        "crowd_label": "",
    }


def generate_weekly_review(config: Config) -> Path | None:
    """周报：最近 5 个交易日的持仓 + 自选全量分析

    数据区（Markdown）+ LLM 一次调用生成下周展望，仅落盘 md，不推送微信。
    """
    from concurrent.futures import ThreadPoolExecutor
    from app.data_fetcher import (
        fetch_quotes_rich,
        fetch_global_markets,
        fetch_margin_data,
        fetch_stock_margin_detail,
    )
    from app.helpers import is_a_share_stock
    from app.technical import fetch_historical_kline

    log.info("Generating weekly review...")

    all_items = _get_unique_items(config)
    if not all_items:
        log.warning("Weekly review: 无标的（持仓+自选均为空）")
        return None

    quotes = fetch_quotes_rich(all_items)
    quote_map = {q.code: q for q in quotes}

    index_items = [i for i in all_items if i.type == "指数"]
    tradable_items = [i for i in all_items if i.type != "指数"]

    # 基准：中证500ETF；缺失时回退到第一个可用的指数
    benchmark_item = next((i for i in all_items if i.code == WEEKLY_BENCHMARK_CODE), None)
    benchmark_return = None
    if benchmark_item:
        benchmark_return = _weekly_return_pct(
            fetch_historical_kline(benchmark_item.code, benchmark_item.market, days=60))
    if benchmark_return is None:
        for idx in index_items:
            ik = fetch_historical_kline(idx.code, idx.market, days=60)
            if ik:
                wr = _weekly_return_pct(ik)
                if wr is not None:
                    benchmark_item, benchmark_return = idx, wr
                    break

    # 并行计算各可交易标的周度数据
    def _one(item: WatchItem) -> dict | None:
        return _build_weekly_item(item, quote_map.get(item.code), benchmark_return)

    weekly_data: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_one, tradable_items):
            if r:
                weekly_data.append(r)

    weekly_data.sort(
        key=lambda x: x["week_return"] if x["week_return"] is not None else -999.0,
        reverse=True,
    )

    # ===== 数据区（Markdown）=====
    data_lines: list[str] = []

    # 一、大盘周度复盘
    data_lines.append("## 一、大盘周度复盘")
    data_lines.append("")
    if benchmark_item is not None and benchmark_return is not None:
        data_lines.append(f"- **基准: {benchmark_item.name} 近5日 {benchmark_return:+.2f}%**")
    for idx in index_items:
        ik = fetch_historical_kline(idx.code, idx.market, days=60)
        wr = _weekly_return_pct(ik)
        if wr is not None:
            data_lines.append(f"- {idx.name}({idx.code}): 近5日 {wr:+.2f}%")
    global_data = fetch_global_markets()
    if global_data:
        data_lines.append("\n### 外盘参考")
        for k, v in global_data.items():
            data_lines.append(f"- {k}: {v}")

    # 全市场两融（杠杆资金面，替代已停止披露的北向资金）
    margin_data = fetch_margin_data()
    margin_llm_brief = ""
    if margin_data and margin_data.financing_balance > 0:
        data_lines.append("\n### 全市场两融（杠杆资金面）")
        data_lines.append(f"- 融资余额：{margin_data.financing_balance:.1f}亿")
        data_lines.append(f"- 融资净买入：{margin_data.financing_net_buy:+.1f}亿（{margin_data.financing_change_direction}）")
        data_lines.append(f"- 融券余额：{margin_data.securities_lending_balance:.1f}亿")
        data_lines.append(f"- 两融总余额：{margin_data.total_balance:.1f}亿（数据日期 {margin_data.date}）")
        margin_llm_brief = (
            f"全市场两融：融资余额{margin_data.financing_balance:.0f}亿，"
            f"融资净买入{margin_data.financing_net_buy:+.1f}亿（{margin_data.financing_change_direction}），"
            f"两融总余额{margin_data.total_balance:.0f}亿"
        )

    # 二、周度强弱榜
    data_lines.append("\n## 二、列表标的周度强弱榜（超额 vs 基准）")
    data_lines.append("")
    data_lines.append("| 标的 | 类型 | 近5日 | 超额 | 最新价 | 复合评分 |")
    data_lines.append("|------|------|-------|------|--------|---------|")
    for d in weekly_data:
        wr = f"{d['week_return']:+.2f}%" if d["week_return"] is not None else "--"
        ex = f"{d['excess_return']:+.2f}%" if d["excess_return"] is not None else "--"
        price = f"{d['price']:.3f}" if d["price"] else "--"
        score = str(d["composite_score"]) if d["composite_score"] else "--"
        data_lines.append(f"| {d['name']}({d['code']}) | {d['type']} | {wr} | {ex} | {price} | {score} |")

    # 三、技术面趋势
    data_lines.append("\n## 三、技术面趋势")
    data_lines.append("")
    data_lines.append("| 标的 | 均线 | RSI | MACD | KDJ | OBV | 支撑 | 压力 |")
    data_lines.append("|------|------|-----|------|-----|-----|------|------|")
    for d in weekly_data:
        sup = f"{d['support']:.3f}" if d["support"] else "--"
        res = f"{d['resistance']:.3f}" if d["resistance"] else "--"
        rsi = f"{d['rsi']:.1f}({d['rsi_signal']})" if d["rsi"] is not None else "--"
        data_lines.append(
            f"| {d['name']} | {d['ma_alignment']} | {rsi} | {d['macd_signal']} "
            f"| {d['kdj_signal']} | {d['obv_signal']} | {sup} | {res} |")

    # 四、个股两融（杠杆资金，仅普通 A 股；ETF/指数/基金/港股无个股两融明细）
    stock_margin = fetch_stock_margin_detail()
    margin_targets = []  # [(name, code, StockMarginData)]
    margin_llm_lines = []
    seen_codes = set()
    for item in all_items:
        if item.code in seen_codes:
            continue
        seen_codes.add(item.code)
        # 指数无个股两融，且上证指数 000001 与深市个股号段冲突，必须先排除
        if item.type == "指数":
            continue
        # 仅普通 A 股股票可能有两融；ETF/基金/港股由号段排除
        if not is_a_share_stock(item.code, item.market):
            continue
        md = stock_margin.get(item.code)
        if md is None:
            continue
        name = item.name or md.name
        margin_targets.append((name, item.code, md))
        margin_llm_lines.append(
            f"{name}({item.code}): 融资余额{md.financing_balance/1e8:.2f}亿，"
            f"融资净买入{md.financing_net_buy/1e8:+.2f}亿（{md.financing_change_direction}）"
        )

    if margin_targets:
        data_lines.append("\n## 四、个股两融（杠杆资金，仅普通 A 股）")
        data_lines.append("")
        data_lines.append("| 标的 | 融资余额 | 融资净买入 | 融券余额 | 数据日期 |")
        data_lines.append("|------|---------|-----------|---------|---------|")
        for name, code, md in margin_targets:
            data_lines.append(
                f"| {name}({code}) | {md.financing_balance/1e8:.2f}亿 | "
                f"{md.financing_net_buy/1e8:+.2f}亿（{md.financing_change_direction}） | "
                f"{md.securities_lending_balance/1e8:.2f}亿 | {md.date} |"
            )
        data_lines.append("")
        data_lines.append("*注：仅普通 A 股为两融标的，ETF/指数/基金/港股无个股两融数据；融资净买入为最新日相对前一日的余额变化（T+1 披露）。*")

    # 五、标的体检（妙想：资金面+筹码+基本面）
    mx_fundamental = ""
    try:
        from app.miaoxiang import fetch_holdings_fundamental
        mx_fundamental = fetch_holdings_fundamental(config, all_items)
    except Exception:
        mx_fundamental = ""
    if mx_fundamental:
        data_lines.append("\n## 五、标的体检（资金面+筹码+基本面）")
        data_lines.append(mx_fundamental)

    # 六、消息面
    data_lines.append("\n## 六、消息面/本周事件")
    data_lines.append("")
    try:
        from app.miaoxiang import fetch_data_for_report
        mx_events = fetch_data_for_report(config, "本周A股重要政策 板块热点 利好利空")
        if mx_events:
            data_lines.append(mx_events)
        else:
            data_lines.append("（妙想消息面数据暂不可用）")
    except Exception:
        data_lines.append("（消息面数据获取失败）")

    data_section = "\n".join(data_lines)

    # ===== LLM prompt（一次调用，覆盖所有标的）=====
    llm_lines: list[str] = [
        f"请生成一份周度复盘报告（最近 5 个交易日，截至 {datetime.now().strftime('%Y-%m-%d')}）。"
    ]
    if benchmark_item is not None and benchmark_return is not None:
        llm_lines.append(f"\n[基准] {benchmark_item.name} 近5日 {benchmark_return:+.2f}%")
    if margin_llm_brief:
        llm_lines.append(f"\n[全市场两融（杠杆资金面）] {margin_llm_brief}")
    llm_lines.append("\n[标的周度数据（每个都要分析）]")
    for d in weekly_data:
        wr = f"{d['week_return']:+.2f}%" if d["week_return"] is not None else "--"
        ex = f"{d['excess_return']:+.2f}%" if d["excess_return"] is not None else "--"
        sup = f"{d['support']:.3f}" if d["support"] else "--"
        res = f"{d['resistance']:.3f}" if d["resistance"] else "--"
        llm_lines.append(
            f"  {d['name']}({d['code']})[{d['type']}]: 近5日{wr} 超额{ex} "
            f"评分{d['composite_score']} 均线{d['ma_alignment']} RSI{d['rsi_signal']} "
            f"MACD{d['macd_signal']} 支撑{sup} 压力{res}")
    if margin_llm_lines:
        llm_lines.append("\n[个股两融（杠杆资金，仅普通 A 股）]")
        llm_lines.append("  " + "；".join(margin_llm_lines))
    if mx_fundamental:
        llm_lines.append("\n[标的体检（资金面+筹码+基本面，妙想）]")
        llm_lines.append(mx_fundamental[:2500])
    llm_lines.append(f"""

请按以下结构输出周报分析（Markdown，关键判断标注置信度）：

### 一、本周市场综述
- 大盘（三大指数 + 基准）本周走势定性，与上周相比的变化方向
- 列表标的整体涨跌分布（几只上涨/几只下跌，领涨领跌板块）
- 结合全市场两融（融资余额/融资净买入）判断市场杠杆资金情绪

### 二、标的强弱点评
- 对每个标的逐一给出 1-2 句点评：本周表现、相对基准强弱、技术面趋势、资金面态度
- 重点标注：逆势流入（主力买、价格跌/滞涨）与逢高流出（主力卖、价格涨）的背离信号
- 结合个股两融的融资净买入方向，判断杠杆资金对普通 A 股是否在加/减仓

### 三、行业/风格轮动
- 宽基 / 行业 ETF / 港股 ETF 三类本周谁强谁弱
- 本周主线板块与退潮板块

### 四、下周展望
- 下周最值得关注的风险点（1-2 个）
- 重点关注的标的（2-4 个）及其关键支撑/压力位
- 操作建议（仓位/加减仓方向，避免给出具体买卖指令）

要求：每个判断性结论标注置信度 [高/中/低]；数据不足时如实说"数据不足"。""")

    llm_content = _call_llm("\n".join(llm_lines), config, role="analyst", temperature=0.4, max_tokens=3500)
    if not llm_content:
        log.warning("Weekly review: LLM generation failed")

    report = _build_report(data_section, llm_content)

    # 落盘（不推送微信），同日重复生成覆盖旧文件
    report_dir = Path(config.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    filepath = report_dir / f"Weekly_{datetime.now().strftime('%Y-%m-%d')}.md"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# 周度复盘报告\n\n")
        f.write(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("---\n\n")
        f.write(report)

    log.info(f"Weekly review generated: {filepath}")
    return filepath


def _save_morning_cache(
    quotes: list[Quote],
    holdings: list[Holding],
    tech_data: list[dict],
    sector_boards: list,
    major_indices: list[Quote],
) -> None:
    """保存次日早报所需数据到缓存文件"""
    from pathlib import Path
    import json
    cache_path = Path(__file__).resolve().parent.parent / "state" / "morning_cache.json"
    try:
        cache_dir = cache_path.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "_date": datetime.now().strftime("%Y-%m-%d"),
            "_time": datetime.now().strftime("%H:%M"),
            "holdings": [],
            "fund_flow": [],
            "scores": [],
            "sectors": [],
            "indices": [],
        }
        # 持仓收盘数据 + 资金流
        for q in quotes:
            entry = {
                "code": q.code, "name": q.name, "price": q.price, "change_pct": q.change_pct,
            }
            flow_entry = None
            if q.main_net_inflow is not None and q.amount and q.amount > 0:
                fp = round(q.main_net_inflow / q.amount * 100, 1)
                entry["flow_pct"] = fp
                ff = q.fund_flow
                label = ff.flow_structure if ff else ""
                entry["flow_label"] = label
                flow_entry = {"code": q.code, "name": q.name, "change_pct": q.change_pct, "flow_pct": fp, "flow_label": label}
            if flow_entry:
                data["fund_flow"].append(flow_entry)
            data["holdings"].append(entry)
        # 技术评分
        for t in tech_data:
            data["scores"].append({
                "code": t["code"], "name": t["name"],
                "score": t.get("composite_score", 0),
                "label": t.get("composite_label", ""),
                "regime": t.get("market_regime", ""),
            })
        # 板块
        for sb in sector_boards[:10]:
            data["sectors"].append({"name": sb.name, "change_pct": sb.change_pct})
        # 指数
        for idx in major_indices[:7]:
            data["indices"].append({"name": idx.name, "price": idx.price, "change_pct": idx.change_pct})
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
        log.debug(f"早报缓存已保存: {cache_path}")
    except Exception as e:
        log.debug(f"早报缓存保存失败: {e}")
