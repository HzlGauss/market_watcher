#!/usr/bin/env python3
"""早盘竞价雷达：个股竞价强弱 + 全市场短线风向标（同花顺集合竞价）。

核心思路：
    1. 拉短线风向标基准（auction/short-term-benchmark）→ 全市场竞价情绪 tags（龙头/补涨/退潮…）
    2. 拉自选/持仓/指定板块的个股竞价快照（auction/snapshot, final）→ 竞价涨跌/量比/换手/未匹配量
    3. 按竞价涨跌幅降序排名，输出「高开强势 / 低开弱势」分层，供早盘 9:15–9:25 决策

本脚本只做「取数 + 排名」，竞价强弱 / 情绪周期的最终结论由 AI 依据 SKILL.md 框架生成。

用法:
    py .claude/skills/auction/scan_auction.py                       # 默认扫自选+持仓 A 股
    py .claude/skills/auction/scan_auction.py 半导体                 # 扫指定板块/行业成分股
    py .claude/skills/auction/scan_auction.py 600519,000333,300750  # 扫指定代码列表

参数:
    目标   可选。默认=watchlist.csv+holdings.csv 里的 A 股；也可给板块/行业名或逗号分隔代码
    数量   可选。输出候选数上限（默认 30）

数据源: 同花顺官方金融数据（HITHINK_FINANCE_API_KEY，无 key 直接报错）。不依赖 MX_APIKEY。
注意: 竞价快照只支持 .SH/.SZ/.BJ 的 A 股，指数/基金/ETF 不支持；非竞价时段返回未就绪/停牌。
"""
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

# 定位项目根目录（skills/auction 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

import logging
logging.disable(logging.WARNING)

try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass


def _num(v):
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v, signed=True):
    if v is None:
        return "   --"
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v):.2f}%" if signed else f"{v:.2f}%"


def _short(s, n=9):
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _is_ashare(code: str) -> bool:
    """竞价快照只支持 A 股（.SH/.SZ/.BJ），排除 ETF/LOF/基金/B 股/港股。"""
    try:
        from app.helpers import is_a_share_stock
        return bool(is_a_share_stock(code))
    except Exception:
        c = str(code).strip()
        return len(c) == 6 and c.startswith(("60", "68", "00", "30"))


def _read_csv_codes(filename: str) -> list[tuple[str, str]]:
    """读 CSV 的 name/code 列，返回 [(code, name)]（仅 A 股）。"""
    path = _ROOT / filename
    if not path.exists():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = str(row.get("code", "")).strip().zfill(6)
                name = str(row.get("name", "")).strip()
                if len(code) == 6 and _is_ashare(code):
                    out.append((code, name))
    except Exception:
        pass
    return out


def _default_targets() -> list[tuple[str, str]]:
    """默认目标 = holdings.csv + watchlist.csv 里的 A 股（去重，持仓优先）。"""
    seen = {}
    for code, name in _read_csv_codes("holdings.csv") + _read_csv_codes("watchlist.csv"):
        seen.setdefault(code, name)
    return list(seen.items())


def _resolve_targets(arg: str) -> list[tuple[str, str]]:
    """解析命令行目标：代码列表 / 板块行业 / 空=默认自选。"""
    if not arg:
        return _default_targets()
    # 逗号分隔代码列表（或单个 6 位代码）
    if all(re.fullmatch(r"\d{6}", t.strip()) for t in arg.split(",")):
        import re
        return [(t.strip().zfill(6), "") for t in arg.split(",") if _is_ashare(t.strip())]
    # 板块/行业名 → 成分股池
    try:
        from app.board_pool import resolve_board_pool
        pool = resolve_board_pool(arg)
        return [(code, info.get("name", "")) for code, info in pool.items() if _is_ashare(code)]
    except Exception:
        return []


def _print_benchmark():
    """【1. 短线风向标】全市场竞价基准（tags 保留服务端标签，不自行推导）。"""
    print()
    print("=" * 76)
    print("【1. 短线风向标 · 全市场竞价基准】")
    print("=" * 76)
    try:
        from app import hithink
        items = hithink.fetch_auction_benchmark()
    except Exception:
        items = []
    if not items:
        print("  ⚠️ 无短线风向标数据（无 key / 接口不可达 / 非交易日）")
        return
    header = f"  {'名称':<12}{'竞价涨跌':>10}{'标签':<32}"
    print(header)
    print("  " + "-" * 72)
    for it in items:
        name = _short(it.get("name") or it.get("ticker") or "", 11)
        pct = _pct(_num(it.get("auction_pct")))
        tags = " / ".join(str(t) for t in (it.get("tags") or []))
        print(f"  {name:<12}{pct:>10}  {_short(tags, 30):<32}")


def _print_snapshot(targets: list[tuple[str, str]], limit: int):
    """【2. 个股竞价强弱】按竞价涨跌幅降序排名。"""
    print()
    print("=" * 76)
    print("【2. 个股竞价强弱排名（按竞价涨跌幅降序）】")
    print("=" * 76)
    codes = [c for c, _ in targets][:100]  # 竞价快照上限 100 只
    if not codes:
        print("  ⚠️ 无有效 A 股目标（自选/持仓里没有 A 股，或板块无成分）")
        return
    try:
        from app import hithink
        meta = hithink.fetch_auction_snapshot_meta(codes, "final")
    except Exception:
        meta = {}
    items = list(meta.get("item") or []) if meta else []
    status = meta.get("data_status") or ""
    phase = meta.get("auction_phase") or ""
    if status:
        print(f"  数据状态: {status}" + (f" / 竞价阶段: {phase}" if phase else ""))
    if not items:
        print("  ⚠️ 无竞价快照数据（非竞价时段 / 停牌 / 未就绪；status 见上）")
        return

    # name 兜底：用目标列表里的名称补
    name_map = {c: n for c, n in targets}
    rows = []
    for it in items:
        ticker = str(it.get("ticker") or "").zfill(6)
        rows.append({
            "code": ticker,
            "name": str(it.get("name") or "") or name_map.get(ticker, ""),
            "price": _num(it.get("auction_price")),
            "pct": _num(it.get("auction_pct")),
            "vol_ratio": _num(it.get("auction_volume_ratio")),
            "turnover": _num(it.get("auction_turnover_pct")),
            "yday_ratio": _num(it.get("auction_yesterday_ratio_pct")),
            "unmatched": _num(it.get("auction_unmatched")),
        })
    rows.sort(key=lambda x: -(x["pct"] if x["pct"] is not None else -1e9))

    header = (f"  {'代码':<8}{'名称':<10}{'竞价价':>8}{'竞价涨跌':>10}"
              f"{'量比':>7}{'竞价换手':>9}{'昨量比':>9}{'未匹配':>9}")
    print(header)
    print("  " + "-" * 72)
    for r in rows[:limit]:
        price = f"{r['price']:.2f}" if r["price"] is not None else "     --"
        vr = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "    --"
        tr = _pct(r["turnover"], signed=False)
        yr = _pct(r["yday_ratio"], signed=False)
        um = f"{r['unmatched']:.0f}" if r["unmatched"] is not None else "      --"
        print(f"  {r['code']:<8}{_short(r['name']):<10}{price:>8}{_pct(r['pct']):>10}"
              f"{vr:>7}{tr:>9}{yr:>9}{um:>9}")

    if len(rows) > limit:
        print(f"  ... 省略 {len(rows) - limit} 只 ...")
    # 高开/低开分层统计
    high = sum(1 for r in rows if r["pct"] is not None and r["pct"] > 3)
    low = sum(1 for r in rows if r["pct"] is not None and r["pct"] < -3)
    print(f"  汇总: 竞价 >+3% 强势高开 {high} 只 / <-3% 弱势低开 {low} 只 / 共 {len(rows)} 只")


def main():
    argv = sys.argv[1:]
    target = ""
    limit = 30
    for a in argv:
        a = a.strip()
        if a.isdigit():
            limit = max(1, min(int(a), 100))
        elif a and not a.startswith("-"):
            target = a

    import re  # noqa: F401  (供 _resolve_targets 使用)

    print("=" * 76)
    print("早盘竞价雷达（个股竞价强弱 + 全市场短线风向标 · 同花顺）")
    print("=" * 76)

    targets = _resolve_targets(target)
    if target and not targets:
        print(f"❌ 目标「{target}」解析为空（非 A 股 / 板块无成分 / 需 HITHINK_FINANCE_API_KEY）")
        return 1

    _print_benchmark()
    _print_snapshot(targets, limit)

    print()
    print("  说明:")
    print("    - 竞价快照只支持 A 股（.SH/.SZ/.BJ），ETF/基金/指数不支持")
    print("    - 竞价涨跌 = 竞价价相对昨收涨跌幅；量比/换手/昨量比反映竞价承接力度")
    print("    - 未匹配量 = 竞价未成交委托量；大额未匹配 + 高开 = 抢筹，低开 = 抛压")
    print("    - 短线风向标 tags 为服务端原标签，未自行推导；非竞价时段数据未就绪/停牌属正常")
    print("    - 本脚本只做取数排名，『竞价强弱 / 情绪周期』结论由 AI 依据 SKILL.md 框架生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
