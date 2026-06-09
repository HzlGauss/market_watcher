#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path

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
    print(f"  {Color.CYAN}0{Color.RESET}. Exit")
    print()

    while True:
        try:
            choice = input(f" Enter option [0-7]: ").strip()
            if choice in ("0", "1", "2", "3", "4", "5", "6", "7"):
                return choice
            print(f"{Color.YELLOW}  Please enter 0-7{Color.RESET}")
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

    # 从后台缓存获取量比、换手率、主力净流入（不阻塞主循环）
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

    # Analyze all quotes together (with technical signals and history)
    alerts, stats = analyze(
        quotes, prev_state, config, tech_summaries,
        north_data=north_fetcher.fetch()
    )
    print_sentiment(stats)

    # 保存当前成交量，供下一周期量价分析
    try:
        cur_state = {q.code: {"volume": q.volume} for q in quotes if q.volume is not None}
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
        log.info(f"持仓统计: {len(holdings_quotes)} 只, 上涨 {holdings_up} 只, 下跌 {holdings_down} 只")

    if watchlist_quotes:
        watchlist_up = sum(1 for q in watchlist_quotes if q.change_pct and q.change_pct > 0)
        watchlist_down = sum(1 for q in watchlist_quotes if q.change_pct and q.change_pct < 0)
        log.info(f"标的列表统计: {len(watchlist_quotes)} 只, 上涨 {watchlist_up} 只, 下跌 {watchlist_down} 只")

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

        if call_llm and config.llm_enabled and config.deepseek_key:
            llm_result = analyze_with_llm(quotes, alerts, stats, config, tech_summaries)
            print_llm_result(llm_result)

            # 保存LLM分析结果到扫描记录
            scan_record.llm_analysis = llm_result

            if config.push_enabled and config.sct_sendkey:
                push_alert(alerts, stats, config, llm_result)
        else:
            if not call_llm:
                log.info("LLM 跳过（本轮不请求）")
            if config.push_enabled and config.sct_sendkey:
                push_alert(alerts, stats, config)
    else:
        log.info("No alerts triggered")

    # 保存扫描历史
    scan_history.append(scan_record)
    _save_scan_history(scan_history)

    save_brief(quotes, alerts, stats, BRIEF_DIR)
    print_tail(config.scan_interval)


def _run_once_new(config: Config, north_fetcher: NorthFlowFetcher, data_pool,
                  call_llm: bool = False, scan_count: int = 0) -> None:
    """Run one scan cycle using shared data pool"""
    from app.analyzer import analyze, _load_scan_history, _save_scan_history
    from app.ai_analyzer import analyze as analyze_with_llm
    from app.notifier import push_alert, send_desktop_notification
    from app.presenter import (
        print_quotes_table, print_sentiment, print_alerts,
        print_llm_result, print_tail, save_brief, print_key_levels
    )
    from app.technical import get_technical_summary, TechnicalSummary
    from app.models import ScanRecord, FundScanStatus, TechSnapshot

    log.info("Scanning market data (from shared pool)...")

    # Get data from shared pool
    quotes = list(data_pool.get_all_quotes().values())
    if not quotes:
        log.warning("No quote data available in data pool")
        return

    # Get K-line data from shared pool
    klines_map = {}
    for q in quotes:
        klines = data_pool.get_klines(q.code)
        if klines:
            klines_map[q.code] = klines

    # Separate quotes by type for statistics
    holdings_quotes = [q for q in quotes if q.type == "持仓"]
    watchlist_quotes = [q for q in quotes if q.type != "持仓"]

    # Calculate technical summaries
    tech_summaries: dict[str, TechnicalSummary] = {}
    quote_map = {q.code: q for q in quotes}

    for code in quote_map:
        klines = klines_map.get(code)
        if klines:
            tech_summaries[code] = get_technical_summary(quote_map[code], klines)

    print_quotes_table(quotes)

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
    all_strategy_signals: list[tuple[str, str, str]] = []
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
                if signals:
                    for s in signals:
                        all_strategy_signals.append((quote.name, code, s.to_alert_text()))
                else:
                    # Show "no signal" for all holdings
                    all_strategy_signals.append((quote.name, code, "⚪ [无信号]"))
        except Exception as e:
            log.warning(f"Strategy evaluation failed for {code}: {e}")

    # Analyze market
    alerts, stats = analyze(holdings_quotes, prev_state, config, tech_summaries)

    # Print sentiment and alerts
    print_sentiment(stats)
    print_alerts(alerts)

    # 发送桌面通知
    if alerts:
        alert_summary = " | ".join([f"{a.name}: {', '.join(a.messages)}" for a in alerts[:3]])
        if len(alerts) > 3:
            alert_summary += f" | ...还有 {len(alerts)-3} 条"
        send_desktop_notification(
            title=f"📈 盯盘提醒 | {stats.sentiment.label} {stats.up}涨{stats.down}跌",
            message=alert_summary
        )

    # LLM analysis
    llm_result = ""
    if call_llm and config.llm_enabled:
        try:
            llm_result = analyze_with_llm(config, quotes, stats)
            print_llm_result(llm_result)
        except Exception as e:
            log.error(f"LLM analysis failed: {e}")

    # Push alerts if enabled
    if config.push_enabled and alerts:
        try:
            push_alert(alerts, stats, config)
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
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        funds_status=funds_status,
        llm_analysis=llm_result
    )

    scan_history.append(scan_record)
    _save_scan_history(scan_history)

    save_brief(quotes, alerts, stats, BRIEF_DIR)
    print_tail(config.scan_interval)


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
        enable_push = getattr(config, 't0_push_enabled', False)
        t0_interval = getattr(config, 't0_interval', 30)
        t0_thread = T0MonitorThread(monitor_items, data_pool, interval=t0_interval,
                                    enable_sound=True, enable_push=enable_push)
        t0_thread.start()

    # Wait for initial data to be ready (up to 10 seconds)
    log.info("Initializing data pool...")
    for _ in range(10):
        if data_pool.is_fresh(max_age=30):
            log.info(f"Data pool ready, {data_pool.update_count} updates received")
            break
        time.sleep(1)

    first_run = True
    scan_count = 0
    try:
        while True:
            if is_trading_time(datetime.now(), config.sessions)[0]:
                scan_count += 1
                call_llm = (scan_count % 2 == 0)
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


if __name__ == "__main__":
    main()
