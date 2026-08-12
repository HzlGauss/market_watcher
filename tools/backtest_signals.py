#!/usr/bin/env python3
"""回测系统信号准确率，优化各参数阈值

用法: py -3 tools/backtest_signals.py
"""

import sys
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.technical import (
    calc_rsi, calc_macd, calc_kdj, calc_obv, calc_bollinger,
    calc_ma_alignment, calc_support_resistance, get_technical_summary,
    calc_composite_score, detect_market_regime,
)
from app.models import KlineData, Quote, WatchItem


def fetch_klines(code: str, market: str, days: int = 500) -> list[KlineData]:
    """从 Sina 获取历史K线"""
    from app.technical import fetch_historical_kline
    return fetch_historical_kline(code, market, days=days)


def load_holdings(path: str) -> list[dict]:
    """加载持仓文件"""
    items = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("code") and row.get("market") and row["market"].strip():
                items.append(row)
    return items


def backtest_one(code: str, market: str, name: str = "") -> dict:
    """回测单只标的"""
    klines = fetch_klines(code, market, days=500)
    if len(klines) < 120:
        return {"name": name, "code": code, "error": f"K线不足({len(klines)}条)"}

    results = []
    for i in range(60, len(klines) - 10):  # 至少60根计算指标，留10根做前向收益
        window = klines[:i+1]
        today = window[-1]
        if not today.close:
            continue
        price = today.close

        # 计算技术指标
        quote = Quote(code=code, name=name, price=price)
        tech = get_technical_summary(quote, window)
        if tech.rsi is None:
            continue

        # 计算评分（无资金流数据 → flow_pct=None）
        score_info = calc_composite_score(tech, price, flow_pct=None)
        regime = detect_market_regime(tech, price, tech.atr)

        # 前向收益
        fwd_5d = None
        fwd_10d = None
        if i + 5 < len(klines):
            fwd_5d = (klines[i+5].close - price) / price * 100 if klines[i+5].close else None
        if i + 10 < len(klines):
            fwd_10d = (klines[i+10].close - price) / price * 100 if klines[i+10].close else None

        # 趋势标注
        ma20 = tech.ma20 or price
        trend = "up" if price > ma20 * 1.02 else ("down" if price < ma20 * 0.98 else "flat")

        results.append({
            "date": today.date,
            "price": price,
            "score": score_info["score"],
            "label": score_info["label"],
            "regime": regime.regime,
            "rsi": tech.rsi,
            "fwd_5d": fwd_5d,
            "fwd_10d": fwd_10d,
            "trend": trend,
            "bb_width": tech.bb_width,
        })

    # 按评分分段统计（匹配新阈值：≥75极多, ≥60偏多, ≥45中性, ≥35偏空, <35极空）
    brackets = [(0, 35, "极空"), (35, 45, "偏空"), (45, 60, "中性"), (60, 75, "偏多"), (75, 100, "极多")]
    stats = {}
    for lo, hi, label in brackets:
        subset = [r for r in results if lo <= r["score"] < hi]
        win_5d = [r for r in subset if r["fwd_5d"] is not None and r["fwd_5d"] > 0]
        win_10d = [r for r in subset if r["fwd_10d"] is not None and r["fwd_10d"] > 0]
        avg_5d = sum(r["fwd_5d"] for r in subset if r["fwd_5d"] is not None) / max(1, len([r for r in subset if r["fwd_5d"] is not None]))
        avg_10d = sum(r["fwd_10d"] for r in subset if r["fwd_10d"] is not None) / max(1, len([r for r in subset if r["fwd_10d"] is not None]))
        n = len(subset)
        stats[label] = {
            "n": n, "win5d_pct": round(len(win_5d)/max(1,n)*100, 1),
            "win10d_pct": round(len(win_10d)/max(1,n)*100, 1),
            "avg5d": round(avg_5d, 2), "avg10d": round(avg_10d, 2),
        }

    # 按市场状态分组
    regime_stats = {}
    for reg in set(r["regime"] for r in results):
        subset = [r for r in results if r["regime"] == reg]
        if len(subset) < 30:
            continue
        wins = [r for r in subset if r["fwd_10d"] is not None and r["fwd_10d"] > 0]
        avg10 = sum(r["fwd_10d"] for r in subset if r["fwd_10d"] is not None) / max(1, len([r for r in subset if r["fwd_10d"] is not None]))
        regime_stats[reg] = {"n": len(subset), "win10d": round(len(wins)/len(subset)*100,1), "avg10d": round(avg10,2)}

    # 按趋势分组
    trend_stats = {}
    for tr in ["up", "flat", "down"]:
        subset = [r for r in results if r["trend"] == tr]
        if len(subset) < 30:
            continue
        wins_5 = [r for r in subset if r["fwd_5d"] is not None and r["fwd_5d"] > 0]
        wins_10 = [r for r in subset if r["fwd_10d"] is not None and r["fwd_10d"] > 0]
        avg10 = sum(r["fwd_10d"] for r in subset if r["fwd_10d"] is not None) / max(1, len([r for r in subset if r["fwd_10d"] is not None]))
        trend_stats[tr] = {"n": len(subset), "win5d": round(len(wins_5)/len(subset)*100,1),
                           "win10d": round(len(wins_10)/len(subset)*100,1), "avg10d": round(avg10,2)}

    return {
        "name": name, "code": code, "days": len(results),
        "brackets": stats, "regimes": regime_stats, "trends": trend_stats,
        "raw": [{"score": r["score"], "fwd_10d": r["fwd_10d"],
                 "bb_width": r.get("bb_width")} for r in results],
    }


def main():
    holdings = load_holdings("holdings.csv")
    print(f"回测 {len(holdings)} 只标的，每只取约500日K线...\n")

    all_results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(backtest_one, h["code"], h["market"], h["name"]): h["code"] for h in holdings}
        for fut in futures:
            try:
                r = fut.result(timeout=120)
                all_results.append(r)
                print(f"  {r['name']}({r['code']}): {r.get('days', 0)} 天数据")
            except Exception as e:
                print(f"  {futures[fut]}: 失败 - {e}")

    # 汇总统计
    print("\n" + "="*70)
    print("一、评分分段回测（所有标的合并）\n")
    merged = defaultdict(lambda: {"n":0, "w5":0, "w10":0, "a5":0, "a10":0})
    for r in all_results:
        for label, s in r.get("brackets", {}).items():
            merged[label]["n"] += s["n"]
            merged[label]["w5"] += s["win5d_pct"] * s["n"] / 100
            merged[label]["w10"] += s["win10d_pct"] * s["n"] / 100
            merged[label]["a5"] += s.get("avg5d", 0) * s["n"]
            merged[label]["a10"] += s.get("avg10d", 0) * s["n"]

    print(f"{'评分段':<10} {'样本数':>8} {'5日胜率':>8} {'10日胜率':>8} {'5日均收益':>10} {'10日均收益':>10}")
    print("-" * 56)
    for label in ["极空", "偏空", "中性", "偏多", "极多"]:
        m = merged[label]
        n = m["n"]
        if n == 0:
            continue
        print(f"{label:<10} {n:>8} {m['w5']/n*100:>7.1f}% {m['w10']/n*100:>7.1f}% {m['a5']/n:>9.2f}% {m['a10']/n:>9.2f}%")

    print("\n二、市场状态回测\n")
    merged_regime = defaultdict(lambda: {"n":0, "w10":0, "a10":0})
    for r in all_results:
        for reg, s in r.get("regimes", {}).items():
            merged_regime[reg]["n"] += s["n"]
            merged_regime[reg]["w10"] += s["win10d"] * s["n"] / 100
            merged_regime[reg]["a10"] += s["avg10d"] * s["n"]
    for reg, m in sorted(merged_regime.items(), key=lambda x: -x[1]["n"]):
        n = m["n"]
        print(f"  {reg:<12} n={n:>5}  10日胜率={m['w10']/n*100:.1f}%  10日均收益={m['a10']/n:.2f}%")

    print("\n三、趋势环境回测\n")
    merged_trend = defaultdict(lambda: {"n":0, "w5":0, "w10":0, "a10":0})
    for r in all_results:
        for tr, s in r.get("trends", {}).items():
            merged_trend[tr]["n"] += s["n"]
            merged_trend[tr]["w5"] += s["win5d"] * s["n"] / 100
            merged_trend[tr]["w10"] += s["win10d"] * s["n"] / 100
            merged_trend[tr]["a10"] += s["avg10d"] * s["n"]
    for tr in ["up", "flat", "down"]:
        m = merged_trend[tr]
        n = m["n"]
        if n == 0:
            continue
        print(f"  {tr:<8} n={n:>5}  5日胜率={m['w5']/n*100:.1f}%  10日胜率={m['w10']/n*100:.1f}%  10日均={m['a10']/n:.2f}%")

    print("\n四、窄幅震荡 vs 正常环境\n")
    narrow = defaultdict(lambda: {"n":0, "w10":0, "a10":0})
    normal = defaultdict(lambda: {"n":0, "w10":0, "a10":0})
    for r in all_results:
        for res in r.get("raw", []):
            is_narrow = res.get("bb_width") and res["bb_width"] < 5.0
            target = narrow if is_narrow else normal
            lo, hi = 0, 35
            for label, l, h in [("极空",0,35),("偏空",35,45),("中性",45,60),("偏多",60,75),("极多",75,100)]:
                if l <= res["score"] < h:
                    lo, hi = l, h
                    break
            label_t = f"{lo}-{hi}"
            for tgt, dst in [(is_narrow, narrow), (not is_narrow, normal)]:
                if tgt:
                    dst[f"{label_t}_n"] = dst.get(f"{label_t}_n", 0) + 1
                    if res.get("fwd_10d") and res["fwd_10d"] > 0:
                        dst[f"{label_t}_w"] = dst.get(f"{label_t}_w", 0) + 1
                    if res.get("fwd_10d") is not None:
                        dst[f"{label_t}_a"] = dst.get(f"{label_t}_a", 0) + res["fwd_10d"]

    print(f"{'评分段':<12} {'正常样本':>8} {'正常胜率':>8} {'正常均收益':>10} | {'窄幅样本':>8} {'窄幅胜率':>8} {'窄幅均收益':>10}")
    print("-" * 78)
    for label, lo, hi in [("极空",0,35),("偏空",35,45),("中性",45,60),("偏多",60,75),("极多",75,100)]:
        n_n = normal.get(f"{lo}-{hi}_n", 0)
        n_w = normal.get(f"{lo}-{hi}_w", 0)
        n_a = normal.get(f"{lo}-{hi}_a", 0)
        r_n = narrow.get(f"{lo}-{hi}_n", 0)
        r_w = narrow.get(f"{lo}-{hi}_w", 0)
        r_a = narrow.get(f"{lo}-{hi}_a", 0)
        n_wr = f"{n_w/n_n*100:.1f}%" if n_n>0 else "--"
        r_wr = f"{r_w/r_n*100:.1f}%" if r_n>0 else "--"
        n_ar = f"{n_a/n_n:.2f}%" if n_n>0 else "--"
        r_ar = f"{r_a/r_n:.2f}%" if r_n>0 else "--"
        print(f"{label:<12} {n_n:>8} {n_wr:>8} {n_ar:>10} | {r_n:>8} {r_wr:>8} {r_ar:>10}")

    print("\n五、参数建议\n")
    # 找最佳阈值
    print("  基于回测结果，建议：")
    scores_10d = {}
    for r in all_results:
        for label, s in r.get("brackets", {}).items():
            if label not in scores_10d:
                scores_10d[label] = {"n": 0, "w10": 0, "a10": 0}
            scores_10d[label]["n"] += s["n"]
            scores_10d[label]["w10"] += s["win10d_pct"] * s["n"] / 100
            scores_10d[label]["a10"] += s.get("avg10d", 0) * s["n"]

    for label in ["极空", "偏空", "中性", "偏多", "极多"]:
        m = scores_10d[label]
        if m["n"] == 0:
            continue
        n = m["n"]
        wr = m["w10"] / n * 100
        ar = m["a10"] / n
        print(f"  - {label}评分区间: n={n}, 10日胜率={wr:.1f}%, 均收益={ar:.2f}%")


if __name__ == "__main__":
    main()
