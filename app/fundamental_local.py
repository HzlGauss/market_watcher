"""本地 DuckDB（marketdb）估值分位 + 基本面适配层 —— 可选依赖。

把两个原本依赖远端 key 的能力改由本地 market.duckdb 已落盘的表直接计算：

1. **估值历史分位**（原走妙想 MX_APIKEY 的 ``pb_pct``/``pe_pct``）：
   读 ``valuation_daily``（估值快照逐日累积），对当前 PE/PB 在历史每日序列里
   算百分位（越低越便宜）。分位需足够长的历史才有意义，累积天数不足时返回 None，
   由调用方回退妙想。

2. **成长股/基本面指标**（原走同花顺 ``fetch_financial_indicators`` / 妙想）：
   读 ``fin_income`` / ``fin_balance`` / ``fin_cashflow`` 三张多期报表，本地算
   ROE（年化）/毛利率/负债率/营收同比/净利同比/经营现金流净额。

duckdb 为可选重依赖，此处惰性 import，保持 app/ 核心「no pandas、依赖最小化」约定。
库不存在 / duckdb 未安装 / 该标的未落盘 / 查询失败时返回 None，由调用方走远端兜底，
绝不因此报错或卡住。只读（read_only=True），不写 marketdb。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]

# 估值分位最少历史交易日数：少于该天数分位无意义，返回 None 回退妙想
_MIN_HISTORY = 20


def _db_path() -> Path:
    p = os.environ.get("MARKETDB_DB_PATH")
    if p:
        return Path(p).expanduser()
    return _ROOT / "data" / "market.duckdb"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN


def _thscode(code: str, market: str = "") -> str:
    """6 位代码 → 带交易所后缀 thscode；已带后缀则原样返回。"""
    c = str(code).strip()
    if "." in c:
        return c
    try:
        from .hithink import thscode
        return thscode(c, market)
    except Exception:
        pass
    if c[:2] in ("60", "68", "51", "56", "58", "90", "11", "50"):
        return f"{c}.SH"
    return f"{c}.SZ"


def _percentile(series: list[Optional[float]], current: Optional[float]) -> Optional[float]:
    """current 在 series 中的百分位（0~100，越低越便宜）。

    只对正值算分位（负 PE=亏损、负 PB=资不抵债，无「便宜/贵」可言，直接剔除）。
    """
    valid = [v for v in series if v is not None and v == v and v > 0]
    if not valid or current is None or current != current or current <= 0:
        return None
    below = sum(1 for v in valid if v <= current)
    return below / len(valid) * 100.0


def valuation_percentile(code: str, market: str = "") -> Optional[dict]:
    """本地算 PE/PB 历史分位（读 valuation_daily），累积天数不足返回 None 由调用方回退妙想。

    返回 {pb_pct, pe_pct, pb, pe, n}：
    - pb_pct/pe_pct 为 0~100 分位（越低越便宜）；任一算不出（当前为负/无正值历史）为 None。
    - pb/pe 为最新快照原始值（本地收盘累积，非盘中实时），仅作参考，调用方通常仍用同花顺实时快照。
    - n 为参与分位的历史交易日数。
    """
    db = _db_path()
    if not db.exists():
        return None
    try:
        import duckdb
    except Exception:
        return None
    ts = _thscode(code, market)
    try:
        with duckdb.connect(str(db), read_only=True) as con:
            rows = con.execute(
                "SELECT trade_date, pe_ttm, pb_mrq FROM valuation_daily "
                "WHERE thscode = ? ORDER BY trade_date",
                [ts],
            ).fetchall()
    except Exception:
        return None
    if not rows:
        return None

    dates = sorted({r[0] for r in rows})
    if len(dates) < _MIN_HISTORY:
        return None

    cur_pe, cur_pb = _f(rows[-1][1]), _f(rows[-1][2])
    return {
        "pb_pct": _percentile([_f(r[2]) for r in rows], cur_pb),
        "pe_pct": _percentile([_f(r[1]) for r in rows], cur_pe),
        "pb": cur_pb,
        "pe": cur_pe,
        "n": len(dates),
    }


def _prior_year_period(period_end: str) -> str:
    """「2026-06-30」→「2025-06-30」，用于同比对上年同期。"""
    try:
        y, m, d = str(period_end).split("-")
        return f"{int(y) - 1}-{m}-{d}"
    except Exception:
        return ""


def _yoy(cur: Optional[float], prior: Optional[float]) -> Optional[float]:
    """同比增速（%）。上年同期为 0/负时（扭亏等）无意义，返回 None。"""
    if cur is None or prior is None or prior <= 0:
        return None
    return (cur / prior - 1.0) * 100.0


def _annualize_month(period_end: str) -> float:
    """按报告期末月份把累计净利年化：3月→×4、6月→×2、9月→×4/3、12月→×1。"""
    try:
        m = int(str(period_end).split("-")[1])
    except Exception:
        return 1.0
    return 12.0 / max(1, m)


def financial_snapshot(code: str, market: str = "") -> Optional[dict]:
    """本地算基本面指标（读三张报表），缺数据返回 None 由调用方回退同花顺。

    返回 {roe, gross_margin, debt_ratio, revenue_growth, profit_growth, act_cash_flow}：
    - roe/gross_margin/debt_ratio/revenue_growth/profit_growth 为百分数（15.2 = 15.2%）。
    - roe 优先用最新年报（12-31）净利/期末净资产；无年报时用最新报告期累计净利按期末月年化。
    - 营收/净利同比 = 最新报告期 vs 去年同期同报告期。
    - act_cash_flow 为最新报告期经营现金流净额（元，累计值），可能为负。
    """
    db = _db_path()
    if not db.exists():
        return None
    try:
        import duckdb
    except Exception:
        return None
    ts = _thscode(code, market)
    try:
        with duckdb.connect(str(db), read_only=True) as con:
            income = con.execute(
                "SELECT period_end, operating_income, operating_costs, parent_holder_net_profit "
                "FROM fin_income WHERE thscode = ? ORDER BY period_end DESC",
                [ts],
            ).fetchall()
            bal = con.execute(
                "SELECT period_end, assets_total, total_debt, holder_equity_total "
                "FROM fin_balance WHERE thscode = ? ORDER BY period_end DESC",
                [ts],
            ).fetchall()
            cf = con.execute(
                "SELECT period_end, act_cash_flow_net FROM fin_cashflow "
                "WHERE thscode = ? ORDER BY period_end DESC",
                [ts],
            ).fetchall()
    except Exception:
        return None
    if not income:
        return None

    latest = income[0]
    cur_rev, cur_cost, cur_profit = _f(latest[1]), _f(latest[2]), _f(latest[3])

    out = {
        "roe": None, "gross_margin": None, "debt_ratio": None,
        "revenue_growth": None, "profit_growth": None, "act_cash_flow": None,
    }

    # 毛利率（最新报告期累计）：(营收-成本)/营收
    if cur_rev is not None and cur_rev > 0 and cur_cost is not None:
        out["gross_margin"] = (cur_rev - cur_cost) / cur_rev * 100.0

    # 资产负债率（最新报告期）：总负债/总资产
    if bal:
        assets, debt = _f(bal[0][1]), _f(bal[0][2])
        if assets is not None and assets > 0 and debt is not None:
            out["debt_ratio"] = debt / assets * 100.0

    # 经营现金流净额（最新报告期，元）
    if cf:
        out["act_cash_flow"] = _f(cf[0][1])

    # 同比增速：最新报告期 vs 去年同期同报告期
    prior = next((r for r in income if str(r[0]) == _prior_year_period(latest[0])), None)
    if prior is not None:
        out["revenue_growth"] = _yoy(cur_rev, _f(prior[1]))
        out["profit_growth"] = _yoy(cur_profit, _f(prior[3]))

    # ROE：优先最新年报净利/期末净资产；无年报则最新报告期累计净利按期末月年化
    inc_annual = next((r for r in income if str(r[0]).endswith("12-31")), None)
    bal_annual = next((r for r in bal if str(r[0]).endswith("12-31")), None)
    if inc_annual is not None and bal_annual is not None:
        profit_roe, equity_roe, factor = _f(inc_annual[3]), _f(bal_annual[3]), 1.0
    else:
        profit_roe = cur_profit
        equity_roe = _f(bal[0][3]) if bal else None
        factor = _annualize_month(str(latest[0]))
    if profit_roe is not None and equity_roe is not None and equity_roe > 0:
        out["roe"] = profit_roe * factor / equity_roe * 100.0

    return out
