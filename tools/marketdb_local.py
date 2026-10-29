#!/usr/bin/env python3
"""同花顺本地 marketdb（DuckDB 落盘）—— 脚本级研究工具，减历史行情远端请求。

把同花顺「全市场 10 年日K + 复权事件」Parquet 落盘到本地 DuckDB，之后日K/面板
查询走本地 SQL，不再逐次打远端历史接口。**仅脚本级引入，不进 app/ 核心**：
依赖 pandas/pyarrow/duckdb + Proprietary license，与项目「app/ 核心 no numpy/pandas、
依赖最小化」决策冲突。

子命令:
    bootstrap           安装 marketdb + 建库 + 全量同步（3 次 Parquet 请求）
    daily <thscode>     读单只/批量本地日K（--start/--end/--adjust，批量逗号分隔）
    panel               读全市场日K面板并落盘 CSV（--start/--end/--out）
    symbols             列出本地证券表（可按 --exchange/--asset-type 过滤）
    status              查看库状态（各表行数 + 最大日期）
    sync                增量同步：只跑 auto-sync（自动判断 skip/incremental/full），不装依赖不重建库
    sync-symbols        刷新 dim_symbol 证券维度表（symbols 子命令依赖）
    sync-financials     三张报表落库（利润/资产负债/现金流量，同花顺 REST，非 marketdb Parquet）
    sync-valuation      估值快照逐日累积（PE/PB/PS/PCF，同花顺 REST，历史分位需每日积累）
    sync-valuation-history  估值历史回填（东财 akshare，PE/PB/PS/PCF 自上市起全历史，补历史分位）
    streaks             全市场「连续上涨/下跌/放量/缩量」区间扫描（窗口函数三步法）
    screen-golden-pit   全市场黄金坑技术初筛（回撤20~45% + 站半年线 + 缩量，单条 SQL）
    screen-bottoming    全市场磨底技术初筛（距低点≤8% + 磨底≥10日 + 箱宽≤20% + 缩量，单条 SQL）

用法:
    py tools/marketdb_local.py bootstrap
    py tools/marketdb_local.py daily 600519.SH --adjust forward --start 2025-01-01
    py tools/marketdb_local.py panel --start 2026-01-01 --end 2026-01-31 --out out/panel.csv

数据源: 同花顺 marketdb 包（pip install -e <Financial-API/python>；源码目录用 MARKETDB_SRC
环境变量或 .env 配置，默认 C:/work/code/Financial-API/python）。
API Key 走环境变量 HITHINK_FINANCE_API_KEY（snowball .env 已配置，自动注入）。
库文件默认 data/market.duckdb（可用环境变量 MARKETDB_DB_PATH 覆盖）。
跨环境: data/market.duckdb 已 gitignore，新环境需 bootstrap 落库（子命令缺库时会打印完整引导）。

注意: marketdb 只覆盖历史行情/面板，**不覆盖** trading-days / limit-up-pool /
dragon-tiger-list 等特色数据实时端点 —— 涨跌停/龙虎榜/日历仍走 app/hithink.py REST。
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（tools/marketdb_local.py 的上一级）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# marketdb 默认源码目录（同花顺 Financial-API monorepo）
_DEFAULT_FINANCE_PY = "C:/work/code/Financial-API/python"
_DEFAULT_DB = _ROOT / "data" / "market.duckdb"


def _finance_py() -> Path:
    """marketdb 源码目录：优先 MARKETDB_SRC 环境变量（.env 或系统），否则用默认本机路径。

    必须在 _load_key()（load_env 注入 .env 变量）之后调用，否则 .env 里的 MARKETDB_SRC 不生效。
    """
    return Path(os.environ.get("MARKETDB_SRC", _DEFAULT_FINANCE_PY))


def _db_path(explicit: str | None = None) -> Path:
    p = explicit or os.environ.get("MARKETDB_DB_PATH")
    return Path(p).expanduser() if p else _DEFAULT_DB


def market_db_path(db=None) -> Path:
    """本地库 DuckDB 文件路径（对外公开，供 skill 直接 duckdb.connect 读 raw 表）。"""
    return _db_path(db)


def _load_key() -> None:
    """把 snowball .env 里的 HITHINK_FINANCE_API_KEY 注入 os.environ（marketdb 认这个变量）。"""
    try:
        from app.utils import load_env
        load_env(_ROOT)
    except Exception:
        pass


def _ensure_installed() -> bool:
    """marketdb 可导入则跳过，否则 pip install -e（含 duckdb/pyarrow/pandas 等重依赖）。"""
    try:
        import marketdb  # noqa: F401
        return True
    except ImportError:
        pass
    src = _finance_py()
    if not src.exists():
        print(f"❌ 未找到 marketdb 源码目录: {src}")
        print("   请在 .env 或系统环境变量设置 MARKETDB_SRC 指向 Financial-API/python，或 clone 到默认路径。")
        return False
    print(f"==> 安装 marketdb（pip install -e {src}）...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(src)])
    return r.returncode == 0


def _cli(*args: str, db: Path) -> subprocess.CompletedProcess:
    """调用 marketdb CLI（python -m marketdb.cli），继承环境变量（含 key）。

    提高 Parquet dump 下载重试次数，缓解连续两次大下载时的全局限流(429)。
    """
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("MARKETDB_DUMP_DOWNLOAD_RETRIES", "3")
    return subprocess.run(
        [sys.executable, "-m", "marketdb.cli", *args, "--db", str(db)], env=env
    )


def _norm_thscode(code: str) -> str:
    """补全交易所后缀：600519 -> 600519.SH。已带后缀则原样返回。"""
    c = code.strip()
    if "." in c:
        return c
    try:
        from app.hithink import thscode
        return thscode(c)
    except Exception:
        pass
    if c[:2] in ("60", "68", "51", "56", "58", "90", "11", "50"):
        return f"{c}.SH"
    if c[:2] in ("00", "30", "15", "16", "18", "12", "39"):
        return f"{c}.SZ"
    if c[:1] in ("8", "4", "92"):
        return f"{c}.BJ"
    return c


def _bootstrap_guide() -> str:
    """新环境本地库缺失时的落库引导（data/market.duckdb 已 gitignore，跨环境需手动落库）。"""
    _load_key()  # 确保 .env 的 MARKETDB_SRC 已注入，引导文案准确（幂等）
    src = _finance_py()
    return f"""本地库不存在（data/market.duckdb 已 gitignore，不进 git，新环境需手动落库一次）：

  1. 前置条件
     - .env 配置 HITHINK_FINANCE_API_KEY（下载 Parquet 需要）
     - marketdb 源码目录存在（当前读取: {src}）
       缺失时在 .env 或系统环境变量设置 MARKETDB_SRC 指向 Financial-API/python

  2. 落库（装 marketdb + 建库 + 全量同步）
     py tools/marketdb_local.py bootstrap

  3. 补证券维度表（bootstrap 的 auto-sync 不自动同步 dim_symbol）
     py tools/marketdb_local.py sync-symbols

  注意: 全量同步连续下载两个大 Parquet 易触发同花顺全局限流(429)，
        失败等 1~2 分钟重跑 bootstrap 即可（auto-sync 会续传，不重复下）。"""


# ---------------------------------------------------------------- 子命令

def cmd_bootstrap(args) -> int:
    _load_key()
    if not _ensure_installed():
        return 1
    db = _db_path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)

    print(f"==> 初始化 DuckDB: {db}")
    r = _cli("init", db=db)
    if r.returncode != 0:
        print("❌ 初始化失败")
        return r.returncode

    print("==> 全量同步（下载 daily-k + adjustment-factors 两个 Parquet 并入库，约 945 万行）")
    sync_args = ["auto-sync"]
    if args.force:
        sync_args.append("--force")
    if args.keep_cache:
        sync_args.append("--keep-cache")
    r = _cli(*sync_args, db=db)
    if r.returncode != 0:
        print(f"❌ 同步失败（exit {r.returncode}）")
        return r.returncode

    print("==> 状态")
    _cli("status", db=db)
    _validate(db)
    print(f"\n✅ 完成。本地库: {db}")
    print("   示例: py tools/marketdb_local.py daily 600519.SH --adjust forward")
    return 0


def cmd_daily(args) -> int:
    if not args.codes:
        print("用法: py tools/marketdb_local.py daily <thscode> [--start] [--end] [--adjust]")
        return 2
    _load_key()
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    try:
        from marketdb import MarketDB
    except ImportError:
        print("❌ marketdb 未安装（先运行 bootstrap）")
        return 1
    codes = [_norm_thscode(c) for c in args.codes.split(",")]
    with MarketDB.open(db) as mdb:
        df = mdb.get_daily(codes, start=args.start, end=args.end, adjust=args.adjust)
    if df is None or df.empty:
        print(f"⚠️ 本地无 {args.codes} 在 [{args.start or '-∞'}, {args.end or '+∞'}] 的日K")
        return 0
    cols = ["thscode", "date", "open", "high", "low", "close", "volume", "turnover"]
    cols = [c for c in cols if c in df.columns]
    print(f"共 {len(df)} 行（adjust={args.adjust}）:")
    print(df[cols].to_string(index=False, max_rows=40))
    return 0


def cmd_panel(args) -> int:
    _load_key()
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    try:
        from marketdb import MarketDB
    except ImportError:
        print("❌ marketdb 未安装（先运行 bootstrap）")
        return 1
    with MarketDB.open(db) as mdb:
        df = mdb.get_panel(start=args.start, end=args.end, adjust=args.adjust)
    if df is None or df.empty:
        print(f"⚠️ 面板 [{args.start or '-∞'}, {args.end or '+∞'}] 无数据")
        return 0
    out = Path(args.out) if args.out else _ROOT / "out" / f"panel_{args.start or 'all'}_{args.end or 'all'}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"✅ 面板 {len(df)} 行已落盘: {out}")
    print(f"   日期范围 {df['date'].min()} ~ {df['date'].max()}，证券数 {df['thscode'].nunique()}")
    return 0


def cmd_symbols(args) -> int:
    _load_key()
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    try:
        from marketdb import MarketDB
    except ImportError:
        print("❌ marketdb 未安装（先运行 bootstrap）")
        return 1
    with MarketDB.open(db) as mdb:
        df = mdb.get_symbols(exchange=args.exchange, asset_type=args.asset_type)
    if df is None or df.empty:
        print("⚠️ 本地证券表为空")
        return 0
    print(f"共 {len(df)} 只:")
    print(df.head(args.limit).to_string(index=False))
    if len(df) > args.limit:
        print(f"... 省略 {len(df) - args.limit} 只 ...")
    return 0


def cmd_status(args) -> int:
    _load_key()
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    _cli("status", db=db)
    return 0


def cmd_sync_symbols(args) -> int:
    """刷新 dim_symbol 证券维度表（一次 REST tickers 请求，symbols 子命令依赖它）。"""
    _load_key()
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    r = _cli("sync-symbols", db=db)
    return r.returncode


def cmd_sync(args) -> int:
    """增量同步：只跑 auto-sync（自动判断 skip/incremental/full），不装依赖、不重建库。"""
    return sync_db(args.db)


# ---------------------------------------------------------------- 三张报表落库

_FIN_COMMON = [("thscode", "VARCHAR"), ("ticker", "VARCHAR"), ("fiscal_year", "INTEGER"),
               ("fiscal_period", "VARCHAR"), ("period_end", "VARCHAR"), ("report_date", "VARCHAR")]

# 三张报表各自数值列（金额单位为元，每股类为元/股；DOUBLE 足够容纳亿元级精确值）
_FIN_NUMS = {
    "fin_income": ["operating_income", "operating_costs", "operating_expenses", "operating_profit",
                   "profit_total", "net_profit", "parent_holder_net_profit", "basic_eps",
                   "income_tax_expense", "interest_expenses", "sales_fee", "manage_fee",
                   "research_and_development_expenses"],
    "fin_balance": ["assets_total", "total_debt", "holder_equity_total", "total_current_assets",
                    "non_current_nets_total", "cash", "accounts_receivable"],
    "fin_cashflow": ["act_cash_flow_net", "invest_cash_flow_net", "financing_cash_flow_net",
                     "cash_equivalents_net_addition", "pay_dividends_profits_interest_cash",
                     "pay_fixed_assets_etc_cash"],
}


def _fin_num(v):
    """数值列安全转换：None/空串/NaN → None，否则 float。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN（akshare/东财缺值）→ None


def _ensure_fin_tables(con) -> None:
    """建三张报表表（幂等）。主键语义 (thscode, period_end)，落库时按 thscode 删旧插新。"""
    for table, nums in _FIN_NUMS.items():
        cols = [f"{n} {t}" for n, t in _FIN_COMMON] + [f"{n} DOUBLE" for n in nums]
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)})")


def _sync_fin_one(con, hithink, ticker: str, period: str, keep: int) -> int:
    """同步一只股票的三张报表，返回落库总条数（任一表失败不影响其它表）。"""
    total = 0
    for table, fn in (("fin_income", hithink.fetch_income_statements),
                      ("fin_balance", hithink.fetch_balance_sheets),
                      ("fin_cashflow", hithink.fetch_cash_flow_statements)):
        try:
            items = fn(ticker, "", period=period, limit=keep)
        except Exception:
            items = []
        if not items:
            continue
        thscode_s = items[0].get("thscode") or _norm_thscode(ticker)
        con.execute(f"DELETE FROM {table} WHERE thscode = ?", [thscode_s])
        rows = [[thscode_s, ticker, it.get("fiscal_year"), it.get("fiscal_period"),
                 it.get("period_end"), it.get("report_date")]
                + [_fin_num(it.get(n)) for n in _FIN_NUMS[table]]
                for it in items]
        cols = [n for n, _ in _FIN_COMMON] + _FIN_NUMS[table]
        con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in cols)})", rows)
        total += len(rows)
    return total


def cmd_sync_financials(args) -> int:
    """三张报表落库：利润/资产负债/现金流量表（同花顺 Financial-API，季度/年度多期）。

    默认同步前 100 只 A 股（约 300 次请求），--all 全市场、--codes 指定代码。
    幂等：按 thscode 删旧插新，重跑自愈。
    """
    import time

    _load_key()
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    try:
        import duckdb
    except ImportError:
        print("❌ duckdb 未安装（先运行 bootstrap 安装 marketdb）")
        return 1
    try:
        from app import hithink
    except Exception:
        print("❌ 无法导入 app.hithink")
        return 1
    if not os.environ.get("HITHINK_FINANCE_API_KEY"):
        print("❌ 未配置 HITHINK_FINANCE_API_KEY（.env 中设置）")
        return 1

    con = duckdb.connect(str(db))
    _ensure_fin_tables(con)

    if args.codes:
        tickers = [c.strip().split(".", 1)[0] for c in args.codes.split(",") if c.strip()]
    else:
        tickers = [r[0] for r in con.execute(
            "SELECT ticker FROM dim_symbol WHERE asset_type='a-share' ORDER BY ticker").fetchall()]
        if not args.all:
            if args.limit and args.limit > 0:
                tickers = tickers[:args.limit]

    if not tickers:
        print("⚠️ 无目标代码（dim_symbol 为空？先跑 sync-symbols）")
        con.close()
        return 0

    print(f"==> 三张报表落库：{len(tickers)} 只（period={args.period}，每只保留 {args.keep} 期）")
    ok = 0
    for i, tk in enumerate(tickers, 1):
        n = _sync_fin_one(con, hithink, tk, args.period, args.keep)
        ok += 1 if n > 0 else 0
        if i % 50 == 0 or i == len(tickers):
            print(f"    进度 {i}/{len(tickers)}，成功 {ok} 只", flush=True)
        if args.sleep:
            time.sleep(args.sleep)
    con.close()

    con = duckdb.connect(str(db), read_only=True)
    for t in _FIN_NUMS:
        cnt = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        mx = con.execute(f"SELECT MAX(period_end) FROM {t}").fetchone()[0]
        print(f"  {t}: {cnt} 行，最新报告期 {mx}")
    con.close()
    print(f"✅ 完成：成功落库 {ok}/{len(tickers)} 只")
    return 0


# ---------------------------------------------------------------- 估值逐日累积

_VAL_NUMS = ["pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm"]
# 东财个股估值（akshare stock_value_em）源列名，顺序与 _VAL_NUMS 一一对应
_VAL_SRC_COLS = ["PE(TTM)", "PE(静)", "市净率", "市销率", "市现率"]
_VAL_DATE_COL = "数据日期"


def _ensure_valuation_table(con) -> None:
    cols = ["thscode VARCHAR", "ticker VARCHAR", "name VARCHAR", "trade_date VARCHAR"] + \
           [f"{n} DOUBLE" for n in _VAL_NUMS]
    con.execute(f"CREATE TABLE IF NOT EXISTS valuation_daily ({', '.join(cols)})")
    # 索引：回填按 thscode 删旧插新、每日增量按 trade_date 删旧，避免全表扫描
    con.execute("CREATE INDEX IF NOT EXISTS idx_valuation_thscode ON valuation_daily(thscode)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_valuation_trade_date ON valuation_daily(trade_date)")


def cmd_sync_valuation(args) -> int:
    """估值快照逐日累积（同花顺 fetch_valuations_snapshot，批量 100/次）。

    估值无历史回填，历史分位只能从今天起每日收盘后跑一次累积。幂等：按 trade_date 删旧插新，
    当日重跑自愈（覆盖当天已落库的）。估值允许 null（未披露）或负数（亏损/负现金流），原样保留。
    trade_date 取同花顺最近交易日（非 date.today()），周末/节假日重跑会覆盖上一交易日快照，不产生脏日期。
    """
    import time as _time

    _load_key()
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    try:
        import duckdb
    except ImportError:
        print("❌ duckdb 未安装（先运行 bootstrap 安装 marketdb）")
        return 1
    try:
        from app import hithink
    except Exception:
        print("❌ 无法导入 app.hithink")
        return 1
    if not os.environ.get("HITHINK_FINANCE_API_KEY"):
        print("❌ 未配置 HITHINK_FINANCE_API_KEY（.env 中设置）")
        return 1

    con = duckdb.connect(str(db))
    _ensure_valuation_table(con)

    if args.codes:
        tickers = [c.strip().split(".", 1)[0] for c in args.codes.split(",") if c.strip()]
    else:
        tickers = [r[0] for r in con.execute(
            "SELECT ticker FROM dim_symbol WHERE asset_type='a-share' ORDER BY ticker").fetchall()]
        if not args.all:
            if args.limit and args.limit > 0:
                tickers = tickers[:args.limit]

    if not tickers:
        print("⚠️ 无目标代码（dim_symbol 为空？先跑 sync-symbols）")
        con.close()
        return 0

    trade_date = _latest_trade_day_str()
    con.execute("DELETE FROM valuation_daily WHERE trade_date = ?", [trade_date])

    batch = max(1, min(args.batch, 100))  # 接口一次最多 100 只
    n_batch = (len(tickers) + batch - 1) // batch
    print(f"==> 估值快照落库：{len(tickers)} 只（{n_batch} 批，trade_date={trade_date}）")
    ok = 0
    skipped: list[str] = []
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            items = hithink.fetch_valuations_snapshot(chunk)
        except Exception:
            items = []
        if not items:
            # 整批失败（多为批内混入退市/*ST 等已从 API 代码表剔除的代码，接口按整批 3001 拒绝）。
            # 退化为逐只请求：跳过查不到的代码，避免一只坏码拖垮同批其余 99 只。
            items = []
            for tk in chunk:
                try:
                    one = hithink.fetch_valuations_snapshot([tk])
                except Exception:
                    one = []
                if one:
                    items.extend(one)
                else:
                    skipped.append(tk)
                    print(f"    ⚠️ 跳过 {tk}（同花顺代码表查不到，可能已退市）", flush=True)
        rows = []
        for it in items or []:
            thscode_s = it.get("thscode") or _norm_thscode(it.get("ticker") or "")
            rows.append([thscode_s, it.get("ticker") or "", it.get("name") or "", trade_date]
                        + [_fin_num(it.get(n)) for n in _VAL_NUMS])
        if rows:
            placeholders = ",".join("?" for _ in range(4 + len(_VAL_NUMS)))
            con.executemany(f"INSERT INTO valuation_daily VALUES ({placeholders})", rows)
            ok += len(rows)
        if args.sleep:
            _time.sleep(args.sleep)
    con.close()

    con = duckdb.connect(str(db), read_only=True)
    cnt = con.execute("SELECT COUNT(*) FROM valuation_daily WHERE trade_date = ?", [trade_date]).fetchone()[0]
    days = con.execute("SELECT COUNT(DISTINCT trade_date) FROM valuation_daily").fetchone()[0]
    con.close()
    print(f"  今日落库 {cnt} 条，累计 {days} 个交易日")
    if skipped:
        print(f"  ⚠️ 跳过 {len(skipped)} 只（同花顺代码表查不到，多为退市/*ST）：{', '.join(skipped)}")
    print(f"✅ 完成：成功落库 {ok}/{len(tickers)} 只")
    return 0


def cmd_sync_valuation_history(args) -> int:
    """估值历史回填（东财数据中心 akshare stock_value_em，逐日 PE/PB/PS/PCF 自上市起全历史）。

    同花顺 fetch_valuations_snapshot 只有当前快照、无历史，历史分位原本靠妙想（已配额用尽）或
    valuation_daily 数月累积。本命令用东财逐日估值一次性补全历史，使 fundamental_local.
    valuation_percentile 的本地分位立刻可用（分位只需 pe_ttm/pb_mrq，二者东财直接提供）。
    免费、无需 key（区别于妙想）。幂等：按 thscode 删旧插新；--resume 跳过已回填，断点续传。
    """
    import time

    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    try:
        import duckdb
    except ImportError:
        print("❌ duckdb 未安装（先运行 bootstrap 安装 marketdb）")
        return 1
    try:
        import akshare as ak
        import pandas as pd
    except ImportError:
        print("❌ akshare 未安装（pip install akshare）")
        return 1

    con = duckdb.connect(str(db))
    _ensure_valuation_table(con)

    sym_rows = con.execute(
        "SELECT ticker, thscode, name FROM dim_symbol WHERE asset_type='a-share' ORDER BY ticker").fetchall()
    sym_map = {r[0]: (r[1], r[2]) for r in sym_rows}

    if args.codes:
        tickers = [c.strip().split(".", 1)[0] for c in args.codes.split(",") if c.strip()]
    else:
        tickers = [r[0] for r in sym_rows]
        if args.limit and args.limit > 0:
            tickers = tickers[:args.limit]

    if not tickers:
        print("⚠️ 无目标代码（dim_symbol 为空？先跑 sync-symbols）")
        con.close()
        return 0

    # 断点续传：跳过已回填的股票（--resume 时行数 >= --resume-min 视为已回填）
    todo: list[str] = []
    skipped = 0
    for tk in tickers:
        ths = sym_map.get(tk, (_norm_thscode(tk), ""))[0]
        if args.resume:
            n = con.execute("SELECT COUNT(*) FROM valuation_daily WHERE thscode = ?", [ths]).fetchone()[0]
            if n >= args.resume_min:
                skipped += 1
                continue
        todo.append(tk)

    est_min = len(todo) * (args.sleep + 0.7) / 60
    print(f"==> 估值历史回填（东财 akshare stock_value_em）：{len(todo)} 只，预计 ~{est_min:.0f} 分钟"
          + (f"（已回填跳过 {skipped} 只）" if skipped else ""))
    ok = 0
    fail: list[str] = []
    t0 = time.time()
    total_rows = 0
    for i, tk in enumerate(todo, 1):
        ths, name = sym_map.get(tk, (_norm_thscode(tk), ""))
        try:
            df = ak.stock_value_em(symbol=tk)
        except Exception as e:
            fail.append(tk)
            print(f"  ⚠️ [{i}/{len(todo)}] {tk} 拉取失败: {e}", flush=True)
            if args.sleep:
                time.sleep(args.sleep)
            continue
        if df is None or len(df) == 0:
            fail.append(tk)
            print(f"  ⚠️ [{i}/{len(todo)}] {tk} 无数据（可能已退市/北交所不覆盖）", flush=True)
            if args.sleep:
                time.sleep(args.sleep)
            continue

        con.execute("DELETE FROM valuation_daily WHERE thscode = ?", [ths])
        # pandas register + INSERT SELECT（DuckDB 零拷贝快路径）；executemany 逐行慢约 300 倍
        ins = pd.DataFrame({
            "thscode": ths, "ticker": tk, "name": name,
            "trade_date": df[_VAL_DATE_COL].astype(str).str[:10],
        })
        for dst, src in zip(_VAL_NUMS, _VAL_SRC_COLS):
            ins[dst] = pd.to_numeric(df[src], errors="coerce")
        con.register("df_ins", ins)
        con.execute("INSERT INTO valuation_daily SELECT * FROM df_ins")
        con.unregister("df_ins")
        total_rows += len(ins)
        ok += 1
        print(f"  [{i}/{len(todo)}] {tk} {name} → {len(ins)} 行", flush=True)
        if args.sleep:
            time.sleep(args.sleep)

    con.close()

    con = duckdb.connect(str(db), read_only=True)
    tot = con.execute("SELECT COUNT(*) FROM valuation_daily").fetchone()[0]
    nsym = con.execute("SELECT COUNT(DISTINCT thscode) FROM valuation_daily").fetchone()[0]
    con.close()
    print(f"✅ 完成：成功 {ok}/{len(todo)} 只（失败 {len(fail)}），本次新增 {total_rows} 行，"
          f"估值表累计 {tot} 行 / {nsym} 只，耗时 {time.time()-t0:.0f}s")
    if fail:
        tail = fail[:50] + (["..."] if len(fail) > 50 else [])
        print(f"  ⚠️ 失败 {len(fail)} 只（重跑加 --resume 自动跳过已成功的）：{', '.join(tail)}")
    return 0


# ---------------------------------------------------------------- streaks

_STREAK_LABEL = {
    "up": "连续上涨",
    "down": "连续下跌",
    "vol-up": "连续放量",
    "vol-down": "连续缩量",
    "vol-price-up": "量价齐升",
    "vol-price-down": "量价齐缩",
    "above-ma": "连续站稳均线",
    "new-high": "连续创新高",
}

# kind -> 断点条件（True=断点，即中断当前连续段）。above-ma/new-high 的断点引用 px 里的
# 窗口列（ma / prev_max），由 query_streaks 按 --ma / --window 动态生成，不在此表里。
_STREAK_BREAK = {
    "up": "prev IS NULL OR adj <= prev",
    "down": "prev IS NULL OR adj >= prev",
    "vol-up": "prev_vol IS NULL OR vol <= prev_vol",
    "vol-down": "prev_vol IS NULL OR vol >= prev_vol",
    "vol-price-up": "prev IS NULL OR NOT (adj > prev AND vol > prev_vol)",
    "vol-price-down": "prev IS NULL OR NOT (adj < prev AND vol < prev_vol)",
}


def _streak_spec(kind: str, val_expr: str, ma: int = 20, window: int = 20) -> tuple[str, str, str]:
    """返回 (px_extra, px_col, brk) —— px 里需额外计算的窗口列、列名、断点条件。

    above-ma/new-high 需要窗口列（AVG/MAX），其余 kind 直接查 _STREAK_BREAK。
    px_extra 里的窗口函数引用 px 源表列 k.thscode/k.date 与 val_expr。
    """
    if kind == "above-ma":
        ma_n = int(ma)
        px_extra = (f"AVG({val_expr}) OVER (PARTITION BY k.thscode ORDER BY k.date "
                    f"ROWS BETWEEN {ma_n - 1} PRECEDING AND CURRENT ROW) AS ma")
        return px_extra, "ma", "ma IS NULL OR adj < ma"
    if kind == "new-high":
        win_n = int(window)
        px_extra = (f"MAX({val_expr}) OVER (PARTITION BY k.thscode ORDER BY k.date "
                    f"ROWS BETWEEN {win_n} PRECEDING AND 1 PRECEDING) AS prev_max")
        return px_extra, "prev_max", "prev_max IS NULL OR adj <= prev_max"
    return "", "", _STREAK_BREAK[kind]


def _board_of(code) -> str:
    """6 位代码 → 板块（主板/创业板/科创板/北交所），供回测分组用。"""
    c = str(code).zfill(6)
    if c.startswith("68"):
        return "科创板"
    if c.startswith("30"):
        return "创业板"
    if c.startswith(("4", "8", "92")):
        return "北交所"
    return "主板"


def query_streaks(kind: str, min_days: int = 3, start: str | None = None,
                  end: str | None = None, end_after: str | None = None,
                  codes: list[str] | None = None,
                  limit: int = 20, db=None, adjust: str = "forward",
                  ma: int = 20, window: int = 20):
    """全市场/指定代码池「连续区间」扫描，返回 pandas DataFrame。

    窗口函数三步法（断点标记→累计求和→分组聚合）：LAG() 取前一行（above-ma/new-high 取窗口
    列）→ CASE 标记断点 → SUM() OVER() 生成组号 → GROUP BY 组号聚合出每段起止日期与持续天数。

    返回列: thscode / name / start_date / end_date / streak_days / start_val / end_val
    （量/涨跌/均线/新高类 start_val/end_val 为前复权价，可另算 total_pct）。无库/无 duckdb
    返回 None，无匹配返回空 DataFrame。

    语义（已定死，供 skill 批量初筛引用）:
      - 涨跌/站稳/新高判定用前复权价 close*forward_factor（除权除息日不误判断点）；adjust='none' 用未复权收盘。
      - 「连续」指连续有交易行：停牌日无行、既不累计也不中断（按有行日连算）。
      - 每段区间 = 该证券一条连续满足条件的交易日序列；同一证券可有多段（断点分界）。
      - vol-price-up/down = 量价齐升/齐缩（当日价与量同向，二者需同时成立）。
      - above-ma：adj >= MA{ma} 即「站稳」，跌破即断点；MA 为窗口 AVG（ROWS N-1 PRECEDING）。
      - new-high：adj > 前 {window} 日最高价即「创新高」，否则断点（窗口 MAX，排除当日）。
      - above-ma/new-high 首 N-1 行窗口不完整用部分值（对「最近仍在状态」的近端初筛无影响）。
    """
    db_path = _db_path(db)
    if not db_path.exists():
        return None
    try:
        import duckdb
    except ImportError:
        return None

    price_based = kind not in ("vol-up", "vol-down")
    val_expr = "k.close" if adjust == "none" else "k.close * COALESCE(a.forward_factor, 1.0)"
    px_extra, px_col, brk = _streak_spec(kind, val_expr, ma, window)

    where, params = [], []
    if codes:
        norms = [_norm_thscode(c) for c in codes]
        where.append("k.thscode IN (%s)" % ",".join("?" for _ in norms))
        params.extend(norms)
    if start:
        where.append("k.date >= ?")
        params.append(start)
    if end:
        where.append("k.date <= ?")
        params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # end_after 过滤「结束日 ≥ X」的已完成区间（聚合后 HAVING，非输入日期边界），
    # 供初筛/回测做近端过滤：保证短近的区间（如刚开始跌 2~3 天）不被 ORDER BY+LIMIT 截掉。
    having_extra = ""
    if end_after:
        having_extra = " AND MAX(g.date) >= ?"
        params.append(end_after)

    agg = (
        "arg_min(adj, date) AS start_val, arg_max(adj, date) AS end_val"
        if price_based
        else "arg_min(vol, date) AS start_val, arg_max(vol, date) AS end_val"
    )

    lagged_extra = f", {px_col}" if px_col else ""
    sql = f"""
WITH px AS (
  SELECT k.thscode, k.date, {val_expr} AS adj, k.volume AS vol{"," + px_extra if px_extra else ""}
  FROM raw_kline_daily k
  LEFT JOIN calc_adjust_factor_daily a ON a.thscode = k.thscode AND a.date = k.date
  {where_sql}
),
lagged AS (
  SELECT thscode, date, adj, vol{lagged_extra},
         LAG(adj) OVER (PARTITION BY thscode ORDER BY date) AS prev,
         LAG(vol) OVER (PARTITION BY thscode ORDER BY date) AS prev_vol
  FROM px
),
flagged AS (
  SELECT thscode, date, adj, vol,
         CASE WHEN {brk} THEN 1 ELSE 0 END AS brk
  FROM lagged
),
grouped AS (
  SELECT thscode, date, adj, vol, brk,
         SUM(brk) OVER (PARTITION BY thscode ORDER BY date ROWS UNBOUNDED PRECEDING) AS gid
  FROM flagged
)
SELECT g.thscode, s.name, MIN(g.date) AS start_date, MAX(g.date) AS end_date,
       COUNT(*) AS streak_days, {agg}
FROM grouped g
LEFT JOIN dim_symbol s ON s.thscode = g.thscode
WHERE g.brk = 0
GROUP BY g.thscode, s.name, g.gid
HAVING COUNT(*) >= {int(min_days)}{having_extra}
ORDER BY streak_days DESC, g.thscode, start_date
LIMIT {int(limit)}
"""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()
    return df


def db_max_date(db=None) -> str | None:
    """本地库 raw_kline_daily 最大日期（YYYY-MM-DD），无库/无行/无 duckdb 返回 None。"""
    db_path = _db_path(db)
    if not db_path.exists():
        return None
    try:
        import duckdb
    except ImportError:
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            row = con.execute("SELECT MAX(date) FROM raw_kline_daily").fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    d = row[0]
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)


def _latest_trade_day_str() -> str:
    """最近交易日 YYYY-MM-DD（app.hithink.fetch_trade_days 优先，退回今天/上一工作日）。"""
    try:
        from app import hithink
        days = hithink.fetch_trade_days()
        if days:
            today = datetime.date.today().strftime("%Y%m%d")
            past = [d for d in days if d <= today]
            d = (past or days)[-1]
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    except Exception:
        pass
    today = datetime.date.today()
    while today.weekday() >= 5:
        today -= datetime.timedelta(days=1)
    return today.strftime("%Y-%m-%d")


def db_freshness(db=None) -> dict:
    """本地库新鲜度: {max_date, latest_trade_date, behind_days, stale}。

    max_date=None 表示本地库不存在/为空。behind_days 为自然日差（含周末/节假日），
    仅作「是否落后于最近交易日」的粗判；盘中当日数据本来就不会在库（收盘后才同步）。
    """
    maxd = db_max_date(db)
    latest = _latest_trade_day_str()
    r = {"max_date": maxd, "latest_trade_date": latest, "behind_days": 0, "stale": False}
    if maxd is None:
        return r
    md = datetime.date.fromisoformat(maxd)
    lt = datetime.date.fromisoformat(latest)
    behind = (lt - md).days
    r["behind_days"] = max(0, behind)
    r["stale"] = behind > 0
    return r


def db_freshness_note(db=None) -> str:
    """新鲜度提示文案（skill 初筛时打印）。无库返回空串（不打扰）。"""
    f = db_freshness(db)
    if f["max_date"] is None:
        return ""
    if f["stale"]:
        return (f"  ⚠️ 本地库最新 {f['max_date']}，落后最近交易日 {f['latest_trade_date']}"
                f" 约 {f['behind_days']} 天；初筛与最终打分均按此本地日 K 快照（不补当日缺口），"
                f"实时信息仅东财换手率（可先 py tools/marketdb_local.py sync 同步）。")
    return f"  ℹ️ 本地库已最新（{f['max_date']}）。"


def prescreen_codes(codes, kinds, min_days: int = 2, lookback_days: int = 3,
                    db=None, limit: int = 10000) -> set[str] | None:
    """对给定代码池做 streaks 初筛，返回处于指定连续区间状态的 6 位代码集合。

    语义: 只保留「streak 结束日距本地库最新日期 ≤ lookback_days 天」的区间（即最近仍在
    该状态，而非很久以前）；多 kinds 取并集。返回 6 位代码（与 board_pool 键一致，去交易所
    后缀）。无库/无 duckdb/查询失败返回 None（调用方应回退全池）。
    """
    db_path = _db_path(db)
    if not db_path.exists():
        return None
    maxd = db_max_date(db_path)
    if maxd is None:
        return None
    cutoff = datetime.date.fromisoformat(maxd) - datetime.timedelta(days=lookback_days)

    result: set[str] = set()
    got_any = False
    for kind in kinds:
        df = query_streaks(kind, min_days=min_days, codes=list(codes), limit=limit,
                           end_after=cutoff.isoformat(), db=db_path)
        if df is None:
            continue
        got_any = True
        if df.empty:
            continue
        for _, row in df.iterrows():
            ed = row["end_date"]
            if hasattr(ed, "date"):
                ed = ed.date()
            if ed >= cutoff:
                result.add(str(row["thscode"]).split(".")[0])
    return result if got_any else None


def _validate(db: Path) -> None:
    """auto-sync 后跑数据质量校验（8 项）；失败只告警、不改变 sync 退出码。

    校验失败多为半截同步/坏库，早期暴露可避免批量扫描静默读到脏数据。
    """
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    r = subprocess.run(
        [sys.executable, "-m", "marketdb.cli", "validate", "--json", "--db", str(db)],
        capture_output=True, text=True, env=env,
    )
    if r.returncode == 0:
        print("    ✅ 数据质量校验通过")
        return
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    print(f"    ⚠️ 数据质量校验未通过（exit {r.returncode}），可 `marketdb validate --json --db {db}` 排查")
    if out:
        print("    " + out.replace("\n", "\n    ")[:800])


def sync_db(db=None) -> int:
    """增量同步（auto-sync，自动判断 skip/incremental/full）。不装依赖、不重建库。"""
    _load_key()
    db_path = _db_path(db)
    if not db_path.exists():
        print(_bootstrap_guide())
        return 1
    try:
        import marketdb  # noqa: F401
    except ImportError:
        print("❌ marketdb 未安装（先运行 bootstrap）")
        return 1
    rc = _cli("auto-sync", db=db_path).returncode
    if rc == 0:
        _validate(db_path)
    return rc


def sync_db_echo(db=None) -> None:
    """增量同步并打印结果（skill --sync 复用）。失败不抛出，只提示后继续按现有库跑。"""
    try:
        print("==> 增量同步本地 marketdb（auto-sync）...")
        rc = sync_db(db)
        print("    " + ("✅ 同步完成" if rc == 0 else f"⚠️ 同步退出码 {rc}，继续按现有库跑"))
    except Exception as e:
        print(f"    ⚠️ 同步失败: {e}")


def prescreen_pool(pool, kinds, min_pool: int = 40, label: str = "", db=None) -> tuple:
    """对 board_pool 形态的 {6位代码: 股票dict} 做 streaks 初筛，返回 (缩减后池, 提示文案)。

    仅池 ≥min_pool 时启用；无本地库/查询失败/初筛后过少均回退全池（宁多勿漏）。初筛是
    recall 优化、非 precision 过滤：只缩小「逐股拉新浪K线」的范围，最终打分仍逐股用新浪
    实时复核，故本地库过期（如盘中当日数据不在库）只影响初筛范围、不改变单股结论。
    label 用于提示文案（如「连续下跌/缩量」），缺省用 kinds 拼。
    """
    if len(pool) < min_pool:
        return pool, ""
    try:
        note = db_freshness_note(db)
    except Exception:
        return pool, ""
    try:
        cands = prescreen_codes(list(pool.keys()), kinds, min_days=2, db=db)
    except Exception:
        return pool, note
    if not cands:
        return pool, note + "\n  ℹ️ 本地库 streaks 初筛无候选，回退全池逐股检测。"
    reduced = {c: s for c, s in pool.items() if c in cands}
    if len(reduced) < 3:
        return pool, note + "\n  ℹ️ 初筛后过少，回退全池逐股检测。"
    label = label or "/".join(kinds)
    note += (f"\n  ℹ️ 本地库 streaks 初筛：{len(pool)} → {len(reduced)} 只"
             f"（{label}），逐股细看范围已缩小。")
    return reduced, note


def backtest_streaks(kind: str, min_days: int = 3, horizons: tuple[int, ...] = (5, 10, 20),
                     db=None, adjust: str = "forward", ma: int = 20, window: int = 20,
                     start: str | None = None, end: str | None = None):
    """对指定 streak kind 做「信号 → 未来 H 交易日收益」事件研究回测，返回 pandas DataFrame。

    对每条结束于 end_date 的 streak（连续天数 ≥ min_days），取 end_date 之后第 H 个交易日的
    前复权价算 forward return（H ∈ horizons）。future 价用 LEAD(adj, H)，未来不足 H 行的样本
    该 horizon 记为 NaN（自动排除停牌/退市/接近库最新日的样本）。返回列：
    thscode / start_date / end_date / days / end_val / f{H} / ret{H}。无库/无 duckdb 返回
    None，无匹配返回空 DataFrame。

    start/end 按「streak 结束日 end_date」过滤样本（区间历史仍用全量计算，保证区间不被
    截断），用于分段看信号在不同时间区间的有效性（regime 依赖）。

    注意：这是「事后漂移」事件研究，非逐日择时回测；不含交易成本，且用当前在库证券
    （有幸存者偏差）。每条连续段聚合为一行（无段内重复），但样本在时间上高度相关（同一
    市场环境、同期涨跌同向），且不同 min_days 的样本互相嵌套，N 不能直接读显著性，仅作
    信号方向与强度的粗判。
    """
    db_path = _db_path(db)
    if not db_path.exists():
        return None
    try:
        import duckdb
    except ImportError:
        return None

    val_expr = "k.close" if adjust == "none" else "k.close * COALESCE(a.forward_factor, 1.0)"
    px_extra, px_col, brk = _streak_spec(kind, val_expr, ma, window)
    lagged_extra = f", {px_col}" if px_col else ""
    leads = ", ".join(
        f"LEAD(adj, {h}) OVER (PARTITION BY thscode ORDER BY date) AS f{h}" for h in horizons
    )
    fcols = ", ".join(f"f.f{h}" for h in horizons)

    bw, bparams = [], []
    if start:
        bw.append("s.end_date >= ?")
        bparams.append(start)
    if end:
        bw.append("s.end_date <= ?")
        bparams.append(end)
    bwhere = ("WHERE " + " AND ".join(bw)) if bw else ""

    sql = f"""
WITH px AS (
  SELECT k.thscode, k.date, {val_expr} AS adj, k.volume AS vol{"," + px_extra if px_extra else ""}
  FROM raw_kline_daily k
  LEFT JOIN calc_adjust_factor_daily a ON a.thscode = k.thscode AND a.date = k.date
),
lagged AS (
  SELECT thscode, date, adj, vol{lagged_extra},
         LAG(adj) OVER (PARTITION BY thscode ORDER BY date) AS prev,
         LAG(vol) OVER (PARTITION BY thscode ORDER BY date) AS prev_vol
  FROM px
),
flagged AS (
  SELECT thscode, date, adj,
         CASE WHEN {brk} THEN 1 ELSE 0 END AS brk
  FROM lagged
),
grouped AS (
  SELECT thscode, date, adj, brk,
         SUM(brk) OVER (PARTITION BY thscode ORDER BY date ROWS UNBOUNDED PRECEDING) AS gid
  FROM flagged
),
streaks AS (
  SELECT thscode, MIN(date) AS start_date, MAX(date) AS end_date, COUNT(*) AS days,
         arg_max(adj, date) AS end_val
  FROM grouped
  WHERE brk = 0
  GROUP BY thscode, gid
  HAVING COUNT(*) >= {int(min_days)}
),
fwd AS (
  SELECT thscode, date, {leads}
  FROM px
)
SELECT s.thscode, s.start_date, s.end_date, s.days, s.end_val, {fcols}
FROM streaks s
JOIN fwd f ON f.thscode = s.thscode AND f.date = s.end_date
{bwhere}
ORDER BY s.end_date
"""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(sql, bparams).fetchdf()
    finally:
        con.close()
    for h in horizons:
        df[f"ret{h}"] = df[f"f{h}"] / df["end_val"] - 1.0
    return df


def _baseline_fwd(db_path, horizons, val_expr, start=None, end=None) -> dict:
    """全市场无条件 forward return 基准（同期对照），返回 {h: (n, win%, mean%, median%)}。

    start/end 过滤基准日 date 区间，使基准与信号回测同窗（regime 对照不串期）。
    """
    leads = ", ".join(
        f"LEAD(adj, {h}) OVER (PARTITION BY thscode ORDER BY date) / adj - 1.0 AS r{h}"
        for h in horizons
    )
    sel = ", ".join(
        f"COUNT(r{h}) AS n{h}, 100.0*AVG((r{h}>0)::INT) AS win{h}, "
        f"100.0*AVG(r{h}) AS mean{h}, 100.0*MEDIAN(r{h}) AS med{h}"
        for h in horizons
    )
    where, params = [], []
    if start:
        where.append("k.date >= ?")
        params.append(start)
    if end:
        where.append("k.date <= ?")
        params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
WITH px AS (
  SELECT k.thscode, k.date, {val_expr} AS adj
  FROM raw_kline_daily k
  LEFT JOIN calc_adjust_factor_daily a ON a.thscode = k.thscode AND a.date = k.date
  {where_sql}
),
fwd AS (
  SELECT {leads} FROM px
)
SELECT {sel} FROM fwd
"""
    try:
        import duckdb
    except ImportError:
        return {}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()
    out = {}
    for h in horizons:
        out[h] = (int(df[f"n{h}"][0]), float(df[f"win{h}"][0]),
                  float(df[f"mean{h}"][0]), float(df[f"med{h}"][0]))
    return out


def cmd_streaks_backtest(args) -> int:
    """streaks 信号 → 未来 N 交易日收益 回测（事件研究，非逐日择时）。"""
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    try:
        import duckdb  # noqa: F401
    except ImportError:
        print("❌ duckdb 未安装（先运行 bootstrap 安装 marketdb）")
        return 1

    horizons = tuple(int(x) for x in (args.horizons or "5,10,20").split(",") if x.strip())
    mins = [int(x) for x in (args.min or "3").split(",") if x.strip()]
    val_expr = "k.close" if args.adjust == "none" else "k.close * COALESCE(a.forward_factor, 1.0)"
    label = _STREAK_LABEL[args.kind]
    if args.kind == "above-ma":
        label = f"{label}(MA{args.ma})"
    elif args.kind == "new-high":
        label = f"{label}({args.window}日)"

    base = _baseline_fwd(db, horizons, val_expr, start=args.start, end=args.end)

    print(f"== streaks 回测：{label}（kind={args.kind}，{args.adjust}复权）==")
    print(f"   未来 {list(horizons)} 交易日；每格 = 胜率% / 均收益% / 中位收益%")
    if getattr(args, "start", None) or getattr(args, "end", None):
        print(f"   样本按 streak 结束日过滤：{args.start or '...'} ~ {args.end or '...'}")
    print()

    print(f"  {'min':>3} {'N':>7} | " + " | ".join(f"fut{h} 胜/均/中" for h in horizons))
    print("  " + "-" * (18 + 22 * len(horizons)))
    for mn in mins:
        df = backtest_streaks(args.kind, min_days=mn, horizons=horizons, db=db,
                              adjust=args.adjust, ma=args.ma, window=args.window,
                              start=args.start, end=args.end)
        if df is None or df.empty:
            print(f"  {mn:>3} {0:>7} | " + " | ".join("-- / -- / --" for _ in horizons))
            continue
        cells = []
        for h in horizons:
            s = df[f"ret{h}"].dropna()
            if s.empty:
                cells.append("-- / -- / --")
            else:
                win = (s > 0).mean() * 100.0
                cells.append(f"{win:.1f} / {s.mean()*100.0:+.2f} / {s.median()*100.0:+.2f}")
        print(f"  {mn:>3} {len(df):>7} | " + " | ".join(cells))

    if base:
        cells = []
        for h in horizons:
            _, win, mean, med = base[h]
            cells.append(f"{win:.1f} / {mean:+.2f} / {med:+.2f}")
        print(f"  {'基':>3} {'-':>7} | " + " | ".join(cells) + "   ← 全市场无条件基准")

    print()
    print("  解读：胜率/均收益高于基准 = 该信号对未来 H 日有正向预测力；低于 = 反向/均值回归。")
    print("  ⚠️ 事件研究非逐日择时：不含交易成本、用当前在库证券（幸存者偏差）；样本在时间上")
    print("     高度相关、且不同 --min 互相嵌套（N 不能直接读显著性），仅供粗判信号方向。")

    def _cells(sub):
        out = []
        for h in horizons:
            s = sub[f"ret{h}"].dropna()
            if s.empty:
                out.append("-- / -- / --")
            else:
                out.append(f"{(s > 0).mean()*100.0:.1f} / {s.mean()*100.0:+.2f} / {s.median()*100.0:+.2f}")
        return out

    if getattr(args, "by_board", False) or getattr(args, "by_year", False):
        sdf = backtest_streaks(args.kind, min_days=mins[0], horizons=horizons, db=db,
                               adjust=args.adjust, ma=args.ma, window=args.window,
                               start=args.start, end=args.end)
        if sdf is not None and not sdf.empty:
            head = " | ".join(f"fut{h} 胜/均/中" for h in horizons)
            if getattr(args, "by_board", False):
                sdf["board"] = sdf["thscode"].map(_board_of)
                print()
                print(f"  ── 分板块（min_days={mins[0]}，{label}）──")
                print(f"  {'板块':<6} {'N':>7} | " + head)
                print("  " + "-" * (18 + 22 * len(horizons)))
                for board in ("主板", "创业板", "科创板", "北交所"):
                    sub = sdf[sdf["board"] == board]
                    if sub.empty:
                        continue
                    print(f"  {board:<6} {len(sub):>7} | " + " | ".join(_cells(sub)))
            if getattr(args, "by_year", False):
                sdf["year"] = sdf["end_date"].map(lambda d: str(d)[:4])
                print()
                print(f"  ── 分年（min_days={mins[0]}，{label}，按 streak 结束年）──")
                print(f"  {'年份':<6} {'N':>7} | " + head)
                print("  " + "-" * (18 + 22 * len(horizons)))
                for yr in sorted(sdf["year"].unique()):
                    sub = sdf[sdf["year"] == yr]
                    print(f"  {yr:<6} {len(sub):>7} | " + " | ".join(_cells(sub)))
    return 0


def cmd_streaks(args) -> int:
    """全市场「连续上涨/下跌/放量/缩量」区间扫描（CLI 入口，复用 query_streaks）。"""
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    try:
        import duckdb  # noqa: F401
    except ImportError:
        print("❌ duckdb 未安装（先运行 bootstrap 安装 marketdb）")
        return 1

    codes = [c for c in (args.codes or "").split(",") if c] or None
    df = query_streaks(args.kind, min_days=args.min, start=args.start, end=args.end,
                       codes=codes, limit=args.limit, db=db, adjust=args.adjust,
                       ma=args.ma, window=args.window)
    if df is None or df.empty:
        print(f"⚠️ 未找到「{_STREAK_LABEL[args.kind]}」≥{args.min} 天的区间")
        return 0

    price_based = args.kind not in ("vol-up", "vol-down")
    label = _STREAK_LABEL[args.kind]
    if args.kind == "above-ma":
        label = f"{label}(MA{args.ma})"
    elif args.kind == "new-high":
        label = f"{label}({args.window}日)"
    if price_based:
        df["total_pct"] = (df["end_val"] / df["start_val"] - 1.0) * 100.0
        df = df.rename(columns={"start_val": "start_price", "end_val": "end_price"})
        show = df[["thscode", "name", "start_date", "end_date", "streak_days",
                   "start_price", "end_price", "total_pct"]].copy()
        show["total_pct"] = show["total_pct"].map(lambda v: f"{v:+.1f}%")
        show["start_price"] = show["start_price"].map(lambda v: f"{v:.2f}")
        show["end_price"] = show["end_price"].map(lambda v: f"{v:.2f}")
    else:
        df = df.rename(columns={"start_val": "start_vol", "end_val": "end_vol"})
        show = df[["thscode", "name", "start_date", "end_date", "streak_days",
                   "start_vol", "end_vol"]].copy()
        show["start_vol"] = show["start_vol"].map(lambda v: f"{v:,.0f}")
        show["end_vol"] = show["end_vol"].map(lambda v: f"{v:,.0f}")

    print(f"== {label} ≥{args.min}天 的区间（共 {len(df)} 段，按连续天数降序，前 {args.limit}）==")
    print(show.to_string(index=False))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n✅ 已落盘 {len(df)} 段: {out}")

    return 0


# ---------------------------------------------------------------- 白马蓝筹全市场筛选

_BLUECHIP_CONFIG_PATH = _ROOT / "data" / "bluechip_config.json"

# 内置默认阈值（首跑自动生成 data/bluechip_config.json，用户可编辑覆盖，按 key 合并）
_BLUECHIP_DEFAULTS: dict = {
    "market_cap_yi": 300,       # 市值门槛（亿元）
    "roe_min": 8.0,             # 蓝筹 ROE 门槛（%，最新年报归母净利/归母净资产；银行股普遍 7~11%，取 8 以纳入工行/建行/招行）
    "roe_baima_min": 15.0,      # 白马 ROE 门槛（%，最新年报，在蓝筹基础上更严）
    "roe_prior_min": 10.0,      # 白马 上年 ROE 门槛（%，滤掉「一年暴增」型次新/题材股）
    "profit_growth_min": 0.0,   # 白马 净利同比下限（%）
    "revenue_growth_min": 0.0,  # 白马 营收同比下限（%）
    "pb_pct_max": 90.0,         # 蓝筹 PB 历史分位上限（%，排除极端高估；PE 分位不设硬门槛，只作展示列）
    "years": 2,                 # 连续盈利年数（需最新 N 份年报净利均为正）
    "exclude_st": True,         # 剔除 ST / 退市
}


def _load_bluechip_config() -> dict:
    """读 data/bluechip_config.json（缺文件用默认值生成），用户文件按 key 覆盖默认值。"""
    import json

    cfg = dict(_BLUECHIP_DEFAULTS)
    try:
        if _BLUECHIP_CONFIG_PATH.exists():
            with open(_BLUECHIP_CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update({k: user[k] for k in _BLUECHIP_DEFAULTS if k in user})
        else:
            _BLUECHIP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_BLUECHIP_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(_BLUECHIP_DEFAULTS, f, ensure_ascii=False, indent=2)
            print(f"  ℹ️ 已生成默认配置: {_BLUECHIP_CONFIG_PATH}")
    except Exception as e:
        print(f"  ⚠️ 读取配置失败（改用内置默认值）: {e}")
    return cfg


def _num(v):
    """None/NaN → None，否则 float。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _roe(np_, eq):
    if np_ is None or eq is None or eq <= 0:
        return None
    return np_ / eq * 100.0


def _growth(cur, prior):
    if cur is None or prior is None or prior <= 0:
        return None
    return (cur / prior - 1.0) * 100.0


def _margin(rev, cost):
    if rev is None or rev <= 0 or cost is None:
        return None
    return (rev - cost) / rev * 100.0


def _ratio(num, den):
    if num is None or den is None or den <= 0:
        return None
    return num / den * 100.0


def _wpad(s, width: int) -> str:
    """CJK 宽度感知左对齐补齐（中文占 2 显示列），用于控制台表格对齐。"""
    s = str(s)
    w = sum(2 if ord(ch) > 127 else 1 for ch in s)
    return s + " " * max(0, width - w)


def _fmt(x, nd=1) -> str:
    return f"{x:.{nd}f}" if x is not None else "  --"


def _r(x, nd=2):
    """round 安全版：None/NaN → None，否则 round(x, nd)。"""
    if x is None:
        return None
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def screen_bluechip(db=None) -> int:
    """全市场白马/蓝筹基本面筛选（纯本地 DuckDB，离线），落库 + CSV + 控制台排名表。

    市值 = PB_MRQ(最新) × 净资产(最新年报)；ROE/增长用最新+上年年报；PB/PE 历史分位用
    valuation_daily 全历史正值序列。分层：蓝筹（大盘+连续盈利+ROE 达标+估值不极端）→
    白马（蓝筹基础上 ROE 更严 + 营收/净利正增长）。阈值全部来自 data/bluechip_config.json。
    """
    db_path = _db_path(db)
    if not db_path.exists():
        print(_bootstrap_guide())
        return 1
    try:
        import duckdb
    except ImportError:
        print("❌ duckdb 未安装（先运行 bootstrap 安装 marketdb）")
        return 1

    cfg = _load_bluechip_config()
    mc = float(cfg["market_cap_yi"])
    roe_min = float(cfg["roe_min"])
    roe_bm = float(cfg["roe_baima_min"])
    roe_prior_min = float(cfg["roe_prior_min"])
    pg_min = float(cfg["profit_growth_min"])
    rg_min = float(cfg["revenue_growth_min"])
    pb_pct_max = float(cfg["pb_pct_max"])
    years = int(cfg["years"])
    exclude_st = bool(cfg["exclude_st"])

    print("=" * 96)
    print(f"白马蓝筹全市场筛选（本地库）  市值≥{mc:.0f}亿 | 蓝筹ROE≥{roe_min:.0f}% | "
          f"白马ROE≥{roe_bm:.0f}%(上年≥{roe_prior_min:.0f}%) + 营收/净利同比≥{pg_min:.0f}% | "
          f"PB分位≤{pb_pct_max:.0f}% | 连续{years}年盈利")
    print("=" * 96)

    base_sql = """
WITH inc AS (
  SELECT thscode, period_end, operating_income, operating_costs, parent_holder_net_profit
  FROM fin_income WHERE period_end LIKE '%-12-31'
),
bal AS (
  SELECT thscode, period_end, holder_equity_total, assets_total, total_debt
  FROM fin_balance WHERE period_end LIKE '%-12-31'
),
val AS (
  SELECT thscode, pe_ttm, pb_mrq
  FROM valuation_daily
  QUALIFY ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY trade_date DESC) = 1
),
years AS (
  SELECT i.thscode, MAX(i.period_end) AS cur_pe
  FROM inc i JOIN bal b ON b.thscode = i.thscode AND b.period_end = i.period_end
  GROUP BY i.thscode
)
SELECT y.thscode, s.ticker, s.name, s.exchange,
       v.pb_mrq, v.pe_ttm,
       ic.operating_income AS rev_cur, ic.operating_costs AS cost_cur,
       ic.parent_holder_net_profit AS np_cur,
       ip.operating_income AS rev_prior, ip.parent_holder_net_profit AS np_prior,
       bc.holder_equity_total AS eq_cur, bc.assets_total AS assets_cur,
       bc.total_debt AS debt_cur, bp.holder_equity_total AS eq_prior
FROM years y
JOIN val v ON v.thscode = y.thscode
JOIN dim_symbol s ON s.thscode = y.thscode
JOIN inc ic ON ic.thscode = y.thscode AND ic.period_end = y.cur_pe
LEFT JOIN inc ip ON ip.thscode = y.thscode
  AND ip.period_end = CAST(CAST(LEFT(y.cur_pe, 4) AS INTEGER) - 1 AS VARCHAR) || '-12-31'
JOIN bal bc ON bc.thscode = y.thscode AND bc.period_end = y.cur_pe
LEFT JOIN bal bp ON bp.thscode = y.thscode
  AND bp.period_end = CAST(CAST(LEFT(y.cur_pe, 4) AS INTEGER) - 1 AS VARCHAR) || '-12-31'
WHERE s.asset_type = 'a-share' AND s.exchange IN ('SH', 'SZ')
"""

    pct_sql = """
WITH cur AS (
  SELECT thscode, pe_ttm AS pe_cur, pb_mrq AS pb_cur
  FROM valuation_daily
  QUALIFY ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY trade_date DESC) = 1
)
SELECT v.thscode,
  100.0 * SUM(CASE WHEN v.pb_mrq > 0 AND v.pb_mrq <= c.pb_cur THEN 1.0 ELSE 0.0 END)
    / NULLIF(SUM(CASE WHEN v.pb_mrq > 0 THEN 1.0 ELSE 0.0 END), 0) AS pb_pct,
  100.0 * SUM(CASE WHEN v.pe_ttm > 0 AND v.pe_ttm <= c.pe_cur THEN 1.0 ELSE 0.0 END)
    / NULLIF(SUM(CASE WHEN v.pe_ttm > 0 THEN 1.0 ELSE 0.0 END), 0) AS pe_pct
FROM valuation_daily v
JOIN cur c ON c.thscode = v.thscode
WHERE c.pb_cur > 0 OR c.pe_cur > 0
GROUP BY v.thscode
"""

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(base_sql).fetchall()
        pct_rows = con.execute(pct_sql).fetchall()
    finally:
        con.close()
    pct_map = {r[0]: (_num(r[1]), _num(r[2])) for r in pct_rows}

    baima: list[dict] = []
    lanchou: list[dict] = []
    for r in rows:
        (thscode, ticker, name, exchange, pb_mrq, pe_ttm,
         rev_cur, cost_cur, np_cur, rev_prior, np_prior,
         eq_cur, assets_cur, debt_cur, eq_prior) = r
        name = name or ""
        if exclude_st and ("ST" in name.upper() or "退" in name):
            continue
        pb_mrq = _num(pb_mrq); pe_ttm = _num(pe_ttm)
        eq_cur = _num(eq_cur); eq_prior = _num(eq_prior)
        np_cur = _num(np_cur); np_prior = _num(np_prior)
        rev_cur = _num(rev_cur); rev_prior = _num(rev_prior)
        cost_cur = _num(cost_cur)
        assets_cur = _num(assets_cur); debt_cur = _num(debt_cur)

        mkt = pb_mrq * eq_cur / 1e8 if (pb_mrq and eq_cur and pb_mrq > 0 and eq_cur > 0) else None
        if mkt is None or mkt < mc:
            continue

        # 连续 N 年盈利（最新 + 往前 N-1 份年报净利均为正）
        profits = [p for p in (np_cur, np_prior) if p is not None]
        if len(profits) < years or any(p <= 0 for p in profits[:years]):
            continue

        roe_cur = _roe(np_cur, eq_cur)
        roe_prior = _roe(np_prior, eq_prior)
        if roe_cur is None or roe_cur < roe_min:
            continue

        pb_pct, pe_pct = pct_map.get(thscode, (None, None))
        if pb_pct is not None and pb_pct > pb_pct_max:
            continue

        rev_g = _growth(rev_cur, rev_prior)
        np_g = _growth(np_cur, np_prior)
        rec = {
            "thscode": thscode, "ticker": ticker, "name": name, "exchange": exchange,
            "market_cap_yi": mkt, "roe_cur": roe_cur, "roe_prior": roe_prior,
            "revenue_growth": rev_g, "profit_growth": np_g,
            "gross_margin": _margin(rev_cur, cost_cur),
            "debt_ratio": _ratio(debt_cur, assets_cur),
            "pb_mrq": pb_mrq, "pe_ttm": pe_ttm, "pb_pct": pb_pct, "pe_pct": pe_pct,
        }
        is_bm = (roe_cur >= roe_bm and roe_prior is not None and roe_prior >= roe_prior_min
                 and rev_g is not None and rev_g >= rg_min
                 and np_g is not None and np_g >= pg_min)
        rec["tier"] = "白马" if is_bm else "蓝筹"
        (baima if is_bm else lanchou).append(rec)

    for lst in (baima, lanchou):
        lst.sort(key=lambda x: -x["market_cap_yi"])

    # 落库（删旧插新，幂等）
    w = duckdb.connect(str(db_path))
    try:
        w.execute("""
CREATE TABLE IF NOT EXISTS bluechip_screen (
  thscode VARCHAR, ticker VARCHAR, name VARCHAR, exchange VARCHAR,
  market_cap_yi DOUBLE, roe_cur DOUBLE, roe_prior DOUBLE,
  revenue_growth DOUBLE, profit_growth DOUBLE, gross_margin DOUBLE, debt_ratio DOUBLE,
  pb_mrq DOUBLE, pe_ttm DOUBLE, pb_pct DOUBLE, pe_pct DOUBLE,
  tier VARCHAR, screen_date VARCHAR
)""")
        w.execute("DELETE FROM bluechip_screen")
        cols = ["thscode", "ticker", "name", "exchange", "market_cap_yi", "roe_cur",
                "roe_prior", "revenue_growth", "profit_growth", "gross_margin",
                "debt_ratio", "pb_mrq", "pe_ttm", "pb_pct", "pe_pct", "tier", "screen_date"]
        screen_date = datetime.date.today().strftime("%Y-%m-%d")
        placeholders = ",".join("?" for _ in cols)
        for rec in baima + lanchou:
            w.execute(f"INSERT INTO bluechip_screen ({','.join(cols)}) VALUES ({placeholders})",
                      [rec[c] for c in cols[:-1]] + [screen_date])
    finally:
        w.close()

    # CSV 快照（utf-8-sig，Excel 直接打开不乱码）
    import csv

    csv_path = _ROOT / "data" / "bluechip_whitelist.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["代码", "名称", "层级", "市值(亿)", "ROE今%", "ROE上年%",
                         "营收同比%", "净利同比%", "毛利率%", "负债率%", "PB", "PE-TTM",
                         "PB分位%", "PE分位%", "筛选日期"])
        for rec in baima + lanchou:
            writer.writerow([
                rec["ticker"], rec["name"], rec["tier"],
                _r(rec["market_cap_yi"], 1), _r(rec["roe_cur"]), _r(rec["roe_prior"]),
                _r(rec["revenue_growth"]), _r(rec["profit_growth"]),
                _r(rec["gross_margin"]), _r(rec["debt_ratio"]),
                _r(rec["pb_mrq"]), _r(rec["pe_ttm"]),
                _r(rec["pb_pct"]), _r(rec["pe_pct"]), screen_date,
            ])

    # 控制台排名表
    def _print_section(title, lst):
        print()
        print(f"{title}（{len(lst)} 只）")
        print(f"  {'代码':<7}{'名称':<12}{'市值(亿)':>9}{'ROE今%':>8}{'ROE上年%':>9}"
              f"{'营收同比%':>9}{'净利同比%':>9}{'毛利率%':>8}{'负债率%':>8}{'PB分位%':>8}")
        print("  " + "-" * 92)
        if not lst:
            print("  （无）")
            return
        for r in lst:
            print(f"  {r['ticker']:<7}{_wpad(r['name'], 12)}"
                  f"{_fmt(r['market_cap_yi'], 0):>9}{_fmt(r['roe_cur']):>8}"
                  f"{_fmt(r['roe_prior']):>9}{_fmt(r['revenue_growth']):>9}"
                  f"{_fmt(r['profit_growth']):>9}{_fmt(r['gross_margin']):>8}"
                  f"{_fmt(r['debt_ratio']):>8}{_fmt(r['pb_pct']):>8}")

    _print_section("🐎 白马（高ROE + 成长）", baima)
    _print_section("🔵 蓝筹（大盘 + 稳定盈利）", lanchou)

    print()
    print(f"  合计 {len(baima) + len(lanchou)} 只（白马 {len(baima)} / 蓝筹 {len(lanchou)}）")
    print(f"  ✅ 已落库 bluechip_screen（{len(baima) + len(lanchou)} 行）+ CSV: {csv_path}")
    print("  说明: 市值=PB×净资产估算；ROE=最新年报归母净利/归母净资产；分位=当前值在10年估值历史中的百分位（越低越便宜）")
    return 0


def cmd_screen_bluechip(args) -> int:
    return screen_bluechip(db=args.db)


# ---------------------------------------------------------------- 全市场黄金坑 SQL 初筛

# B1 全市场黄金坑第一步：纯 SQL 扫全市场（60/68/00/30 前缀），按技术硬门槛初筛。
# 常规坑（回撤 20~45%）门槛与 scan_golden_pit._score_candidate 的「常规硬门槛」对齐
# （距高点≥5 日、站上半年线 MA120、非放量下跌 vol_ratio<1.2）；MACD/KDJ/止跌形态
# 与基本面门槛由 scan_golden_pit 在短名单上逐股细算（B1 的「基本面兜底」）。
# 深度坑（回撤 >45%）技术面几乎筛不动（站半年线+缩量的只有 0 只），故只保留
# 「距高点≥5 日 + 缩量<1.2」两道弱门槛，把「深度错杀 vs 暴雷」交给基本面门槛裁决。
_GOLDEN_PIT_CORE = """
WITH px AS (
  SELECT thscode, date, close, high, volume,
         ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY date) AS rn
  FROM v_daily_qfq
  WHERE substr(thscode, 1, 2) IN ('60', '68', '00', '30')
),
lasts AS (
  SELECT thscode, close AS price, rn AS last_rn,
         MAX(high) OVER w AS peak,
         arg_max(rn, high) OVER w AS peak_rn,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 119 PRECEDING AND CURRENT ROW) AS ma120,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 249 PRECEDING AND CURRENT ROW) AS ma250,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ma5,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS ma10,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20
  FROM px
  WINDOW w AS (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 119 PRECEDING AND 1 PRECEDING)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY rn DESC) = 1
),
vols AS (
  SELECT l.thscode,
         AVG(x.volume) FILTER (WHERE x.rn >= l.peak_rn) AS pit_vol,
         AVG(x.volume) FILTER (WHERE x.rn >= l.peak_rn - 20 AND x.rn < l.peak_rn) AS pre_vol
  FROM lasts l JOIN px x ON x.thscode = l.thscode
  GROUP BY l.thscode
),
scored AS (
  SELECT l.thscode, s.ticker, s.name,
         l.price, l.peak,
         (l.peak - l.price) / l.peak * 100.0 AS dd,
         (l.last_rn - l.peak_rn) AS gap,
         l.ma120, l.ma250,
         l.ma5, l.ma10, l.ma20,
         v.pit_vol / NULLIF(v.pre_vol, 0) AS vol_ratio,
         l.last_rn
  FROM lasts l
  JOIN dim_symbol s ON s.thscode = l.thscode
  LEFT JOIN vols v ON v.thscode = l.thscode
  WHERE l.peak IS NOT NULL AND l.peak > 0 AND l.last_rn >= 60
)
SELECT * FROM scored
WHERE {where}
ORDER BY {order}
LIMIT {limit}
"""

_GOLDEN_PIT_NORMAL_WHERE = (
    "dd BETWEEN 20.0 AND 45.0 "
    "AND gap >= 5 "
    "AND (ma120 IS NULL OR price >= ma120) "
    "AND (vol_ratio IS NULL OR vol_ratio < 1.2)"
)

_GOLDEN_PIT_DEEP_WHERE = (
    "dd > 45.0 "
    "AND gap >= 5 "
    "AND (vol_ratio IS NULL OR vol_ratio < 1.2)"
)

# 牛市浅回调（右侧）：回撤 5~20% + 均线多头(MA5>MA10>MA20) + 站上 MA20。
# 回测：牛市里深坑抄底无 edge，换「浅回调上车」才有正 edge（全市值层牛市 +0.84~1.33%）。
# 仅牛市 regime 使用（熊市该信号反向 −0.98~−3.82%），市值门槛在 Python 层（微盘排除）。
_GOLDEN_PIT_BULL_WHERE = (
    "dd BETWEEN 5.0 AND 20.0 "
    "AND ma5 > ma10 AND ma10 > ma20 "
    "AND price >= ma20"
)


def _golden_pit_query(where: str, order: str, limit: int) -> str:
    """把 WHERE/ORDER/LIMIT 组装进公共 CTE，生成常规坑/深度坑两条查询。"""
    return _GOLDEN_PIT_CORE.format(where=where, order=order, limit=int(limit))


def _golden_pit_query_run(sql: str, db=None):
    """执行黄金坑初筛 SQL，返回 pandas DataFrame；无库/无 duckdb/失败返回 None。"""
    db_path = _db_path(db)
    if not db_path.exists():
        return None
    try:
        import duckdb
    except ImportError:
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            try:
                con.execute("SET enable_progress_bar = false")
            except Exception:
                pass
            return con.execute(sql).fetchdf()
        finally:
            con.close()
    except Exception:
        return None


def query_golden_pit_prescreen(db=None, limit: int = 500):
    """全市场黄金坑「常规坑」技术初筛（纯本地 DuckDB，单条 SQL）。

    返回 pandas DataFrame（列: thscode/ticker/name/price/peak/dd/gap/ma120/ma250/vol_ratio/last_rn），
    已按「回撤 20~45% + 距高点≥5日 + 站上半年线 + 缩量(非放量下跌)」硬门槛过滤。
    无库/无 duckdb/查询失败返回 None；无匹配返回空 DataFrame。
    """
    sql = _golden_pit_query(_GOLDEN_PIT_NORMAL_WHERE, "dd DESC, vol_ratio ASC", limit)
    df = _golden_pit_query_run(sql, db)
    if df is None or getattr(df, "empty", True):
        return df
    return df.head(limit)


def query_golden_pit_deep(db=None, limit: int = 1000):
    """全市场黄金坑「深度坑」技术初筛（回撤 >45%，纯本地 DuckDB）。

    深度坑技术面筛不动（站半年线+缩量≈0 只），故只保留「距高点≥5日 + 缩量<1.2」弱门槛，
    把「深度错杀 vs 暴雷」交给 scan_golden_pit 的基本面门槛裁决。列同 query_golden_pit_prescreen。
    limit 默认 1000 覆盖全部深度坑（当前约 965 只），避免只截到最深跌的一批、漏掉 45~55%
    的「浅深度坑」（那里深度错杀最可能）。
    """
    sql = _golden_pit_query(_GOLDEN_PIT_DEEP_WHERE, "dd DESC, vol_ratio ASC", limit)
    df = _golden_pit_query_run(sql, db)
    if df is None or getattr(df, "empty", True):
        return df
    return df.head(limit)


def query_golden_pit_bull(db=None, limit: int = 2000):
    """全市场黄金坑「牛市浅回调」技术初筛（回撤 5~20% + 均线多头 + 站上 MA20）。

    回测结论：牛市里深坑抄底无 edge（超跌是真弱），该换「浅回调上车」右侧顺势接回调。
    故牛市 regime 下用本查询替代（或补充）深坑 SQL 初筛，捞 5~20% 浅回调多头候选。
    列同 query_golden_pit_prescreen（多了 ma5/ma10/ma20）。limit 默认 2000 覆盖全部浅回调
    候选（全市值层约几百只），市值门槛（微盘排除）由 scan_golden_pit 在 Python 层用
    size_tier 做。
    """
    sql = _golden_pit_query(_GOLDEN_PIT_BULL_WHERE, "dd ASC, ma20 ASC", limit)
    df = _golden_pit_query_run(sql, db)
    if df is None or getattr(df, "empty", True):
        return df
    return df.head(limit)


def cmd_screen_golden_pit(args) -> int:
    """全市场黄金坑技术初筛（CLI 入口，复用 query_golden_pit_prescreen）。"""
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    df = query_golden_pit_prescreen(db=db, limit=args.limit)
    if df is None:
        print("❌ 本地库不可用或缺少 duckdb")
        return 1
    if df is None or df.empty:
        print("⚠️ 全市场无符合黄金坑硬门槛（回撤20~45% + 站半年线 + 缩量）的候选")
        return 0

    show = df[["ticker", "name", "price", "dd", "gap", "vol_ratio", "ma120", "ma250"]].copy()
    show["price"] = show["price"].map(lambda v: f"{v:.2f}")
    show["dd"] = show["dd"].map(lambda v: f"{v:.1f}%")
    show["gap"] = show["gap"].map(lambda v: f"{int(v)}日")
    show["vol_ratio"] = show["vol_ratio"].map(lambda v: f"{v:.2f}" if v == v else "--")
    show["ma120"] = show["ma120"].map(lambda v: f"{v:.2f}" if v == v else "--")
    show["ma250"] = show["ma250"].map(lambda v: f"{v:.2f}" if v == v else "--")
    show = show.rename(columns={"ticker": "代码", "name": "名称", "price": "现价", "dd": "回撤%",
                                "gap": "距高点", "vol_ratio": "缩量比", "ma120": "MA120", "ma250": "MA250"})

    print(f"== 全市场黄金坑技术初筛（共 {len(df)} 只，按回撤降序）==")
    print(show.to_string(index=False))
    print()
    print("  说明: 这是 B1 第一步（技术硬门槛）。缩量比=坑段均量/高点前20日均量(<1.2 非放量)；")
    print("        MACD/KDJ/止跌形态 + 基本面门槛(净利同比/ROE/PB分位) 需跑 scan_golden_pit.py 全市场。")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n✅ 已落盘 {len(df)} 只: {out}")
    return 0


# ---------------------------------------------------------------- 左右侧全市场 SQL 初筛

# 左侧（超跌埋伏）：复用 _GOLDEN_PIT_CORE（同一套 px/lasts/vols CTE，缩量口径一致 ——
# 坑段均量/高点前20日均量），只换硬门槛：回撤 12~45%（常规）/ >45%（深度）+ 距高点≥3日。
# RSI/J 超买排除、止跌形态、基本面门槛由 scan_left_side 在短名单上逐股细算
# （与 golden-pit「SQL 只做 recall 缩减、不做最终判定」同一精神）。
_LEFT_SIDE_NORMAL_WHERE = (
    "dd BETWEEN 12.0 AND 45.0 "
    "AND gap >= 3"
)

_LEFT_SIDE_DEEP_WHERE = (
    "dd > 45.0 "
    "AND gap >= 3"
)


def query_left_side_prescreen(db=None, limit: int = 800):
    """全市场左侧「常规超跌」技术初筛（回撤 12~45% + 距高点≥3日，纯本地 DuckDB）。

    返回列同 query_golden_pit_prescreen（thscode/ticker/name/price/peak/dd/gap/ma120/
    ma250/ma5/ma10/ma20/vol_ratio/last_rn），供 scan_left_side 全市场模式逐股细算。
    """
    sql = _golden_pit_query(_LEFT_SIDE_NORMAL_WHERE, "dd ASC, vol_ratio ASC", limit)
    df = _golden_pit_query_run(sql, db)
    if df is None or getattr(df, "empty", True):
        return df
    return df.head(limit)


def query_left_side_deep(db=None, limit: int = 800):
    """全市场左侧「深度超跌」技术初筛（回撤 >45% + 距高点≥3日）。

    深跌多半是暴雷，但 scan_left_side 把 dd>45 单列（deep_results）供人工判断「深度
    错杀 vs 暴雷」，故初筛同样召回。列同 query_left_side_prescreen。
    """
    sql = _golden_pit_query(_LEFT_SIDE_DEEP_WHERE, "dd DESC, vol_ratio ASC", limit)
    df = _golden_pit_query_run(sql, db)
    if df is None or getattr(df, "empty", True):
        return df
    return df.head(limit)


# 右侧（突破追入 + 回踩买点）：量比口径是「今日量/前5日均量」（区别于 golden-pit/left 的
# 「坑段均量/高点前20日均量」），且需 20/60 日新高、均线多头、MA20 走平、近5日新高、
# 缩量回踩等列，故单独一套 CTE。最终打分仍由 scan_right_side 逐股用本地/新浪 K 重算。
_RIGHT_SIDE_CORE = """
WITH px AS (
  SELECT thscode, date, close, high, volume,
         ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY date) AS rn
  FROM v_daily_qfq
  WHERE substr(thscode, 1, 2) IN ('60', '68', '00', '30')
),
lasts AS (
  SELECT thscode, close AS price, volume AS today_vol, rn AS last_rn,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ma5,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS ma10,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60,
         AVG(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 24 PRECEDING AND 5 PRECEDING) AS ma20_prev,
         MAX(high) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20,
         MAX(high) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS high60,
         MAX(high) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS recent5_high,
         AVG(volume) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS prev5_vol
  FROM px
  QUALIFY ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY rn DESC) = 1
),
scored AS (
  SELECT l.thscode, s.ticker, s.name,
         l.price, l.ma5, l.ma10, l.ma20, l.ma60, l.ma20_prev,
         l.high20, l.high60, l.recent5_high,
         l.today_vol / NULLIF(l.prev5_vol, 0) AS vol_ratio,
         l.last_rn
  FROM lasts l
  JOIN dim_symbol s ON s.thscode = l.thscode
  WHERE l.last_rn >= 60
)
SELECT * FROM scored
WHERE {where}
ORDER BY {order}
LIMIT {limit}
"""

_RIGHT_SIDE_BREAKOUT_WHERE = (
    "price >= ma20 "
    "AND NOT (name LIKE '%ST%' OR name LIKE '%退%') "
    "AND ("
    "  (high20 IS NOT NULL AND price > high20) "
    "  OR (high60 IS NOT NULL AND price > high60) "
    "  OR (ma5 > ma10 AND ma10 > ma20) "
    "  OR (vol_ratio >= 1.2) "
    ")"
)

_RIGHT_SIDE_PULLBACK_WHERE = (
    "price >= ma20 "
    "AND ma20_prev IS NOT NULL AND ma20 >= ma20_prev "
    "AND high20 IS NOT NULL AND recent5_high IS NOT NULL "
    "AND high20 > 0 AND recent5_high > 0 "
    "AND recent5_high >= high20 * 0.99 "
    "AND (recent5_high - price) / recent5_high * 100.0 BETWEEN 2.0 AND 10.0 "
    "AND vol_ratio < 1.2 "
    "AND NOT (name LIKE '%ST%' OR name LIKE '%退%') "
)


def _right_side_query(where: str, order: str, limit: int) -> str:
    return _RIGHT_SIDE_CORE.format(where=where, order=order, limit=int(limit))


def query_right_side_prescreen(db=None, limit: int = 800):
    """全市场右侧「突破/走强」技术初筛（站上MA20 + 至少一个走强确认，纯本地 DuckDB）。

    走强确认 = 创20/60日新高 或 均线多头(MA5>MA10>MA20) 或 放量(量比≥1.2)，与
    scan_right_side._score_candidate 的 has_confirm 硬门槛对齐；MACD 金叉/多头、换手率
    留给 Python 短名单细算。返回列: thscode/ticker/name/price/ma5/ma10/ma20/ma60/
    high20/high60/vol_ratio/last_rn。
    """
    order = (
        "COALESCE((price > high20)::INT, 0) + COALESCE((price > high60)::INT, 0) "
        "+ COALESCE(((ma5 > ma10 AND ma10 > ma20))::INT, 0) "
        "+ COALESCE((vol_ratio >= 1.2)::INT, 0) DESC, "
        "COALESCE(vol_ratio, 0) DESC"
    )
    sql = _right_side_query(_RIGHT_SIDE_BREAKOUT_WHERE, order, limit)
    df = _golden_pit_query_run(sql, db)
    if df is None or getattr(df, "empty", True):
        return df
    return df.head(limit)


def query_right_side_pullback(db=None, limit: int = 800):
    """全市场右侧「回踩买点」技术初筛（趋势多头 + 近5日创20日新高 + 缩量回踩 2~10%）。

    与 scan_right_side._score_pullback 硬门槛对齐（站上MA20且MA20走平、近5日创20日新高、
    回撤2~10%、量比<1.2）。列同 query_right_side_prescreen（多了 ma20_prev/recent5_high）。
    """
    sql = _right_side_query(_RIGHT_SIDE_PULLBACK_WHERE, "vol_ratio ASC", limit)
    df = _golden_pit_query_run(sql, db)
    if df is None or getattr(df, "empty", True):
        return df
    return df.head(limit)


# ---------------------------------------------------------------- 磨底全市场 SQL 初筛

# 磨底（止跌横盘缩量打底）：独立一套 CTE —— 需要「近60日最低收盘（磨底锚点）及其首次
# 出现位置（磨底时长）、近60日真实高低区间（箱宽）、近20/120日均量（缩量比）」，与
# left/right/golden-pit 的「距高点回撤」口径不同。最终打分仍由 scan_bottoming 在短名单
# 上逐股用本地/新浪 K 重算（detect_stage 阶段 / RSI 超买排除留给 Python）。
_BOTTOMING_CORE = """
WITH px AS (
  SELECT thscode, date, close, low, high, volume,
         ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY date) AS rn
  FROM v_daily_qfq
  WHERE substr(thscode, 1, 2) IN ('60', '68', '00', '30')
),
lasts AS (
  SELECT thscode, close AS price, rn AS last_rn,
         MIN(close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS low60_close,
         arg_min(rn, close) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS low60_rn,
         MIN(low) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS lo60,
         MAX(high) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS hi60,
         AVG(volume) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS vol20,
         AVG(volume) OVER (PARTITION BY thscode ORDER BY rn ROWS BETWEEN 119 PRECEDING AND CURRENT ROW) AS vol120
  FROM px
  QUALIFY ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY rn DESC) = 1
),
scored AS (
  SELECT l.thscode, s.ticker, s.name,
         l.price,
         (l.price - l.low60_close) / l.low60_close * 100.0 AS dist_low,
         (l.last_rn - l.low60_rn) AS days_since_low,
         (l.hi60 - l.lo60) / l.lo60 * 100.0 AS box_w,
         l.vol20 / NULLIF(l.vol120, 0) AS vol_ratio,
         l.last_rn
  FROM lasts l
  JOIN dim_symbol s ON s.thscode = l.thscode
  WHERE l.last_rn >= 120
    AND l.low60_close IS NOT NULL AND l.low60_close > 0
    AND l.lo60 IS NOT NULL AND l.hi60 IS NOT NULL AND l.hi60 > l.lo60
)
SELECT * FROM scored
WHERE {where}
ORDER BY {order}
LIMIT {limit}
"""

_BOTTOMING_WHERE = (
    "dist_low <= 8.0 "
    "AND days_since_low >= 10 "
    "AND box_w <= 20.0 "
    "AND vol_ratio < 0.8 "
    "AND NOT (name LIKE '%ST%' OR name LIKE '%退%')"
)


def _bottoming_query(where: str, order: str, limit: int) -> str:
    """把 WHERE/ORDER/LIMIT 组装进磨底公共 CTE。"""
    return _BOTTOMING_CORE.format(where=where, order=order, limit=int(limit))


def query_bottoming_prescreen(db=None, limit: int = 800):
    """全市场磨底「止跌横盘缩量打底」技术初筛（纯本地 DuckDB，单条 SQL）。

    与 scan_bottoming._score_bottoming 的硬门槛对齐：距低点≤8% + 磨底≥10日 + 箱宽≤20%
    + 缩量<0.8（近20日均量/近120日均量）。detect_stage 阶段 / RSI 超买排除由 scan_bottoming
    在短名单上逐股细算。返回 pandas DataFrame（列: thscode/ticker/name/price/dist_low/
    days_since_low/box_w/vol_ratio/last_rn）。无库/无 duckdb/失败返回 None。
    """
    sql = _bottoming_query(_BOTTOMING_WHERE, "dist_low ASC, box_w ASC, vol_ratio ASC", limit)
    df = _golden_pit_query_run(sql, db)
    if df is None or getattr(df, "empty", True):
        return df
    return df.head(limit)


def cmd_screen_bottoming(args) -> int:
    """全市场磨底技术初筛（CLI 入口，复用 query_bottoming_prescreen）。"""
    db = _db_path(args.db)
    if not db.exists():
        print(_bootstrap_guide())
        return 1
    df = query_bottoming_prescreen(db=db, limit=args.limit)
    if df is None:
        print("❌ 本地库不可用或缺少 duckdb")
        return 1
    if df.empty:
        print("⚠️ 全市场无符合磨底硬门槛（距低点≤8% + 磨底≥10日 + 箱宽≤20% + 缩量<0.8）的候选")
        return 0

    show = df[["ticker", "name", "price", "dist_low", "days_since_low", "box_w", "vol_ratio"]].copy()
    show["price"] = show["price"].map(lambda v: f"{v:.2f}")
    show["dist_low"] = show["dist_low"].map(lambda v: f"{v:.1f}%")
    show["days_since_low"] = show["days_since_low"].map(lambda v: f"{int(v)}日")
    show["box_w"] = show["box_w"].map(lambda v: f"{v:.1f}%")
    show["vol_ratio"] = show["vol_ratio"].map(lambda v: f"{v:.2f}" if v == v else "--")
    show = show.rename(columns={"ticker": "代码", "name": "名称", "price": "现价", "dist_low": "距低点%",
                                "days_since_low": "磨底天数", "box_w": "箱宽%", "vol_ratio": "缩量比"})

    print(f"== 全市场磨底技术初筛（共 {len(df)} 只，按距低点升序）==")
    print(show.to_string(index=False))
    print()
    print("  说明: 这是第一步（技术硬门槛）。detect_stage 阶段 / RSI 超买 / 均线企稳打分需跑 scan_bottoming.py 全市场。")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n✅ 已落盘 {len(df)} 只: {out}")
    return 0


# ---------------------------------------------------------------- 入口

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    pb = sub.add_parser("bootstrap", help="安装 + 建库 + 全量同步")
    pb.add_argument("--db")
    pb.add_argument("--force", action="store_true")
    pb.add_argument("--keep-cache", action="store_true")

    pd_ = sub.add_parser("daily", help="读本地日K")
    pd_.add_argument("codes", help="thscode，批量逗号分隔（600519.SH,000001.SZ）")
    pd_.add_argument("--start", help="YYYY-MM-DD")
    pd_.add_argument("--end", help="YYYY-MM-DD")
    pd_.add_argument("--adjust", default="forward", choices=["none", "forward", "backward"])
    pd_.add_argument("--db")

    pp = sub.add_parser("panel", help="全市场日K面板落盘 CSV")
    pp.add_argument("--start")
    pp.add_argument("--end")
    pp.add_argument("--adjust", default="none", choices=["none", "forward", "backward"])
    pp.add_argument("--out")
    pp.add_argument("--db")

    ps = sub.add_parser("symbols", help="列出本地证券表")
    ps.add_argument("--exchange", choices=["SH", "SZ", "BJ"])
    ps.add_argument("--asset-type")
    ps.add_argument("--limit", type=int, default=30)
    ps.add_argument("--db")

    st = sub.add_parser("status", help="库状态")
    st.add_argument("--db")

    sy = sub.add_parser("sync", help="增量同步（只 auto-sync，自动判断 skip/incremental/full）")
    sy.add_argument("--db")

    ss = sub.add_parser("sync-symbols", help="刷新 dim_symbol 证券维度表（symbols 子命令依赖）")
    ss.add_argument("--db")

    sf = sub.add_parser("sync-financials", help="三张报表落库（利润/资产负债/现金流量，同花顺 Financial-API）")
    sf.add_argument("--codes", help="逗号分隔代码（如 600519,000858），与 --limit/--all 互斥")
    sf.add_argument("--limit", type=int, default=100, help="同步前 N 只 A 股（默认 100；0=全部，等价 --all）")
    sf.add_argument("--all", action="store_true", help="全市场同步（覆盖 --limit）")
    sf.add_argument("--period", default="quarterly", choices=["quarterly", "annual"], help="报告期口径（默认 quarterly）")
    sf.add_argument("--keep", type=int, default=8, help="每只股票保留最近 N 期（默认 8）")
    sf.add_argument("--sleep", type=float, default=0.05, help="每只股票间休眠秒数，限频保护（默认 0.05）")
    sf.add_argument("--db")

    sv = sub.add_parser("sync-valuation", help="估值快照逐日累积（PE/PB/PS/PCF，同花顺 Financial-API）")
    sv.add_argument("--codes", help="逗号分隔代码（如 600519,000858），与 --limit/--all 互斥")
    sv.add_argument("--limit", type=int, default=100, help="同步前 N 只 A 股（默认 100；0=全部，等价 --all）")
    sv.add_argument("--all", action="store_true", help="全市场同步（覆盖 --limit）")
    sv.add_argument("--batch", type=int, default=100, help="每批请求只数（接口上限 100，默认 100）")
    sv.add_argument("--sleep", type=float, default=0.1, help="每批之间休眠秒数，限频保护（默认 0.1）")
    sv.add_argument("--db")

    svh = sub.add_parser("sync-valuation-history", help="估值历史回填（东财 akshare，PE/PB/PS/PCF 自上市起全历史）")
    svh.add_argument("--codes", help="逗号分隔代码（如 600519,000858），与 --limit/--all 互斥")
    svh.add_argument("--limit", type=int, default=0, help="只回填前 N 只 A 股（默认 0=全部）")
    svh.add_argument("--all", action="store_true", help="全市场回填（默认即全部，等价 --limit 0）")
    svh.add_argument("--sleep", type=float, default=0.2, help="每只之间休眠秒数，限频保护（默认 0.2）")
    svh.add_argument("--resume", action="store_true", help="跳过已有 >=--resume-min 行的股票（断点续传）")
    svh.add_argument("--resume-min", type=int, default=20, help="--resume 时判定已回填的最少行数（默认 20）")
    svh.add_argument("--db")

    sk = sub.add_parser("streaks", help="全市场连续区间扫描（上涨/下跌/放量/缩量/量价齐升/量价齐缩/站稳均线/创新高）")
    sk.add_argument("kind", choices=["up", "down", "vol-up", "vol-down",
                                     "vol-price-up", "vol-price-down", "above-ma", "new-high"],
                    help="区间类型: up=连续上涨 down=连续下跌 vol-up=连续放量 vol-down=连续缩量 "
                         "vol-price-up=量价齐升 vol-price-down=量价齐缩 above-ma=连续站稳均线 new-high=连续创新高")
    sk.add_argument("--min", type=int, default=3, help="最少连续天数（默认 3）")
    sk.add_argument("--start", help="YYYY-MM-DD 起始日期（含）")
    sk.add_argument("--end", help="YYYY-MM-DD 结束日期（含）")
    sk.add_argument("--adjust", default="forward", choices=["none", "forward"],
                    help="涨跌判定用价: forward=前复权价(默认,推荐) none=未复权收盘价")
    sk.add_argument("--ma", type=int, default=20, help="above-ma 的均线周期（默认 20）")
    sk.add_argument("--window", type=int, default=20, help="new-high 的新高回看窗口（默认 20 日）")
    sk.add_argument("--codes", help="限定 thscode，逗号分隔（默认全市场）")
    sk.add_argument("--limit", type=int, default=20, help="输出前 N 段（默认 20）")
    sk.add_argument("--out", help="落盘 CSV 路径（可选）")
    sk.add_argument("--db")

    sb = sub.add_parser("streaks-backtest", help="streaks 信号 → 未来 N 日收益回测（事件研究）")
    sb.add_argument("kind", choices=["up", "down", "vol-up", "vol-down",
                                     "vol-price-up", "vol-price-down", "above-ma", "new-high"],
                    help="streak 类型（同 streaks）")
    sb.add_argument("--min", default="3", help="最少连续天数，可逗号分隔多个（默认 3）")
    sb.add_argument("--horizons", default="5,10,20", help="未来交易日，逗号分隔（默认 5,10,20）")
    sb.add_argument("--adjust", default="forward", choices=["none", "forward"],
                    help="涨跌判定用价（默认 forward 前复权）")
    sb.add_argument("--ma", type=int, default=20, help="above-ma 均线周期（默认 20）")
    sb.add_argument("--window", type=int, default=20, help="new-high 新高窗口（默认 20 日）")
    sb.add_argument("--start", help="仅回测「结束日 ≥ 此日期」的 streak（YYYY-MM-DD，分段看 regime）")
    sb.add_argument("--end", help="仅回测「结束日 ≤ 此日期」的 streak（YYYY-MM-DD）")
    sb.add_argument("--by-board", action="store_true", help="追加按板块（主板/创业板/科创板/北交所）分组收益")
    sb.add_argument("--by-year", action="store_true", help="追加按 streak 结束年分组的收益（看信号逐年有效性）")
    sb.add_argument("--db")

    sc = sub.add_parser("screen-bluechip", help="全市场白马/蓝筹基本面筛选（本地 DuckDB，落库+CSV）")
    sc.add_argument("--db")

    sgp = sub.add_parser("screen-golden-pit", help="全市场黄金坑技术初筛（本地 DuckDB，回撤+站半年线+缩量）")
    sgp.add_argument("--db")
    sgp.add_argument("--limit", type=int, default=500, help="输出前 N 只（默认 500）")
    sgp.add_argument("--out", help="落盘 CSV 路径（可选）")

    sb2 = sub.add_parser("screen-bottoming", help="全市场磨底技术初筛（本地 DuckDB，止跌+横盘+缩量）")
    sb2.add_argument("--db")
    sb2.add_argument("--limit", type=int, default=800, help="输出前 N 只（默认 800）")
    sb2.add_argument("--out", help="落盘 CSV 路径（可选）")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 2
    return {
        "bootstrap": cmd_bootstrap,
        "daily": cmd_daily,
        "panel": cmd_panel,
        "symbols": cmd_symbols,
        "status": cmd_status,
        "sync": cmd_sync,
        "sync-symbols": cmd_sync_symbols,
        "sync-financials": cmd_sync_financials,
        "sync-valuation": cmd_sync_valuation,
        "sync-valuation-history": cmd_sync_valuation_history,
        "streaks": cmd_streaks,
        "streaks-backtest": cmd_streaks_backtest,
        "screen-bluechip": cmd_screen_bluechip,
        "screen-golden-pit": cmd_screen_golden_pit,
        "screen-bottoming": cmd_screen_bottoming,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
