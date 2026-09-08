"""回测引擎：对 skill 的候选打分函数做严格历史收益回测。

供 ``.claude/skills/*/backtest_*.py`` 复用。数据源：中证1000 成分
（akshare ``index_stock_cons``，小盘股，涨停/超跌/突破候选集中）+ 新浪日K线
（``app.technical.fetch_historical_kline``）。

不依赖 akshare 涨停池（``stock_zt_pool_em`` 只保留 ~2 周，无法回测历史），历史
可回溯到 K 线覆盖的任意时间。

对每个历史交易日 T：截断 K 线到 T，调用 skill 的打分函数，算未来 5/10/20 个交易日
收益（T 收盘 -> 未来收盘），按分数档 + 自定义阈值维度切片统计命中率/平均收益，
对照同期全宇宙基准。

注意：akshare 仅在函数内延迟导入，主程序不依赖它（仍是可选依赖）。
"""
import re
import sys
from datetime import date

HORIZONS = (5, 10, 20)
_SCANNABLE = re.compile(r"^(60|68|00|30)")


def _dstr(d) -> str:
    return d.strftime("%Y-%m-%d")


def pick_dates(n: int) -> list:
    """最近 n 个历史回测日（每隔 ~15 个交易日，覆盖约 n*15 交易日）。"""
    import akshare as ak

    cal = ak.tool_trade_date_hist_sina()
    dates = sorted([d for d in cal["trade_date"] if d < date.today()])
    return [dates[-(55 + 15 * i)] for i in range(n)]


def load_universe_klines(days: int = 250) -> tuple[dict, dict]:
    """中证1000 成分 + 日K线缓存。返回 (stocks: {code: name}, kcache: {code: [KlineData]})。"""
    import akshare as ak

    from app.helpers import _detect_market
    from app.technical import fetch_historical_kline

    df = ak.index_stock_cons(symbol="000852")  # 中证1000
    stocks, kcache = {}, {}
    for _, r in df.iterrows():
        code = str(r.get("品种代码", "")).zfill(6)
        name = str(r.get("品种名称", ""))
        if code and code != "000000" and _SCANNABLE.match(code):
            stocks[code] = name
    print(f"  中证1000 股票池 {len(stocks)} 只，拉日K线（days={days}）...", file=sys.stderr)
    for i, code in enumerate(stocks):
        try:
            k = fetch_historical_kline(code, _detect_market(code), days=days, scale=240)
            if k and len(k) >= 60:
                kcache[code] = k
        except Exception:
            pass
        if (i + 1) % 200 == 0:
            print(f"    已拉 {i + 1}/{len(stocks)} ...", file=sys.stderr)
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


def summarize(cands, base, slices):
    """汇总统计。

    cands: list[dict]，每个候选含 ``fut5/fut10/fut20`` 及特征字段。
    base:  list[dict(int->pct)]，同期基准（全体或候选来源池）的未来收益。
    slices: {name: predicate(r)->bool}，自定义阈值维度切片。
    """
    out = {"n": len(cands), "n_base": len(base)}
    out["baseline"] = {f"fut{h}": _stat_pure([f.get(h) for f in base if f.get(h) is not None])
                       for h in HORIZONS}
    out["all"] = per_horizon(lambda k: stat(cands, k))
    for name, fn in slices.items():
        rows = [r for r in cands if fn(r)]
        out[name] = per_horizon(lambda k: stat(rows, k))
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
    for sec in sections:
        if sec in report:
            print(f"  {sec:<16} {_row(report[sec])}")
