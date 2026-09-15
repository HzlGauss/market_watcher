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
    sync-symbols        刷新 dim_symbol 证券维度表（symbols 子命令依赖）
    streaks             全市场「连续上涨/下跌/放量/缩量」区间扫描（窗口函数三步法）

用法:
    py tools/marketdb_local.py bootstrap
    py tools/marketdb_local.py daily 600519.SH --adjust forward --start 2025-01-01
    py tools/marketdb_local.py panel --start 2026-01-01 --end 2026-01-31 --out out/panel.csv

数据源: 同花顺 marketdb 包（pip install -e C:/work/code/Financial-API/python）。
API Key 走环境变量 HITHINK_FINANCE_API_KEY（snowball .env 已配置，自动注入）。
库文件默认 data/market.duckdb（可用环境变量 MARKETDB_DB_PATH 覆盖）。

注意: marketdb 只覆盖历史行情/面板，**不覆盖** trading-days / limit-up-pool /
dragon-tiger-list 等特色数据实时端点 —— 涨跌停/龙虎榜/日历仍走 app/hithink.py REST。
"""
from __future__ import annotations

import argparse
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

# marketdb 源码目录（同花顺 Financial-API monorepo，本机已 clone）
_FINANCE_PY = Path(os.environ.get("MARKETDB_SRC", "C:/work/code/Financial-API/python"))
_DEFAULT_DB = _ROOT / "data" / "market.duckdb"


def _db_path(explicit: str | None = None) -> Path:
    p = explicit or os.environ.get("MARKETDB_DB_PATH")
    return Path(p).expanduser() if p else _DEFAULT_DB


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
    if not _FINANCE_PY.exists():
        print(f"❌ 未找到 marketdb 源码目录: {_FINANCE_PY}")
        print("   请设置环境变量 MARKETDB_SRC 指向 Financial-API/python，或 clone 到默认路径。")
        return False
    print(f"==> 安装 marketdb（pip install -e {_FINANCE_PY}）...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(_FINANCE_PY)])
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
        print(f"❌ 本地库不存在: {db}（先运行 bootstrap）")
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
        print(f"❌ 本地库不存在: {db}（先运行 bootstrap）")
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
        print(f"❌ 本地库不存在: {db}（先运行 bootstrap）")
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
        print(f"❌ 本地库不存在: {db}（先运行 bootstrap）")
        return 1
    _cli("status", db=db)
    return 0


def cmd_sync_symbols(args) -> int:
    """刷新 dim_symbol 证券维度表（一次 REST tickers 请求，symbols 子命令依赖它）。"""
    _load_key()
    db = _db_path(args.db)
    if not db.exists():
        print(f"❌ 本地库不存在: {db}（先运行 bootstrap）")
        return 1
    r = _cli("sync-symbols", db=db)
    return r.returncode


# ---------------------------------------------------------------- streaks

_STREAK_LABEL = {
    "up": "连续上涨",
    "down": "连续下跌",
    "vol-up": "连续放量",
    "vol-down": "连续缩量",
}

# kind -> 断点条件（True=断点，即中断当前连续段）
_STREAK_BREAK = {
    "up": "prev IS NULL OR adj <= prev",
    "down": "prev IS NULL OR adj >= prev",
    "vol-up": "prev_vol IS NULL OR vol <= prev_vol",
    "vol-down": "prev_vol IS NULL OR vol >= prev_vol",
}


def cmd_streaks(args) -> int:
    """全市场「连续上涨/下跌/放量/缩量」区间扫描（窗口函数三步法：断点标记→累计求和→分组聚合）。

    借鉴 DuckDB 窗口函数「连续区间」技巧：LAG() 取前一行 → CASE 标记断点 →
    SUM() OVER() 生成组号 → GROUP BY 组号聚合出每段起止日期与持续天数。
    涨跌判定默认用前复权价（close*forward_factor），除权除息日不会误判断点。
    """
    db = _db_path(args.db)
    if not db.exists():
        print(f"❌ 本地库不存在: {db}（先运行 bootstrap）")
        return 1
    try:
        import duckdb
    except ImportError:
        print("❌ duckdb 未安装（先运行 bootstrap 安装 marketdb）")
        return 1

    price_based = args.kind in ("up", "down")
    val_expr = "k.close" if args.adjust == "none" else "k.close * COALESCE(a.forward_factor, 1.0)"
    brk = _STREAK_BREAK[args.kind]

    where, params = [], []
    if args.codes:
        codes = [_norm_thscode(c) for c in args.codes.split(",")]
        where.append("k.thscode IN (%s)" % ",".join("?" for _ in codes))
        params.extend(codes)
    if args.start:
        where.append("k.date >= ?")
        params.append(args.start)
    if args.end:
        where.append("k.date <= ?")
        params.append(args.end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    agg = (
        "arg_min(adj, date) AS start_val, arg_max(adj, date) AS end_val"
        if price_based
        else "arg_min(vol, date) AS start_val, arg_max(vol, date) AS end_val"
    )

    sql = f"""
WITH px AS (
  SELECT k.thscode, k.date, {val_expr} AS adj, k.volume AS vol
  FROM raw_kline_daily k
  LEFT JOIN calc_adjust_factor_daily a ON a.thscode = k.thscode AND a.date = k.date
  {where_sql}
),
lagged AS (
  SELECT thscode, date, adj, vol,
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
HAVING COUNT(*) >= {int(args.min)}
ORDER BY streak_days DESC, g.thscode, start_date
LIMIT {int(args.limit)}
"""
    con = duckdb.connect(str(db), read_only=True)
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()

    if df.empty:
        print(f"⚠️ 未找到「{_STREAK_LABEL[args.kind]}」≥{args.min} 天的区间")
        return 0

    label = _STREAK_LABEL[args.kind]
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

    ss = sub.add_parser("sync-symbols", help="刷新 dim_symbol 证券维度表（symbols 子命令依赖）")
    ss.add_argument("--db")

    sk = sub.add_parser("streaks", help="全市场连续区间扫描（连续上涨/下跌/放量/缩量）")
    sk.add_argument("kind", choices=["up", "down", "vol-up", "vol-down"],
                    help="区间类型: up=连续上涨 down=连续下跌 vol-up=连续放量 vol-down=连续缩量")
    sk.add_argument("--min", type=int, default=3, help="最少连续天数（默认 3）")
    sk.add_argument("--start", help="YYYY-MM-DD 起始日期（含）")
    sk.add_argument("--end", help="YYYY-MM-DD 结束日期（含）")
    sk.add_argument("--adjust", default="forward", choices=["none", "forward"],
                    help="涨跌判定用价: forward=前复权价(默认,推荐) none=未复权收盘价")
    sk.add_argument("--codes", help="限定 thscode，逗号分隔（默认全市场）")
    sk.add_argument("--limit", type=int, default=20, help="输出前 N 段（默认 20）")
    sk.add_argument("--out", help="落盘 CSV 路径（可选）")
    sk.add_argument("--db")

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
        "sync-symbols": cmd_sync_symbols,
        "streaks": cmd_streaks,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
