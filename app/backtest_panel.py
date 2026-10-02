"""全市场日K回测共享层：duckdb 面板加载 + 指标 + regime + 分层报告。

仅供 ``.claude/skills/*/backtest_*.py`` 离线回测复用（**非热路径**，需 duckdb/pandas，
用 miniconda 解释器跑）。抽出卖出/买入侧回测的公共部分，避免两套实现漂移：

- 面板：``load_daily`` 读 v_daily_qfq（前复权全市场，按 thscode/date 升序）
- 指标：``add_indicators`` 向量化 MA/RSI(Wilder)/KDJ/量比/乖离/回撤/前高低/均线排列
- 前瞻：``add_forward`` 未来 5/10/20 交易日收盘收益
- regime：``index_regime``（中证1000 MA20/MA60）+ ``board_regime_map`` / ``size_regime_map``
  （板块/市值分层内「站上MA20占比」5日平滑 → 牛/熊/震荡）
- 分层：``add_board``（代码前缀）/ ``load_size_tier``+``add_size_tier``（沪深300/500/1000/微盘，
  akshare 中证官网权威成分，缓存 state/size_tier_map.json）
- 报告：``_report_split``（样本占比 + regime 分布 + 规则×分层 + 规则×分层×regime 的净 edge）

注意：duckdb 仅存当前在市标的，退市股缺失 → 触发后「最坏情形」（退市归零）被系统性低估，
即回测对卖出信号有效性偏保守（偏乐观）；对买入信号则偏悲观。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_DB = _ROOT / "data" / "market.duckdb"
_SIZE_CACHE = _ROOT / "state" / "size_tier_map.json"

HORIZONS = (5, 10, 20)
_PREFIX_OK = ("60", "68", "00", "30")  # 主板/科创板/中小板/创业板（剔除 B 股等）
_RNG = np.random.default_rng(42)


def load_daily(years: int | None) -> pd.DataFrame:
    """从 duckdb 读前复权日K，返回按 (thscode, date) 升序的 DataFrame。"""
    con = duckdb.connect(str(_DB), read_only=True)
    where = "substr(thscode,1,2) IN ('60','68','00','30')"
    if years:
        where += f" AND date >= current_date - interval {years} year"
    df = con.execute(f"""
        SELECT thscode, date, open, high, low, close, volume
        FROM v_daily_qfq
        WHERE {where}
        ORDER BY thscode, date
    """).df()
    con.close()
    df["code"] = df["thscode"].str[:6]
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """按标的向量化算指标（groupby.transform，避免逐只纯 Python）。"""
    g = df.groupby("thscode", sort=False)
    close = df["close"]
    high, low, vol = df["high"], df["low"], df["volume"]

    for n in (5, 10, 20, 60, 120):
        df[f"ma{n}"] = g["close"].transform(lambda s: s.rolling(n).mean())

    # RSI(Wilder)：ewm alpha=1/period 与原实现「首 period 均值作种子」长序列收敛一致
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    ag = gain.groupby(df["thscode"]).transform(lambda s: s.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean())
    al = loss.groupby(df["thscode"]).transform(lambda s: s.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean())
    rs = ag / al
    df["rsi"] = (100 - 100 / (1 + rs)).where(al != 0, 100.0)

    # KDJ J
    ll = low.groupby(df["thscode"]).transform(lambda s: s.rolling(9).min())
    hh = high.groupby(df["thscode"]).transform(lambda s: s.rolling(9).max())
    rsv = (close - ll) / (hh - ll) * 100
    rsv = rsv.where(hh != ll, 50.0)
    k = rsv.groupby(df["thscode"]).transform(lambda s: s.ewm(alpha=1 / 3, adjust=False).mean())
    d = k.groupby(df["thscode"]).transform(lambda s: s.ewm(alpha=1 / 3, adjust=False).mean())
    df["kdj_j"] = 3 * k - 2 * d

    # 量比 = 当日量 / 前5日均量
    df["vol_ratio"] = vol / vol.groupby(df["thscode"]).transform(lambda s: s.shift(1).rolling(5).mean())

    # 乖离 / 近20日高点回撤（收盘口径，与派发规则一致）
    df["bias20"] = (close - df["ma20"]) / df["ma20"] * 100
    hi20 = close.groupby(df["thscode"]).transform(lambda s: s.rolling(20).max())
    df["drawdown_high20"] = (hi20 - close) / hi20 * 100

    # 近120日高点回撤（high 口径，与左侧超跌深度一致）
    hi120 = high.groupby(df["thscode"]).transform(lambda s: s.rolling(120).max())
    df["drawdown_high120"] = (hi120 - close) / hi120 * 100

    # 前 N 日低点（不含当日，shift(1) 后再 rolling）
    for n in (20, 60):
        df[f"low{n}"] = low.groupby(df["thscode"]).transform(lambda s: s.shift(1).rolling(n).min())

    # 突破前 N 日高点（不含当日：close > 前 N 日 high 的最大值）
    for n in (20, 60):
        df[f"broke{n}"] = close > high.groupby(df["thscode"]).transform(
            lambda s: s.shift(1).rolling(n).max()
        )

    # 均线排列（与 detect_stage 同一判定，ma60 缺失时用 ma20 单边判断）
    ma5, ma10, ma20, ma60 = df["ma5"], df["ma10"], df["ma20"], df["ma60"]
    ma20_prev = df["ma20"].groupby(df["thscode"]).transform(lambda s: s.shift(1))
    df["ma20_rising"] = df["ma20"] > ma20_prev
    df["ma20_falling"] = df["ma20"] < ma20_prev
    bull = (ma5 > ma10) & (ma10 > ma20) & (ma60.isna() | (ma20 > ma60))
    bear = (ma5 < ma10) & (ma10 < ma20) & (ma60.isna() | (ma20 < ma60))
    bull_pull = (ma5 < ma10) & df["ma20_rising"]
    bear_bounce = (ma5 > ma10) & df["ma20_falling"]
    df["ma_align"] = np.select(
        [bull, bear, bull_pull, bear_bounce],
        ["多头排列", "空头排列", "多头回调", "空头反弹"],
        default="缠绕",
    )
    return df


def add_forward(df: pd.DataFrame) -> None:
    """未来 5/10/20 个交易日收盘收益（%）。"""
    for h in HORIZONS:
        fwd = df.groupby("thscode")["close"].transform(lambda s: s.shift(-h))
        df[f"fut{h}"] = (fwd / df["close"] - 1) * 100


def load_index() -> pd.Series:
    """中证1000 指数（000852.SZ）收盘序列，用于定义市场牛熊/震荡。"""
    con = duckdb.connect(str(_DB), read_only=True)
    df = con.execute(
        "select date, close from v_daily_qfq where thscode='000852.SZ' order by date"
    ).df()
    con.close()
    return df.set_index("date")["close"]


def index_regime(idx_close: pd.Series) -> pd.Series:
    """按指数 MA20/MA60 相对位置定义逐日 regime：牛市 / 熊市 / 震荡。"""
    ma20 = idx_close.rolling(20).mean()
    ma60 = idx_close.rolling(60).mean()
    ratio = ma20 / ma60 - 1
    regime = np.select([ratio > 0.02, ratio < -0.02], ["牛市", "熊市"], default="震荡")
    return pd.Series(regime, index=idx_close.index, name="regime")


def add_board(df: pd.DataFrame) -> None:
    """按代码前缀划分板块（时间稳定，零外部数据）。"""
    p = df["code"].str[:2]
    df["board"] = np.select(
        [p == "60", p == "00", p == "30", p == "68"],
        ["沪主板", "深主板", "创业板", "科创板"],
        default="其他",
    )


def _breadth_regime(df: pd.DataFrame, dim: str) -> pd.Series:
    """某分层维度内「站上MA20占比」5日平滑 → 牛/熊/震荡，返回 (dim, date) 索引 Series。"""
    above = (df["close"] > df["ma20"]).astype(float)
    raw = above.groupby([df[dim], df["date"]]).mean()
    sm = raw.groupby(level=0, group_keys=False).apply(
        lambda s: s.rolling(5, min_periods=1).mean()
    )
    return sm.map(lambda x: "牛市" if x >= 0.6 else ("熊市" if x <= 0.4 else "震荡"))


def board_regime_map(df: pd.DataFrame) -> pd.Series:
    """板块广度（站上 MA20 占比，5 日平滑）定牛熊震荡，返回 (board, date) 索引 Series。"""
    return _breadth_regime(df, "board")


_SIZE_TIERS = (("000300", "沪深300"), ("000905", "中证500"), ("000852", "中证1000"))


def load_size_tier() -> dict[str, str]:
    """沪深300/中证500/中证1000 成分 → 市值分层；其余 = 微盘。

    用中证官网 ``index_stock_cons_csindex``（权威、完整 300/500/1000，且三指数
    按市值排名互斥）。结果缓存到 state/size_tier_map.json，重复运行不重拉。
    akshare 不可达时读缓存；两者都失败返回空 dict（调用方回退「纯微盘」）。
    """
    if _SIZE_CACHE.exists():
        try:
            return json.loads(_SIZE_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    mapping: dict[str, str] = {}
    try:
        import akshare as ak

        for sym, label in _SIZE_TIERS:
            df = ak.index_stock_cons_csindex(symbol=sym)
            for c in df["成分券代码"]:
                code = re.sub(r"\D", "", str(c)).zfill(6)
                if code:
                    mapping[code] = label
    except Exception as e:
        print(f"⚠️ akshare 中证官网成分获取失败：{e}", file=sys.stderr)
        return {}
    if mapping:
        try:
            _SIZE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _SIZE_CACHE.write_text(
                json.dumps(mapping, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
    return mapping


def add_size_tier(df: pd.DataFrame, mapping: dict[str, str]) -> None:
    """按 6 位代码映射市值分层，未命中（不在三大指数内）= 微盘。"""
    df["size"] = df["code"].map(mapping).fillna("微盘")


def size_regime_map(df: pd.DataFrame) -> pd.Series:
    """市值分层广度（站上 MA20 占比，5 日平滑）定牛熊震荡，返回 (size, date) 索引 Series。"""
    return _breadth_regime(df, "size")


def _stat(vals) -> dict | None:
    vals = [v for v in vals if v is not None and not np.isnan(v)]
    if not vals:
        return None
    arr = np.asarray(vals)
    return {"n": len(arr), "avg": float(arr.mean()), "med": float(np.median(arr)),
            "win%": float((arr > 0).mean() * 100),
            "p5": float(np.percentile(arr, 5)),
            "crash%": float((arr < -10).mean() * 100),    # 触发后 20 日续跌超 10%
            "rebound%": float((arr > 10).mean() * 100)}   # 触发后 20 日反弹超 10%


def _random_mean(df: pd.DataFrame, n_trig: int, k: int = 100) -> dict | None:
    """同频率随机信号的 fut20 均值分布（仅 20 日，供分层×regime 细分格快速抽样）。

    用放回抽样（integers）：n_trig << n 时与无放回结果一致，且 O(n_trig) 远快于
    ``choice(..., replace=False)`` 背后 permutation 的 O(n)。
    """
    if n_trig <= 0:
        return None
    fut20 = df["fut20"].to_numpy()
    n = len(fut20)
    means = []
    for _ in range(k):
        picks = _RNG.integers(0, n, size=min(n_trig, n))
        v = fut20[picks]
        v = v[~np.isnan(v)]
        if len(v):
            means.append(float(v.mean()))
    if not means:
        return None
    m = np.asarray(means)
    return {"avg": float(m.mean()), "std": float(m.std())}


def _random_baseline(df: pd.DataFrame, n_trig: int, k: int = 400) -> dict:
    """同频率随机信号基准：抽与真实触发数相同的随机 (标的,日)，算其前瞻收益均值分布。"""
    fut = {h: df[f"fut{h}"].to_numpy() for h in HORIZONS}
    n = len(df)
    means = {h: [] for h in HORIZONS}
    idx = np.arange(n)
    for _ in range(k):
        picks = _RNG.choice(idx, size=min(n_trig, n), replace=False)
        for h in HORIZONS:
            v = fut[h][picks]
            v = v[~np.isnan(v)]
            if len(v):
                means[h].append(float(v.mean()))
    out = {}
    for h in HORIZONS:
        if means[h]:
            m = np.asarray(means[h])
            out[h] = {"avg": float(m.mean()), "std": float(m.std())}
        else:
            out[h] = None
    return out


def _report_split(valid, dim_col, regime_col, labels, regimes, RULES, title, edge_note="越负越该卖"):
    """按某一分层维度（板块/市值）打印：样本占比 + regime 分布 + 规则×分层 + 规则×分层×regime。"""
    share = {l: int((valid[dim_col] == l).sum()) for l in labels}
    total = sum(share.values())
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    print("  样本占比: " + "  ".join(
        f"{l}{share[l] / total * 100:.0f}%" for l in labels))
    print(f"  各{dim_col}内 regime 分布（广度>=0.6牛 / <=0.4熊 / 中间震荡）:")
    for l in labels:
        sub = valid[valid[dim_col] == l]
        if len(sub) == 0:
            continue
        cells = "  ".join(
            f"{r}{(sub[regime_col] == r).sum() / len(sub) * 100:.0f}%"
            for r in regimes)
        print(f"    {l:<6} {cells}")

    print()
    print(f"{'规则':<16}" + "".join(f"{l:>12}" for l in labels) + "   (20日净edge by 分层)")
    print("-" * 78)
    for label, col in RULES:
        cells = []
        for l in labels:
            sub = valid[valid[dim_col] == l]
            trig = sub[sub[col]]
            n_trig = len(trig)
            if n_trig == 0:
                cells.append("--")
                continue
            s = _stat(trig["fut20"].tolist())
            rnd = _random_mean(sub, n_trig, k=200)
            if s and rnd:
                e = s["avg"] - rnd["avg"]
                sig = "*" if abs(e) > 2 * rnd["std"] else ""
                cells.append(f"{e:+.2f}%{sig}")
            else:
                cells.append("--")
        print(f"{label:<16}" + "".join(f"{c:>12}" for c in cells))

    print()
    print(f"规则 × 分层 × regime（20日净edge；{edge_note}；* = |edge|>2×std）")
    print("-" * 78)
    for label, col in RULES:
        print(f"  {label}:")
        for l in labels:
            cells = []
            for r in regimes:
                sub = valid[(valid[dim_col] == l) & (valid[regime_col] == r)]
                trig = sub[sub[col]]
                n_trig = len(trig)
                if n_trig == 0:
                    cells.append(f"{r} --")
                    continue
                s = _stat(trig["fut20"].tolist())
                rnd = _random_mean(sub, n_trig, k=100)
                if s and rnd:
                    e = s["avg"] - rnd["avg"]
                    sig = "*" if abs(e) > 2 * rnd["std"] else ""
                    cells.append(f"{r}{e:+.2f}%{sig}(n{n_trig})")
                else:
                    cells.append(f"{r} --")
            print(f"    {l:<6} " + "  ".join(cells))
