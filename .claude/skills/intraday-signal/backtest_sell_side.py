#!/usr/bin/env python3
"""卖出侧回测：验证「减仓信号」与「止损位」的前瞻收益。

面板加载/指标/regime/分层报告复用 ``app.backtest_panel``。本文件只保留卖出专属的
规则定义与入口。针对此前没有历史回测的卖出逻辑，用本地 duckdb（market.duckdb 的
v_daily_qfq，前复权全市场日K）做严格历史验证。回答一个问题：**卖出信号触发后，
如果不卖，未来 5/10/20 个交易日是继续跌（信号有效）还是反弹（误杀）？**

测三类卖出规则（阈值逐条抄自 ``app/technical.py:detect_stage`` 与各 skill 的止损定义）：

1. 减仓·赶顶期：多头排列/多头回调 且 超买(RSI>=70 或 KDJ J>=90)
   且 (乖离>=15% 或 放量>=1.3 倍)
2. 减仓·派发期：收盘跌破 MA20 且 距近20日高点回落 8%~25% 且 非空头排列 且 量能未缩(>=0.9)
3. 止损位（收盘跌破）：MA10 / MA20 / 前20日低点 / 前60日低点（首次跌破 cross-below）

运行（需 duckdb/pandas 的 miniconda 解释器）：
    /Users/hanzhanli/miniconda3/bin/python3.13 .claude/skills/intraday-signal/backtest_sell_side.py [回溯年数]
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
    index_regime,
    load_daily,
    load_index,
    load_size_tier,
    size_regime_map,
)


def add_rules(df: pd.DataFrame) -> None:
    """逐条复现卖出规则，生成布尔触发列。止损为「首次跌破」（cross-below）。"""
    close = df["close"]
    prev_close = close.groupby(df["thscode"]).transform(lambda s: s.shift(1))
    overbought = (df["rsi"] >= 70) | (df["kdj_j"] >= 90)

    # 减仓·赶顶期
    df["rule_赶顶"] = (
        df["ma_align"].isin(["多头排列", "多头回调"])
        & overbought
        & ((df["bias20"] >= 15) | (df["vol_ratio"] >= 1.3))
    )
    # 减仓·派发期
    df["rule_派发"] = (
        (close < df["ma20"])
        & (df["drawdown_high20"] >= 8)
        & (df["drawdown_high20"] < 25)
        & (df["ma_align"] != "空头排列")
        & (df["vol_ratio"].isna() | (df["vol_ratio"] >= 0.9))
    )

    # 止损位（首次跌破：今日收盘 < 位，昨日收盘 >= 位）
    def _cross_below(level) -> pd.Series:
        prev_level = level.groupby(df["thscode"]).transform(lambda s: s.shift(1))
        return (close < level) & (prev_close >= prev_level)

    df["rule_ma10"] = _cross_below(df["ma10"])
    df["rule_ma20"] = _cross_below(df["ma20"])
    df["rule_前20低"] = _cross_below(df["low20"])
    df["rule_前60低"] = _cross_below(df["low60"])


def main() -> int:
    years = None
    for a in sys.argv[1:]:
        if a.strip().isdigit():
            years = int(a.strip())

    print(f"读取 duckdb 前复权日K（{'全历史' if years is None else f'近{years}年'}）...", file=sys.stderr)
    df = load_daily(years)
    print(f"  原始 {len(df)} 行 / {df['code'].nunique()} 只标的", file=sys.stderr)

    # 每标的至少 120 根（60 日低点 + 60 日均线 + 20 日前瞻的余量），并留 60 根 warmup
    df = df.groupby("thscode", sort=False).filter(lambda s: len(s) >= 160)
    print(f"  过滤后 {len(df)} 行 / {df['code'].nunique()} 只", file=sys.stderr)

    add_board(df)
    add_indicators(df)
    add_rules(df)
    add_forward(df)

    # 市场 regime（中证1000 MA20/MA60 相对位置）→ 逐日打标
    idx = load_index()
    reg = index_regime(idx).astype(str)
    df["regime"] = df["date"].map(reg.to_dict())

    # 板块 regime（板块广度=站上MA20占比 5日平滑）→ 逐(板块,日)打标
    br = board_regime_map(df)
    keys = pd.MultiIndex.from_arrays([df["board"].to_numpy(), df["date"].to_numpy()])
    df["board_regime"] = br.reindex(keys).to_numpy()

    # 市值分层（沪深300/中证500/中证1000/微盘）→ 分层广度 regime
    add_size_tier(df, load_size_tier())
    sr = size_regime_map(df)
    keys = pd.MultiIndex.from_arrays([df["size"].to_numpy(), df["date"].to_numpy()])
    df["size_regime"] = sr.reindex(keys).to_numpy()

    # 丢弃指标 warmup 期（前 60 根）与尾部缺前瞻的样本，统计在有效区间内进行
    df["_warm"] = df.groupby("thscode", sort=False).cumcount()
    valid = df[df["_warm"] >= 60]

    RULES = [
        ("减仓·赶顶期", "rule_赶顶"),
        ("减仓·派发期", "rule_派发"),
        ("止损·跌破MA10", "rule_ma10"),
        ("止损·跌破MA20", "rule_ma20"),
        ("止损·跌破前20低", "rule_前20低"),
        ("止损·跌破前60低", "rule_前60低"),
    ]

    base = {h: _stat(valid[f"fut{h}"].tolist()) for h in HORIZONS}

    print()
    print("=" * 78)
    print("卖出侧回测：信号触发后前瞻收益（越负 = 卖出信号越有效，避免了下跌）")
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
        n = len(trig)
        print(f"{label:<16}{n:>7} | {cells}")

    print("-" * 78)

    # 随机基准对照（对减仓信号与各止损，取各自的触发数做同频率随机抽样）
    print()
    print("净 edge（规则均值 − 同频率随机均值；|edge|>2×std 记 *）:")
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

    print()
    print("尾部风险（20 日）：P5=5% 分位（最差情形），续跌% = 触发后 20 日再跌超 10% 占比，")
    print("反弹% = 触发后 20 日反弹超 10% 占比。止损的价值主要看能否压低「续跌%」。")
    print(f"{'规则':<16}{'P5(20日)':>12}{'续跌>10%':>12}{'反弹>10%':>12}")
    print("-" * 78)
    print(f"{'【全市场基准】':<16}{base[20]['p5']:>11.2f}%{base[20]['crash%']:>11.1f}%{base[20]['rebound%']:>11.1f}%")
    for label, _ in RULES:
        r = results[label][20]
        print(f"{label:<16}{r['p5']:>11.2f}%{r['crash%']:>11.1f}%{r['rebound%']:>11.1f}%")

    # ---- 市场状态拆分：各规则在牛/熊/震荡下的 20 日净 edge（规则 − 同状态随机）----
    regimes = ["牛市", "熊市", "震荡"]
    regime_share = {r: int((valid["regime"] == r).sum()) for r in regimes}
    total = sum(regime_share.values())
    print()
    print("=" * 78)
    print("市场状态拆分（中证1000 MA20/MA60 相对位置定义牛/熊/震荡）")
    print("=" * 78)
    share = "  ".join(f"{r}{regime_share[r] / total * 100:.0f}%" for r in regimes)
    print(f"  状态样本占比: {share}")
    print()
    print(f"{'规则':<16}{'牛市':>12}{'熊市':>12}{'震荡':>12}   (20日净edge，越负越该卖)")
    print("-" * 78)
    for label, col in RULES:
        cells = []
        for r in regimes:
            sub = valid[valid["regime"] == r]
            trig = sub[sub[col]]
            n_trig = len(trig)
            if n_trig == 0:
                cells.append("--")
                continue
            s = _stat(trig["fut20"].tolist())
            rnd = _random_baseline(sub, n_trig, k=200)
            rr = rnd[20]
            e = (s["avg"] - rr["avg"]) if (s and rr) else None
            sig = "*" if (e is not None and abs(e) > 2 * rr["std"]) else ""
            cells.append(f"{e:+.2f}%{sig}(n{n_trig})" if e is not None else "--")
        print(f"{label:<16}" + "".join(f"{c:>12}" for c in cells))

    # ---- 分层拆分：板块广度 / 市值分层广度 分别定牛/熊/震荡 ----
    _report_split(
        valid, "board", "board_regime",
        ["沪主板", "深主板", "创业板", "科创板"], regimes, RULES,
        "板块拆分（沪主板/深主板/创业板/科创板；板块广度=站上MA20占比 5日平滑）",
    )
    _report_split(
        valid, "size", "size_regime",
        ["沪深300", "中证500", "中证1000", "微盘"], regimes, RULES,
        "市值分层拆分（沪深300/中证500/中证1000/微盘；分层广度=站上MA20占比 5日平滑）",
    )

    print()
    print("读法：某规则触发后前瞻收益若显著低于【全市场基准】与随机基准，说明")
    print("「在该处卖出/止损」确实躲过了后续下跌（信号有效）；若接近基准或为正，")
    print("说明该卖出点位是误杀（卖了就反弹）。")
    print()
    print("⚠️ 生存者偏差：duckdb 无退市股，触发后退市归零的最坏情形被低估，")
    print("   故卖出信号的真实有效性只会比下表更强（偏乐观估计）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
