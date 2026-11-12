#!/usr/bin/env python3
"""回测「地量地价反转」策略的底背离条件 + 触发阈值。

对比各配置的历史前向收益（5/10/20 日胜率 + 均收益）：
- 底背离单独：MACD 底背离是否本身有预测力
- 原3/4：4 条件（无底背离），任意 3 触发 —— 加底背离前的基线
- 原3/4+背离：基线信号 且 近3日有底背离
- 新3/5：5 条件，任意 3 触发（松）
- 新4/5：5 条件，任意 4 触发（当前默认，紧）
- 新5/5：5 条件全满足（最严）

样本默认取 data/bluechip_whitelist.csv 前 N 只（本地 DuckDB 前复权日K，零网络）。

用法: py tools/backtest_bottom_divergence.py [样本数]
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline_local import fetch_daily  # 本地 duckdb 读前复权日K（无网络）
from app.strategy import calc_macd_dif_series
from app.technical import calc_rsi, calc_kdj, is_low_volume, detect_macd_divergence

HORIZONS = (5, 10, 20)


def _had_bottom_divergence(closes, dif_vals, within=3, lookback=60) -> bool:
    """近 `within` 根内（含当日）是否出现过 MACD 底背离。"""
    n = len(closes)
    for k in range(within):
        end = n - k
        if end <= 0:
            break
        if detect_macd_divergence(closes[:end], dif_vals[:end], lookback).direction == "底背离":
            return True
    return False


def load_codes(limit: int) -> list[tuple[str, str, str]]:
    """读 bluechip_whitelist.csv 前 limit 只，返回 (code, market, name)。"""
    items: list[tuple[str, str, str]] = []
    p = Path("data/bluechip_whitelist.csv")
    with open(p, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = (row.get("代码") or "").strip()
            name = (row.get("名称") or "").strip()
            if not code:
                continue
            items.append((code, "", name))
            if len(items) >= limit:
                break
    return items


def backtest_stock(code: str, market: str, name: str) -> dict | None:
    klines = fetch_daily(code, market, 500)
    if not klines or len(klines) < 120:
        return None

    closes = [k.close for k in klines if k.close is not None]
    if len(closes) < 120:
        return None
    dif_full = calc_macd_dif_series(closes)

    # 各配置的前向收益累计
    # config -> {horizon -> [returns]}
    acc: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

    max_h = max(HORIZONS)
    for i in range(60, len(klines) - max_h):
        today = klines[i]
        if today.close is None:
            continue
        window = klines[: i + 1]
        win_closes = closes[: i + 1]
        highs = [k.high for k in window if k.high is not None]
        lows = [k.low for k in window if k.low is not None]

        prev_close = win_closes[-2] if len(win_closes) >= 2 else today.close
        change_pct = (today.close - prev_close) / prev_close * 100 if prev_close else 0.0

        # 5 条件
        c1 = is_low_volume(window, 20)                       # 地量
        c2 = abs(change_pct) <= 1.0                          # 企稳
        rsi = calc_rsi(win_closes)
        c3 = rsi is not None and rsi <= 30                   # RSI 超卖
        kdj = calc_kdj(highs, lows, win_closes)
        c4 = kdj.signal in ("超卖", "金叉")                   # KDJ 超卖/金叉
        c5 = _had_bottom_divergence(win_closes, dif_full[: i + 1])  # 近3日底背离

        matched_orig = int(c1) + int(c2) + int(c3) + int(c4)
        matched_new = matched_orig + (1 if c5 else 0)

        # 前向收益
        fwd = {}
        for h in HORIZONS:
            fk = klines[i + h]
            if fk.close is not None and today.close:
                fwd[h] = (fk.close - today.close) / today.close * 100

        def _record(cfg: str):
            for h, r in fwd.items():
                acc[cfg][h].append(r)

        if c5:
            _record("底背离单独")
        if matched_orig >= 3:
            _record("原3/4")
            if c5:
                _record("原3/4+背离")
        if matched_new >= 3:
            _record("新3/5")
        if matched_new >= 4:
            _record("新4/5")
        if matched_new >= 5:
            _record("新5/5")

    # 汇总该股
    out = {}
    for cfg, hd in acc.items():
        out[cfg] = {}
        for h in HORIZONS:
            rs = hd.get(h, [])
            if not rs:
                out[cfg][h] = {"n": 0, "win": None, "avg": None}
            else:
                wins = sum(1 for r in rs if r > 0)
                out[cfg][h] = {
                    "n": len(rs),
                    "win": wins / len(rs) * 100,
                    "avg": sum(rs) / len(rs),
                }
    return out


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    codes = load_codes(limit)
    print(f"回测 {len(codes)} 只蓝筹股，本地 DuckDB 前复权日K，前向 {HORIZONS} 日...\n")

    merged: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {"n": 0, "wins": 0.0, "s": 0.0}))
    done = 0
    for code, market, name in codes:
        r = backtest_stock(code, market, name)
        if not r:
            continue
        done += 1
        for cfg, hd in r.items():
            for h, st in hd.items():
                if st["n"] == 0:
                    continue
                m = merged[cfg][h]
                m["n"] += st["n"]
                m["wins"] += st["win"] / 100 * st["n"]
                m["s"] += st["avg"] * st["n"]

    print(f"有效标的 {done}/{len(codes)}\n")
    header = f"{'配置':<16} | {'样本':>6} | " + " | ".join(
        f"{h}日胜率 {h}日均收益" for h in HORIZONS
    )
    print(header)
    print("-" * len(header) * 2)
    order = ["底背离单独", "原3/4", "原3/4+背离", "新3/5", "新4/5", "新5/5"]
    for cfg in order:
        if cfg not in merged:
            continue
        cells = []
        for h in HORIZONS:
            m = merged[cfg][h]
            if m["n"] == 0:
                cells.append(f"     -- / --")
            else:
                cells.append(f"{m['wins'] / m['n'] * 100:>4.1f}% {m['s'] / m['n']:>+.2f}%")
        n = merged[cfg][HORIZONS[1]]["n"]  # 用 10 日样本数代表
        print(f"{cfg:<16} | {n:>6} | " + " | ".join(cells))


if __name__ == "__main__":
    main()
