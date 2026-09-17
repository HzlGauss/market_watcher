#!/usr/bin/env python3
"""盘中决策（intraday-signal）两套规则信号的历史收益回测。

覆盖两个可回测的信号（均只用日 K 线可推导，不依赖盘中实时/资金流数据）：

1. **抄底信号（⑥）**：核心是「近20日深度回撤」+「止跌形态」正向确认；「底背离 /
   资金背离吸筹 / 技术超卖 / 靠近支撑」仅为参考项。回测验证各分量的前向收益增量。
2. **仓位决策底层评分（calc_composite_score）**：0-100 共振评分，按分数档切片，
   验证评分档是否单调（结论：20日档反向，见 SKILL.md）。资金维度（15分）因历史
   无资金流数据恒为 0，故该评分此处仅反映趋势/动量/量价/关键位四维。

做 T（⑤）依赖 5 分钟级盘中数据，日 K 无法忠实回测，不在本脚本覆盖——见 SKILL.md 注意。

数据源：中证1000 成分 + 250 日K线（保证 MA60 可算）。对每个历史交易日 T，截断 K 线
到 T，用 T 日收盘当「现价」，算未来 5/10/20 交易日收益，对照同期全宇宙基准。

用法:
    py .claude/skills/intraday-signal/backtest_intraday.py [回测日期数]

结果写入本目录 backtest_result.json，并打印文字摘要。日K线缓存到 _klines_cache_250.json，
删除该文件可强制重新拉取。
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
from app.models import KlineData, Quote  # noqa: E402
from app.technical import calc_composite_score, get_technical_summary  # noqa: E402
from analyze_intraday import _detect_bottom_divergence, _detect_reversal_pattern  # noqa: E402

DAYS = 250
CACHE = Path(__file__).resolve().parent / f"_klines_cache_{DAYS}.json"


def _load_cache() -> tuple[dict, dict] | None:
    if not CACHE.exists():
        return None
    try:
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
        stocks = raw["stocks"]
        kcache = {
            code: [KlineData(**d) for d in rows]
            for code, rows in raw["kcache"].items()
        }
        return stocks, kcache
    except Exception:
        return None


def _save_cache(stocks, kcache):
    try:
        raw = {
            "stocks": stocks,
            "kcache": {code: [k.__dict__ for k in rows] for code, rows in kcache.items()},
        }
        CACHE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _get_universe():
    cached = _load_cache()
    if cached and cached[1]:
        print(f"  命中日K线缓存（{len(cached[1])} 只），跳过拉取", file=sys.stderr)
        return cached
    stocks, kcache = load_universe_klines(days=DAYS)
    _save_cache(stocks, kcache)
    return stocks, kcache


def main():
    n_dates = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    dates = pick_dates(n_dates)
    print(f"回测日期: {[_dstr(t) for t in dates]}", file=sys.stderr)

    stocks, kcache = _get_universe()

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
            if len(kT) < 60:  # 保证 MA60 / MACD 真实可算
                continue
            last = kT[-1]
            close_T = last.close
            if close_T is None or close_T <= 0:
                continue
            fut = future_returns(klines, T_str, close_T)
            base.append(fut)
            day_futs.append(fut)

            # 用 T 日收盘当「现价」，构造最小 Quote 供技术汇总复用
            prev = kT[-2] if len(kT) >= 2 else None
            change_pct = None
            if prev and prev.close and prev.close > 0:
                change_pct = (close_T - prev.close) / prev.close * 100
            q = Quote(code=code, name=stocks.get(code, ""), price=close_T,
                      open=last.open, high=last.high, low=last.low,
                      pre_close=prev.close if prev else None, change_pct=change_pct,
                      volume=last.volume)
            try:
                tech = get_technical_summary(q, kT)
                comp = calc_composite_score(tech, close_T, None)
            except Exception:
                continue

            closes = [k.close for k in kT if k.close is not None]
            pct20 = (closes[-1] - closes[-20]) / closes[-20] * 100

            # ---- 抄底信号（与 _print_bottom_signal 一致的硬门槛；个股门槛）----
            deep_drop = pct20 <= -15.0
            deep_strong = pct20 <= -20.0
            oversold = (tech.rsi is not None and tech.rsi < 30) or \
                       (tech.kdj_j is not None and tech.kdj_j < 0)
            div, _ = _detect_bottom_divergence(closes)
            pattern, _ = _detect_reversal_pattern(kT)
            near_support = False
            if tech.support and close_T:
                if tech.atr and tech.atr > 0:
                    near_support = (close_T - tech.support) / tech.atr <= 1.5
                else:
                    near_support = (close_T - tech.support) / tech.support <= 0.03

            r = {
                "code": code, "pct20": round(pct20, 2),
                "deep_drop": deep_drop, "deep_strong": deep_strong,
                "oversold": oversold, "div": div, "pattern": pattern,
                "near_support": near_support,
                "score": comp["score"], "label": comp["label"],
                "rsi": tech.rsi, "kdj_j": tech.kdj_j,
                "ma_alignment": tech.ma_alignment,
            }
            for h in HORIZONS:
                r[f"fut{h}"] = fut.get(h)
            cands.append(r)
            day_cands += 1
        base_by_date[T_str] = day_futs
        cand_count_by_date[T_str] = day_cands
        print(f"  {T_str}: 宇宙 {len(day_futs)} 只 -> 候选 {day_cands} 只", file=sys.stderr)

    slices = {
        # 抄底：深度回撤核心 + 参考确认项
        "deep_drop": lambda r: r["deep_drop"],
        "deep_strong": lambda r: r["deep_strong"],
        "deep_no_conf": lambda r: r["deep_drop"] and not (r["div"] or r["pattern"]),
        "deep_confirm": lambda r: r["deep_drop"] and (r["div"] or r["pattern"]),
        "deep_div": lambda r: r["deep_drop"] and r["div"],
        "deep_pattern": lambda r: r["deep_drop"] and r["pattern"],
        "deep_near_sup": lambda r: r["deep_drop"] and r["near_support"],
        "deep_oversold": lambda r: r["deep_drop"] and r["oversold"],
        "oversold_only": lambda r: r["oversold"] and not r["deep_drop"],
        # 仓位评分：按分数档
        "score_ge75": lambda r: r["score"] >= 75,
        "score_60_74": lambda r: 60 <= r["score"] < 75,
        "score_45_59": lambda r: 45 <= r["score"] < 60,
        "score_35_44": lambda r: 35 <= r["score"] < 45,
        "score_lt35": lambda r: r["score"] < 35,
    }

    report = summarize(cands, base, slices, base_by_date, cand_count_by_date)
    report["dates"] = [_dstr(t) for t in dates]
    out = Path(__file__).resolve().parent / "backtest_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print(f"\n盘中决策回测（{len(cands)} 候选 / {len(base)} 宇宙）:")
    print("【抄底信号 ⑥ —— 深度回撤为核心，止跌形态为正向确认】")
    print_report(report, ["deep_drop", "deep_strong", "deep_no_conf", "deep_confirm",
                          "deep_div", "deep_pattern", "deep_near_sup",
                          "deep_oversold", "oversold_only"])
    print("【仓位决策评分 calc_composite_score —— 分数档】")
    print_report(report, ["score_ge75", "score_60_74", "score_45_59",
                          "score_35_44", "score_lt35"])
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    main()
