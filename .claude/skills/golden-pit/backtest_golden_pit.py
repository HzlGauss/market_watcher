#!/usr/bin/env python3
"""黄金坑技术信号 regime 回测：验证「深坑抄底」是否分牛熊、不同 regime 的最优指标配置。

复用 app.backtest_panel（全市场前复权日K面板 + 板块/市值分层广度 regime，与买入/卖出侧
回测同口径）。回答：黄金坑的技术硬门槛（回撤深度 / 缩量 / 站半年线）是否该像 left-side
（熊市深坑）vs right-side（牛市突破）一样，按牛市/熊市换一套指标筛选？

规则（向量化近似 scan_golden_pit._score_candidate 的技术硬门槛，仅价格/量能口径；
「白马成色/基本面兜底」无法在价格面板回测，故本回测只覆盖技术面「坑」的部分）：

  1. 深坑20~45 站半年线+缩量  = 当前黄金坑标准配置（熊市深坑抄底）
  2. 深坑20~45 跌破半年线     = 破位对照（长期趋势走坏）
  3. 深坑20~45 放量           = 放量下跌对照（出货/暴雷）
  4. 深度坑>45 + 缩量          = 深度超阈值（技术面筛不动，靠基本面兜底）
  5. 温和超跌12~20 站半年线+缩量 = 左侧甜点
  6. 浅回调5~20 站上MA20+均线多头 = 牛市回调上车（右侧，对照「深坑」）

缩量口径：vol_trend = 近20日均量 / 近60日均量（<1 坑段量能较前段萎缩），比面板 vol_ratio
（当日/前5日）更贴合「坑段缩量」语义；放量对照用 vol_trend>=1。

运行：
    /Users/hanzhanli/miniconda3/bin/python3.13 .claude/skills/golden-pit/backtest_golden_pit.py [回溯年数]

注意：duckdb 仅存当前在市标的，退市股缺失 → 买入后暴雷退市的最坏情形被系统性低估
（买入信号真实有效性只会比下表更弱，偏乐观）。
"""
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.backtest_panel import (  # noqa: E402
    HORIZONS,
    _random_baseline,
    _report_split,
    _stat,
    add_board,
    add_forward,
    add_indicators,
    add_size_tier,
    board_regime_map,
    load_daily,
    load_size_tier,
    size_regime_map,
)


def add_golden_rules(df: pd.DataFrame) -> None:
    """向量化黄金坑技术规则（价格/量能口径，对齐 scan_golden_pit 硬门槛）。"""
    close = df["close"]
    dd120 = df["drawdown_high120"]
    above120 = close >= df["ma120"]
    above20 = close >= df["ma20"]
    bull = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])

    # 缩量趋势：近20日均量 / 近60日均量（<1 = 坑段量能较前段萎缩）
    vol = df.groupby("thscode")["volume"]
    vol20 = vol.transform(lambda s: s.rolling(20).mean())
    vol60 = vol.transform(lambda s: s.rolling(60).mean())
    df["vol_trend"] = vol20 / vol60
    vt = df["vol_trend"]

    df["r_深坑标准"] = (dd120 >= 20) & (dd120 < 45) & above120 & (vt < 1.0)
    df["r_深坑破位"] = (dd120 >= 20) & (dd120 < 45) & (~above120) & (vt < 1.0)
    df["r_深坑放量"] = (dd120 >= 20) & (dd120 < 45) & above120 & (vt >= 1.0)
    df["r_深度坑"] = (dd120 >= 45) & (vt < 1.0)
    df["r_温和超跌"] = (dd120 >= 12) & (dd120 < 20) & above120 & (vt < 1.0)
    df["r_浅回调多头"] = (dd120 >= 5) & (dd120 < 20) & above20 & bull


def main() -> int:
    years = None
    for a in sys.argv[1:]:
        if a.strip().isdigit():
            years = int(a.strip())

    print(f"读取 duckdb 前复权日K（{'全历史' if years is None else f'近{years}年'}）...", file=sys.stderr)
    df = load_daily(years)
    print(f"  原始 {len(df)} 行 / {df['code'].nunique()} 只标的", file=sys.stderr)

    df = df.groupby("thscode", sort=False).filter(lambda s: len(s) >= 250)
    print(f"  过滤后 {len(df)} 行 / {df['code'].nunique()} 只（≥250根K线，保证 MA120 真实可算）", file=sys.stderr)

    add_board(df)
    add_indicators(df)
    add_golden_rules(df)
    add_forward(df)

    br = board_regime_map(df)
    keys = pd.MultiIndex.from_arrays([df["board"].to_numpy(), df["date"].to_numpy()])
    df["board_regime"] = br.reindex(keys).to_numpy()

    add_size_tier(df, load_size_tier())
    sr = size_regime_map(df)
    keys = pd.MultiIndex.from_arrays([df["size"].to_numpy(), df["date"].to_numpy()])
    df["size_regime"] = sr.reindex(keys).to_numpy()

    df["_warm"] = df.groupby("thscode", sort=False).cumcount()
    valid = df[df["_warm"] >= 60]

    RULES = [
        ("深坑20~45缩量", "r_深坑标准"),
        ("深坑20~45破位", "r_深坑破位"),
        ("深坑20~45放量", "r_深坑放量"),
        ("深度坑>45", "r_深度坑"),
        ("温和超跌12~20", "r_温和超跌"),
        ("浅回调5~20多头", "r_浅回调多头"),
    ]

    base = {h: _stat(valid[f"fut{h}"].tolist()) for h in HORIZONS}

    print()
    print("=" * 78)
    print("黄金坑技术信号回测：触发后前瞻收益（越正 = 越该抄底/上车）")
    print("=" * 78)
    print(f"有效样本 {len(valid)} 行 / {valid['code'].nunique()} 只标的")
    print()
    print(f"{'规则':<16}{'触发数':>7} | " + " | ".join(f"{h}日 均/胜率" for h in HORIZONS))
    print("-" * 78)
    print(f"{'【全市场基准】':<16}{'-':>7} | " + " | ".join(
        f"{base[h]['avg']:+.2f}%/{base[h]['win%']:.0f}%" for h in HORIZONS))
    print("-" * 78)

    results = {}
    for label, col in RULES:
        trig = valid[valid[col]]
        row = {h: _stat(trig[f"fut{h}"].tolist()) for h in HORIZONS}
        results[label] = row
        cells = " | ".join(
            f"{row[h]['avg']:+.2f}%/{row[h]['win%']:.0f}%" if row[h] else "--"
            for h in HORIZONS)
        print(f"{label:<16}{len(trig):>7} | {cells}")

    print("-" * 78)
    print()
    print("净 edge（规则均值 − 同频率随机均值；正 = 触发后跑赢随机；|edge|>2×std 记 *）:")
    for label, col in RULES:
        trig = valid[valid[col]]
        n_trig = len(trig)
        if n_trig == 0:
            print(f"  {label:<16} 无触发")
            continue
        rnd = _random_baseline(valid, n_trig)
        edges = []
        for h in HORIZONS:
            s, r = results[label][h], rnd[h]
            if s and r:
                e = s["avg"] - r["avg"]
                sig = "*" if abs(e) > 2 * r["std"] else ""
                edges.append(f"{h}日:{e:+.2f}%{sig}")
            else:
                edges.append(f"{h}日:--")
        print(f"  {label:<16} {'  '.join(edges)}")

    regimes = ["牛市", "熊市", "震荡"]
    _report_split(
        valid, "size", "size_regime",
        ["沪深300", "中证500", "中证1000", "微盘"], regimes, RULES,
        "市值分层拆分（沪深300=大盘白马蓝筹代理 / 中证500 / 中证1000 / 微盘；分层广度=站上MA20占比 5日平滑）",
        edge_note="越正越该抄底/上车",
    )
    _report_split(
        valid, "board", "board_regime",
        ["沪主板", "深主板", "创业板", "科创板"], regimes, RULES,
        "板块拆分（沪主板/深主板/创业板/科创板；板块广度=站上MA20占比 5日平滑）",
        edge_note="越正越该抄底/上车",
    )

    print()
    print("读法：")
    print("  - 若「深坑20~45缩量」在熊市净edge为正、牛市为负 → 深坑抄底是熊市专属（黄金坑=熊市信号）。")
    print("  - 若「浅回调5~20多头」在牛市净edge为正、熊市为负 → 牛市该换成浅回调上车（黄金坑牛熊换指标）。")
    print("  - 若「深度坑>45」各 regime 都接近基准 → 深跌无 edge，靠基本面兜底拆暴雷是对的。")
    print()
    print("⚠️ 生存者偏差：duckdb 无退市股，买入后暴雷退市的最坏情形被低估，")
    print("   故买入信号真实有效性只会比下表更弱（偏乐观）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
