#!/usr/bin/env python3
"""尾盘资金方向（close-drift）：用腾讯 1 分钟 K 线近似「尾盘最后 3 分钟资金在抢还是逃」。

核心思路：
    1. 拉目标池每标的的腾讯 m1（1 分钟 K，近 2 日）
    2. 提取当日 14:56（连续竞价末分钟）与 15:00（收盘竞价定盘）两根
    3. drift = (15:00 收盘价 − 14:56 价) / 14:56 价，按 drift 降序排名
    4. 输出「尾盘抢筹拉升 / 砸盘出逃 / 平盘」分层，供收盘后复盘 + 次日开盘铺垫

本脚本只做「取数 + 排名」，尾盘资金方向的最终结论由 AI 依据 SKILL.md 框架生成。

用法:
    py .claude/skills/close-drift/scan_close_drift.py                # 默认扫持仓+自选（含 ETF + A 股）
    py .claude/skills/close-drift/scan_close_drift.py 半导体         # 扫指定板块/行业成分股
    py .claude/skills/close-drift/scan_close_drift.py 600036,510300  # 扫指定代码列表
    py .claude/skills/close-drift/scan_close_drift.py 半导体 20      # 指定板块 + 输出上限

数据源: 腾讯分钟 K（ifzq.gtimg.cn），免费，不依赖 MX_APIKEY / HITHINK_FINANCE_API_KEY。
注意: 需收盘后（15:00）跑才有尾盘竞价定盘数据；盘中跑 15:00 那根尚未生成，会提示未收盘。
"""
import csv
import re
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/close-drift 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

import logging
logging.disable(logging.WARNING)

try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass

from app.helpers import _detect_market
from app.technical import _fetch_tencent_minute_kline

# 目标范围：A 股（60/68/00/30）+ 场内 ETF/LOF（51/56/58/15/16/18）
_TARGET_PREFIXES = ("60", "68", "00", "30", "51", "56", "58", "15", "16", "18")
_ETF_PREFIXES = ("51", "56", "58", "15", "16", "18")


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


def _is_target(code: str) -> bool:
    """是否在尾盘分析目标范围：A 股个股 + 场内 ETF/LOF（排除 B 股/港股/北交所）。"""
    c = str(code).strip()
    return len(c) == 6 and c.startswith(_TARGET_PREFIXES)


def _kind(code: str) -> str:
    return "ETF" if code.startswith(_ETF_PREFIXES) else "个股"


def _read_csv_codes(filename: str) -> list[tuple[str, str]]:
    """读 CSV 的 name/code 列，返回 [(code, name)]（仅目标范围）。"""
    path = _ROOT / filename
    if not path.exists():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = str(row.get("code", "")).strip().zfill(6)
                name = str(row.get("name", "")).strip()
                if _is_target(code):
                    out.append((code, name))
    except Exception:
        pass
    return out


def _default_targets() -> list[tuple[str, str]]:
    """默认目标 = holdings.csv + watchlist.csv（去重，持仓优先）。"""
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
        return [(t.strip().zfill(6), "") for t in arg.split(",") if _is_target(t.strip())]
    # 板块/行业名 → 成分股池
    try:
        from app.board_pool import resolve_board_pool
        pool = resolve_board_pool(arg)
        return [(code, info.get("name", "")) for code, info in pool.items() if _is_target(code)]
    except Exception:
        return []


def _analyze(code: str, market: str) -> dict:
    """单标的尾盘资金方向分析。返回 ok/note + drift/价格/竞价量/当日涨跌。"""
    out = {"code": code, "market": market, "ok": False, "note": ""}
    bars = _fetch_tencent_minute_kline(code, market, scale=1, days=2)
    if not bars:
        out["note"] = "无 1 分钟 K 数据"
        return out

    # 按日期分组（date 形如 YYYY-MM-DD HH:MM:SS）
    by_day: dict[str, list] = {}
    for b in bars:
        by_day.setdefault(b.date[:10], []).append(b)
    days = sorted(by_day.keys())
    if not days:
        out["note"] = "无有效分钟数据"
        return out

    last_day = days[-1]
    day_bars = by_day[last_day]

    # 当日 14:56（连续竞价末分钟）与 15:00（收盘竞价定盘）
    c1456 = c1500 = vol1500 = None
    for b in day_bars:
        hm = b.date[11:16]
        if hm == "14:56":
            c1456 = b.close
        elif hm == "15:00":
            c1500 = b.close
            vol1500 = b.volume

    if c1500 is None:
        out["note"] = "尚未收盘（无 15:00 收盘竞价）"
        return out

    # 14:56 缺根（停牌/异常）→ 退到当日 14:00 之后、15:00 之前的最后一根连续竞价 bar
    if c1456 is None:
        for b in day_bars:
            hm = b.date[11:16]
            if "14:00" <= hm < "15:00":
                c1456 = b.close
    if c1456 is None:
        c1456 = c1500

    # 昨收 = 前一交易日 15:00 close；取不到则用当日 9:30 首根 open 近似
    prev_close = None
    if len(days) >= 2:
        for b in by_day[days[-2]]:
            if b.date[11:16] == "15:00":
                prev_close = b.close
    if prev_close is None and day_bars:
        prev_close = day_bars[0].open  # 当日首根 open（近似开盘基准）

    drift = (c1500 - c1456) / c1456 * 100 if c1456 else None
    day_pct = (c1500 - prev_close) / prev_close * 100 if prev_close else None

    out.update({
        "ok": True,
        "date": last_day,
        "c1456": c1456,
        "c1500": c1500,
        "vol1500": vol1500,
        "prev_close": prev_close,
        "drift": drift,
        "day_pct": day_pct,
    })
    return out


def _print_rows(rows: list[dict], limit: int, name_map: dict[str, str]):
    print()
    print("=" * 90)
    print("【尾盘资金方向排名（按尾盘漂移降序）】")
    print("=" * 90)
    if not rows:
        print("  ⚠️ 无有效尾盘数据（未收盘 / 无分钟 K）")
        return

    header = (f"  {'代码':<8}{'名称':<10}{'类型':<5}{'尾盘漂移':>9}"
              f"{'14:56价':>9}{'收盘价':>9}{'竞价量(手)':>10}{'当日涨跌':>9}")
    print(header)
    print("  " + "-" * 86)
    for r in rows[:limit]:
        name = name_map.get(r["code"]) or r.get("name") or ""
        c1456 = f"{r['c1456']:.2f}" if r["c1456"] is not None else "    --"
        c1500 = f"{r['c1500']:.2f}" if r["c1500"] is not None else "    --"
        vol = f"{r['vol1500']:.0f}" if r["vol1500"] is not None else "      --"
        print(f"  {r['code']:<8}{_short(name):<10}{r['kind']:<5}{_pct(r['drift']):>9}"
              f"{c1456:>9}{c1500:>9}{vol:>10}{_pct(r['day_pct']):>9}")

    if len(rows) > limit:
        print(f"  ... 省略 {len(rows) - limit} 只 ...")

    up = sum(1 for r in rows if r["drift"] is not None and r["drift"] > 0.3)
    down = sum(1 for r in rows if r["drift"] is not None and r["drift"] < -0.3)
    flat = len(rows) - up - down
    print(f"  汇总: 尾盘抢筹(>+0.3%) {up} 只 / 平盘 {flat} 只 / 尾盘砸盘(<-0.3%) {down} 只 / 共 {len(rows)} 只")


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

    print("=" * 90)
    print("尾盘资金方向（close-drift · 腾讯 1 分钟 K）")
    print("=" * 90)

    targets = _resolve_targets(target)
    if target and not targets:
        print(f"❌ 目标「{target}」解析为空（无 A 股/ETF / 板块无成分）")
        return 1

    # 目标上限：逐只拉腾讯 m1，避免过多请求
    targets = targets[:100]

    name_map = {c: n for c, n in targets}
    rows = []
    skipped = []
    for code, _name in targets:
        market = _detect_market(code)
        r = _analyze(code, market)
        r["kind"] = _kind(code)
        if r["ok"]:
            rows.append(r)
        else:
            skipped.append((code, _name or name_map.get(code, ""), r["note"]))

    rows.sort(key=lambda x: -(x["drift"] if x["drift"] is not None else -1e9))

    _print_rows(rows, limit, name_map)

    if skipped:
        print()
        print("  未产出（未收盘/无数据）:")
        for code, name, note in skipped:
            print(f"    {code} {_short(name)}  — {note}")

    print()
    print("  说明:")
    print("    - 尾盘漂移 = (15:00 收盘竞价价 − 14:56 连续竞价末价) / 14:56 价，近似尾盘 3 分钟资金方向")
    print("    - 竞价量 = 15:00 收盘集合竞价成交量；漂移 + 大量 = 抢筹拉升，漂移 − 大量 = 砸盘出逃")
    print("    - ETF 有做市商平滑，漂移天然更小，±0.3% 阈值仅供参考，ETF 可放宽到 ±0.15%")
    print("    - 当日涨跌 = 收盘价 vs 昨收（昨收取前一日 15:00 收盘，缺则用当日开盘近似）")
    print("    - 本脚本只做取数排名，『尾盘资金方向 / 次日开盘铺垫』结论由 AI 依据 SKILL.md 框架生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
