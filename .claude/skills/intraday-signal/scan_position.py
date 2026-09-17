#!/usr/bin/env python3
"""加减仓批量扫描：扫描 holdings.csv 各持仓，按日K周期阶段给出加/减仓信号 + 建议挂单价。

核心思路：
    1. 读 holdings.csv 持仓池
    2. 批量拉实时快照（新浪 + 腾讯量比/换手率）
    3. 逐标的拉日 K 线（本地 duckdb 前复权 + 远端补缺口）→ detect_stage 周期阶段
    4. 阶段 → 加/减仓动作（启动期/磨底期/下跌期=加，赶顶期/派发期=减），按置信度降序

与做 T 扫描（scan_t0，5 分钟日内）不同，加减仓是日K级、慢变量，盘中/盘后均可运行。
本脚本只做「取数 + 排序」，是否操作由 AI 依据 SKILL.md 框架生成。

用法:
    py .claude/skills/intraday-signal/scan_position.py [数量]

数据源: 实时快照（新浪+腾讯）、日 K 线（本地 duckdb + 新浪兜底）。不依赖 MX_APIKEY。
"""
import csv
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/intraday-signal 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

import logging
logging.disable(logging.WARNING)

try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass

from app.models import WatchItem
from app.helpers import _detect_market
from app.data_fetcher import fetch_quotes
from app.technical import fetch_historical_kline
from app.t0_monitor import evaluate_position_signal, PositionSignal


def _is_etf(code: str) -> bool:
    return code.startswith(("51", "56", "58", "15", "16", "18"))


def _read_holdings() -> list[dict]:
    """读 holdings.csv → [{code, name, market, is_etf}]（market 空则自动检测）。"""
    path = _ROOT / "holdings.csv"
    if not path.exists():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = str(row.get("code", "")).strip().zfill(6)
                name = str(row.get("name", "")).strip()
                market = str(row.get("market", "")).strip().upper()
                if len(code) != 6:
                    continue
                market = _detect_market(code, market)
                out.append({"code": code, "name": name, "market": market,
                            "is_etf": _is_etf(code)})
    except Exception:
        pass
    return out


def _fetch_daily(code: str, market: str, days: int = 60):
    """日K线：本地 duckdb 优先（前复权 + 补当日），失败回退新浪。"""
    try:
        from app import kline_local
        out = kline_local.fetch_daily_hybrid(code, market, days=days)
        if out:
            return out
    except Exception:
        pass
    return fetch_historical_kline(code, market, days=days, scale=240)


def main():
    argv = sys.argv[1:]
    limit = None
    for a in argv:
        if a.strip().isdigit():
            limit = max(1, int(a.strip()))

    holdings = _read_holdings()
    print("=" * 72)
    print(f"加减仓批量扫描（holdings.csv 共 {len(holdings)} 标的 · 日K周期阶段）")
    print("=" * 72)

    if not holdings:
        print("  ⚠️ holdings.csv 为空或不存在")
        return 1

    items = [WatchItem(name=h["name"], code=h["code"], market=h["market"],
                       type="ETF" if h["is_etf"] else "个股") for h in holdings]
    quotes = fetch_quotes(items) or []
    quote_map = {q.code: q for q in quotes}

    signals = []
    for h, item in zip(holdings, items):
        q = quote_map.get(item.code)
        if q is None:
            continue
        klines = _fetch_daily(item.code, item.market)
        sig = evaluate_position_signal(item, q, klines)
        if sig:
            signals.append(sig)

    if not signals:
        print("  ⚠️ 当前无加减仓信号（持仓均处于主升浪/震荡，或日K数据不足）")
        return 0

    adds = [s for s in signals if s.action == PositionSignal.ACTION_ADD]
    reduces = [s for s in signals if s.action == PositionSignal.ACTION_REDUCE]
    adds.sort(key=lambda s: s.confidence, reverse=True)
    reduces.sort(key=lambda s: s.confidence, reverse=True)

    if adds:
        shown = adds if limit is None else adds[:limit]
        print()
        print(f"  ── 🟢 加仓信号（{len(adds)} 只，按置信度降序）──")
        for s in shown:
            sugg = f"｜建议挂单价 {s.suggested_price:.2f}" if s.suggested_price > 0 else ""
            print(f"  {s.action_label}  {s.name}({s.code})  现价 {s.price:.2f}  "
                  f"阶段[{s.stage}] 置信{s.confidence_label}({s.confidence}%){sugg}")
            if s.reasons:
                print(f"      └─ {'；'.join(s.reasons)}")

    if reduces:
        shown = reduces if limit is None else reduces[:limit]
        print()
        print(f"  ── 🔴 减仓信号（{len(reduces)} 只，按置信度降序）──")
        for s in shown:
            sugg = f"｜建议挂单价 {s.suggested_price:.2f}" if s.suggested_price > 0 else ""
            print(f"  {s.action_label}  {s.name}({s.code})  现价 {s.price:.2f}  "
                  f"阶段[{s.stage}] 置信{s.confidence_label}({s.confidence}%){sugg}")
            if s.reasons:
                print(f"      └─ {'；'.join(s.reasons)}")

    print()
    print("  说明:")
    print("    - 阶段映射：启动期/磨底期/下跌期=加仓（右侧/左侧埋伏/左侧接刀），赶顶期/派发期=减仓")
    print("    - 置信度 = detect_stage 阶段判定置信；加仓挂单价=支撑上方低吸，减仓=压力下方高抛")
    print("    - 日K级慢变量，盘中/盘后均可运行；是否操作由 AI 依据 SKILL.md 框架生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
