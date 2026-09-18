"""本地 DuckDB 5 分钟 K 线缓存 —— 可选依赖，解决新浪 456 限流。

盘中盯盘线程 + 多个 skill 高频打新浪 scale=5 分钟 K 线接口，触发 456 限流。
本模块给 5 分钟 K 线加一层本地 duckdb 缓存：读时先查库、只补增量、
当天首次访问清理历史（保留「当天 + 前 1 交易日」，删更早的）。

duckdb 为可选重依赖，此处惰性 import，保持 app/ 核心「no pandas、依赖最小化」约定。
库不存在 / duckdb 未安装 / 读写失败时静默降级（try_read_fresh 返回 None、write 跳过），
由调用方走远端兜底，绝不因此报错或卡住。
"""
from __future__ import annotations

import datetime
import os
import threading
from pathlib import Path
from typing import Optional

from .models import KlineData

_ROOT = Path(__file__).resolve().parents[1]

_WRITE_LOCK = threading.Lock()
_last_clean_date: Optional[str] = None  # 每日清理节流标记（进程内）


def _db_path() -> Path:
    p = os.environ.get("MIN5_DB_PATH")
    if p:
        return Path(p).expanduser()
    return _ROOT / "data" / "min5.duckdb"


def _today() -> str:
    return datetime.date.today().isoformat()


def _cutoff_date() -> str:
    """清理分界线：保留当天 + 前 1 自然日，删更早的。"""
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN


def _is_trading_now(now: datetime.datetime) -> bool:
    """简化 A 股交易时段判断（周一~周五，9:30-11:30 / 13:00-15:00）。"""
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def _ensure_schema(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS min5_kline (
            code TEXT, market TEXT, trade_date TEXT, ts TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_min5_uniq "
        "ON min5_kline(code, market, ts)"
    )


def try_read_fresh(code: str, market: str) -> Optional[list[KlineData]]:
    """读当日（+前 1 日）5 分钟 K 线，够新则返回列表，否则返回 None 由调用方走远端。"""
    db = _db_path()
    if not db.exists():
        return None
    try:
        import duckdb
    except Exception:
        return None

    global _last_clean_date
    today = _today()
    cutoff = _cutoff_date()

    try:
        with duckdb.connect(str(db)) as con:
            _ensure_schema(con)
            # 每日节流清理：删早于「前 1 日」的历史（5min 数据只对当天有意义）
            if _last_clean_date != today:
                con.execute("DELETE FROM min5_kline WHERE trade_date < ?", [cutoff])
                _last_clean_date = today

            rows = con.execute(
                "SELECT ts, open, high, low, close, volume FROM min5_kline "
                "WHERE code = ? AND market = ? AND trade_date >= ? ORDER BY ts",
                [code, market, cutoff],
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    klines = [
        KlineData(date=str(r[0]), open=_f(r[1]), high=_f(r[2]),
                  low=_f(r[3]), close=_f(r[4]), volume=_f(r[5]))
        for r in rows
    ]

    last_ts = klines[-1].date
    try:
        last_dt = datetime.datetime.fromisoformat(last_ts)
    except (TypeError, ValueError):
        return None

    now = datetime.datetime.now()
    # 盘中：最后 bar 距 now < 8 分钟算新鲜；午休（11:30-13:00）期间 11:30 的 bar 也算新鲜
    if _is_trading_now(now):
        if (now - last_dt).total_seconds() < 480:
            return klines
        hm = now.hour * 60 + now.minute
        if 11 * 60 + 30 < hm < 13 * 60 and last_dt.hour * 60 + last_dt.minute >= 11 * 60 + 30:
            return klines
        return None

    # 非交易时段：最后 bar 是当天（已定格）则直接返回
    if last_dt.date().isoformat() == today:
        return klines
    return None


def write(code: str, market: str, klines: list[KlineData]) -> None:
    """把远端结果 merge 写回（唯一索引去重），失败静默降级。"""
    if not klines:
        return
    try:
        import duckdb
    except Exception:
        return

    rows = []
    for k in klines:
        ts = k.date or ""
        td = ts[:10]
        if not td:
            continue
        rows.append((code, market, td, ts, k.open, k.high, k.low, k.close, k.volume))
    if not rows:
        return

    with _WRITE_LOCK:
        try:
            with duckdb.connect(str(_db_path())) as con:
                _ensure_schema(con)
                con.executemany(
                    "INSERT OR IGNORE INTO min5_kline "
                    "(code, market, trade_date, ts, open, high, low, close, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except Exception:
            pass
