#!/usr/bin/env python3
"""持仓 ETF 日K 本地落盘 —— 补齐 marketdb 只含 A 股、无 ETF 的缺口。

同花顺 marketdb 的 Parquet dump 只提供 a-share，本地 data/market.duckdb 里没有
ETF 号段，持仓 ETF 的日K 每次都远端（新浪 → 同花顺 → akshare）。本脚本读
holdings.csv 里的 ETF（号段 51/56/58/15/16/18），用同花顺 /api/fund/market/historical
（ETF 固定前复权、无分钟线）拉日K，落 data/etf_kline.duckdb，供
kline_local.fetch_daily 对 ETF 号段本地优先读取。

子命令:
    full [--days 2500]    全历史回填（端点数据深度约 5 年，实际最多 ~1210 根，覆盖上市满 5 年 ETF 全部历史）
    daily [--days 250]    收盘后增量（默认近 250 交易日 ≈ 1 年，upsert 自愈任意缺口）
    status                查看落盘状态（各 ETF bar 数 + 最新日期）

用法:
    py tools/sync_etf_kline.py full
    py tools/sync_etf_kline.py daily
    py tools/sync_etf_kline.py status

数据源: 同花顺 /api/fund/market/historical（app.hithink.fetch_daily_kline）。
API Key 走环境变量 HITHINK_FINANCE_API_KEY（snowball .env 已配置，自动注入）。
库文件默认 data/etf_kline.duckdb（可用环境变量 ETF_KLINE_DB_PATH 覆盖）。

注意: 只覆盖 holdings.csv 里的 ETF；若要扩大范围（watchlist 或任意 ETF 按需补拉），
改 _holdings_etfs 的数据源即可。ETF 号段的 market 从代码推断，holdings.csv 的 market 留空也能补。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（tools/sync_etf_kline.py 的上一级）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_HOLDINGS = _ROOT / "holdings.csv"


def _load_key() -> None:
    """把 snowball .env 里的 HITHINK_FINANCE_API_KEY 注入 os.environ（hithink 按次读取）。"""
    try:
        from app.utils import load_env
        load_env(_ROOT)
    except Exception:
        pass


def _holdings_etfs() -> list[tuple[str, str, str]]:
    """读 holdings.csv，返回 [(code, market, name)] 中的 ETF 子集（market 已推断补全）。"""
    from app.etf_kline_local import is_etf_code
    from app.helpers import _detect_market

    out: list[tuple[str, str, str]] = []
    if not _HOLDINGS.exists():
        print(f"❌ 未找到 {_HOLDINGS}")
        return out
    with open(_HOLDINGS, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("code") or "").strip()
            if not code or not is_etf_code(code):
                continue
            market = _detect_market(code, row.get("market") or "")
            name = (row.get("name") or "").strip()
            out.append((code, market, name))
    # 去重（同代码多行只拉一次），保持出现顺序
    seen: set[str] = set()
    dedup: list[tuple[str, str, str]] = []
    for code, market, name in out:
        if code in seen:
            continue
        seen.add(code)
        dedup.append((code, market, name))
    return dedup


def _pull(code: str, market: str, days: int) -> list:
    """拉单只 ETF 日K（升序 dict 列表），失败/无数据返回空。"""
    from app import hithink
    from app.models import KlineData

    try:
        raw = hithink.fetch_daily_kline(code, market, days=days)
    except Exception as e:
        print(f"  ⚠️ {code} 拉取异常: {e}")
        return []
    if not raw:
        return []
    out = []
    for it in raw:
        k = KlineData(
            date=it.get("date", ""),
            open=it.get("open"),
            high=it.get("high"),
            low=it.get("low"),
            close=it.get("close"),
            volume=it.get("volume"),
        )
        if k.date:
            out.append(k)
    return out


def _sync(days: int) -> int:
    """对 holdings 里的 ETF 逐只拉近 days 日并 upsert，返回成功落盘的 ETF 数。"""
    from app import etf_kline_local

    etfs = _holdings_etfs()
    if not etfs:
        print("❌ holdings.csv 里没有 ETF（号段 51/56/58/15/16/18）")
        return 0
    print(f"==> 同步 {len(etfs)} 只持仓 ETF 日K（近 {days} 交易日）...")
    ok = 0
    for code, market, name in etfs:
        kl = _pull(code, market, days)
        if not kl:
            print(f"  ✗ {code} {name}: 无数据（跳过）")
            continue
        etf_kline_local.write(code, market, kl)
        total = etf_kline_local.count(code, market)
        latest = etf_kline_local.latest_date(code, market)
        print(f"  ✓ {code} {name}: 本次 {len(kl)} 根，落库 {total} 根，最新 {latest}")
        ok += 1
    return ok


def _status() -> None:
    from app import etf_kline_local

    etfs = _holdings_etfs()
    if not etfs:
        print("❌ holdings.csv 里没有 ETF")
        return
    print(f"{'code':<8} {'name':<14} {'bars':>6} {'latest':>12}")
    print("-" * 44)
    for code, market, name in etfs:
        n = etf_kline_local.count(code, market)
        d = etf_kline_local.latest_date(code, market) or "-"
        print(f"{code:<8} {name:<14} {n:>6} {d:>12}")


def main() -> int:
    parser = argparse.ArgumentParser(description="持仓 ETF 日K 本地落盘（同花顺 → DuckDB）")
    sub = parser.add_subparsers(dest="cmd")

    p_full = sub.add_parser("full", help="全历史回填")
    p_full.add_argument("--days", type=int, default=2500, help="回填交易日数（默认 2500；端点仅存约 5 年，实际最多 ~1210 根）")

    p_daily = sub.add_parser("daily", help="收盘后增量")
    p_daily.add_argument("--days", type=int, default=250, help="增量交易日数（默认 250 ≈ 1 年，自愈缺口）")

    sub.add_parser("status", help="查看落盘状态")

    args = parser.parse_args()
    _load_key()

    if args.cmd in (None, "full"):
        days = getattr(args, "days", 2500)
        n = _sync(days)
        print(f"==> 完成：{n} 只 ETF 落盘")
    elif args.cmd == "daily":
        n = _sync(args.days)
        print(f"==> 完成：{n} 只 ETF 落盘")
    elif args.cmd == "status":
        _status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
