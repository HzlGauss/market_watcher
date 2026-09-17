#!/usr/bin/env python3
"""做 T 批量扫描：扫描 holdings.csv 各持仓，找出适合 T+0 的标的并给挂单建议。

核心思路：
    1. 读 holdings.csv 持仓池
    2. 批量拉实时快照（新浪 + 腾讯量比/换手率）
    3. 逐标的拉 5 分钟 K 线 → 跑 evaluate_t0_measure 三门槛（非单边 / 振幅够 / 区间够）
    4. ✅ 适合做 T 的排前面（含买/卖挂单价），❌ 的列出原因

本脚本只做「取数 + 排序」，是否下单由 AI 依据 SKILL.md 框架生成。做 T 需已有底仓，仅交易时段有意义。

用法:
    py .claude/skills/intraday-signal/scan_t0.py [数量]

数据源: 实时快照（新浪+腾讯）、5 分钟 K 线（新浪）。不依赖 MX_APIKEY。
"""
import csv
import sys
import time
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
from app.t0_monitor import evaluate_t0_measure


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


def _f(x, nd=2):
    return f"{x:.{nd}f}" if x is not None else "  --"


def main():
    argv = sys.argv[1:]
    limit = None
    for a in argv:
        if a.strip().isdigit():
            limit = max(1, int(a.strip()))

    holdings = _read_holdings()
    print("=" * 72)
    print(f"做 T 批量扫描（holdings.csv 共 {len(holdings)} 标的 · 5 分钟 K 线）")
    print("=" * 72)

    if not holdings:
        print("  ⚠️ holdings.csv 为空或不存在")
        return 1

    # 批量拉实时快照（type 决定 ETF/个股 振幅门槛）
    items = [WatchItem(name=h["name"], code=h["code"], market=h["market"],
                       type="ETF" if h["is_etf"] else "个股") for h in holdings]
    quotes = fetch_quotes(items) or []
    quote_map = {q.code: q for q in quotes}

    rows = []
    for h, item in zip(holdings, items):
        q = quote_map.get(item.code)
        if q is None or (q.price or q.pre_close or 0) <= 0:
            continue
        try:
            time.sleep(0.3)  # 降低请求频率，避免新浪 456 限频
            klines = fetch_historical_kline(item.code, item.market, days=2, scale=5)
        except Exception:
            klines = []
        if not klines:
            continue
        m = evaluate_t0_measure(q, klines)
        rows.append({
            "code": item.code, "name": item.name,
            "price": q.price, "amp": m["amp"], "ma_align": m["ma_align"],
            "suitable": m["suitable"], "reasons": m["reasons"],
            "buy": m["buy_price"], "sell": m["sell_price"],
        })

    if not rows:
        print("  ⚠️ 无有效实时行情 / 5 分钟 K 线（非交易时段或网络异常）")
        return 1

    ok = [r for r in rows if r["suitable"]]
    no = [r for r in rows if not r["suitable"]]
    ok.sort(key=lambda r: -(r["amp"] or 0))  # 振幅大 → 做 T 空间足，排前

    print()
    print("  ── ✅ 适合做 T ──")
    print(f"  {'名称':<12}{'现价':>9}{'振幅':>8}{'状态':<10}{'买单价':>9}{'卖单价':>9}")
    print("  " + "-" * 64)
    if ok:
        shown = ok if limit is None else ok[:limit]
        for r in shown:
            print(f"  {r['name']:<12}{r['price']:>9.2f}{_f(r['amp']):>8}"
                  f"{r['ma_align']:<10}{_f(r['buy'], 3):>9}{_f(r['sell'], 3):>9}")
    else:
        print("  （无）")

    print()
    print(f"  ── ❌ 今日不适合做 T（{len(no)} 只）──")
    for r in no:
        print(f"    {r['name']}({r['code']})  {'；'.join(r['reasons'])}")

    print()
    print("  说明:")
    print("    - 做 T 需已有底仓：正 T（先买后卖）受 A 股 T+1 限制，反 T（先卖后买）需持仓")
    print("    - ✅ 门槛：5 分钟非单边（震荡）+ 日内振幅 ≥1.5%（个股）/0.8%（ETF）+ 支撑压力区间 ≥0.8%")
    print("    - 挂单价为技术位参考，未含佣金/印花税校验；是否下单由 AI 依据 SKILL.md 框架生成")
    print("    - 仅交易时段有效；收盘后运行得到的是收盘快照，做 T 结论失效，仅供复盘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
