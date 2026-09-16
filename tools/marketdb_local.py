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
    streaks             全市场「连续上涨/下跌/放量/缩量」区间扫描（窗口函数三步法）

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
                  end: str | None = None, codes: list[str] | None = None,
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
HAVING COUNT(*) >= {int(min_days)}
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
                f" 约 {f['behind_days']} 天；streaks 初筛按此快照，今日/盘中新信号以新浪实时复核为准"
                f"（可先 py tools/marketdb_local.py sync 同步）。")
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
        df = query_streaks(kind, min_days=min_days, codes=list(codes), limit=limit, db=db_path)
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
    return _cli("auto-sync", db=db_path).returncode


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
                     db=None, adjust: str = "forward", ma: int = 20, window: int = 20):
    """对指定 streak kind 做「信号 → 未来 H 交易日收益」事件研究回测，返回 pandas DataFrame。

    对每条结束于 end_date 的 streak（连续天数 ≥ min_days），取 end_date 之后第 H 个交易日的
    前复权价算 forward return（H ∈ horizons）。future 价用 LEAD(adj, H)，未来不足 H 行的样本
    该 horizon 记为 NaN（自动排除停牌/退市/接近库最新日的样本）。返回列：
    thscode / start_date / end_date / days / end_val / f{H} / ret{H}。无库/无 duckdb 返回
    None，无匹配返回空 DataFrame。

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
ORDER BY s.end_date
"""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(sql).fetchdf()
    finally:
        con.close()
    for h in horizons:
        df[f"ret{h}"] = df[f"f{h}"] / df["end_val"] - 1.0
    return df


def _baseline_fwd(db_path, horizons, val_expr) -> dict:
    """全市场无条件 forward return 基准（同期对照），返回 {h: (n, win%, mean%, median%)}。"""
    leads = ", ".join(
        f"LEAD(adj, {h}) OVER (PARTITION BY thscode ORDER BY date) / adj - 1.0 AS r{h}"
        for h in horizons
    )
    sel = ", ".join(
        f"COUNT(r{h}) AS n{h}, 100.0*AVG((r{h}>0)::INT) AS win{h}, "
        f"100.0*AVG(r{h}) AS mean{h}, 100.0*MEDIAN(r{h}) AS med{h}"
        for h in horizons
    )
    sql = f"""
WITH px AS (
  SELECT k.thscode, k.date, {val_expr} AS adj
  FROM raw_kline_daily k
  LEFT JOIN calc_adjust_factor_daily a ON a.thscode = k.thscode AND a.date = k.date
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
        df = con.execute(sql).fetchdf()
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

    base = _baseline_fwd(db, horizons, val_expr)

    print(f"== streaks 回测：{label}（kind={args.kind}，{args.adjust}复权）==")
    print(f"   未来 {list(horizons)} 交易日；每格 = 胜率% / 均收益% / 中位收益%")
    print()

    print(f"  {'min':>3} {'N':>7} | " + " | ".join(f"fut{h} 胜/均/中" for h in horizons))
    print("  " + "-" * (18 + 22 * len(horizons)))
    for mn in mins:
        df = backtest_streaks(args.kind, min_days=mn, horizons=horizons, db=db,
                              adjust=args.adjust, ma=args.ma, window=args.window)
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

    if getattr(args, "by_board", False):
        bdf = backtest_streaks(args.kind, min_days=mins[0], horizons=horizons, db=db,
                               adjust=args.adjust, ma=args.ma, window=args.window)
        if bdf is not None and not bdf.empty:
            bdf["board"] = bdf["thscode"].map(_board_of)
            print()
            print(f"  ── 分板块（min_days={mins[0]}，{label}）──")
            print(f"  {'板块':<6} {'N':>7} | " + " | ".join(f"fut{h} 胜/均/中" for h in horizons))
            print("  " + "-" * (18 + 22 * len(horizons)))
            for board in ("主板", "创业板", "科创板", "北交所"):
                sub = bdf[bdf["board"] == board]
                if sub.empty:
                    continue
                cells = []
                for h in horizons:
                    s = sub[f"ret{h}"].dropna()
                    if s.empty:
                        cells.append("-- / -- / --")
                    else:
                        cells.append(f"{(s > 0).mean()*100.0:.1f} / {s.mean()*100.0:+.2f} / {s.median()*100.0:+.2f}")
                print(f"  {board:<6} {len(sub):>7} | " + " | ".join(cells))
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
    sb.add_argument("--by-board", action="store_true", help="追加按板块（主板/创业板/科创板/北交所）分组收益")
    sb.add_argument("--db")

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
        "streaks": cmd_streaks,
        "streaks-backtest": cmd_streaks_backtest,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
