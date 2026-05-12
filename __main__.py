#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import signal
import logging
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Config
from app.data_fetcher import NorthFlowFetcher
from app.reporter import generate_morning_brief, generate_midday_review, generate_evening_review
from app.fund_analyzer import analyze_funds
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
    print(f"  {Color.CYAN}0{Color.RESET}. Exit")
    print()

    while True:
        try:
            choice = input(f" Enter option [0-5]: ").strip()
            if choice in ("0", "1", "2", "3", "4", "5"):
                return choice
            print(f"{Color.YELLOW}  Please enter 0-5{Color.RESET}")
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

    log.info("Scanning market data...")

    quotes = fetch_quotes(config.watch_items)
    if not quotes:
        log.warning("No quote data received")
        return

    print_quotes_table(quotes)

    result = analyze(quotes, config)
    print_sentiment(result.stats)

    if result.alerts:
        print_alerts(result.alerts)

        if config.llm_enabled and config.deepseek_key:
            llm_result = analyze_with_llm(result, quotes, config)
            print_llm_result(llm_result)

            if config.push_enabled and config.sct_sendkey:
                for alert in result.alerts:
                    push_alert(alert, config, llm_result)
        else:
            if config.push_enabled and config.sct_sendkey:
                for alert in result.alerts:
                    push_alert(alert, config)
    else:
        log.info("No alerts triggered")

    save_brief(result, quotes, config, BRIEF_DIR)
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


if __name__ == "__main__":
    main()
