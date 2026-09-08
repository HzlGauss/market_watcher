#!/usr/bin/env python3
"""龙回头阈值历史收益回测。

数据源限制：akshare 涨停池只保留 ~2 周，故用「中证1000 成分 + K线自检测涨停」
（涨跌幅 >= 涨停幅度即算涨停）构建候选来源，历史可回溯到 K 线覆盖的任意时间。

对每个历史交易日 T：截断 K 线到 T，预筛「T 之前 lookback 日有涨停」，算首波连板，
跑 scan 的 _score_candidate，算未来 5/10/20 交易日收益。对照：同期「近 lookback 日
有涨停」全体（不筛龙回头）的未来收益 = 基准。

用法:
    py .claude/skills/dragon-pullback/backtest_dragon_pullback.py [回测日期数]

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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.backtest import (  # noqa: E402
    HORIZONS, _dstr, future_returns, load_universe_klines,
    pick_dates, print_report, summarize,
)
from scan_dragon_pullback import _find_first_wave, _score_candidate  # noqa: E402

LOOKBACK = 10  # 涨停候选回看天数，与 scan 默认一致


def _limit_pct(code):
    if code.startswith(("300", "301", "302", "688", "689")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return 0.10


def _is_zt(pct, code):
    return pct is not None and pct >= _limit_pct(code) * 100 - 0.6


def calc_pcts(klines):
    pcts, prev = [], None
    for k in klines:
        if prev is not None and k.close is not None:
            pcts.append((k.close - prev) / prev * 100)
        else:
            pcts.append(0.0)
        if k.close is not None:
            prev = k.close
    return pcts


def consec_at(pcts, code, end_idx):
    n = 0
    for i in range(end_idx, -1, -1):
        if _is_zt(pcts[i], code):
            n += 1
        else:
            break
    return n


def main():
    n_dates = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    dates = pick_dates(n_dates)
    print(f"回测日期: {[_dstr(t) for t in dates]}", file=sys.stderr)

    stocks, kcache = load_universe_klines(days=250)

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
            pcts = calc_pcts(kT)
            T_idx = len(kT) - 1
            if not any(_is_zt(p, code) for p in pcts[max(0, T_idx - LOOKBACK):T_idx]):
                continue
            close_T = kT[-1].close
            if close_T is None or close_T <= 0:
                continue
            fut = future_returns(klines, T_str, close_T)
            base.append(fut)  # 基准：近 LOOKBACK 日有涨停的票
            wave = _find_first_wave(kT)
            if wave is None:
                continue
            consec = consec_at(pcts, code, wave["hi"])
            stock = {"name": stocks[code], "consec": consec, "industry": "", "last_date": ""}
            try:
                r = _score_candidate(code, stock, kT)
            except Exception:
                continue
            if r is None:
                continue
            r["code"] = code
            for h in HORIZONS:
                r[f"fut{h}"] = fut.get(h)
            cands.append(r)
        print(f"  {T_str}: 涨停股 {len(base)} 只 -> 候选 {len(cands)} 只", file=sys.stderr)

    slices = {
        "strong_ge75": lambda r: r["score"] >= 75,
        "mid_55_74": lambda r: 55 <= r["score"] < 75,
        "weak_lt55": lambda r: r["score"] < 55,
        "vol_lt0.7": lambda r: r["vol_ratio"] is not None and r["vol_ratio"] < 0.7,
        "vol_0.7_1.0": lambda r: r["vol_ratio"] is not None and 0.7 <= r["vol_ratio"] < 1.0,
        "vol_ge1.0": lambda r: r["vol_ratio"] is not None and r["vol_ratio"] >= 1.0,
        "gap_le5": lambda r: r["gap"] <= 5,
        "gap_6_8": lambda r: 6 <= r["gap"] <= 8,
        "gap_9_11": lambda r: 9 <= r["gap"] <= 11,
        "gap_ge12": lambda r: r["gap"] >= 12,
        "fib_lt0.382": lambda r: r["fib"] is not None and r["fib"] < 0.382,
        "fib_0.382_0.5": lambda r: r["fib"] is not None and 0.382 <= r["fib"] < 0.5,
        "fib_0.5_0.618": lambda r: r["fib"] is not None and 0.5 <= r["fib"] < 0.618,
        "fib_ge0.618": lambda r: r["fib"] is not None and r["fib"] >= 0.618,
    }

    report = summarize(cands, base, slices)
    report["dates"] = [_dstr(t) for t in dates]
    out = Path(__file__).resolve().parent / "backtest_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n龙回头回测（{len(cands)} 候选 / {len(base)} 涨停池）:")
    print_report(report, ["all", "strong_ge75", "mid_55_74", "weak_lt55",
                          "vol_lt0.7", "vol_ge1.0", "gap_le5", "gap_ge12",
                          "fib_0.382_0.5", "fib_0.5_0.618", "fib_ge0.618"])
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    main()
