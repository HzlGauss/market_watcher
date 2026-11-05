#!/usr/bin/env python
"""本地数据统一查询入口 —— 查单个标的「本地已落盘」的全部数据。

把分散在 4 个 DuckDB 库里的数据（基本面/估值分位/日K/资金流/5分钟K）收拢成一条命令，
供 Claude 和用户一键查询，避免「本地有数据却去外网搜」。

用法：
    py tools/local_query.py <代码> [名称] [日K天数]

示例：
    py tools/local_query.py 300661 圣邦股份
    py tools/local_query.py 688012 中微公司 20

覆盖的本地数据源（优先级：本地 > 远端兜底）：
    - 基本面（ROE/毛利率/负债率/营收同比/净利同比/经营现金流）  ← data/market.duckdb 三张报表
    - 估值历史分位（PB/PE 3年/5年/上市以来 + 当前值）            ← data/market.duckdb valuation_daily
    - 日K（前复权，最近 N 日）                                  ← data/market.duckdb raw_kline_daily
    - 当日资金流（五档净流入时序，最近快照 + 当日累计）          ← data/fund_flow_intraday.duckdb
    - 5分钟K（保留 15 自然日）                                  ← data/min5.duckdb

只读、零网络、零 key 依赖。duckdb 缺失时提示并退出。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 强制 UTF-8 输出（对齐各 skill 脚本，避免 Windows 控制台中文乱码）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

try:
    import duckdb
except ImportError:
    print("⚠️ 当前解释器无 duckdb；请用 miniconda python（`py`）运行。")
    sys.exit(1)

_DB = _ROOT / "data" / "market.duckdb"
_FF = _ROOT / "data" / "fund_flow_intraday.duckdb"
_M5 = _ROOT / "data" / "min5.duckdb"


def _norm(code: str) -> tuple[str, str]:
    """输入代码 → (裸6位代码, market SH/SZ/BJ)。带后缀原样拆。"""
    c = str(code).strip()
    if "." in c:
        bare, mkt = c.split(".", 1)
        return bare, mkt.upper()
    if c[:2] in ("60", "68", "51", "56", "58", "90", "11", "50"):
        return c, "SH"
    if c[:1] in ("8", "4", "9") and c.startswith(("8", "4", "92")):
        return c, "BJ"
    return c, "SZ"


def _thscode(bare: str, mkt: str) -> str:
    return f"{bare}.{mkt}"


def _f(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _fmt_pct(v, digits=1) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"


def _fmt_num(v, digits=2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def _section(title: str) -> None:
    print(f"\n[{title}]")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    bare, mkt = _norm(sys.argv[1])
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    ts = _thscode(bare, mkt)

    print(f"本地数据快照: {bare} {name} ({ts})")

    # 1) 基本面（复用 app.fundamental_local）
    try:
        from app.fundamental_local import financial_snapshot, valuation_percentile_multi
        fs = financial_snapshot(bare, mkt)
        if fs:
            _section("基本面 (fin_income/balance/cashflow)")
            print(f"  ROE {_fmt_pct(fs.get('roe'))}  毛利率 {_fmt_pct(fs.get('gross_margin'))}  负债率 {_fmt_pct(fs.get('debt_ratio'))}")
            print(f"  营收同比 {_fmt_pct(fs.get('revenue_growth'))}  净利同比 {_fmt_pct(fs.get('profit_growth'))}")
            acf = fs.get("act_cash_flow")
            if acf is not None:
                print(f"  经营现金流净额 {'+' if acf >= 0 else ''}{acf / 1e8:.2f} 亿")
        else:
            _section("基本面")
            print("  （本地无该标的三张报表，可 `sync-financials` 补）")

        vm = valuation_percentile_multi(bare, mkt)
        if vm:
            _section("估值历史分位 (valuation_daily)")
            print(f"  当前 PB {_fmt_num(vm.get('pb'))} / PE {_fmt_num(vm.get('pe'))}  (历史样本 {vm.get('n')} 日)")
            for label in ("3年", "5年", "上市以来"):
                pb = vm.get("pb_pct", {}).get(label)
                pe = vm.get("pe_pct", {}).get(label)
                print(f"    {label:<4} PB分位 {_fmt_pct(pb)}   PE分位 {_fmt_pct(pe)}")
        else:
            _section("估值历史分位")
            print("  （本地估值历史不足 20 日或无记录）")
    except Exception as e:
        print(f"  （基本面/估值查询失败: {e}）")

    # 2) 日K（前复权）
    if _DB.exists():
        _section(f"日K 最近 {days} 日（前复权, raw_kline_daily）")
        try:
            with duckdb.connect(str(_DB), read_only=True) as con:
                rows = con.execute(
                    "SELECT k.date, k.close*COALESCE(a.forward_factor,1.0), "
                    "k.volume FROM raw_kline_daily k "
                    "LEFT JOIN calc_adjust_factor_daily a ON a.thscode=k.thscode AND a.date=k.date "
                    "WHERE k.thscode=? ORDER BY k.date DESC LIMIT ?",
                    [ts, days],
                ).fetchall()
            for d, c, v in reversed(rows):
                print(f"    {d}  收 {_fmt_num(c)}  量 {_fmt_num(v, 0)}")
        except Exception as e:
            print(f"  （日K查询失败: {e}）")

    # 3) 当日资金流
    if _FF.exists():
        _section("当日资金流 (fund_flow_intraday)")
        try:
            with duckdb.connect(str(_FF), read_only=True) as con:
                row = con.execute(
                    "SELECT ts, main_net, super_large_net, large_net, medium_net, small_net, "
                    "source, change_pct, turnover_rate, volume_ratio FROM fund_flow_intraday "
                    "WHERE code=? ORDER BY ts DESC LIMIT 1", [bare],
                ).fetchone()
                seq = con.execute(
                    "SELECT ts, main_net FROM fund_flow_intraday WHERE code=? "
                    "ORDER BY ts", [bare],
                ).fetchall()
            if row:
                ts_, main, sl, lg, md, sm, src, chg, to, vr = row
                print(f"  最新 {ts_}  ({src})  涨跌 {_fmt_pct(chg)}  换手 {_fmt_pct(to)}  量比 {_fmt_num(vr)}")
                print(f"  主力 {_fmt_num((main or 0)/1e4, 0)}万  超大单 {_fmt_num((sl or 0)/1e4, 0)}  大单 {_fmt_num((lg or 0)/1e4, 0)}  中单 {_fmt_num((md or 0)/1e4, 0)}  小单 {_fmt_num((sm or 0)/1e4, 0)}")
                if seq:
                    first = _f(seq[0][1]); last = _f(seq[-1][1])
                    if first is not None and last is not None:
                        print(f"  当日时序 {len(seq)} 条：主力净流入 {first/1e4:.0f}万 → {last/1e4:.0f}万")
            else:
                print("  （本地无该标的当日资金流；盯盘线程 300s 落一条）")
        except Exception as e:
            print(f"  （资金流查询失败: {e}）")

    # 4) 5分钟K
    if _M5.exists():
        _section("5分钟K (min5_kline)")
        try:
            with duckdb.connect(str(_M5), read_only=True) as con:
                rows = con.execute(
                    "SELECT ts, close FROM min5_kline WHERE code=? AND market=? "
                    "ORDER BY ts DESC LIMIT 8", [bare, mkt],
                ).fetchall()
            if rows:
                for t, c in reversed(rows):
                    print(f"    {t}  收 {_fmt_num(c)}")
            else:
                print("  （本地无该标的 5 分钟K）")
        except Exception as e:
            print(f"  （5分钟K查询失败: {e}）")

    print("\n（以上全部来自本地 DuckDB，零网络零 key；本地没有的字段会显式标注「本地无」）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
