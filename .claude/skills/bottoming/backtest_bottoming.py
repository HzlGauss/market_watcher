#!/usr/bin/env python3
"""磨底选股阈值历史收益回测。

数据源：中证1000 成分 + 250 日K线。对每个历史交易日 T，截断 K 线到 T，跑
scan_bottoming 的 _score_bottoming，算未来 5/10/20 交易日收益。
对照：同期全宇宙（中证1000 全体）未来收益 + 同频率随机基准 = 净 edge。

用法:
    py .claude/skills/bottoming/backtest_bottoming.py [回测日期数]

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
from app.regime import board_of, board_regime_at  # noqa: E402
from scan_bottoming import _score_bottoming  # noqa: E402


def main():
    n_dates = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    dates = pick_dates(n_dates)
    print(f"回测日期: {[_dstr(t) for t in dates]}", file=sys.stderr)

    stocks, kcache = load_universe_klines(days=500)  # 需覆盖最早回测日（~160 交易日）前的 120 日窗口

    # 重建每日各板块 regime（牛市/熊市/震荡），回放线上 gate_side("left") 的门控口径
    regime_by_date = {_dstr(T): board_regime_at(_dstr(T)) for T in dates}
    for T in dates:
        print(f"    {_dstr(T)} regime: {regime_by_date[_dstr(T)]}", file=sys.stderr)

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
            if len(kT) < 120:  # 需 120 日算缩量比 + 60 日箱体
                continue
            close_T = kT[-1].close
            if close_T is None or close_T <= 0:
                continue
            fut = future_returns(klines, T_str, close_T)
            base.append(fut)
            day_futs.append(fut)
            try:
                r = _score_bottoming(code, {"name": stocks[code], "source": ""}, kT)
            except Exception:
                continue
            if r is None:
                continue
            r["regime"] = regime_by_date.get(T_str, {}).get(board_of(code), "未知")
            for h in HORIZONS:
                r[f"fut{h}"] = fut.get(h)
            cands.append(r)
            day_cands += 1
        base_by_date[T_str] = day_futs
        cand_count_by_date[T_str] = day_cands
        print(f"  {T_str}: 宇宙 {len(day_futs)} 只 -> 磨底候选 {day_cands} 只", file=sys.stderr)

    slices = {
        "strong_ge75": lambda r: r["score"] >= 75,
        "mid_55_74": lambda r: 55 <= r["score"] < 75,
        "weak_lt55": lambda r: r["score"] < 55,
        "dist_le2": lambda r: r["dist_low"] <= 2,
        "dist_2_4": lambda r: 2 < r["dist_low"] <= 4,
        "dist_4_8": lambda r: 4 < r["dist_low"] <= 8,
        "days_ge30": lambda r: r["days_since_low"] >= 30,
        "days_10_19": lambda r: 10 <= r["days_since_low"] < 20,
        "box_le10": lambda r: r["box_w"] <= 10,
        "box_10_15": lambda r: 10 < r["box_w"] <= 15,
        "box_15_20": lambda r: 15 < r["box_w"] <= 20,
        "vol_lt0.6": lambda r: r["vol_ratio"] < 0.6,
        "vol_0.6_0.8": lambda r: 0.6 <= r["vol_ratio"] < 0.8,
        "above_ma20": lambda r: r["above_ma20"],
        "below_ma20": lambda r: not r["above_ma20"],
        "stage_md": lambda r: r["stage"] == "磨底期",
        "regime_bull": lambda r: r["regime"] == "牛市",
        "regime_bear": lambda r: r["regime"] == "熊市",
        "regime_choppy": lambda r: r["regime"] == "震荡",
        "gated_nonbull": lambda r: r["regime"] != "牛市",  # 线上 gate_side("left") 实际口径
    }

    report = summarize(cands, base, slices, base_by_date, cand_count_by_date)
    report["dates"] = [_dstr(t) for t in dates]
    out = Path(__file__).resolve().parent / "backtest_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n磨底选股回测（{len(cands)} 候选 / {len(base)} 宇宙）:")
    print_report(report, ["all", "gated_nonbull", "regime_bull", "regime_bear", "regime_choppy",
                          "strong_ge75", "mid_55_74", "weak_lt55",
                          "dist_le2", "dist_4_8",
                          "days_ge30", "days_10_19",
                          "box_le10", "box_15_20",
                          "vol_lt0.6", "vol_0.6_0.8",
                          "above_ma20", "below_ma20", "stage_md"])
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    main()
