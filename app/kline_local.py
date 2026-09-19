"""本地 DuckDB（marketdb）日K适配层 —— 可选依赖。

优先用本地库读日K，避免逐只打远端历史接口（新浪限流/退避/串行 sleep）。
库不存在 / marketdb 未安装 / 查询失败时返回 None，由调用方 fallback 远端。

仅支持日线（duckdb 无分钟线），且历史数据到最近一次 ``/marketdb-sync`` 为止，
盘中缺当日 bar，故 ``fetch_daily_hybrid`` 用远端补当日。

marketdb 依赖 pandas/pyarrow/duckdb 重依赖，此处惰性 import，
保持 app/ 核心「no pandas、依赖最小化」约定不被破坏。
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
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


_REEXEC_ENV = "_MW_MARKETDB_REEXEC"


def _imports_marketdb(py: str) -> bool:
    """该解释器能否 import marketdb（用于探测正确的运行解释器）。"""
    try:
        r = subprocess.run([py, "-c", "import marketdb"], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def _candidate_pythons() -> list[str]:
    """候选解释器路径：MARKETDB_PYTHON → 常见 conda 安装（跨平台）。"""
    out = []
    explicit = os.environ.get("MARKETDB_PYTHON")
    if explicit:
        out.append(explicit)
    home = Path.home()
    if os.name == "nt":  # Windows
        for name in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"):
            out.append(str(home / name / "python.exe"))
            out.append(str(home / "AppData" / "Local" / name / "python.exe"))
    else:  # macOS / Linux
        for name in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"):
            out.append(str(home / name / "bin" / "python3"))
            out.append(str(home / name / "bin" / "python"))
    return out


def _find_marketdb_python() -> Optional[str]:
    """找一个已安装 marketdb 的解释器：先 load_env 注入 .env 的 MARKETDB_PYTHON，再探测 conda 路径。"""
    try:
        from .utils import load_env
        load_env(_ROOT)  # 注入 .env 的 MARKETDB_PYTHON（每台机器各自配置本机路径）
    except Exception:
        pass
    seen = set()
    for py in _candidate_pythons():
        if not py or py in seen or py == sys.executable:
            continue
        seen.add(py)
        if os.path.exists(py) and _imports_marketdb(py):
            return py
    return None


_warned_local_db = False


def ensure_marketdb_interpreter() -> None:
    """当前解释器缺 marketdb 时，自动切换到已装 marketdb 的解释器重跑（os.execv 替换进程）。

    用于 skill 批量扫描：本地库日 K 是主源，用错解释器会静默回退新浪并触发 456 限流。
    主程序（__main__.py）不依赖本地库也能跑，故不在此自动切换（仅由 skill 脚本路径调用）。
    找不到可用解释器时打印一次警告，避免静默降级。
    """
    global _warned_local_db
    try:
        import marketdb  # noqa: F401
        return  # 当前解释器已具备，无需切换
    except Exception:
        pass
    if os.environ.get(_REEXEC_ENV):
        return  # 已切换过一次仍失败，放弃，避免死循环
    if not available():
        return  # 本地库不存在，走新浪是预期（新环境未 bootstrap），不提示
    py = _find_marketdb_python()
    if py and py != sys.executable:
        os.environ[_REEXEC_ENV] = "1"
        os.execv(py, [py, *sys.argv])
        return
    if not _warned_local_db:
        _warned_local_db = True
        print("⚠️ 本地库 data/market.duckdb 存在，但当前解释器缺 marketdb，且未找到可用解释器",
              file=sys.stderr)
        print("   → 已回退新浪日K（批量扫描可能触发 456 限流、候选不全）。", file=sys.stderr)
        print("   → 解决：在 .env 配置 MARKETDB_PYTHON 指向已装 marketdb 的解释器（每台机器各自配置本机路径）。",
              file=sys.stderr)


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
    """盯盘日K：本地 duckdb 读历史 + 远端补缺口；库不可用/严重过期则整体远端兜底。

    本地库只覆盖到最近一次 /marketdb-sync，盘中缺当日 bar，故远端按「今日 - 本地最新日」
    的日历缺口动态拉取补齐（仅 days=2 会在本地停更多个交易日时出现日期断档）。
    缺口太大（>20 自然日）时前复权基准漂移、补缺口成本高，整体走远端兜底保证口径一致。
    """
    from .technical import fetch_historical_kline

    local = fetch_daily(code, market, days)
    if local is None:
        return fetch_historical_kline(code, market, days=days, scale=240)

    last_date = local[-1].date if local else ""
    try:
        gap_days = (datetime.date.today() - datetime.date.fromisoformat(last_date)).days
    except (TypeError, ValueError):
        gap_days = 0
    if gap_days > 20:
        return fetch_historical_kline(code, market, days=days, scale=240)

    if gap_days <= 0:
        # 本地已含当日 bar，无缺口，直接返回，省一次远端请求
        return local

    # 按日历缺口动态拉远端，只 append 比本地最新更晚的（去重），补齐中间缺失的交易日。
    try:
        for k in fetch_historical_kline(code, market, days=max(2, gap_days + 2), scale=240):
            if k.date > last_date:
                local.append(k)
    except Exception:
        pass
    return local


def fetch_daily_local_first(code: str, market: str, days: int = 60) -> list[KlineData]:
    """批量扫描日K：纯本地读（前复权），本地无该标的才远端兜底；不补当日缺口。

    与 ``fetch_daily_hybrid`` 的区别：不做「今日 - 本地最新日」的缺口远端补拉，
    避免批量扫描（几十~几百只）时对远端连续发起 datalen 小请求、再次触发新浪限流。
    筛选/打分的「深度回撤/箱体/均线/量能」只需历史日K到最近一次同步即可，
    当日实时变化由行情快照（东财 turnover_map）另行提供。
    """
    local = fetch_daily(code, market, days)
    if local:
        return local
    ensure_marketdb_interpreter()  # 本地库存在却读不到 → 多为解释器缺 marketdb，尝试切解释器重跑
    from .technical import fetch_historical_kline
    return fetch_historical_kline(code, market, days=days, scale=240)
