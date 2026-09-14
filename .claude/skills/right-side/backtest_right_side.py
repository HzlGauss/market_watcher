#!/usr/bin/env python3
"""右侧选股阈值历史收益回测。

数据源：中证1000 成分 + 500 日K线（与左侧同口径）。对每个历史交易日 T，截断
K 线到 T，跑 scan_right_side 的 _score_candidate，算未来 5/10/20 交易日收益。
对照：同期全宇宙（中证1000 全体）未来收益 = 基准。

注意：右侧是「顺势追涨」策略，仅在上升市有效；回测期若横盘/偏弱，其动量维度会
系统性反向（追高 = 接盘），解读时需结合当时的市场环境。

用法:
    py .claude/skills/right-side/backtest_right_side.py [回测日期数]

结果写入本目录 backtest_result.json，并打印文字摘要。
"""
import json
import logging
import os
import sys
from pathlib import Path

logging.disable(logging.WARNING)
os.environ.setdefault("TQDM_DISABLE", "1")
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 加载 .env（API Key 等）
try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.backtest import (  # noqa: E402
    HORIZONS, _dstr, future_returns, load_universe_klines,
    pick_dates, print_report, summarize,
)
from scan_right_side import _score_candidate  # noqa: E402


def main():
    n_dates = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    dates = pick_dates(n_dates)
    print(f"回测日期: {[_dstr(t) for t in dates]}", file=sys.stderr)

    stocks, kcache = load_universe_klines(days=500)

    cands, base = [], []
    for T in dates:
        T_str = _dstr(T)
        for code in stocks:
            klines = kcache.get(code)
            if not klines:
                continue
            kT = [k for k in klines if str(k.date)[:10] <= T_str]
            if len(kT) < 60:
                continue
            close_T = kT[-1].close
            if close_T is None or close_T <= 0:
                continue
            fut = future_returns(klines, T_str, close_T)
            base.append(fut)
            try:
                r = _score_candidate(code, {"name": stocks[code], "source": ""}, kT)
            except Exception:
                continue
            if r is None:
                continue
            for h in HORIZONS:
                r[f"fut{h}"] = fut.get(h)
            cands.append(r)
        print(f"  {T_str}: 宇宙 {len(base)} 只 -> 右侧候选 {len(cands)} 只", file=sys.stderr)

    slices = {
        "strong_ge75": lambda r: r["score"] >= 75,
        "mid_55_74": lambda r: 55 <= r["score"] < 75,
        "weak_lt55": lambda r: r["score"] < 55,
        "vol_lt1.2": lambda r: r["vol_ratio"] is not None and r["vol_ratio"] < 1.2,
        "vol_1.2_1.5": lambda r: r["vol_ratio"] is not None and 1.2 <= r["vol_ratio"] < 1.5,
        "vol_1.5_2.0": lambda r: r["vol_ratio"] is not None and 1.5 <= r["vol_ratio"] < 2.0,
        "vol_ge2.0": lambda r: r["vol_ratio"] is not None and r["vol_ratio"] >= 2.0,
        "gain5_lt3": lambda r: r["gain5"] is not None and r["gain5"] < 3,
        "gain5_3_6": lambda r: r["gain5"] is not None and 3 <= r["gain5"] < 6,
        "gain5_6_10": lambda r: r["gain5"] is not None and 6 <= r["gain5"] < 10,
        "gain5_ge10": lambda r: r["gain5"] is not None and r["gain5"] >= 10,
        "align_full": lambda r: r["align"] == "多头排列",
        "align_ma5_10_20": lambda r: r["align"] == "MA5>10>20",
        "align_ma5_10": lambda r: r["align"] == "MA5>10",
        "align_stand20": lambda r: r["align"] == "站MA20",
    }

    report = summarize(cands, base, slices)
    report["dates"] = [_dstr(t) for t in dates]
    out = Path(__file__).resolve().parent / "backtest_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n右侧选股回测（{len(cands)} 候选 / {len(base)} 宇宙）:")
    print_report(report, ["all", "strong_ge75", "mid_55_74", "weak_lt55",
                          "vol_lt1.2", "vol_ge2.0", "gain5_lt3", "gain5_ge10",
                          "align_full", "align_ma5_10"])
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    main()
