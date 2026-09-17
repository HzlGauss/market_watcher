#!/usr/bin/env python3
"""左侧选股阈值历史收益回测。

数据源：中证1000 成分 + 500 日K线（保证 MA250 可算）。对每个历史交易日 T，截断
K 线到 T，跑 scan_left_side 的 _score_candidate，算未来 5/10/20 交易日收益。
对照：同期全宇宙（中证1000 全体）未来收益 = 基准。

用法:
    py .claude/skills/left-side/backtest_left_side.py [回测日期数]

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
from scan_left_side import _score_candidate  # noqa: E402


def main():
    n_dates = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    dates = pick_dates(n_dates)
    print(f"回测日期: {[_dstr(t) for t in dates]}", file=sys.stderr)

    stocks, kcache = load_universe_klines(days=500)

    cands, base = [], []
    base_by_date, cand_count_by_date = {}, {}
    for T in dates:
        T_str = _dstr(T)
        day_futs, day_cands = [], 0
        for code in stocks:
            klines = kcache.get(code)
            if not klines:
                continue
            kT = [k for k in klines if str(k.date)[:10] <= T_str]
            if len(kT) < 250:  # 保证 MA250 真实可算（未破长期趋势信号不退化）
                continue
            close_T = kT[-1].close
            if close_T is None or close_T <= 0:
                continue
            fut = future_returns(klines, T_str, close_T)
            base.append(fut)
            day_futs.append(fut)
            try:
                r = _score_candidate(code, {"name": stocks[code], "source": ""}, kT)
            except Exception:
                continue
            if r is None:
                continue
            for h in HORIZONS:
                r[f"fut{h}"] = fut.get(h)
            cands.append(r)
            day_cands += 1
        base_by_date[T_str] = day_futs
        cand_count_by_date[T_str] = day_cands
        print(f"  {T_str}: 宇宙 {len(day_futs)} 只 -> 左侧候选 {day_cands} 只", file=sys.stderr)

    slices = {
        "strong_ge75": lambda r: r["score"] >= 75,
        "mid_55_74": lambda r: 55 <= r["score"] < 75,
        "weak_lt55": lambda r: r["score"] < 55,
        "dd_12_20": lambda r: 12 <= r["dd"] < 20,
        "dd_20_30": lambda r: 20 <= r["dd"] < 30,
        "dd_30_40": lambda r: 30 <= r["dd"] < 40,
        "dd_ge40": lambda r: r["dd"] >= 40,
        "dd_ge45": lambda r: r["dd"] >= 45,
        "vol_lt0.6": lambda r: r["vol_ratio"] is not None and r["vol_ratio"] < 0.6,
        "vol_0.6_0.8": lambda r: r["vol_ratio"] is not None and 0.6 <= r["vol_ratio"] < 0.8,
        "vol_ge1.0": lambda r: r["vol_ratio"] is not None and r["vol_ratio"] >= 1.0,
        "rsi_le30": lambda r: r["rsi"] is not None and r["rsi"] <= 30,
        "rsi_40_60": lambda r: r["rsi"] is not None and 40 < r["rsi"] <= 60,
        "above_ma120": lambda r: r["above_ma120"],
        "below_ma120": lambda r: not r["above_ma120"],
        "pattern_stop": lambda r: r["pattern"],
    }

    report = summarize(cands, base, slices, base_by_date, cand_count_by_date)
    report["dates"] = [_dstr(t) for t in dates]
    out = Path(__file__).resolve().parent / "backtest_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n左侧选股回测（{len(cands)} 候选 / {len(base)} 宇宙）:")
    print_report(report, ["all", "strong_ge75", "mid_55_74", "weak_lt55",
                          "dd_12_20", "dd_ge40", "dd_ge45",
                          "vol_lt0.6", "vol_ge1.0", "rsi_le30",
                          "above_ma120", "below_ma120", "pattern_stop"])
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    main()
