#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
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

# Path definitions
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_PATH = STATE_DIR / "market_state.json"
BRIEF_DIR = BASE_DIR / "monitoring_briefs"

CONFIG_PATH = BASE_DIR / "watchlist_config.json"
REPORT_DIR = BASE_DIR / "investment_reports"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)


def _print_banner(config: Config) -> None:
    """Print system status banner"""
    bt = config.thresholds
    llm_ok = config.llm_enabled and config.deepseek_key
    push_ok = config.push_enabled and config.sct_sendkey
    nf_ok = config.north_flow_enabled

    print(f"""
{Color.BOLD}{Color.CYAN}======================================================={Color.RESET}
      Stock Market Monitoring Radar v2
      Standalone Version - Modular Refactor
{Color.BOLD}{Color.CYAN}======================================================={Color.RESET}
    """)
    log.info(f"Watch items: {len(config.watch_items)}")
    log.info(f"Thresholds: Up>{bt.get('涨幅预警',4)}% Down<{bt.get('跌幅预警',-3)}%")
    log.info(f"AI Analysis:  {'OK' if llm_ok else 'OFF'}  |  Push: {'OK' if push_ok else 'OFF'}")
    log.info(f"North Flow: {'OK' if nf_ok else 'OFF'} |  Briefs/Reports: {BRIEF_DIR}")
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
    print(f"  {Color.CYAN}0{Color.RESET}. Exit")
    print()

    while True:
        try:
            choice = input(f" Enter option [0-6]: ").strip()
            if choice in ("0", "1", "2", "3", "4", "5", "6"):
                return choice
            print(f"{Color.YELLOW}  Please enter 0-6{Color.RESET}")
        except (EOFError, KeyboardInterrupt):
            return "0"


def _gen_report(report_type: str, config: Config,
                north_fetcher: NorthFlowFetcher) -> None:
    """Generate single report"""
    if report_type == "Morning Brief":
        log.info("Generating morning brief...")
        generate_morning_brief(config)
    elif report_type == "Midday Review":
        log.info("Generating midday review...")
        generate_midday_review(config, north_fetcher)
    elif report_type == "Evening Review":
        log.info("Generating evening review...")
        generate_evening_review(config, north_fetcher)


def _in_trading_hours(config: Config) -> bool:
    """Check if current time is within trading hours"""
    now = datetime.now()
    today = now.weekday()

    if today >= 5:
        return False

    sessions = config.sessions
    morning = sessions.get("上午", ["09:30", "11:30"])
    afternoon = sessions.get("下午", ["13:00", "15:00"])

    current_minutes = now.hour * 60 + now.minute

    def time_to_minutes(time_str: str) -> int:
        h, m = map(int, time_str.split(":"))
        return h * 60 + m

    am_start, am_end = time_to_minutes(morning[0]), time_to_minutes(morning[1])
    pm_start, pm_end = time_to_minutes(afternoon[0]), time_to_minutes(afternoon[1])

    return (am_start <= current_minutes <= am_end) or (pm_start <= current_minutes <= pm_end)


def _wait_until_next_slot(interval: int) -> None:
    """Wait until next scan slot"""
    now = datetime.now()
    seconds_to_next = (interval * 60) - (now.minute % interval) * 60 - now.second - 1
    if seconds_to_next < 0:
        seconds_to_next += interval * 60

    for i in range(int(seconds_to_next), 0, -1):
        time.sleep(1)


def _run_once(config: Config, north_fetcher: NorthFlowFetcher) -> None:
    """Run one scan cycle"""
    from app.data_fetcher import fetch_quotes
    from app.analyzer import analyze
    from app.ai_analyzer import analyze as analyze_with_llm
    from app.notifier import push_alert
    from app.presenter import (
        print_quotes_table, print_sentiment, print_alerts,
        print_llm_result, print_tail, save_brief
    )
    from app.technical import fetch_historical_kline, get_technical_summary

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

    # Separate quotes by type for statistics
    holdings_quotes = [q for q in quotes if q.type == "持仓"]
    watchlist_quotes = [q for q in quotes if q.type != "持仓"]

    # Fetch technical indicators for holdings & watchlist items
    tech_summaries: dict[str, "TechnicalSummary"] = {}
    quote_map = {q.code: q for q in quotes}
    item_map = {item.code: item for item in monitor_items}

    for code in quote_map:
        item = item_map.get(code)
        if not item:
            continue
        klines = fetch_historical_kline(code, item.market, days=30)
        if klines:
            tech_summaries[code] = get_technical_summary(quote_map[code], klines)

    print_quotes_table(quotes)

    # Analyze all quotes together (with technical signals)
    alerts, stats = analyze(quotes, {}, config, tech_summaries)
    print_sentiment(stats)

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

        if config.llm_enabled and config.deepseek_key:
            llm_result = analyze_with_llm(quotes, alerts, stats, config)
            print_llm_result(llm_result)

            if config.push_enabled and config.sct_sendkey:
                push_alert(alerts, stats, config, llm_result)
        else:
            if config.push_enabled and config.sct_sendkey:
                push_alert(alerts, stats, config)
    else:
        log.info("No alerts triggered")

    save_brief(quotes, alerts, stats, BRIEF_DIR)
    print_tail(config.scan_interval)


def _run_monitoring_loop(config: Config,
                         north_fetcher: NorthFlowFetcher) -> None:
    """Monitoring main loop"""
    log.info(f"Entering monitor mode, scan every {config.scan_interval} minutes")
    log.info(f"Press Ctrl+C to return to menu")
    print()

    first_run = True
    try:
        while True:
            if _in_trading_hours(config):
                _run_once(config, north_fetcher)
            else:
                now = datetime.now()
                if first_run or now.minute % 15 == 0:
                    reason = "Weekend closed" if now.weekday() >= 5 else "Non-trading hours"
                    log.info(f"Paused: {reason} ({now.strftime('%H:%M')})")
                    first_run = False

            _wait_until_next_slot(config.scan_interval)

    except KeyboardInterrupt:
        log.info("Returning to menu...")


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
            _gen_report(rtype, config, north_fetcher)
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


if __name__ == "__main__":
    main()
