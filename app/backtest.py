"""回测引擎：对 skill 的候选打分函数做严格历史收益回测。

供 ``.claude/skills/*/backtest_*.py`` 复用。数据源：中证1000 成分
（akshare ``index_stock_cons``，小盘股，涨停/超跌/突破候选集中，单次元数据调用）
+ 本地 duckdb 前复权日K线（``data/market.duckdb`` 的 ``v_daily_qfq``，批量一次读，
快且不限流，与 scan 同源；本地缺该标的/库不可用时回退新浪 ``fetch_historical_kline``）。

不依赖 akshare 涨停池（``stock_zt_pool_em`` 只保留 ~2 周，无法回测历史），历史
可回溯到 K 线覆盖的任意时间。

对每个历史交易日 T：截断 K 线到 T，调用 skill 的打分函数，算未来 5/10/20 个交易日
收益（T 收盘 -> 未来收盘），按分数档 + 自定义阈值维度切片统计命中率/平均收益，
对照同期全宇宙基准。

注意：akshare 仅在函数内延迟导入，主程序不依赖它（仍是可选依赖）。
"""
import re
import sys
from datetime import date, timedelta

HORIZONS = (5, 10, 20)
_SCANNABLE = re.compile(r"^(60|68|00|30)")


def _num(v):
    """duckdb/pandas 值 → float（None/NaN → None）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN


def _load_local_klines(codes: list, days: int) -> dict:
    """本地 duckdb 批量读前复权日K（v_daily_qfq）→ {6位代码: [KlineData]}。

    一次查询全池历史（截到最近 ``days`` 交易日，按日期宽松过取再 tail），快、不限流。
    库缺失/查询失败返回 {}（调用方回退新浪远端）。
    """
    import duckdb
    from pathlib import Path as _Path

    from app.hithink import thscode
    from app.models import KlineData

    db = _Path(__file__).resolve().parents[1] / "data" / "market.duckdb"
    if not db.exists():
        return {}
    ths = [thscode(c) for c in codes]
    ph = ",".join("?" for _ in ths)
    # 交易日 ≈ 自然日 * 0.68，多取一段再 tail(days) 保证够
    cutoff = date.today() - timedelta(days=int(days * 1.6) + 90)
    try:
        con = duckdb.connect(str(db), read_only=True)
        df = con.execute(
            f"select thscode, date, open, high, low, close, volume from v_daily_qfq "
            f"where thscode in ({ph}) and date >= ? order by thscode, date",
            [*ths, cutoff],
        ).df()
        con.close()
    except Exception:
        return {}
    if df is None or len(df) == 0:
        return {}

    df = df.sort_values(["thscode", "date"])
    out: dict = {}
    for ths_code, grp in df.groupby("thscode", sort=False):
        g = grp.tail(days)
        out[str(ths_code)[:6]] = [
            KlineData(
                date=str(v["date"])[:10],
                open=_num(v["open"]),
                high=_num(v["high"]),
                low=_num(v["low"]),
                close=_num(v["close"]),
                volume=_num(v["volume"]),
            )
            for _, v in g.iterrows()
        ]
    return out


def _dstr(d) -> str:
    return d.strftime("%Y-%m-%d")


def pick_dates(n: int) -> list:
    """最近 n 个历史回测日（每隔 ~15 个交易日，覆盖约 n*15 交易日）。"""
    import akshare as ak

    cal = ak.tool_trade_date_hist_sina()
    dates = sorted([d for d in cal["trade_date"] if d < date.today()])
    return [dates[-(55 + 15 * i)] for i in range(n)]


def load_universe_klines(days: int = 250) -> tuple[dict, dict]:
    """中证1000 成分 + 日K线缓存。返回 (stocks: {code: name}, kcache: {code: [KlineData]})。

    K 线优先本地 duckdb 前复权日K（批量一次读，与 scan 同源），本地缺该标的才回退新浪。
    """
    import akshare as ak

    from app.helpers import _detect_market
    from app.technical import fetch_historical_kline

    df = ak.index_stock_cons(symbol="000852")  # 中证1000
    stocks, codes = {}, []
    for _, r in df.iterrows():
        code = str(r.get("品种代码", "")).zfill(6)
        name = str(r.get("品种名称", ""))
        if code and code != "000000" and _SCANNABLE.match(code):
            stocks[code] = name
            codes.append(code)
    print(f"  中证1000 股票池 {len(stocks)} 只，读本地日K线（days={days}）...", file=sys.stderr)

    kcache = _load_local_klines(codes, days)
    missing = [c for c in codes if c not in kcache or not kcache[c]]
    if missing:
        print(f"  本地缺 {len(missing)} 只，回退新浪远端...", file=sys.stderr)
        for i, code in enumerate(missing):
            try:
                k = fetch_historical_kline(code, _detect_market(code), days=days, scale=240)
                if k and len(k) >= 60:
                    kcache[code] = k
            except Exception:
                pass
            if (i + 1) % 200 == 0:
                print(f"    已拉 {i + 1}/{len(missing)} ...", file=sys.stderr)
    return stocks, kcache


def future_returns(klines, T_str: str, close_T: float) -> dict:
    """T 收盘 -> 未来第 h 个交易日收盘收益（%）。不足 h 根则为 None。"""
    after = [k for k in klines if str(k.date)[:10] > T_str]
    out = {}
    for h in HORIZONS:
        if len(after) >= h and after[h - 1].close is not None and close_T and close_T > 0:
            out[h] = (after[h - 1].close - close_T) / close_T * 100
        else:
            out[h] = None
    return out


def _stat_pure(vals):
    if not vals:
        return None
    n = len(vals)
    return {"n": n, "avg": round(sum(vals) / n, 2),
            "med": round(sorted(vals)[n // 2], 2),
            "win%": round(sum(1 for v in vals if v > 0) / n * 100, 1)}


def stat(rows, key):
    return _stat_pure([r[key] for r in rows if r.get(key) is not None])


def per_horizon(fn):
    return {f"fut{h}": fn(f"fut{h}") for h in HORIZONS}


def random_baseline(base_by_date, cand_count_by_date, k=200, seed=42):
    """同频率随机信号基准（蒙特卡洛）。

    每个历史日期 T，随机抽「与真实信号候选数相同」的股票，算其未来收益均值；
    重复 k 次，得到「随机选股」的期望收益分布。用于剥离信号的真实选股 edge
    与「市场 β / 运气」——真实信号均值 vs 随机均值 = 净 edge。

    Args:
        base_by_date:       {date_str: [fut dicts]}  当日宇宙的未来收益
        cand_count_by_date: {date_str: int}          真实信号当日候选数
    Returns:
        {fut{h}: {"avg":.., "std":.., "n_runs":..} or None}
    """
    import random
    rng = random.Random(seed)
    run_means = {h: [] for h in HORIZONS}
    for _ in range(k):
        run_vals = {h: [] for h in HORIZONS}
        for T, n in cand_count_by_date.items():
            day = base_by_date.get(T) or []
            if n <= 0 or not day:
                continue
            picks = rng.sample(day, min(n, len(day)))
            for h in HORIZONS:
                run_vals[h].extend(p.get(h) for p in picks if p.get(h) is not None)
        for h in HORIZONS:
            if run_vals[h]:
                run_means[h].append(sum(run_vals[h]) / len(run_vals[h]))
    out = {}
    for h in HORIZONS:
        v = run_means[h]
        if v:
            m = sum(v) / len(v)
            sd = (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5
            out[f"fut{h}"] = {"avg": round(m, 2), "std": round(sd, 2), "n_runs": len(v)}
        else:
            out[f"fut{h}"] = None
    return out


def _edge(sig, rnd):
    """真实信号 - 随机基准 = 净 edge；|edge| > 2×随机 std 记 `*`（粗略显著性）。"""
    out = {}
    for h in HORIZONS:
        s = sig.get(f"fut{h}")
        r = rnd.get(f"fut{h}")
        if s and r and s.get("avg") is not None and r.get("avg") is not None:
            e = round(s["avg"] - r["avg"], 2)
            out[f"fut{h}"] = {"edge": e, "sig": "*" if abs(e) > 2 * (r.get("std") or 0) else ""}
        else:
            out[f"fut{h}"] = None
    return out


def summarize(cands, base, slices, base_by_date=None, cand_count_by_date=None):
    """汇总统计。

    cands: list[dict]，每个候选含 ``fut5/fut10/fut20`` 及特征字段。
    base:  list[dict(int->pct)]，同期基准（全体或候选来源池）的未来收益。
    slices: {name: predicate(r)->bool}，自定义阈值维度切片。
    base_by_date/cand_count_by_date: 可选，传入则附「随机基准」与「净 edge」。
    """
    out = {"n": len(cands), "n_base": len(base)}
    out["baseline"] = {f"fut{h}": _stat_pure([f.get(h) for f in base if f.get(h) is not None])
                       for h in HORIZONS}
    out["all"] = per_horizon(lambda k: stat(cands, k))
    for name, fn in slices.items():
        rows = [r for r in cands if fn(r)]
        out[name] = per_horizon(lambda k: stat(rows, k))
    if base_by_date is not None and cand_count_by_date is not None:
        out["random"] = random_baseline(base_by_date, cand_count_by_date)
        out["edge"] = _edge(out["all"], out["random"])
    return out


def print_report(report, sections):
    """打印紧凑文字摘要。sections 为切片名列表（含 "all"）。"""
    H = [f"fut{h}" for h in HORIZONS]

    def _fmt(stat_dict, h):
        x = stat_dict.get(h) if stat_dict else None
        return f"{h}:{x['avg']:+.1f}%/{x['win%']}%(n{x['n']})" if x else f"{h}:--"

    def _row(s):
        return "  ".join(_fmt(s, h) for h in H)

    print(f"基准(n={report['n_base']}): " + "  ".join(_fmt(report["baseline"], h) for h in H))
    if "random" in report:
        rnd = report["random"]
        print("随机基准(同频率): " + "  ".join(
            f"{h}:{rnd[h]['avg']:+.1f}%±{rnd[h]['std']:.1f}"
            if rnd.get(h) else f"{h}:--" for h in H))
        if "edge" in report:
            print("净 edge(真实-随机): " + "  ".join(
                f"{h}:{report['edge'][h]['edge']:+.1f}%{report['edge'][h]['sig']}"
                if report["edge"].get(h) else f"{h}:--" for h in H))
    for sec in sections:
        if sec in report:
            print(f"  {sec:<16} {_row(report[sec])}")
