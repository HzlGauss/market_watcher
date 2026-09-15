"""本地 DuckDB（marketdb）日K适配层 —— 可选依赖。

优先用本地库读日K，避免逐只打远端历史接口（新浪限流/退避/串行 sleep）。
库不存在 / marketdb 未安装 / 查询失败时返回 None，由调用方 fallback 远端。

仅支持日线（duckdb 无分钟线），且历史数据到最近一次 ``/marketdb-sync`` 为止，
盘中缺当日 bar，故 ``fetch_daily_hybrid`` 用远端补当日。

marketdb 依赖 pandas/pyarrow/duckdb 重依赖，此处惰性 import，
保持 app/ 核心「no pandas、依赖最小化」约定不被破坏。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .models import KlineData

_ROOT = Path(__file__).resolve().parents[1]


def _db_path() -> Path:
    p = os.environ.get("MARKETDB_DB_PATH")
    if p:
        return Path(p).expanduser()
    return _ROOT / "data" / "market.duckdb"


def available() -> bool:
    """本地库是否存在（轻量，仅查文件，不 import marketdb）。"""
    return _db_path().exists()


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN


def fetch_daily(code: str, market: str, days: int = 60) -> Optional[list[KlineData]]:
    """读本地前复权日K（升序，最近 days 日），不可用返回 None。"""
    db = _db_path()
    if not db.exists():
        return None
    try:
        from marketdb import MarketDB
        from .hithink import thscode
    except Exception:
        return None

    try:
        with MarketDB.open(db) as mdb:
            df = mdb.get_daily(thscode(code, market), adjust="forward")
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None

    df = df.sort_values("date").tail(days)
    out: list[KlineData] = []
    for rec in df.to_dict("records"):
        d = rec.get("date")
        out.append(KlineData(
            date=str(d)[:10] if d is not None else "",
            open=_f(rec.get("open")),
            high=_f(rec.get("high")),
            low=_f(rec.get("low")),
            close=_f(rec.get("close")),
            volume=_f(rec.get("volume")),
        ))
    return out


def fetch_daily_hybrid(code: str, market: str, days: int = 60) -> list[KlineData]:
    """盯盘日K：本地 duckdb 读历史 + 远端补当日；库不可用则整体远端兜底。

    当日 bar 的前复权价等于未复权价（前复权基准即最新交易日），
    故远端补当日与本地历史拼接无复权基准跳变。
    """
    from .technical import fetch_historical_kline

    local = fetch_daily(code, market, days)
    if local is None:
        return fetch_historical_kline(code, market, days=days, scale=240)

    # 补当日：拉最近 2 根远端日K，只 append 比本地最新更晚的（去重）
    try:
        last_date = local[-1].date if local else ""
        for k in fetch_historical_kline(code, market, days=2, scale=240):
            if k.date > last_date:
                local.append(k)
    except Exception:
        pass
    return local
