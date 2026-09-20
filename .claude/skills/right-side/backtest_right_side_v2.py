#!/usr/bin/env python3
"""右侧选股 v2 回测（多宇宙 + 宽时间窗，走本地 DuckDB）：对比「突破日追入」vs「回踩买点」。

在第一版（中证1000、8 个日期、新浪拉 K）基础上放宽：
- **数据源**：改走本地 market.duckdb（raw_kline_daily + calc_adjust_factor_daily 前复权），
  一次批量读，不再逐只打新浪。
- **多宇宙**：沪深300（大盘，万华化学所在池）/ 中证500（中盘）/ 中证1000（小盘）
  三档市值层（上证50/中证100/深证100 均为沪深300 子集，故三档已覆盖）。
- **宽时间窗**：默认 40 个历史交易日、从最新往前覆盖近 ~3 年（730 交易日），
  覆盖多段牛熊/震荡 regime。

打分函数 `_score_candidate`/`_score_pullback` 原样复用（与扫描器同一套逻辑），
只把喂给它们的 K 线来源从新浪换成本地前复权。成分用 akshare index_stock_cons
（当前成分，有幸存者偏差，但两信号同池对比、偏差对等）。

用法:
    py .claude/skills/right-side/backtest_right_side_v2.py [回测日期数] [回溯交易日数]

结果按指数逐段打印，并写入 backtest_result_v2.json。
"""
import bisect
import json
import logging
import os
import re
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
try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import KlineData  # noqa: E402
from app.backtest import HORIZONS, print_report, summarize  # noqa: E402
from scan_right_side import _score_candidate, _score_pullback  # noqa: E402

_SCANNABLE = re.compile(r"^(60|68|00|30)")

UNIVERSES = [
    ("沪深300", "000300"),
    ("中证500", "000905"),
    ("中证1000", "000852"),
]

SLICES_BREAKOUT = {
    "strong_ge75": lambda r: r["score"] >= 75,
    "mid_55_74": lambda r: 55 <= r["score"] < 75,
    "weak_lt55": lambda r: r["score"] < 55,
    "gain5_ge10": lambda r: r["gain5"] is not None and r["gain5"] >= 10,
    "vol_ge2.0": lambda r: r["vol_ratio"] is not None and r["vol_ratio"] >= 2.0,
}
SLICES_PULLBACK = {
    "dd_3_6": lambda r: 3.0 <= r["drawdown"] <= 6.0,
    "dd_6_10": lambda r: r["drawdown"] > 6.0,
    "vol_lt0.8": lambda r: r["vol_ratio"] is not None and r["vol_ratio"] < 0.8,
    "near_ma10": lambda r: r["dist_ma10"] <= 2.0,
    "score_ge70": lambda r: r["score"] >= 70,
}


def _thscode(code: str) -> str:
    c = code.strip()
    if "." in c:
        return c
    if c[:2] in ("60", "68", "51", "56", "58", "90", "11", "50"):
        return f"{c}.SH"
    if c[:2] in ("00", "30", "15", "16", "18", "12", "39"):
        return f"{c}.SZ"
    if c[:1] in ("8", "4", "92"):
        return f"{c}.BJ"
    return c


def _db_path():
    from app.kline_local import _db_path as _p
    return _p()


def load_trade_dates():
    import duckdb
    con = duckdb.connect(str(_db_path()), read_only=True)
    try:
        rows = con.execute("SELECT DISTINCT date FROM raw_kline_daily ORDER BY date").fetchall()
    finally:
        con.close()
    return [str(r[0])[:10] for r in rows]


def pick_local_dates(all_dates, n, anchor_back=60, span_back=730):
    """从最新往前，取 n 个交易日，覆盖 anchor_back~span_back 交易日（约 span_back/250 年）。"""
    m = len(all_dates)
    base_idx = m - 1 - anchor_back
    step = max(1, (span_back - anchor_back) // max(1, n - 1))
    out = []
    for i in range(n):
        j = base_idx - step * i
        if j < 0:
            break
        out.append(all_dates[j])
    return out  # 近 -> 远


def load_index_codes():
    """三档指数成分（当前），返回 {label: {6位代码: 名称}}。"""
    import akshare as ak
    universe = {}
    for label, symbol in UNIVERSES:
        codes = {}
        try:
            df = ak.index_stock_cons(symbol=symbol)
        except Exception as e:
            print(f"  {label} 成分拉取失败: {e}", file=sys.stderr)
            universe[label] = codes
            continue
        for _, r in df.iterrows():
            code = str(r.get("品种代码", "")).zfill(6)
            name = str(r.get("品种名称", ""))
            if code and code != "000000" and _SCANNABLE.match(code):
                codes[code] = name
        universe[label] = codes
        print(f"  {label} 成分 {len(codes)} 只", file=sys.stderr)
    return universe


def load_local_klines(codes):
    """本地 DuckDB 批量读前复权日K（升序），返回 {code: [KlineData]}。"""
    import duckdb
    ths = [_thscode(c) for c in codes]
    if not ths:
        return {}
    ph = ",".join("?" for _ in ths)
    sql = f"""
    SELECT k.thscode, k.date,
           k.open  * COALESCE(a.forward_factor, 1.0),
           k.high  * COALESCE(a.forward_factor, 1.0),
           k.low   * COALESCE(a.forward_factor, 1.0),
           k.close * COALESCE(a.forward_factor, 1.0),
           k.volume
    FROM raw_kline_daily k
    LEFT JOIN calc_adjust_factor_daily a ON a.thscode = k.thscode AND a.date = k.date
    WHERE k.thscode IN ({ph})
    ORDER BY k.thscode, k.date
    """
    con = duckdb.connect(str(_db_path()), read_only=True)
    try:
        rows = con.execute(sql, ths).fetchall()
    finally:
        con.close()
    kcache = {}
    for thscode, d, o, h, l, c, v in rows:
        code = thscode.split(".")[0]
        kcache.setdefault(code, []).append(KlineData(
            date=str(d)[:10], open=o, high=h, low=l, close=c, volume=v,
        ))
    return kcache


def _run(fn, stocks, kcache, date_strs):
    """对单个信号函数跑一遍历史回测（bisect 截断 + 内联未来收益）。"""
    cands, base = [], []
    base_by_date, cand_count_by_date = {}, {}
    for code in stocks:
        klines = kcache.get(code)
        if not klines:
            continue
        dts = [k.date for k in klines]
        for T in date_strs:
            idx = bisect.bisect_right(dts, T)
            if idx < 60:
                continue
            kT = klines[:idx]
            close_T = kT[-1].close
            if close_T is None or close_T <= 0:
                continue
            after = klines[idx:]
            fut = {}
            for h in HORIZONS:
                if len(after) >= h and after[h - 1].close is not None:
                    fut[h] = (after[h - 1].close - close_T) / close_T * 100
                else:
                    fut[h] = None
            base.append(fut)
            base_by_date.setdefault(T, []).append(fut)
            try:
                r = fn(code, {"name": stocks[code], "source": ""}, kT)
            except Exception:
                r = None
            if r is not None:
                for h in HORIZONS:
                    r[f"fut{h}"] = fut.get(h)
                cands.append(r)
                cand_count_by_date[T] = cand_count_by_date.get(T, 0) + 1
    return cands, base, base_by_date, cand_count_by_date


def main():
    n_dates = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    span_back = int(sys.argv[2]) if len(sys.argv) > 2 else 730

    all_dates = load_trade_dates()
    if not all_dates:
        print("❌ 本地库无交易日数据", file=sys.stderr)
        return 1
    dates = pick_local_dates(all_dates, n_dates, span_back=span_back)
    print(f"本地库最新 {all_dates[-1]}；回测 {len(dates)} 个交易日: {dates[-1]} ~ {dates[0]}", file=sys.stderr)

    universe = load_index_codes()
    all_codes = {}
    for label, codes in universe.items():
        for c, n in codes.items():
            all_codes.setdefault(c, n)
    print(f"  合计去重 {len(all_codes)} 只，从本地库读前复权日K ...", file=sys.stderr)
    kcache = load_local_klines(all_codes)
    print(f"  本地 K 线命中 {len(kcache)} 只", file=sys.stderr)

    results = {}
    for label, stocks in universe.items():
        if not stocks:
            continue
        hit = sum(1 for c in stocks if c in kcache)
        print()
        print("#" * 78)
        print(f"# 宇宙：{label}（{len(stocks)} 只，本地 K 线命中 {hit}）")
        print("#" * 78)

        cands, base, bbd, ccd = _run(_score_candidate, stocks, kcache, dates)
        report1 = summarize(cands, base, SLICES_BREAKOUT, bbd, ccd)

        pb_cands, pb_base, pbbd, pbccd = _run(_score_pullback, stocks, kcache, dates)
        report2 = summarize(pb_cands, pb_base, SLICES_PULLBACK, pbbd, pbccd)

        print("\n一、突破日追入（现行 _score_candidate）")
        print_report(report1, ["all", "strong_ge75", "mid_55_74", "weak_lt55",
                               "gain5_ge10", "vol_ge2.0"])

        print("\n二、回踩买点（拟新增 _score_pullback）")
        print_report(report2, ["all", "dd_3_6", "dd_6_10", "vol_lt0.8",
                               "near_ma10", "score_ge70"])

        results[label] = {
            "n_stocks": len(stocks),
            "breakout": {k: v for k, v in report1.items() if k != "n_base"},
            "pullback": {k: v for k, v in report2.items() if k != "n_base"},
        }

    out = Path(__file__).resolve().parent / "backtest_result_v2.json"
    payload = {
        "dates": dates,
        "universes": results,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    sys.exit(main())
