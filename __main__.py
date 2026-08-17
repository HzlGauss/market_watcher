#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path

# LLM 调用冷却（模块级，跨 scan 持久）
_last_llm_call_time: float = 0.0
LLM_COOLDOWN: int = 1800  # 30 分钟最小间隔

from app.config import Config
from app.data_fetcher import NorthFlowFetcher
from app.reporter import generate_morning_brief, generate_midday_review, generate_evening_review
from app.fund_analyzer import analyze_funds
from app.broker_api import auto_update_holdings
from app.presenter import Color
from app.utils import log, ensure_dirs, load_env
from app.helpers import is_trading_time
from app.data_pool import SharedDataPool
from app.data_fetcher_thread import DataFetcherThread
from app.t0_monitor import T0MonitorThread
from app.models import WatchItem

# Path definitions
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_PATH = STATE_DIR / "market_state.json"
BRIEF_DIR = BASE_DIR / "monitoring_briefs"

CONFIG_PATH = BASE_DIR / "watchlist_config.json"
REPORT_DIR = BASE_DIR / "investment_reports"

# Logging setup
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
# Set app-level logger to INFO for user-facing messages
for _mod in ["__main__", "app"]:
    logging.getLogger(_mod).setLevel(logging.INFO)

log = logging.getLogger(__name__)


def _print_banner(config: Config) -> None:
    """Print system status banner"""
    bt = config.thresholds
    llm_ok = config.llm_enabled and config.deepseek_key
    push_ok = config.push_enabled and config.sct_sendkey

    print(f"""
{Color.BOLD}{Color.CYAN}======================================================={Color.RESET}
      Stock Market Monitoring Radar v2
      Standalone Version - Modular Refactor
{Color.BOLD}{Color.CYAN}======================================================={Color.RESET}
    """)
    log.info(f"Watch items: {len(config.watch_items)}")
    log.info(f"Thresholds: Up>{bt.get('涨幅预警',4)}% Down<{bt.get('跌幅预警',-3)}%")
    log.info(f"AI Analysis:  {'OK' if llm_ok else 'OFF'}  |  Push: {'OK' if push_ok else 'OFF'}")
    log.info(f"Reports: {BRIEF_DIR}")
    print()


def _show_menu() -> str:
    """Show menu, return user choice"""
    print(f"\n{Color.BOLD}Select Mode:{Color.RESET}")
    print(f"  {Color.CYAN}1{Color.RESET}. Morning Brief")
    print(f"  {Color.CYAN}2{Color.RESET}. Midday Review")
    print(f"  {Color.CYAN}3{Color.RESET}. Evening Review")
    print(f"  {Color.CYAN}4{Color.RESET}. Monitor Mode (15 min interval)")
    print(f"  {Color.CYAN}5{Color.RESET}. Fund Analysis (Rating + Style + Manager Review)")
    print(f"  {Color.CYAN}6{Color.RESET}. Update Holdings from Broker (东方财富)")
    print(f"  {Color.CYAN}7{Color.RESET}. Query Period Returns (近期走势)")
    print(f"  {Color.CYAN}8{Color.RESET}. ETF Bottom Reversal Screen (ETF底部反转)")
    print(f"  {Color.CYAN}9{Color.RESET}. Stock Bottom Reversal Screen (A股底部反转)")
    print(f"  {Color.CYAN}D{Color.RESET}. Dragon Tiger Deep Analysis (龙虎榜深度分析)")
    print(f"  {Color.CYAN}M{Color.RESET}. Miaoxiang AI (东方财富妙想)")
    print(f"  {Color.CYAN}0{Color.RESET}. Exit")
    print()

    while True:
        try:
            choice = input(f" Enter option [0-9/D/M]: ").strip().upper()
            if choice in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "D", "M"):
                return choice
            print(f"{Color.YELLOW}  Please enter 0-9, D or M{Color.RESET}")
        except (EOFError, KeyboardInterrupt):
            return "0"


def _gen_report(report_type: str, config: Config) -> None:
    """Generate single report"""
    if report_type == "Morning Brief":
        log.info("Generating morning brief...")
        generate_morning_brief(config)
    elif report_type == "Midday Review":
        log.info("Generating midday review...")
        generate_midday_review(config)
    elif report_type == "Evening Review":
        log.info("Generating evening review...")
        generate_evening_review(config)


# Dragon Tiger cache (daily refresh)
_dt_cache: dict = {"date": "", "analyses": None}


def _check_dragon_tiger_holdings(config, holding_codes: set) -> None:
    """Check if any holdings appear in today's dragon tiger list.

    Runs once per day (cached). If holdings are found, performs deep
    seat-level analysis and prints risk/signal alerts inline.
    """
    from datetime import date as _date
    today = _date.today().isoformat()

    global _dt_cache
    if _dt_cache["date"] == today and _dt_cache["analyses"] is not None:
        return  # Already fetched today

    if not holding_codes:
        return

    try:
        from app.dragon_tiger import fetch_dragon_tiger_list
        from app.dragon_seat import (
            analyze_dragon_tiger_seats, check_holdings_dragon_tiger,
            detect_consecutive_listings,
        )

        records = fetch_dragon_tiger_list(max_count=50)
        if not records:
            _dt_cache = {"date": today, "analyses": None}
            log.debug("龙虎榜持仓检测: 今日数据尚未发布，跳过")
            return

        # Consecutive listing detection
        consecutive = detect_consecutive_listings(records)
        if consecutive:
            print(f"\n{Color.BOLD}{Color.CYAN}🔄 连续上榜 ({len(consecutive)}只){Color.RESET}")
            for item in consecutive[:5]:
                relay_note = f" {Color.YELLOW}{item['relay_note']}{Color.RESET}" if item['relay_note'] else ""
                today_net = sum(r.net_buy for r in records if r.code == item['code'])
                prev_str = " → ".join([f"{e['date'][-5:]}({e['net_buy']/1e4:+.0f}万)" for e in item['prev_entries']])
                print(f"  {Color.BOLD}{item['name']}({item['code']}){Color.RESET} "
                      f"连续{item['consecutive_days']}天 | {prev_str} → 今日({today_net/1e4:+.0f}万){relay_note}")
            print()

        # Deep analysis on top N by volume
        analyses = analyze_dragon_tiger_seats(records, max_seat_fetch=30)
        _dt_cache = {"date": today, "analyses": analyses}

        # Check holdings
        h_alerts = check_holdings_dragon_tiger(analyses, holding_codes)
        if h_alerts:
            print(f"\n{Color.BOLD}{Color.RED}🐉 龙虎榜持仓预警 ({len(h_alerts)}只){Color.RESET}")
            for a in h_alerts:
                quality = a['quality']
                q_color = Color.RED if quality == '高质' else (Color.GREEN if quality == '风险' else Color.YELLOW)
                chg = f"{a['change_pct']:+.1f}%" if a['change_pct'] else ""
                inst_str = f"{a['institution_net']/1e4:+.0f}万" if a['institution_net'] else "--"
                print(f"  {q_color}[{quality}]{Color.RESET} {Color.BOLD}{a['name']}({a['code']}){Color.RESET}"
                      f" {chg} | 机构{inst_str}" + (
                      f" | 游资{a['hot_money_net']/1e4:+.0f}万" if a['hot_money_net'] else ""))
                if a.get('risk_flags'):
                    flags = ','.join(a['risk_flags'])
                    print(f"    {Color.GREEN}⚠️ {flags}{Color.RESET}")
                if a.get('suggestions'):
                    for sug in a['suggestions']:
                        print(f"    {sug}")
                if a.get('top_seats'):
                    print(f"    {Color.DIM}{' | '.join(a['top_seats'][:3])}{Color.RESET}")
            print()

            # Also push via desktop notification
            try:
                from app.notifier import send_desktop_notification
                risky = [a for a in h_alerts if a.get('risk_flags')]
                quality = [a for a in h_alerts if a['quality'] == '高质']
                title = f"🐉 龙虎榜: {len(quality)}只机构买入, {len(risky)}只风险"
                msg = " | ".join([f"{a['name']}({a['quality']})" for a in h_alerts[:3]])
                send_desktop_notification(title=title, message=msg)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"龙虎榜检测失败: {e}")


def _query_period_returns(config: Config) -> None:
    """查询股票/基金的近期走势"""
    from app.technical import fetch_historical_kline, calc_period_returns
    from app.models import WatchItem
    from app.data_fetcher import fetch_quotes, enrich_quotes_with_flow

    print(f"\n{Color.BOLD}{Color.CYAN}═══ 近期走势查询 ═══{Color.RESET}")
    code = input(f"  请输入股票/基金代码 (如 601899): ").strip()

    if not code:
        print(f"  {Color.YELLOW}已取消{Color.RESET}")
        return

    # 尝试从 holdings/watchlist 中获取市场信息
    market = "SH"  # 默认上海
    name = ""

    for h in config.holdings:
        if h.code == code:
            market = h.market
            name = h.name
            break

    if not name:
        for w in config.watch_items:
            if w.code == code:
                market = w.market
                name = w.name
                break

    # 如果找不到，尝试获取实时行情来得到名称
    if not name:
        items = [WatchItem(name="", code=code, market=market, type="查询")]
        quotes = fetch_quotes(items)
        if quotes:
            name = quotes[0].name
            market = quotes[0].code[:2].upper() in ("SH", "SZ", "HK") and quotes[0].code[:2] or market

    # 获取 K 线数据（需要 60 天才能计算 40 日周期）
    klines = fetch_historical_kline(code, market, days=60)

    if not klines:
        print(f"  {Color.RED}❌ 无法获取 {code} 的K线数据{Color.RESET}")
        return

    # 获取最新价格
    latest_price = klines[-1].close
    latest_date = klines[-1].date

    if name:
        print(f"\n  {Color.BOLD}{name}({code}){Color.RESET}  最新价: {Color.CYAN}{latest_price:.2f}{Color.RESET}  ({latest_date})")
    else:
        print(f"\n  {Color.BOLD}{code}{Color.RESET}  最新价: {Color.CYAN}{latest_price:.2f}{Color.RESET}  ({latest_date})")

    # 计算各周期涨跌幅
    returns = calc_period_returns(klines)

    if not returns:
        print(f"  {Color.YELLOW}历史数据不足，无法计算近期走势{Color.RESET}")
        return

    # 输出结果
    print(f"  {Color.BOLD}📊 近期走势:{Color.RESET}")
    print(f"  {'-' * 60}")

    for ret in returns:
        if ret.return_pct is None:
            continue

        # 根据涨跌选择颜色
        color = Color.GREEN if ret.return_pct > 0 else (Color.RED if ret.return_pct < 0 else Color.YELLOW)
        arrow = "↑" if ret.return_pct > 0 else ("↓" if ret.return_pct < 0 else "—")

        label = f"{ret.label}({ret.days}日)"
        change_str = f"{color}{arrow} {ret.return_pct:+.2f}%{Color.RESET}"

        # 区间高低点
        range_str = ""
        if ret.high_price and ret.low_price:
            range_str = f"  区间: {ret.low_price:.2f} ~ {ret.high_price:.2f}"

        # 起始价格
        price_str = f"({ret.start_price:.2f} → {ret.end_price:.2f})"

        print(f"    {label:12s}  {change_str:15s}  {price_str}{range_str}")

    print(f"  {'-' * 60}")
    print()


def _wait_until_next_slot(interval: int) -> None:
    """Wait until next scan slot"""
    now = datetime.now()
    seconds_to_next = (interval * 60) - (now.minute % interval) * 60 - now.second - 1
    if seconds_to_next < 0:
        seconds_to_next += interval * 60

    for i in range(int(seconds_to_next), 0, -1):
        time.sleep(1)


def _run_once(config: Config, north_fetcher: NorthFlowFetcher, call_llm: bool = True, scan_count: int = 0) -> None:
    """Run one scan cycle"""
    from app.data_fetcher import fetch_quotes
    from app.analyzer import analyze, _load_scan_history, _save_scan_history
    from app.ai_analyzer import analyze as analyze_with_llm
    from app.notifier import push_alert, send_desktop_notification
    from app.presenter import (
        print_quotes_table, print_sentiment, print_alerts,
        print_llm_result, print_tail, save_brief, print_key_levels
    )
    from app.data_fetcher import fetch_quotes, BackgroundDataCache
    from app.technical import fetch_historical_kline, get_technical_summary
    from app.models import ScanRecord, FundScanStatus, TechSnapshot
    from app.models import tech_snapshot_to_summary
    import time

    log.info("Scanning market data (Holdings + Watchlist)...")

    # Get holdings from CSV
    holdings = config.holdings
    # Get watchlist from CSV
    watch_items = config.watch_items

    if not holdings and not watch_items:
        log.warning("No holdings or watchlist items found to monitor")
        return

    # Convert holdings to watch items for fetch_quotes
    from app.models import WatchItem
    monitor_items = []

    # Add holdings with type "持仓"
    for h in holdings:
        monitor_items.append(WatchItem(
            name=h.name,
            code=h.code,
            market=h.market,
            type="持仓"
        ))

    # Add watchlist items (exclude those already in holdings)
    holding_codes = {h.code for h in holdings}
    for item in watch_items:
        if item.code not in holding_codes:
            monitor_items.append(WatchItem(
                name=item.name,
                code=item.code,
                market=item.market,
                type=item.type
            ))

    quotes = fetch_quotes(monitor_items)
    if not quotes:
        log.warning("No quote data received")
        return

    # 从后台缓存获取量比、换手率、资金流向明细（不阻塞主循环）
    if hasattr(_run_once, '_bg_cache') and _run_once._bg_cache.is_fresh():
        for q in quotes:
            cached = _run_once._bg_cache.get_data(q.code)
            if cached:
                if cached.get("volume_ratio") is not None:
                    q.volume_ratio = cached["volume_ratio"]
                if cached.get("turnover_rate") is not None:
                    q.turnover_rate = cached["turnover_rate"]
                if cached.get("main_net_inflow") is not None:
                    q.main_net_inflow = cached["main_net_inflow"]
                if cached.get("fund_flow") is not None:
                    q.fund_flow = cached["fund_flow"]
                if cached.get("bid_volume") is not None:
                    q.bid_volume = cached["bid_volume"]
                if cached.get("ask_volume") is not None:
                    q.ask_volume = cached["ask_volume"]
                if cached.get("bid_ask_ratio") is not None:
                    q.bid_ask_ratio = cached["bid_ask_ratio"]

    # Separate quotes by type for statistics
    holdings_quotes = [q for q in quotes if q.type == "持仓"]
    watchlist_quotes = [q for q in quotes if q.type != "持仓"]

    # Fetch technical indicators for holdings & watchlist items
    tech_summaries: dict[str, "TechnicalSummary"] = {}
    klines_map: dict[str, list] = {}
    quote_map = {q.code: q for q in quotes}
    item_map = {item.code: item for item in monitor_items}

    for i, code in enumerate(quote_map):
        item = item_map.get(code)
        if not item:
            continue
        if i > 0:
            time.sleep(0.5)
        klines = fetch_historical_kline(code, item.market, days=60, scale=60)
        if klines:
            klines_map[code] = klines
            tech_summaries[code] = get_technical_summary(quote_map[code], klines)

    print_quotes_table(quotes)

    # 展示关键价位（支撑/压力位）
    if tech_summaries:
        print_key_levels(tech_summaries, quotes)

    # 加载上一周期成交量，用于量价对比
    prev_state: dict = {}
    if STATE_PATH.exists():
        try:
            prev_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"读取状态文件失败: {e}")

    # 加载扫描历史，构建前一次技术快照
    scan_history = _load_scan_history()
    prev_tech_summaries: dict[str, "TechnicalSummary"] = {}
    if scan_history:
        last_record = scan_history[-1]
        for code, status in last_record.funds_status.items():
            if status.tech_snapshot:
                prev_tech_summaries[code] = tech_snapshot_to_summary(status.tech_snapshot)

    # 展示组合策略信号（盯盘实时）
    from app.strategy import evaluate_all_strategies, calc_macd_dif_series
    all_strategy_signals: list[tuple[str, str, str]] = []  # (name, code, signal_text)
    for code in quote_map:
        quote = quote_map[code]
        klines = klines_map.get(code)
        if not klines or code not in tech_summaries:
            continue
        tech = tech_summaries[code]
        prev_tech = prev_tech_summaries.get(code)
        closes = [k.close for k in klines if k.close is not None]
        dif_vals = calc_macd_dif_series(closes) if closes else None
        signals = evaluate_all_strategies(tech, prev_tech, quote, klines, dif_vals, closes)
        triggering = [s for s in signals if s.is_triggering]
        if triggering:
            for s in triggering:
                all_strategy_signals.append((item_map.get(code, monitor_items[0]).name, code, s.to_alert_text()))
        else:
            # 没有触发信号时显示"无信号"
            all_strategy_signals.append((item_map.get(code, monitor_items[0]).name, code, "⚪ [无信号]"))

    if all_strategy_signals:
        from app.presenter import Color
        print(f"\n{Color.BOLD}{Color.YELLOW}═══ 组合策略信号 ═══{Color.RESET}")
        for name, code, sig_text in all_strategy_signals:
            print(f"  {Color.BOLD}{name}({code}){Color.RESET}  →  {sig_text}")
        print()

    # Analyze all quotes together (with technical signals, history, and market breadth)
    from app.data_fetcher import fetch_market_breadth
    alerts, stats = analyze(
        quotes, prev_state, config, tech_summaries,
        north_data=north_fetcher.fetch(),
        market_breadth=fetch_market_breadth(),
    )
    print_sentiment(stats)

    # 保存当前成交量和资金流，供下一周期对比
    try:
        cur_state = {}
        for q in quotes:
            if q.volume is not None:
                entry = {"volume": q.volume}
                if q.main_net_inflow is not None:
                    entry["main_net_inflow"] = q.main_net_inflow
                    if q.amount and q.amount > 0:
                        entry["flow_pct"] = round(q.main_net_inflow / q.amount * 100, 2)
                cur_state[q.code] = entry
        STATE_PATH.write_text(json.dumps(cur_state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"保存状态文件失败: {e}")

    # 构建本次扫描记录
    funds_status = {}
    quote_map = {q.code: q for q in quotes}
    alert_map = {a.code: a.messages for a in alerts}

    for q in quotes:
        prev_vol = prev_state.get(q.code, {}).get("volume")
        vol_ratio = None
        if prev_vol and prev_vol > 0 and q.volume and q.volume > 0:
            vol_ratio = q.volume / prev_vol

        tech_snapshot = None
        if tech_summaries and q.code in tech_summaries:
            tech_snapshot = tech_summaries[q.code]

        funds_status[q.code] = FundScanStatus(
            price=q.price,
            change_pct=q.change_pct,
            volume=q.volume,
            vol_ratio=vol_ratio,
            alerts=alert_map.get(q.code, []),
            tech_signals=tech_summaries.get(q.code, []).signals if tech_summaries and q.code in tech_summaries else [],
            tech_snapshot=tech_snapshot
        )

    scan_record = ScanRecord(
        scan_id=scan_count,
        time=datetime.now().strftime("%H:%M"),
        timestamp=int(time.time()),
        market_sentiment={
            "score": stats.sentiment.score,
            "label": stats.sentiment.label
        },
        alerts_summary={
            "total_alerts": len(alerts),
            "critical_alerts": stats.alert_count,
            "funds_with_alerts": [a.code for a in alerts]
        },
        funds_status=funds_status,
        llm_analysis=None  # 将在后面填充
    )

    # Print separate statistics
    if holdings_quotes:
        holdings_up = sum(1 for q in holdings_quotes if q.change_pct and q.change_pct > 0)
        holdings_down = sum(1 for q in holdings_quotes if q.change_pct and q.change_pct < 0)
        print(f"{Color.CYAN}💼 持仓统计:{Color.RESET} {len(holdings_quotes)}只 | "
              f"{Color.RED}涨{holdings_up}{Color.RESET} | {Color.GREEN}跌{holdings_down}{Color.RESET}")

    if watchlist_quotes:
        watchlist_up = sum(1 for q in watchlist_quotes if q.change_pct and q.change_pct > 0)
        watchlist_down = sum(1 for q in watchlist_quotes if q.change_pct and q.change_pct < 0)
        print(f"{Color.CYAN}📋 观察统计:{Color.RESET} {len(watchlist_quotes)}只 | "
              f"{Color.RED}涨{watchlist_up}{Color.RESET} | {Color.GREEN}跌{watchlist_down}{Color.RESET}")

    if alerts:
        print_alerts(alerts)

        # 发送桌面通知
        alert_summary = " | ".join([f"{a.name}: {', '.join(a.messages)}" for a in alerts[:3]])
        if len(alerts) > 3:
            alert_summary += f" | ...还有 {len(alerts)-3} 条"
        send_desktop_notification(
            title=f"📈 盯盘提醒 | {stats.sentiment.label} {stats.up}涨{stats.down}跌",
            message=alert_summary
        )

        # 有异动时强制调 LLM（即使奇数轮），无异动时维持隔轮调用
        llm_effective = call_llm
        if not llm_effective and alerts and config.llm_trigger == "仅异动时":
            llm_effective = True
        if llm_effective and config.llm_enabled and config.deepseek_key:
            llm_result = analyze_with_llm(quotes, alerts, stats, config, tech_summaries)
            print_llm_result(llm_result)

            # 保存LLM分析结果到扫描记录
            scan_record.llm_analysis = llm_result

            if config.push_enabled and config.sct_sendkey:
                push_alert(alerts, stats, config, llm_result)
        else:
            if not llm_effective:
                log.info("LLM 跳过（本轮不请求）")
            if config.push_enabled and config.sct_sendkey:
                push_alert(alerts, stats, config)
    else:
        log.info("No alerts triggered")

    # 保存扫描历史
    scan_history.append(scan_record)
    _save_scan_history(scan_history)

    # 保存当前成交量和资金流状态，供下一轮对比（趋势检测用）
    try:
        cur_state = {}
        for q in quotes:
            if q.volume is not None:
                entry: dict = {"volume": q.volume}
                if q.main_net_inflow is not None:
                    entry["main_net_inflow"] = q.main_net_inflow
                    if q.amount and q.amount > 0:
                        entry["flow_pct"] = round(q.main_net_inflow / q.amount * 100, 2)
                cur_state[q.code] = entry
        STATE_PATH.write_text(json.dumps(cur_state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    save_brief(quotes, alerts, stats, BRIEF_DIR)
    print_tail(config.scan_interval)


def _run_once_new(config: Config, north_fetcher: NorthFlowFetcher, data_pool,
                  call_llm: bool = False, scan_count: int = 0) -> None:
    """Run one scan cycle using shared data pool"""
    global _last_llm_call_time
    from app.analyzer import analyze, _load_scan_history, _save_scan_history
    from app.ai_analyzer import analyze as analyze_with_llm
    from app.notifier import push_alert, send_desktop_notification
    from app.presenter import (
        print_quotes_table, print_sentiment, print_alerts,
        print_llm_result, print_tail, save_brief, print_key_levels, Color
    )
    from app.technical import get_technical_summary, TechnicalSummary
    from app.models import ScanRecord, FundScanStatus, TechSnapshot, Alert

    log.info("Scanning market data (from shared pool)...")

    # Get data from shared pool
    quotes = list(data_pool.get_all_quotes().values())
    if not quotes:
        log.warning("No quote data available in data pool")
        return

    # Apply background cache data (fund flow, volume ratio, turnover rate)
    bg = getattr(_run_once_new, '_bg_cache', None)
    if bg and bg.is_fresh():
        for q in quotes:
            cached = bg.get_data(q.code)
            if cached:
                if cached.get("volume_ratio") is not None:
                    q.volume_ratio = cached["volume_ratio"]
                if cached.get("turnover_rate") is not None:
                    q.turnover_rate = cached["turnover_rate"]
                if cached.get("main_net_inflow") is not None:
                    q.main_net_inflow = cached["main_net_inflow"]
                if cached.get("fund_flow") is not None:
                    q.fund_flow = cached["fund_flow"]
                if cached.get("bid_volume") is not None:
                    q.bid_volume = cached["bid_volume"]
                if cached.get("ask_volume") is not None:
                    q.ask_volume = cached["ask_volume"]
                if cached.get("bid_ask_ratio") is not None:
                    q.bid_ask_ratio = cached["bid_ask_ratio"]

    # Get K-line data from shared pool
    klines_map = {}
    for q in quotes:
        klines = data_pool.get_klines(q.code)
        if klines:
            klines_map[q.code] = klines

    # Build holdings code set for dragon tiger check
    holding_codes = {h.code for h in config.holdings}

    # Enrich quotes with industry classification
    from app.data_fetcher import enrich_quotes_with_industry, fetch_sector_boards
    enrich_quotes_with_industry(quotes)
    sector_boards = fetch_sector_boards()  # 5分钟缓存

    # Separate quotes by type for statistics
    holdings_quotes = [q for q in quotes if q.type == "持仓"]
    watchlist_quotes = [q for q in quotes if q.type != "持仓"]

    # Data freshness diagnostic
    pool_age = time.time() - data_pool.last_update if data_pool.last_update > 0 else 999
    if pool_age > 60:
        log.warning(f"数据池最后更新于 {pool_age:.0f} 秒前，行情可能已过期")

    # Calculate technical summaries
    tech_summaries: dict[str, TechnicalSummary] = {}
    quote_map = {q.code: q for q in quotes}

    for code in quote_map:
        klines = klines_map.get(code)
        if klines:
            tech_summaries[code] = get_technical_summary(quote_map[code], klines)

    print_quotes_table(quotes)

    # Print holdings-specific statistics + portfolio P&L
    if holdings_quotes:
        valid_holdings = [q for q in holdings_quotes if q.price is not None]
        up_h = sum(1 for q in holdings_quotes if q.change_pct and q.change_pct > 0)
        down_h = sum(1 for q in holdings_quotes if q.change_pct and q.change_pct < 0)
        flat_h = len(holdings_quotes) - up_h - down_h

        print(f"{Color.CYAN}💼 持仓:{Color.RESET} {len(holdings_quotes)}只 | "
              f"{Color.RED}涨{up_h}{Color.RESET}/{Color.GREEN}跌{down_h}{Color.RESET}"
              f"{'/' + str(flat_h) + '平' if flat_h else ''} | "
              f"有效行情 {len(valid_holdings)}/{len(holdings_quotes)}只")
        if len(valid_holdings) < len(holdings_quotes):
            missing = [q.name for q in holdings_quotes if q.price is None]
            print(f"  {Color.YELLOW}⚠️ 缺少行情数据: {', '.join(missing)}{Color.RESET}")

        # Portfolio P&L (仅计算 amount>0 且 cost>0 的持仓)
        total_cost = 0.0
        total_value = 0.0
        skipped_pnl: list[str] = []
        pnl_details: list[tuple[str, float, float]] = []  # (name, pnl_amt, pnl_pct)
        holding_map = {h.code: h for h in config.holdings}
        for q in valid_holdings:
            h = holding_map.get(q.code)
            if h and h.cost > 0 and h.amount > 0:
                cost_val = h.cost * h.amount
                value_val = q.price * h.amount
                total_cost += cost_val
                total_value += value_val
                pnl = value_val - cost_val
                pnl_pct = (q.price - h.cost) / h.cost * 100
                pnl_details.append((q.name, pnl, pnl_pct))
            elif h:
                skipped_pnl.append(h.name)

        if total_cost > 0:
            total_pnl = total_value - total_cost
            total_pnl_pct = total_pnl / total_cost * 100
            pnl_color = Color.RED if total_pnl > 0 else (Color.GREEN if total_pnl < 0 else "")
            suffix = f" ({len(valid_holdings) - len(pnl_details)}只缺数据未计入)" if skipped_pnl else ""
            print(f"  {Color.CYAN}💰 组合:{Color.RESET} 成本 {total_cost:,.0f} → 市值 {total_value:,.0f}"
                  f" | 盈亏 {pnl_color}{total_pnl:+,.0f} ({total_pnl_pct:+.2f}%){Color.RESET}{Color.DIM}{suffix}{Color.RESET}")

            # Top movers
            if pnl_details:
                top_winners = sorted([p for p in pnl_details if p[1] > 0], key=lambda x: -x[1])[:2]
                top_losers = sorted([p for p in pnl_details if p[1] < 0], key=lambda x: x[1])[:2]
                mover_parts = []
                for name, amt, pct in top_winners:
                    mover_parts.append(f"{Color.RED}↑{name} +{amt:.0f}({pct:+.1f}%){Color.RESET}")
                for name, amt, pct in top_losers:
                    mover_parts.append(f"{Color.GREEN}↓{name} {amt:.0f}({pct:+.1f}%){Color.RESET}")
                if mover_parts:
                    print(f"  {' | '.join(mover_parts)}")

    if watchlist_quotes:
        up_w = sum(1 for q in watchlist_quotes if q.change_pct and q.change_pct > 0)
        down_w = sum(1 for q in watchlist_quotes if q.change_pct and q.change_pct < 0)
        print(f"{Color.CYAN}📋 观察:{Color.RESET} {len(watchlist_quotes)}只 | "
              f"{Color.RED}涨{up_w}{Color.RESET}/{Color.GREEN}跌{down_w}{Color.RESET}")

    # Show key levels (support/resistance)
    if tech_summaries:
        print_key_levels(tech_summaries, quotes)

    # Load previous state for volume comparison
    prev_state: dict = {}
    if STATE_PATH.exists():
        try:
            prev_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"读取状态文件失败: {e}")

    # Load scan history
    scan_history = _load_scan_history()
    prev_tech_summaries: dict[str, TechnicalSummary] = {}
    if scan_history:
        last_record = scan_history[-1]
        for code, status in last_record.funds_status.items():
            if status.tech_snapshot:
                from app.models import tech_snapshot_to_summary
                prev_tech_summaries[code] = tech_snapshot_to_summary(status.tech_snapshot)

    # Evaluate strategy signals
    from app.strategy import evaluate_all_strategies, calc_macd_dif_series
    from app.models import Alert as StrategyAlert
    all_strategy_signals: list[tuple[str, str, str]] = []
    strategy_alerts: list[StrategyAlert] = []
    for code in quote_map:
        quote = quote_map[code]
        klines = klines_map.get(code)
        if not klines:
            continue

        try:
            dif_vals = calc_macd_dif_series([k.close for k in klines])
            closes = [k.close for k in klines]
            prev_tech = prev_tech_summaries.get(code)
            tech = tech_summaries.get(code)

            if tech:
                signals = evaluate_all_strategies(tech, prev_tech, quote, klines, dif_vals, closes)
                # Separate triggering vs non-triggering
                triggering = [s for s in signals if s.is_triggering]
                if triggering:
                    # Build alert text and strategy-alert for pipeline
                    messages = []
                    for s in triggering:
                        alert_text = s.to_alert_text()
                        if alert_text:
                            all_strategy_signals.append((quote.name, code, alert_text))
                            messages.append(alert_text)
                    if messages:
                        strategy_alerts.append(StrategyAlert(
                            code=quote.code, name=quote.name, messages=messages))
                else:
                    all_strategy_signals.append((quote.name, code, "⚪ [无信号]"))
        except Exception as e:
            log.warning(f"Strategy evaluation failed for {code}: {e}")

    # Print strategy signals to console
    if all_strategy_signals:
        from app.presenter import Color
        print(f"\n{Color.BOLD}{Color.YELLOW}═══ 组合策略信号 ═══{Color.RESET}")
        for name, code, sig_text in all_strategy_signals:
            print(f"  {Color.BOLD}{name}({code}){Color.RESET}  →  {sig_text}")
        print()

    # 涨速排名（与前次扫描对比）
    _prev_prices = getattr(_run_once_new, '_prev_prices', {})
    if _prev_prices:
        velocity_items = []
        for q in quotes:
            prev_p = _prev_prices.get(q.code)
            if prev_p and q.price and prev_p > 0 and q.price != prev_p:
                vel = (q.price - prev_p) / prev_p * 100
                if abs(vel) >= 0.3:  # 3分钟涨速超过0.3%才显示
                    velocity_items.append((q.name, q.code, vel))
        if velocity_items:
            velocity_items.sort(key=lambda x: x[2], reverse=True)
            from app.presenter import Color
            print(f"{Color.BOLD}{Color.PURPLE}═══ 涨速排名(3min) ═══{Color.RESET}")
            top = velocity_items[:5]
            bot = velocity_items[-5:]
            top_str = "  ".join(f"{Color.RED}{n}({c}) {v:+.2f}%{Color.RESET}" for n, c, v in top)
            bot_str = "  ".join(f"{Color.GREEN}{n}({c}) {v:+.2f}%{Color.RESET}" for n, c, v in bot)
            print(f"  ▲ {top_str}")
            print(f"  ▼ {bot_str}")
            print()
    _run_once_new._prev_prices = {q.code: q.price for q in quotes if q.price is not None}

    # Sector analysis display
    if sector_boards:
        from app.analyzer import analyze_sector_context
        sector_ctx = analyze_sector_context(quotes, sector_boards)
        from app.presenter import Color
        print(f"{Color.BOLD}{Color.CYAN}═══ 行业板块 ═══{Color.RESET}")
        # Top 3 / Bottom 3
        top3 = sector_ctx["top_sectors"][:3]
        bot3 = sector_ctx["bottom_sectors"][:3]
        print(f"  {Color.GREEN}▲{Color.RESET} ", end="")
        for name, chg in top3:
            print(f"{name}{chg:+.1f}%  ", end="")
        print(f"\n  {Color.RED}▼{Color.RESET} ", end="")
        for name, chg in bot3:
            print(f"{name}{chg:+.1f}%  ", end="")
        print()
        # Per-stock sector context (only show deviating ones)
        deviating = [(q.code, info) for q in quotes
                     if (info := sector_ctx["per_stock"].get(q.code)) and info["label"]
                     and "同步" not in info["label"]]
        if deviating:
            for code, info in deviating[:5]:
                q = next((x for x in quotes if x.code == code), None)
                if q:
                    emoji = "🔥" if info.get("relative_strength", 0) > 0 else "❄️"
                    print(f"  {emoji} {q.name}({q.code}): {info['label']} [{info.get('sector', '')}]")
        print()

    # Fetch market breadth data (cached, 5-min TTL)
    from app.data_fetcher import fetch_market_breadth
    breadth = fetch_market_breadth()

    # Analyze market (all quotes including watchlist for full coverage)
    north_data = north_fetcher.fetch()
    alerts, stats = analyze(quotes, prev_state, config, tech_summaries,
                            north_data=north_data,
                            market_breadth=breadth)

    # 落盘北向资金轨迹（仅当有有效日期，即API成功返回）
    if north_data is not None and getattr(north_data, "date", ""):
        _append_north_flow(north_data)

    # 策略信号已在"组合策略信号"章节单独展示，不再合并进"异动提醒详情"
    # 但告警计数仍计入策略信号，供统计使用
    stats.alert_count += len(strategy_alerts)

    # Print sentiment and alerts（只展示行情/资金/技术异动，不含策略信号）
    print_sentiment(stats)
    print_alerts(alerts)

    # K线形态检测（追加到告警列表）
    from app.technical import detect_candlestick_patterns
    for q in quotes:
        kls = klines_map.get(q.code)
        if not kls:
            continue
        patterns = detect_candlestick_patterns(kls)
        if patterns:
            existing = next((a for a in alerts if a.code == q.code), None)
            if existing:
                for p in patterns:
                    existing.messages.append(p)
            else:
                alerts.append(Alert(code=q.code, name=q.name, messages=list(patterns)))

    # 发送桌面通知
    if alerts:
        alert_summary = " | ".join([f"{a.name}: {', '.join(a.messages)}" for a in alerts[:3]])
        if len(alerts) > 3:
            alert_summary += f" | ...还有 {len(alerts)-3} 条"
        send_desktop_notification(
            title=f"📈 盯盘提醒 | {stats.sentiment.label} {stats.up}涨{stats.down}跌",
            message=alert_summary
        )

    # LLM analysis (with full alerts + tech data)
    # 有异动时强制调 LLM（即使奇数轮），但受冷却时间限制
    llm_result = ""
    should_call_llm = call_llm
    if not should_call_llm and config.llm_enabled and alerts and config.llm_trigger == "仅异动时":
        should_call_llm = True
    # 冷却检查：距上次 LLM 调用不足 30 分钟时跳过
    if should_call_llm and time.time() - _last_llm_call_time < LLM_COOLDOWN:
        log.info(f"LLM 冷却中（距上次调用 {(time.time() - _last_llm_call_time) / 60:.0f} 分钟），跳过本轮")
        should_call_llm = False
    if should_call_llm and config.llm_enabled:
        try:
            llm_result = analyze_with_llm(quotes, alerts, stats, config, tech_summaries)
            print_llm_result(llm_result)
            _last_llm_call_time = time.time()  # 记录调用时间，启动冷却
        except Exception as e:
            log.error(f"LLM analysis failed: {e}")

    # Dragon Tiger holdings check (once per day, after market close ≥15:30)
    # 龙虎榜数据通常在 16:30 后发布，盘中无法获取当日数据
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    after_close = now.hour >= 15 and now.minute >= 30
    if after_close and _dt_cache["date"] != today_str:
        log.info(f"收盘后触发龙虎榜持仓检测 ({today_str})")
        _check_dragon_tiger_holdings(config, holding_codes)

    # Push alerts if enabled (with LLM result for richer notification)
    if config.push_enabled and alerts:
        try:
            push_alert(alerts, stats, config, llm_result=llm_result)
        except Exception as e:
            log.error(f"Push notification failed: {e}")

    # Save scan record
    funds_status = {}
    for code in quote_map:
        tech = tech_summaries.get(code)
        snapshot = tech if tech else None
        funds_status[code] = FundScanStatus(
            price=quote_map[code].price,
            change_pct=quote_map[code].change_pct,
            volume=quote_map[code].volume,
            tech_snapshot=snapshot,
        )

    scan_record = ScanRecord(
        scan_id=scan_count,
        time=datetime.now().strftime("%H:%M"),
        timestamp=int(time.time()),
        market_sentiment={
            "score": stats.sentiment.score if stats.sentiment else 50,
            "label": stats.sentiment.label if stats.sentiment else "未知"
        },
        alerts_summary={
            "total_alerts": len(alerts),
            "critical_alerts": stats.alert_count,
            "funds_with_alerts": [a.code for a in alerts]
        },
        funds_status=funds_status,
        llm_analysis=llm_result
    )

    scan_history.append(scan_record)
    _save_scan_history(scan_history)

    # 保存当前成交量和资金流状态，供下一轮对比（趋势检测用）
    try:
        cur_state = {}
        for q in quotes:
            if q.volume is not None:
                entry: dict = {"volume": q.volume}
                if q.main_net_inflow is not None:
                    entry["main_net_inflow"] = q.main_net_inflow
                    if q.amount and q.amount > 0:
                        entry["flow_pct"] = round(q.main_net_inflow / q.amount * 100, 2)
                cur_state[q.code] = entry
        STATE_PATH.write_text(json.dumps(cur_state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    # 落盘日内时间序列（供午/晚报复盘分时特征）
    try:
        _append_intraday_series(quotes)
    except Exception:
        pass

    save_brief(quotes, alerts, stats, BRIEF_DIR)
    print_tail(config.scan_interval)


def _append_intraday_series(quotes) -> None:
    """把每次扫描的价格/资金流/量比追加到日内序列文件"""
    series_path = STATE_DIR / "intraday_series.json"
    now_str = datetime.now().strftime("%H:%M")
    today_str = datetime.now().strftime("%Y-%m-%d")

    series: dict = {"date": today_str, "stocks": {}}
    if series_path.exists():
        try:
            series = json.loads(series_path.read_text(encoding="utf-8"))
        except Exception:
            series = {"date": today_str, "stocks": {}}
    if series.get("date") != today_str:
        series = {"date": today_str, "stocks": {}}

    stocks = series.setdefault("stocks", {})
    for q in quotes:
        if q.price is None:
            continue
        code = q.code
        if code not in stocks:
            stocks[code] = {"name": q.name, "timeline": []}
        point = {"time": now_str, "price": q.price, "change_pct": q.change_pct}
        if q.main_net_inflow is not None:
            point["fund_flow"] = round(q.main_net_inflow / 1e4, 1)  # 万元
        if q.volume_ratio is not None:
            point["volume_ratio"] = round(q.volume_ratio, 2)
        stocks[code]["timeline"].append(point)

    series_path.write_text(json.dumps(series, ensure_ascii=False), encoding="utf-8")


def _append_north_flow(north_data) -> None:
    """落盘北向资金轨迹"""
    north_path = STATE_DIR / "north_flow_series.json"
    now_str = datetime.now().strftime("%H:%M")
    today_str = datetime.now().strftime("%Y-%m-%d")

    data: dict = {"date": today_str, "timeline": []}
    if north_path.exists():
        try:
            data = json.loads(north_path.read_text(encoding="utf-8"))
        except Exception:
            data = {"date": today_str, "timeline": []}
    if data.get("date") != today_str:
        data = {"date": today_str, "timeline": []}

    timeline = data.setdefault("timeline", [])
    # 避免同一分钟重复记录
    if timeline and timeline[-1].get("time") == now_str:
        return
    timeline.append({
        "time": now_str,
        "total_net": getattr(north_data, "total_net", 0),
        "hk2sh_net": getattr(north_data, "hk2sh_net", 0),
        "hk2sz_net": getattr(north_data, "hk2sz_net", 0),
    })
    north_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _run_monitoring_loop(config: Config, north_fetcher: NorthFlowFetcher) -> None:
    """Monitoring main loop with shared data architecture"""
    log.info(f"Entering monitor mode, scan every {config.scan_interval} minutes")
    log.info(f"Press Ctrl+C to return to menu")
    print()

    # Build monitor items
    monitor_items = []
    for h in config.holdings:
        monitor_items.append(WatchItem(name=h.name, code=h.code, market=h.market, type="持仓"))
    holding_codes = {h.code for h in config.holdings}
    for item in config.watch_items:
        if item.code not in holding_codes:
            monitor_items.append(WatchItem(name=item.name, code=item.code, market=item.market, type=item.type))

    # Initialize shared data pool
    data_pool = SharedDataPool()

    # Start data fetcher thread (producer)
    fetcher_thread = DataFetcherThread(monitor_items, data_pool, interval=30)
    fetcher_thread.start()

    # Start T+0 monitor thread if enabled
    t0_thread = None
    if hasattr(config, 't0_enabled') and config.t0_enabled:
        t0_interval = getattr(config, 't0_interval', 30)
        t0_thread = T0MonitorThread(monitor_items, data_pool, interval=t0_interval,
                                    enable_sound=True, enable_push=False,
                                    sessions=config.sessions)
        t0_thread.start()

    # Start background cache for fund flow / volume ratio / turnover rate
    from app.data_fetcher import BackgroundDataCache
    bg_cache = BackgroundDataCache(monitor_items, refresh_interval=60)
    bg_cache.start()
    _run_once_new._bg_cache = bg_cache  # attach to function for access in _run_once_new

    # Wait for initial data to be ready (up to 10 seconds)
    log.info("Initializing data pool...")
    pool_ready = False
    for _ in range(10):
        if data_pool.is_fresh(max_age=30):
            pool_ready = True
            log.info(f"Data pool ready, {data_pool.update_count} updates received")
            break
        time.sleep(1)
    if not pool_ready:
        log.warning("Data pool not ready after 10s — scans may use stale/empty data. "
                    "Check network connectivity.")

    first_run = True
    scan_count = 0
    try:
        while True:
            if is_trading_time(datetime.now(), config.sessions)[0]:
                scan_count += 1
                call_llm = (scan_count % 10 == 0)
                # 冷却已在 _run_once_new 内部处理
                _run_once_new(config, north_fetcher, data_pool, call_llm=call_llm, scan_count=scan_count)
            else:
                now = datetime.now()
                if first_run or now.minute % 15 == 0:
                    reason = "Weekend closed" if now.weekday() >= 5 else "Non-trading hours"
                    log.info(f"Paused: {reason} ({now.strftime('%H:%M')})")
                    first_run = False

            _wait_until_next_slot(config.scan_interval)

    except KeyboardInterrupt:
        log.info("Returning to menu...")
    finally:
        # Stop threads
        if t0_thread:
            t0_thread.stop()
        fetcher_thread.stop()
        if bg_cache:
            bg_cache.stop()
        log.info("All threads stopped")


def main() -> None:
    """Main entry - menu selection mode"""
    if sys.platform == "win32":
        os.system("chcp 65001 >nul 2>&1")

    ensure_dirs(STATE_DIR, BRIEF_DIR, REPORT_DIR)
    load_env(BASE_DIR)

    config = Config(CONFIG_PATH)
    north_fetcher = NorthFlowFetcher(config,
                                     cache_seconds=config.north_flow_interval * 60)

    _print_banner(config)

    try:
        import akshare as ak
        log.debug(f"AKShare 版本: {ak.__version__}")
    except ImportError:
        log.warning(f"⚠️ AKShare 未安装在当前环境: {sys.executable}")
        log.warning(f"⚠️ ETF筛选功能将不可用，请执行: pip install akshare>=1.14.0")

    while True:
        choice = _show_menu()

        if choice == "0":
            print(f"\n{Color.YELLOW}Goodbye!{Color.RESET}")
            sys.exit(0)
        elif choice in ("1", "2", "3"):
            report_map = {"1": "Morning Brief", "2": "Midday Review", "3": "Evening Review"}
            rtype = report_map[choice]
            rc = config.report_cfg.get(rtype, {})
            if not rc.get("启用", False):
                log.warning(f"'{rtype}' is disabled, please enable in watchlist_config.json")
                continue
            _gen_report(rtype, config)
            print(f"{Color.DIM}Report generated and pushed (see {REPORT_DIR}){Color.RESET}")
        elif choice == "4":
            _run_monitoring_loop(config, north_fetcher)
        elif choice == "5":
            log.info("Starting active fund comprehensive analysis...")
            result = analyze_funds(config)
            if result:
                print(f"{Color.DIM}Fund analysis report generated (see {result}){Color.RESET}")
            else:
                log.error("Fund analysis failed, please check funds_config.json")
        elif choice == "6":
            log.info("Updating holdings from broker (东方财富)...")
            success = auto_update_holdings()
            if success:
                print(f"{Color.GREEN}✅ Holdings updated successfully!{Color.RESET}")
            else:
                print(f"{Color.RED}❌ Failed to update holdings{Color.RESET}")
        elif choice == "7":
            _query_period_returns(config)
        elif choice == "8":
            log.info("Starting ETF bottom reversal screen...")
            print(f"\n{Color.BOLD}{Color.CYAN}🔍 ETF 底部反转筛选{Color.RESET}")
            print(f"{Color.DIM}正在扫描全市场 ETF（约需 10-30 秒），请耐心等待...{Color.RESET}")
            try:
                import akshare as ak
                log.debug(f"AKShare 版本: {ak.__version__}")
            except ImportError:
                print(f"{Color.RED}❌ AKShare 未安装，无法使用 ETF 筛选功能{Color.RESET}")
                print(f"  {Color.DIM}当前 Python: {sys.executable}")
                print(f"  {Color.DIM}请执行: pip install akshare>=1.14.0{Color.RESET}")
                continue
            try:
                from app.etf_screener import screen_etf_bottom_reversal, generate_etf_screen_report
                import time as _time
                _start = _time.time()
                candidates = screen_etf_bottom_reversal(max_candidates=15, max_kline_fetch=60)
                elapsed = _time.time() - _start

                if candidates:
                    report = generate_etf_screen_report(candidates, output_dir=REPORT_DIR)
                    strong = [c for c in candidates if c.signal_level == "强信号 ⭐⭐⭐"]
                    medium = [c for c in candidates if c.signal_level == "中信号 ⭐⭐"]
                    print(f"\n{Color.GREEN}✅ 筛选完成 ({elapsed:.0f}秒){Color.RESET}")
                    print(f"   {Color.RED}强信号 ⭐⭐⭐: {len(strong)}只{Color.RESET}")
                    print(f"   {Color.YELLOW}中信号 ⭐⭐: {len(medium)}只{Color.RESET}")
                    print(f"   {Color.DIM}弱信号/关注: {len(candidates) - len(strong) - len(medium)}只{Color.RESET}")
                    etf_report_filename = f"etf_screen_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
                    print(f"   {Color.DIM}完整报告: {REPORT_DIR / etf_report_filename}{Color.RESET}")
                    # Print top picks immediately
                    if strong:
                        print(f"\n{Color.BOLD}🔥 强信号候选:{Color.RESET}")
                        for c in strong:
                            print(f"  {Color.BOLD}{c.name}({c.code}){Color.RESET} "
                                  f"得分{c.score} | {', '.join(c.signals[:3])}")
                else:
                    print(f"\n{Color.YELLOW}⚠️ 未发现符合条件的底部反转 ETF (耗时{elapsed:.0f}秒){Color.RESET}")
                    if elapsed < 2:
                        print(f"  {Color.DIM}提示: 耗时很短，可能是全市场数据获取失败")
                        print(f"  AKShare 在非交易时段可能限流，请在交易时段(9:30-15:00)重试{Color.RESET}")
            except ImportError as e:
                print(f"{Color.RED}❌ AKShare 未安装，无法使用 ETF 筛选功能{Color.RESET}")
                print(f"  {Color.DIM}请执行: pip install akshare>=1.14.0{Color.RESET}")
            except Exception as e:
                log.error(f"ETF筛选失败: {e}")
                print(f"{Color.RED}❌ ETF筛选失败: {e}{Color.RESET}")
        elif choice == "9":
            log.info("Starting A-share stock bottom reversal screen...")
            print(f"\n{Color.BOLD}{Color.CYAN}🔍 A股底部反转筛选（排除ST）{Color.RESET}")
            print(f"{Color.DIM}正在扫描全市场A股（约需 30-60 秒），请耐心等待...{Color.RESET}")
            try:
                from app.stock_screener import screen_stock_bottom_reversal, generate_stock_screen_report
                import time as _time
                _start = _time.time()
                candidates = screen_stock_bottom_reversal(max_candidates=20, max_kline_fetch=100)
                elapsed = _time.time() - _start

                if candidates:
                    report = generate_stock_screen_report(candidates, output_dir=REPORT_DIR)
                    strong = [c for c in candidates if c.signal_level == "强信号"]
                    medium = [c for c in candidates if c.signal_level == "中信号"]
                    print(f"\n{Color.GREEN}✅ 筛选完成 ({elapsed:.0f}秒){Color.RESET}")
                    print(f"   {Color.RED}强信号 ⭐⭐⭐: {len(strong)}只{Color.RESET}")
                    print(f"   {Color.YELLOW}中信号 ⭐⭐: {len(medium)}只{Color.RESET}")
                    print(f"   {Color.DIM}弱信号/关注: {len(candidates) - len(strong) - len(medium)}只{Color.RESET}")
                    stk_report_filename = f"stock_screen_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
                    print(f"   {Color.DIM}完整报告: {REPORT_DIR / stk_report_filename}{Color.RESET}")
                    if strong:
                        print(f"\n{Color.BOLD}🔥 强信号候选:{Color.RESET}")
                        for c in strong:
                            signals_str = ', '.join(c.signals[:3]) if c.signals else ''
                            mcap_str = f"{c.market_cap_yi:.0f}亿" if c.market_cap > 0 else ""
                            print(f"  {Color.BOLD}{c.name}({c.code}){Color.RESET} "
                                  f"得分{c.score} | {mcap_str} | {signals_str}")
                else:
                    print(f"\n{Color.YELLOW}⚠️ 未发现符合条件的底部反转股票 (耗时{elapsed:.0f}秒){Color.RESET}")
                    if elapsed < 3:
                        print(f"  {Color.DIM}提示: 耗时很短，可能是全市场数据获取失败")
                        print(f"  请在交易时段(9:30-15:00)重试{Color.RESET}")
            except ImportError:
                print(f"{Color.RED}❌ AKShare 未安装，无法使用智能荐股功能{Color.RESET}")
                print(f"  {Color.DIM}请执行: pip install akshare>=1.14.0{Color.RESET}")
            except Exception as e:
                log.error(f"股票筛选失败: {e}")
                print(f"{Color.RED}❌ 股票筛选失败: {e}{Color.RESET}")
        elif choice == "D":
            log.info("Starting dragon tiger deep analysis...")
            print(f"\n{Color.BOLD}{Color.RED}🐉 龙虎榜深度分析{Color.RESET}")
            print(f"{Color.DIM}正在获取龙虎榜数据并进行席位级分析（约需 10-20 秒）...{Color.RESET}")
            try:
                from app.dragon_tiger import fetch_dragon_tiger_list, format_dragon_tiger_report, analyze_dragon_tiger, build_llm_context, analyze_dragon_tiger_llm
                from app.dragon_seat import analyze_dragon_tiger_seats, generate_seat_report, detect_consecutive_listings
                import time as _time

                _start = _time.time()

                # 1. 获取龙虎榜汇总
                records = fetch_dragon_tiger_list(max_count=80)
                if not records:
                    print(f"\n{Color.YELLOW}⚠️ 今天无龙虎榜数据（可能非交易日）{Color.RESET}")
                    return

                print(f"  上榜个股: {len(records)}只")

                # 2. 连续上榜检测
                consecutive = detect_consecutive_listings(records)
                if consecutive:
                    print(f"  {Color.CYAN}🔄 连续上榜: {len(consecutive)}只{Color.RESET}")
                    for item in consecutive[:8]:
                        relay = f" {Color.YELLOW}{item['relay_note']}{Color.RESET}" if item['relay_note'] else ""
                        print(f"    {item['name']}({item['code']}) 连续{item['consecutive_days']}天{relay}")

                # 3. 席位级深度分析（只拉取一次，结果复用给汇总+LLM）
                analyses = analyze_dragon_tiger_seats(records, max_seat_fetch=40)

                # 4. 生成汇总报告（传入席位数据，避免内部重复调用）
                summary = analyze_dragon_tiger(records, seat_analyses=analyses if analyses else None)
                report_lines = [format_dragon_tiger_report(summary)]

                if analyses:
                    seat_report = generate_seat_report(analyses)
                    report_lines.append("\n---\n")
                    report_lines.append(seat_report)

                    # 席位统计
                    inst_count = sum(1 for a in analyses if a.is_institution_driven)
                    hm_count = sum(1 for a in analyses if a.is_hot_money_clustering)
                    risk_count = sum(1 for a in analyses if a.risk_flags)
                    high_q = sum(1 for a in analyses if a.quality_label == "高质")
                    print(f"  {Color.RED}机构主导: {inst_count}只{Color.RESET} | "
                          f"{Color.YELLOW}游资抱团: {hm_count}只{Color.RESET} | "
                          f"{Color.GREEN}风险标记: {risk_count}只{Color.RESET} | "
                          f"高质: {high_q}只")

                    # 持仓交叉
                    holdings_codes = {h.code for h in config.holdings}
                    from app.dragon_seat import check_holdings_dragon_tiger
                    h_alerts = check_holdings_dragon_tiger(analyses, holdings_codes)
                    if h_alerts:
                        print(f"\n  {Color.BOLD}{Color.RED}🔔 持仓上榜: {len(h_alerts)}只{Color.RESET}")
                        for a in h_alerts:
                            print(f"    [{a['quality']}] {a['name']}({a['code']}) "
                                  f"{a['change_pct']:+.1f}% | " +
                                  (f"机构{a['institution_net']/1e4:+.0f}万" if a['institution_net'] else ""))
                else:
                    h_alerts = None

                # 4.5. LLM 龙虎榜深度解读
                if config.dragon_tiger_llm_enabled and config.deepseek_key:
                    print(f"\n  {Color.DIM}🤖 AI 正在解读龙虎榜数据...{Color.RESET}")
                    try:
                        llm_dt = analyze_dragon_tiger_llm(
                            summary=summary,
                            config=config,
                            seat_analyses=analyses if analyses else None,
                            holdings_alerts=h_alerts,
                        )
                        if llm_dt:
                            report_lines.append("\n---\n")
                            report_lines.append("## 🤖 AI 龙虎榜深度解读\n")
                            report_lines.append(llm_dt)
                            print(f"  {Color.GREEN}✅ AI 解读完成{Color.RESET}")
                        else:
                            print(f"  {Color.YELLOW}⚠️ AI 解读生成失败，跳过{Color.RESET}")
                    except Exception as e:
                        log.warning(f"LLM 龙虎榜分析异常: {e}")
                        print(f"  {Color.YELLOW}⚠️ AI 解读异常: {e}{Color.RESET}")

                # 4.6. Agent 深度挖掘（LLM 自主调用工具，多轮推理）
                if config.dragon_tiger_llm_enabled and config.deepseek_key:
                    print(f"\n  {Color.DIM}🔍 Agent 正在深度挖掘龙虎榜数据...{Color.RESET}")
                    try:
                        agent_result = _run_dragon_tiger_agent(
                            summary=summary,
                            seat_analyses=analyses if analyses else [],
                            config=config,
                        )
                        if agent_result:
                            report_lines.append("\n---\n")
                            report_lines.append(agent_result)
                            print(f"  {Color.GREEN}✅ Agent 挖掘完成{Color.RESET}")
                        else:
                            print(f"  {Color.YELLOW}⚠️ Agent 挖掘跳过{Color.RESET}")
                    except Exception as e:
                        log.warning(f"Agent 挖掘异常: {e}")
                        print(f"  {Color.YELLOW}⚠️ Agent 挖掘异常: {e}{Color.RESET}")

                # 5. 写入报告
                full_report = "\n".join(report_lines)
                from pathlib import Path
                dt_report_path = REPORT_DIR / f"dragon_tiger_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
                dt_report_path.write_text(full_report, encoding='utf-8')

                elapsed = _time.time() - _start
                print(f"\n{Color.GREEN}✅ 龙虎榜分析完成 ({elapsed:.0f}秒){Color.RESET}")
                print(f"  {Color.DIM}完整报告: {dt_report_path}{Color.RESET}")

            except ImportError:
                print(f"{Color.RED}❌ AKShare 未安装{Color.RESET}")
            except Exception as e:
                log.error(f"龙虎榜分析失败: {e}")
                print(f"{Color.RED}❌ 龙虎榜分析失败: {e}{Color.RESET}")

        elif choice == "M":
            _run_miaoxiang_menu(config)


def _run_miaoxiang_menu(config: Config) -> None:
    """东方财富妙想 Skills 功能菜单（循环停留在子菜单）"""
    from app.miaoxiang import get_mx_client

    if not config.mx_apikey:
        print(f"\n{Color.YELLOW}⚠️ 未配置 MX_APIKEY{Color.RESET}")
        print(f"  {Color.DIM}请在 .env 文件中设置: MX_APIKEY=你的妙想API_Key{Color.RESET}")
        print(f"  {Color.DIM}获取地址: https://dl.dfcfs.com/m/itc4{Color.RESET}")
        return

    mx = get_mx_client(config)

    while True:
        print(f"\n{Color.BOLD}{Color.CYAN}🧠 东方财富妙想 Skills{Color.RESET}")
        print(f"  {Color.CYAN}1{Color.RESET}. 金融数据查询 (行情/财务/资金流)")
        print(f"  {Color.CYAN}2{Color.RESET}. 智能选股 (自然语言条件)")
        print(f"  {Color.CYAN}3{Color.RESET}. 财经资讯搜索")
        print(f"  {Color.CYAN}4{Color.RESET}. 自选股管理 (查询/添加/删除)")
        print(f"  {Color.CYAN}0{Color.RESET}. 返回主菜单")

        try:
            sub = input(f" Enter option [0-4]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if sub == "0":
            return
        elif sub == "1":
            _run_mx_query(mx)
        elif sub == "2":
            _run_mx_screen(mx)
        elif sub == "3":
            _run_mx_search(mx)
        elif sub == "4":
            _run_mx_selfselect(mx)
        else:
            print(f"{Color.YELLOW} 无效选项{Color.RESET}")


def _run_mx_query(mx) -> None:
    """妙想金融数据查询（连续查询，0 或空行返回上级菜单）"""
    print(f"\n{Color.BOLD}{Color.CYAN}📊 金融数据查询{Color.RESET}")
    print(f"{Color.DIM}示例: 贵州茅台最新价 / 宁德时代主力资金流向 / 沪深300市盈率{Color.RESET}")
    print(f"{Color.DIM}输入 0 或空行返回上级菜单{Color.RESET}")
    while True:
        try:
            query = input("\n请输入查询内容: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not query or query == "0":
            return
        print(f"{Color.DIM}查询中...{Color.RESET}")
        result = mx.query_as_text(query)
        print(result if result else f"{Color.YELLOW}⚠️ 查询失败或未返回数据{Color.RESET}")


def _run_mx_screen(mx) -> None:
    """妙想智能选股（连续选股，0 或空行返回上级菜单）"""
    print(f"\n{Color.BOLD}{Color.CYAN}🔍 智能选股{Color.RESET}")
    print(f"{Color.DIM}示例: 今日涨幅超过3%的半导体股票 / 低估值高股息蓝筹{Color.RESET}")
    print(f"{Color.DIM}输入 0 或空行返回上级菜单{Color.RESET}")
    while True:
        try:
            keyword = input("\n请输入选股条件: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not keyword or keyword == "0":
            return
        print(f"{Color.DIM}筛选中...{Color.RESET}")
        result = mx.stock_screen_as_text(keyword)
        print(result if result else f"{Color.YELLOW}⚠️ 筛选失败或未返回数据{Color.RESET}")


def _run_mx_search(mx) -> None:
    """妙想财经资讯搜索（连续搜索，0 或空行返回上级菜单）"""
    print(f"\n{Color.BOLD}{Color.CYAN}📰 财经资讯搜索{Color.RESET}")
    print(f"{Color.DIM}示例: 人工智能最新消息 / 半导体行业研报{Color.RESET}")
    print(f"{Color.DIM}输入 0 或空行返回上级菜单{Color.RESET}")
    while True:
        try:
            keyword = input("\n请输入搜索关键词: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not keyword or keyword == "0":
            return
        print(f"{Color.DIM}搜索中...{Color.RESET}")
        result = mx.fin_search_as_text(keyword)
        print(result if result else f"{Color.YELLOW}⚠️ 搜索失败或未返回数据{Color.RESET}")


def _run_mx_selfselect(mx) -> None:
    """妙想自选股管理（查询/添加/删除，0 或空行返回上级菜单）"""
    print(f"\n{Color.BOLD}{Color.CYAN}⭐ 自选股管理{Color.RESET}")
    print(f"  {Color.CYAN}1{Color.RESET}. 查询我的自选股")
    print(f"  {Color.CYAN}2{Color.RESET}. 添加自选股 (如: 添加贵州茅台)")
    print(f"  {Color.CYAN}3{Color.RESET}. 删除自选股 (如: 删除贵州茅台)")
    print(f"  {Color.CYAN}0{Color.RESET}. 返回上级菜单")

    while True:
        try:
            choice = input("\n请输入操作 [1/2/3/0]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "0" or choice == "":
            return
        elif choice == "1":
            print(f"{Color.DIM}查询中...{Color.RESET}")
            result = mx.self_select_get_as_text()
            print(result if result else f"{Color.YELLOW}⚠️ 查询失败或未返回数据{Color.RESET}")
        elif choice == "2":
            try:
                instr = input("请输入要添加的股票(名称或代码): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not instr:
                continue
            print(f"{Color.DIM}添加中...{Color.RESET}")
            result = mx.self_select_manage_as_text(f"把{instr}加入自选")
            print(result if result else f"{Color.YELLOW}⚠️ 添加失败{Color.RESET}")
        elif choice == "3":
            try:
                instr = input("请输入要删除的股票(名称或代码): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not instr:
                continue
            print(f"{Color.DIM}删除中...{Color.RESET}")
            result = mx.self_select_manage_as_text(f"把{instr}从自选删除")
            print(result if result else f"{Color.YELLOW}⚠️ 删除失败{Color.RESET}")
        else:
            print(f"{Color.YELLOW} 无效选项{Color.RESET}")


def _run_dragon_tiger_agent(
    summary,
    seat_analyses: list,
    config,
) -> str | None:
    """运行 Agent 深度挖掘龙虎榜数据

    LLM 可主动调用工具函数查询：
    - 具体个股的席位明细
    - 连续上榜历史
    - 行业分类
    - 技术指标
    - 主力资金流向
    """
    try:
        from app.llm_agent import LLMAgent, AgentTool
        from app.dragon_tiger import format_dragon_tiger_report, build_llm_context
        from app.dragon_seat import generate_seat_report, fetch_seat_details
        from app.dragon_seat import detect_consecutive_listings

        # ---- 构建工具列表 ----

        # 预建索引用于工具函数
        all_records = summary.records or []
        record_map = {r.code: r for r in all_records}
        seat_map: dict[str, list] = {}
        if seat_analyses:
            for sa in seat_analyses:
                seat_map[sa.code] = [
                    {
                        "seat_name": s.seat_name,
                        "seat_type": s.seat_type,
                        "buy_amount": s.buy_amount,
                        "sell_amount": s.sell_amount,
                        "net_amount": s.net_amount,
                        "buy_rank": s.buy_rank,
                        "sell_rank": s.sell_rank,
                    }
                    for s in (sa.seats or [])
                ]

        def tool_query_seat_details(codes: list[str]) -> dict:
            """查询指定股票的席位买卖明细"""
            result = {}
            for code in codes:
                if code in seat_map:
                    result[code] = seat_map[code]
                else:
                    result[code] = []
            return result

        def tool_query_industry(codes: list[str]) -> dict:
            """查询股票的所属行业"""
            result = {}
            for code in codes:
                record = record_map.get(code)
                industry = getattr(record, "industry", "") if record else ""
                result[code] = industry or "未知"
            return result

        def tool_query_record_detail(codes: list[str]) -> dict:
            """查询龙虎榜上榜记录详情（净买额/涨跌幅/上榜原因/买卖比）"""
            result = {}
            for code in codes:
                record = record_map.get(code)
                if record:
                    result[code] = {
                        "name": record.name,
                        "net_buy": f"{record.net_buy/1e8:.2f}亿",
                        "change_pct": f"{record.change_pct:+.1f}%" if record.change_pct else "--",
                        "buy_sell_ratio": f"{record.buy_sell_ratio:.2f}" if record.buy_sell_ratio else "--",
                        "turnover_rate": f"{record.turnover_rate:.1f}%" if record.turnover_rate else "--",
                        "reason": getattr(record, "reason", ""),
                    }
                else:
                    result[code] = None
            return result

        def tool_query_abnormal_patterns(codes: list[str]) -> dict:
            """查询指定股票的异常形态（涨停板出货/跌停接筹/对倒等）"""
            result = {}
            patterns = summary.abnormal_patterns or []
            for code in codes:
                matched = [p for p in patterns if p.get("code") == code]
                if matched:
                    result[code] = [
                        {
                            "形态": p.get("pattern_label", ""),
                            "详情": p.get("detail", ""),
                        }
                        for p in matched
                    ]
                else:
                    result[code] = []
            return result

        # ---- 定义工具 ----
        tools = [
            AgentTool(
                name="query_seat_details",
                description="查询指定股票的龙虎榜席位买卖明细。返回每个席位的名称、类型（机构/游资/量化/散户）、买入额、卖出额、净额、排名。",
                func=tool_query_seat_details,
                parameters={
                    "type": "object",
                    "properties": {
                        "codes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "股票代码列表，如 ['000977', '300017']，最多5只",
                        },
                    },
                    "required": ["codes"],
                },
            ),
            AgentTool(
                name="query_record_detail",
                description="查询股票的龙虎榜上榜详情：净买额、涨跌幅、买卖比、换手率、上榜原因。",
                func=tool_query_record_detail,
                parameters={
                    "type": "object",
                    "properties": {
                        "codes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "股票代码列表，最多10只",
                        },
                    },
                    "required": ["codes"],
                },
            ),
            AgentTool(
                name="query_abnormal_patterns",
                description="查询指定股票的异常交易形态：涨停板出货、跌停接筹、封板缩量、放量烂板、机构对倒等。",
                func=tool_query_abnormal_patterns,
                parameters={
                    "type": "object",
                    "properties": {
                        "codes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "股票代码列表，最多10只",
                        },
                    },
                    "required": ["codes"],
                },
            ),
            AgentTool(
                name="query_industry",
                description="查询股票所属行业分类。",
                func=tool_query_industry,
                parameters={
                    "type": "object",
                    "properties": {
                        "codes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "股票代码列表，最多10只",
                        },
                    },
                    "required": ["codes"],
                },
            ),
        ]

        # ---- 构建初始 prompt ----
        init_prompt_parts = [
            f"请对今日({summary.date})龙虎榜数据进行深度挖掘分析。",
            f"上榜个股共 {summary.total_count} 只。",
            "",
            "以下是已预计算的汇总数据，请先仔细阅读，然后使用工具函数进一步查询你感兴趣的股票：",
            "",
        ]

        # 加入汇总数据
        report_md = format_dragon_tiger_report(summary)
        init_prompt_parts.append(report_md)

        if seat_analyses:
            seat_md = generate_seat_report(seat_analyses)
            init_prompt_parts.append("\n---\n")
            init_prompt_parts.append(seat_md)

        init_prompt_parts.append("\n---\n")
        init_prompt_parts.append("""
**分析要求**：
1. 从汇总数据中找出 2-4 只资金博弈最激烈的股票。
2. 使用工具函数查询这些股票的席位明细、异常形态等深度数据。
3. 结合查询结果，给出每只股票的资金博弈解读和次日走势预判。
4. 最终按 Agent 深度挖掘的格式输出完整分析。

**可用的工具**：
- `query_seat_details`: 查询席位买卖明细（机构/游资/量化/散户的买卖金额和排名）
- `query_record_detail`: 查询上榜详情（净买额/涨跌幅/买卖比/上榜原因）
- `query_abnormal_patterns`: 查询异常形态（涨停板出货/跌停接筹/对倒等）
- `query_industry`: 查询行业分类

开始分析吧。""")

        initial_prompt = "\n".join(init_prompt_parts)

        # ---- 运行 Agent ----
        agent = LLMAgent(config, tools=tools)
        log.info("Agent: 开始龙虎榜深度挖掘...")
        result = agent.run(initial_prompt)

        if result and "Agent 未启用" not in result:
            log.info("Agent: 挖掘完成")
            return result
        return None

    except ImportError as e:
        log.debug(f"Agent 依赖缺失: {e}")
        return None
    except Exception as e:
        log.error(f"Agent 挖掘失败: {e}")
        return None


if __name__ == "__main__":
    main()
