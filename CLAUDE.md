# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A real-time A-share market monitoring and analysis system ("盯盘雷达"). Monitors ETFs, indices, and stocks via Sina Finance API, analyzes anomalies with technical indicators, generates AI-driven commentary via DeepSeek, and pushes alerts to WeChat via ServerChan.

## Commands

```bash
# Run the application (interactive menu)
python __main__.py

# Install dependencies
pip install -e ".[dev]"

# Format / lint / type-check
black .
isort .
ruff check .
mypy app/

# Run tests
pytest                                    # all tests with coverage
pytest tests/test_config.py -v            # single test file

# Query a symbol's daily fund flow (via Miaoxiang / 妙想)
py .claude/skills/fund-flow/query_fund_flow.py <代码> [名称]

# Analyze a single stock (fundamental + K-line + fund flow + news data package)
py .claude/skills/stock-analysis/analyze_stock.py <代码> [名称] [天数]

# Analyze an ETF for grid trading (box range + volatility + grid params)
py .claude/skills/etf-grid/analyze_etf_grid.py <代码> [名称] [天数]

# Query sector fund-flow ranking (行业/概念/地域板块资金流，今日/5日/10日)
py .claude/skills/sector-flow/query_sector_flow.py [周期] [板块类型]

# Intraday decision (盘中决策：仓位 + 做T挂单)
py .claude/skills/intraday-signal/analyze_intraday.py <代码> [名称] [天数]
```

## Skills

- **`fund-flow`** — 查询单标的当日主力资金流入（主力/超大单/大单/中单/小单净流入，妙想 Miaoxiang）。用户问「某股票/ETF 今日资金流入」时使用，也可 `/fund-flow <代码>` 调用。需 `.env` 配置 `MX_APIKEY`。详见 `.claude/skills/fund-flow/SKILL.md`。
- **`stock-analysis`** — 个股诊股：用妙想拉取个股基本信息、近N日K线、近5日资金流、近7日资讯，算技术位（MA/高低点），再由 AI 生成走势预测与买卖参考。用户问「分析/诊断某只股票」「预测走势」「值不值得买」「给个买入价」时使用。详见 `.claude/skills/stock-analysis/SKILL.md`。
- **`etf-grid`** — ETF 网格交易分析：拉取 ETF 实时行情 + 日K线，诊断是否箱体震荡、是否适合自动网格，并给出网格区间/间距/格数/底仓/止损。用户问「某 ETF 值不值得买」「能不能网格」「网格怎么设」「是否箱体震荡」时使用。详见 `.claude/skills/etf-grid/SKILL.md`。
- **`sector-flow`** — 查询 A 股行业/概念/地域板块资金流排名（今日/5日/10日主力净流入，东方财富数据中心直连，无需妙想/`.env`）。用户问「各行业板块资金流向」「哪些板块主力流入最多」「板块资金排名」时使用。详见 `.claude/skills/sector-flow/SKILL.md`。
- **`intraday-signal`** — 盘中决策：输入单个标的（个股/ETF），拉实时快照 + 当日资金流 + 近N日K线趋势 + 近5日资金流 + 5分钟K线支撑压力，再由 AI 判断今天建仓/加仓/减仓/清仓/不动，以及是否适合做T、买卖挂单价。用户问「今天该买还是卖」「要不要加仓/减仓」「能不能做T」「做T挂多少钱」时使用。核心功能不依赖 `MX_APIKEY`。详见 `.claude/skills/intraday-signal/SKILL.md`。

## Configuration

The app reads from three sources (priority order):

1. **`.env`** — API keys: `DEEPSEEK_API_KEY`, `SCT_SENDKEY` (ServerChan), `MX_APIKEY` / `MX_APIKEY_2` (East Money, optional), `LLM_BASE_URL` (custom LLM endpoint), `LLM_VERIFY_SSL`
2. **`watchlist_config.json`** — thresholds, scan interval, LLM/push toggles, report schedules, T+0 monitoring, dynamic threshold settings
3. **`watchlist.csv`** / **`holdings.csv`** — preferred over the JSON arrays for watch items and holdings; CSV takes priority when both exist

`Config.__init__` loads JSON then CSV and validates. Missing a required key produces a `ConfigValidationError`.

## Architecture

Data flows through a **producer-consumer pattern** during monitor mode:

```
DataFetcherThread ──(writes)──▶ SharedDataPool ◀──(reads)──┬── _run_once_new() scan loop
                                                            └── T0MonitorThread (optional)
```

### Key Modules

| Module | Role |
|---|---|
| `app/config.py` | `Config` class — loads JSON+CSV+.env, type-safe property access, auto-validates |
| `app/models.py` | All dataclasses: `Quote`, `Alert`, `Holding`, `WatchItem`, `SentimentResult`, `AnalysisStats`, `TechnicalSummary`, `ScanRecord`, `DragonTigerRecord`, etc. |
| `app/data_fetcher.py` | Fetches real-time quotes from Sina Finance API, parses raw response strings into `Quote` objects. Also handles North-bound flow (东方财富) and background rich data cache. |
| `app/data_pool.py` | `SharedDataPool` — thread-safe dict for quotes + K-lines, with RLock |
| `app/data_fetcher_thread.py` | `DataFetcherThread` — daemon thread that polls quotes every N seconds, refreshes K-lines every 10th cycle |
| `app/analyzer.py` | Market sentiment scoring (0-100), dynamic threshold adjustment based on sentiment, anomaly detection (price change, volume, turnover, sector deviation, reversal) |
| `app/technical.py` | Pure-Python technical indicators: RSI, MACD, KDJ, Bollinger Bands, OBV, support/resistance (swing + pivot), ATR. Also `fetch_historical_kline()` via Sina with AKShare fallback. |
| `app/strategy.py` | Combination strategy engine — multi-indicator resonance signals (trend start, top escape, swing arbitrage, volume-price patterns). ~800 lines of signal logic. |
| `app/ai_analyzer.py` | Builds structured prompts for DeepSeek, calls `LLMClient`. Analyzes market status, volume validation, strategy signal interpretation, key technical levels. |
| `app/llm_client.py` | `LLMClient` — unified LLM wrapper with session reuse. Supports custom `LLM_BASE_URL` (Ollama, LM Studio, etc.) and SSL toggle. Contains `SYSTEM_PROMPTS` dict with 5 analyst personas. |
| `app/reporter.py` | Morning/midday/evening report generation via LLM, with dragon-tiger board analysis and holdings P&L. Outputs Markdown reports. |
| `app/presenter.py` | ANSI-colored console output, Markdown briefs to `monitoring_briefs/`, key level tables |
| `app/notifier.py` | ServerChan WeChat push + native desktop notifications (Win32 `Shell_NotifyIconW` / macOS `osascript`) |
| `app/t0_monitor.py` | `T0MonitorThread` — monitors support/resistance for T+0 (intraday swing) signal generation |
| `app/dragon_tiger.py` | Dragon-tiger board analysis via AKShare: institutional flow, hot money tracking, sector flow |
| `app/fund_analyzer.py` | Active fund analysis: ratings, style drift detection, manager review (via East Money Miaoxiang API) |
| `app/broker_api.py` | Auto-update holdings from broker account (东方财富) |
| `app/http_client.py` | `HttpClient` — requests.Session wrapper with connection pooling, retry, and SSL toggle. Pre-configured singletons: `sina_client`, `eastmoney_client`, `serverchan_client`, `llm_client`. |
| `app/helpers.py` | Business-logic helpers: market auto-detection by code prefix, CSV validation, time-range checks, trading-time detection |
| `app/utils.py` | Generic utilities: logging, `.env` loading, directory creation, number formatting |

### Entry Point (`__main__.py`)

Interactive menu (options 1-7 + 0):
- **1-3**: Generate and push investment reports (morning/midday/evening) via `reporter.py`
- **4**: Monitor mode — spawns `DataFetcherThread` + optional `T0MonitorThread`, runs scan loop calling `_run_once_new()` which reads from `SharedDataPool`
- **5**: Fund analysis
- **6**: Auto-update holdings from broker
- **7**: Query period returns for a specific stock/fund

Monitor mode scans every N minutes (configurable, default 15). LLM is called every other scan (even scan numbers) to limit API costs. Trading-time detection via `helpers.is_trading_time()` with configurable sessions.

### State Persistence

- **`state/market_state.json`** — per-code volume snapshots for cross-scan volume comparison
- **`state/scan_history.json`** — `ScanRecord` list with full `FundScanStatus` and `TechnicalSummary` snapshots, used for technical signal change detection

## Key Design Decisions

- **No numpy/pandas** — all technical indicators are pure Python to keep dependencies minimal
- **CSV-first** for watchlist and holdings — `Config` prefers CSV over JSON arrays; CSV is easier to edit manually
- **Data source**: Sina Finance (quotes) + East Money (north flow, stock flow, dragon-tiger) with Tencent fallback for turnover/volume ratio
- **AKShare** is an optional dependency (`pip install akshare`) used for dragon-tiger board and K-line fallback
- **LLM_BASE_URL** env var allows swapping DeepSeek for any OpenAI-compatible endpoint (local models, proxies)
- **`_run_once()`** (legacy, direct fetch) vs **`_run_once_new()`** (shared pool, currently used) — the old path still exists for backward compatibility
