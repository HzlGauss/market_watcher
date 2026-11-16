"""本地 DuckDB ETF 日K 缓存 —— 可选依赖，补齐 marketdb 只含 A 股、无 ETF 的缺口。

同花顺 marketdb 的 Parquet dump 只提供 a-share，本地 data/market.duckdb 的
dim_symbol 里没有 ETF 号段（51/56/58/15/16/18），导致持仓 ETF 的日K 每次都要
远端（新浪 → 同花顺 → akshare）。本模块给持仓 ETF 日K 加一层独立 duckdb 缓存
（data/etf_kline.duckdb），由 tools/sync_etf_kline.py 落盘；读时先查库、命中即返回，
由 kline_local.fetch_daily 对 ETF 号段优先读取。

duckdb 为可选重依赖，此处惰性 import；库不存在 / 读取失败时静默降级返回 None，
由调用方走远端兜底，绝不因此报错或卡住。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from .models import KlineData

_ROOT = Path(__file__).resolve().parents[1]

_WRITE_LOCK = threading.Lock()

# 场内基金（ETF/LOF）代码号段，与 app/hithink.py、app/technical.py 口径一致：
# 沪 51/56/58，深 15/16/18。
_ETF_CODE_PREFIXES = ("51", "56", "58", "15", "16", "18")


def is_etf_code(code: str) -> bool:
    """6 位代码是否为场内基金（ETF/LOF），按号段判断。"""
    return str(code).strip().startswith(_ETF_CODE_PREFIXES)


def db_path() -> Path:
    p = os.environ.get("ETF_KLINE_DB_PATH")
    if p:
        return Path(p).expanduser()
    return _ROOT / "data" / "etf_kline.duckdb"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN


def _ensure_schema(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS etf_kline_daily (
            code TEXT, market TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_etf_kline_uniq "
        "ON etf_kline_daily(code, market, trade_date)"
    )


def read_daily(code: str, market: str, days: int = 60) -> Optional[list[KlineData]]:
    """读本地 ETF 日K（升序，最近 days 日），库不存在/无数据/失败返回 None。"""
    db = db_path()
    if not db.exists():
        return None
    try:
        import duckdb
    except Exception:
        return None
    try:
        with duckdb.connect(str(db), read_only=True) as con:
            rows = con.execute(
                "SELECT trade_date, open, high, low, close, volume FROM etf_kline_daily "
                "WHERE code = ? AND market = ? ORDER BY trade_date DESC LIMIT ?",
                [code, market, int(days)],
            ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    rows = list(reversed(rows))  # 升序
    return [
        KlineData(date=str(r[0]), open=_f(r[1]), high=_f(r[2]),
                  low=_f(r[3]), close=_f(r[4]), volume=_f(r[5]))
        for r in rows
    ]


def latest_date(code: str, market: str) -> Optional[str]:
    """本地已存最新交易日，无数据返回 None（供同步脚本看落盘新鲜度）。"""
    db = db_path()
    if not db.exists():
        return None
    try:
        import duckdb
    except Exception:
        return None
    try:
        with duckdb.connect(str(db), read_only=True) as con:
            row = con.execute(
                "SELECT MAX(trade_date) FROM etf_kline_daily WHERE code = ? AND market = ?",
                [code, market],
            ).fetchone()
    except Exception:
        return None
    return str(row[0]) if row and row[0] else None


def count(code: str, market: str) -> int:
    """本地已存 bar 数，无数据返回 0（供同步脚本看落盘状态）。"""
    db = db_path()
    if not db.exists():
        return 0
    try:
        import duckdb
    except Exception:
        return 0
    try:
        with duckdb.connect(str(db), read_only=True) as con:
            row = con.execute(
                "SELECT COUNT(*) FROM etf_kline_daily WHERE code = ? AND market = ?",
                [code, market],
            ).fetchone()
    except Exception:
        return 0
    return int(row[0]) if row and row[0] else 0


def write(code: str, market: str, klines: list[KlineData]) -> None:
    """merge 写回（唯一索引 upsert），失败静默降级。"""
    if not klines:
        return
    try:
        import duckdb
    except Exception:
        return

    rows = []
    for k in klines:
        td = (k.date or "")[:10]
        if not td:
            continue
        rows.append((code, market, td, k.open, k.high, k.low, k.close, k.volume))
    if not rows:
        return

    with _WRITE_LOCK:
        try:
            with duckdb.connect(str(db_path())) as con:
                _ensure_schema(con)
                con.executemany(
                    "INSERT OR REPLACE INTO etf_kline_daily "
                    "(code, market, trade_date, open, high, low, close, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except Exception:
            pass
