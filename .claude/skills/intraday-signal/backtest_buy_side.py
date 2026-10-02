#!/usr/bin/env python3
"""买入侧回测：验证左侧超跌 / 右侧突破追涨信号的前瞻收益，并按 regime 拆分。

面板/指标/regime/报告复用 ``app.backtest_panel``，与卖出侧同一口径（10 年前复权全市场）。
回答：**买入信号触发后，未来 5/10/20 交易日是继续涨（信号有效）还是接盘（追高被套）？**

买入规则（核心信号向量化，阈值逐条抄自 scan_left_side / scan_right_side 打分逻辑）：

1. 左侧·超跌缩量：近120日高点回撤 12~40%（温和超跌）+ RSI<=40 + 缩量 + 站上MA120
2. 左侧·深坑：回撤>=30% 仍处 MA20 下方 + 缩量（黄金坑雏形）
3. 右侧·放量突破：站上MA20 + 均线多头(MA5>10>20) + 突破前20日高 + 温和放量 1.2~2.0
4. 右侧·追涨放量：站上MA20 + MA5>10 + 放量>=1.5（动量，未必要突破）
5. 抄底·跌破前60低：与前60低止损互为镜像（熊市补跌到位=买点）

运行：
    /Users/hanzhanli/miniconda3/bin/python3.13 .claude/skills/intraday-signal/backtest_buy_side.py [回溯年数]

注意：买入侧对生存者偏差更敏感——退市股缺失会**高估**买入收益（赢家留下来），
即买入信号真实有效性只会比下表更弱（偏乐观）。
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


def add_buy_rules(df: pd.DataFrame) -> None:
    """向量化买入规则（核心信号，非完整 0~100 打分）。"""
    close = df["close"]
    prev_close = close.groupby(df["thscode"]).transform(lambda s: s.shift(1))
    bull = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])
    dd120 = df["drawdown_high120"]

    # 左侧·超跌缩量（温和超跌 + 超卖 + 缩量 + 未破长期趋势）
    df["rule_左侧超跌"] = (
        (dd120 >= 12) & (dd120 < 40)
        & (df["rsi"] <= 40)
        & (df["vol_ratio"] < 0.8)
        & (close >= df["ma120"])
    )
    # 左侧·深坑（深跌且仍处 MA20 下方 + 缩量）
    df["rule_左侧深坑"] = (
        (dd120 >= 30) & (close < df["ma20"]) & (df["vol_ratio"] < 0.8)
    )
    # 右侧·放量突破（站上MA20 + 均线多头 + 突破前20日高 + 温和放量）
    df["rule_右侧突破"] = (
        (close >= df["ma20"]) & bull
        & df["broke20"]
        & (df["vol_ratio"] >= 1.2) & (df["vol_ratio"] < 2.0)
    )
    # 右侧·追涨放量（站上MA20 + 短期均线向上 + 放量）
    df["rule_右侧追涨"] = (
        (close >= df["ma20"]) & (df["ma5"] > df["ma10"]) & (df["vol_ratio"] >= 1.5)
    )
    # 抄底·跌破前60低（与卖出侧「前60低止损」镜像：熊市补跌到位=买点）
    prev_level = df["low60"].groupby(df["thscode"]).transform(lambda s: s.shift(1))
    df["rule_抄底破前低"] = (close < df["low60"]) & (prev_close >= prev_level)


def main() -> int:
    years = None
    for a in sys.argv[1:]:
        if a.strip().isdigit():
            years = int(a.strip())

    print(f"读取 duckdb 前复权日K（{'全历史' if years is None else f'近{years}年'}）...", file=sys.stderr)
    df = load_daily(years)
    print(f"  原始 {len(df)} 行 / {df['code'].nunique()} 只标的", file=sys.stderr)

    df = df.groupby("thscode", sort=False).filter(lambda s: len(s) >= 160)
    print(f"  过滤后 {len(df)} 行 / {df['code'].nunique()} 只", file=sys.stderr)

    add_board(df)
    add_indicators(df)
    add_buy_rules(df)
    add_forward(df)

    # 板块 / 市值分层 regime
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
        ("左侧·超跌缩量", "rule_左侧超跌"),
        ("左侧·深坑", "rule_左侧深坑"),
        ("右侧·放量突破", "rule_右侧突破"),
        ("右侧·追涨放量", "rule_右侧追涨"),
        ("抄底·跌破前60低", "rule_抄底破前低"),
    ]

    base = {h: _stat(valid[f"fut{h}"].tolist()) for h in HORIZONS}

    print()
    print("=" * 78)
    print("买入侧回测：信号触发后前瞻收益（越正 = 买入信号越有效）")
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
    print("净 edge（规则均值 − 同频率随机均值；正 = 买入后跑赢随机；|edge|>2×std 记 *）:")
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
    # ---- 分层拆分：市值分层广度 / 板块广度 分别定牛/熊/震荡 ----
    _report_split(
        valid, "size", "size_regime",
        ["沪深300", "中证500", "中证1000", "微盘"], regimes, RULES,
        "市值分层拆分（沪深300/中证500/中证1000/微盘；分层广度=站上MA20占比 5日平滑）",
        edge_note="越正越该买",
    )
    _report_split(
        valid, "board", "board_regime",
        ["沪主板", "深主板", "创业板", "科创板"], regimes, RULES,
        "板块拆分（沪主板/深主板/创业板/科创板；板块广度=站上MA20占比 5日平滑）",
        edge_note="越正越该买",
    )

    print()
    print("读法：买入规则触发后前瞻收益若显著高于【全市场基准】与随机基准，说明")
    print("「在此买入」确实吃到了后续上涨（信号有效）；若接近基准或为负，说明")
    print("该买入点位是接盘（追高被套）。")
    print()
    print("⚠️ 生存者偏差：duckdb 无退市股，买入后暴雷退市的最坏情形被低估，")
    print("   故买入信号的真实有效性只会比下表更弱（偏乐观估计）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
