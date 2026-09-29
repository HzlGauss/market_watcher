"""
日内资金流时序落库（duckdb）

盯盘时 BackgroundDataCache 每 300s 拉取一次资金流（东财主源 + 妙想兜底），
此处把每次拉到的 5 档资金流 + 来源（source）落库，保留当日日内时序，
供 fund-flow 等 skill 复用当日盘中历史；并每天清理一次过期数据。

依赖 duckdb（仅 miniconda python3.13 有），系统 python3 无 duckdb 时静默降级为 no-op。
"""

from __future__ import annotations
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.utils import log

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "fund_flow_intraday.duckdb"
_PRUNE_DAYS = 7

_lock = threading.Lock()
_last_prune_day = ""


def _connect():
    import duckdb

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(_DB_PATH))


def _ensure_schema(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fund_flow_intraday (
            code VARCHAR,
            name VARCHAR,
            ts TIMESTAMP,
            main_net DOUBLE,
            super_large_net DOUBLE,
            large_net DOUBLE,
            medium_net DOUBLE,
            small_net DOUBLE,
            source VARCHAR
        )
        """
    )


def snapshot_from_detail(detail) -> dict:
    """FundFlowDetail → 增量计算用的快照 dict（含各档净流入 + source）。

    与 latest_snapshot() 返回同构，供 __main__ 跨扫描/跨进程统一比较。
    """
    return {
        "main": detail.main_net,
        "super_large": detail.super_large_net,
        "large": detail.large_net,
        "medium": detail.medium_net,
        "small": detail.small_net,
        "source": detail.source,
    }


def latest_snapshot(code: str, source: Optional[str] = None,
                    exclude_main: Optional[float] = None) -> Optional[dict]:
    """读某代码最近一条资金流快照（进程重启首轮扫描的内存兜底）。

    指定 source 时只取同源（东财/妙想口径不一致，不跨源比较）。duckdb 缺失或无记录返回 None。
    exclude_main 给定时，跳过 main_net 与其相等的最新快照（即本次扫描刚由后台
    write_snapshot 写入的自身快照），从而拿到「上一次」真实快照、算出有意义的增量。
    """
    try:
        with _lock:
            con = _connect()
            try:
                _ensure_schema(con)
                cols = "main_net, super_large_net, large_net, medium_net, small_net, source"
                limit = 3 if exclude_main is not None else 1
                if source:
                    rows = con.execute(
                        f"SELECT {cols} FROM fund_flow_intraday "
                        "WHERE code = ? AND source = ? ORDER BY ts DESC LIMIT ?",
                        [code, source, limit],
                    ).fetchall()
                else:
                    rows = con.execute(
                        f"SELECT {cols} FROM fund_flow_intraday "
                        "WHERE code = ? ORDER BY ts DESC LIMIT ?",
                        [code, limit],
                    ).fetchall()
                for r in rows:
                    if exclude_main is not None and r[0] == exclude_main:
                        continue  # 跳过自身快照
                    return {
                        "main": r[0],
                        "super_large": r[1],
                        "large": r[2],
                        "medium": r[3],
                        "small": r[4],
                        "source": r[5],
                    }
                return None
            finally:
                con.close()
    except ImportError:
        return None  # 无 duckdb（系统 python3），静默降级
    except Exception as e:
        log.debug(f"读资金流快照失败: {e}")
        return None


def write_snapshot(code: str, name: str, detail) -> None:
    """落一条资金流快照（detail: FundFlowDetail）。duckdb 缺失时静默跳过。"""
    if detail is None:
        return
    try:
        with _lock:
            con = _connect()
            try:
                _ensure_schema(con)
                con.execute(
                    """
                    INSERT INTO fund_flow_intraday
                        (code, name, ts, main_net, super_large_net,
                         large_net, medium_net, small_net, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        code,
                        name,
                        datetime.now(),
                        detail.main_net,
                        detail.super_large_net,
                        detail.large_net,
                        detail.medium_net,
                        detail.small_net,
                        detail.source or "eastmoney",
                    ],
                )
            finally:
                con.close()
    except ImportError:
        pass  # 无 duckdb（系统 python3），静默跳过
    except Exception as e:
        log.debug(f"资金流落库失败: {e}")
    _maybe_prune()


def prune_old(days: int = _PRUNE_DAYS) -> int:
    """删除 N 天前的过期数据，返回删除条数。duckdb 缺失时返回 0。"""
    try:
        with _lock:
            con = _connect()
            try:
                _ensure_schema(con)
                cutoff = datetime.now() - timedelta(days=days)
                con.execute(
                    "DELETE FROM fund_flow_intraday WHERE ts < ?", [cutoff]
                )
            finally:
                con.close()
        return 0
    except ImportError:
        return 0
    except Exception as e:
        log.debug(f"资金流过期清理失败: {e}")
        return 0


def _maybe_prune() -> None:
    """每天最多清理一次过期数据（避免每次落库都全表扫描）。"""
    global _last_prune_day
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_prune_day == today:
        return
    _last_prune_day = today
    prune_old(_PRUNE_DAYS)
