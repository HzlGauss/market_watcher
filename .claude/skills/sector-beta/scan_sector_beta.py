#!/usr/bin/env python3
"""sector-beta 行业景气雷达：牛股密度 + 板块动量 + 主力资金流 三信号给行业打分排名。

自上而下选行业（行业 beta 是牛股主因，见调研结论）。三信号：
  1. 牛股密度 —— 行业近 250 交易日「前复权 MAX(high)/MIN(low) ≥ 2」翻倍股数 / 成分股数
  2. 板块动量 —— 行业近 10 日涨跌幅（东财板块资金流）
  3. 主力资金流 —— 行业近 10 日主力净流入（东财板块资金流）

每次运行把全行业排名落到 data/sector_beta.duckdb，供 --history 长期观测
（持续性 / 轮动 / 新晋主线）。

用法:
    py .claude/skills/sector-beta/scan_sector_beta.py [数量]      # 输出 top/bottom N（默认 15）
    py .claude/skills/sector-beta/scan_sector_beta.py --history [topK]  # 历史时间序列分析

数据源: 本地 duckdb（data/market.duckdb，前复权日K）+ 东财板块资金流。不依赖 MX_APIKEY。
"""
import os
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/sector-beta 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

import logging
logging.disable(logging.WARNING)

try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass

# ---------------------------------------------------------------- 解释器切换
# duckdb/marketdb 只装在 miniconda python（见 memory project-skill-python-env）。
# 系统 python 缺 duckdb 时自动切到 miniconda 重跑，避免静默算不出牛股密度。
_DUCKDB = None
try:
    import duckdb as _duckdb
    _DUCKDB = _duckdb
except ImportError:
    try:
        from app import kline_local
        kline_local.ensure_marketdb_interpreter()  # 若找到 miniconda 会 os.execv 替换进程
    except Exception:
        pass
    try:
        import duckdb as _duckdb
        _DUCKDB = _duckdb
    except ImportError:
        _DUCKDB = None

from app.data_fetcher import (
    fetch_sector_boards,
    fetch_sector_fund_flow_rank,
    fetch_stock_industry_map,
)

# 三信号权重（牛股密度是核心，动量+资金流做右侧确认）
W_DENSITY = 0.5
W_MOMENTUM = 0.3
W_FLOW = 0.2

# 成分股数门槛：剔除成分过少的微型板块（「镍」1 只这类噪声），避免窄板块密度失真
MIN_STOCKS = 15

# 上游商品动量窗口（交易日）
COMM_WIN_20 = 20
COMM_WIN_60 = 60

# 行业板块名 → (主力连续合约 thscode, 商品显示名)。商品价格领先对应 A 股板块，
# 作为「行业景气」的前瞻信号（同花顺期货接口，需 HITHINK_FINANCE_API_KEY）。
INDUSTRY_COMMODITY: dict[str, tuple[str, str]] = {
    # 能源
    "石油石化": ("SCZL.INE", "原油"),
    "煤炭": ("ZCZL.CZC", "动力煤"),
    "煤炭开采": ("ZCZL.CZC", "动力煤"),
    "动力煤": ("ZCZL.CZC", "动力煤"),
    # 金属
    "钢铁": ("RBZL.SHF", "螺纹钢"),
    "普钢": ("RBZL.SHF", "螺纹钢"),
    "金属制品": ("RBZL.SHF", "螺纹钢"),
    "铜": ("CUZL.SHF", "沪铜"),
    "铝": ("ALZL.SHF", "沪铝"),
    "工业金属": ("CUZL.SHF", "沪铜"),
    "有色金属": ("CUZL.SHF", "沪铜"),
    "小金属": ("ZNZL.SHF", "沪锌"),
    # 新能源金属
    "锂电池": ("LCZL.GFE", "碳酸锂"),
    "锂电专用设备": ("LCZL.GFE", "碳酸锂"),
    "电池化学品": ("LCZL.GFE", "碳酸锂"),
    # 化工
    "化学原料": ("MAZL.CZC", "甲醇"),
    "化学制品": ("MAZL.CZC", "甲醇"),
    "基础化工": ("MAZL.CZC", "甲醇"),
    "化学纤维": ("TAZL.CZC", "PTA"),
    "塑料": ("LZL.DCE", "塑料"),
    "改性塑料": ("LZL.DCE", "塑料"),
    "其他塑料制品": ("LZL.DCE", "塑料"),
    "橡胶": ("RUZL.SHF", "橡胶"),
    "其他橡胶制品": ("RUZL.SHF", "橡胶"),
    "农化制品": ("URZL.CZC", "尿素"),
    # 建材
    "玻璃玻纤": ("FGZL.CZC", "玻璃"),
    "非金属材料Ⅱ": ("SAZL.CZC", "纯碱"),
    "建筑材料": ("RBZL.SHF", "螺纹钢"),
    # 轻工/农业/食品
    "造纸": ("SPZL.SHF", "纸浆"),
    "纺织制造": ("CFZL.CZC", "棉花"),
    "纺织服饰": ("CFZL.CZC", "棉花"),
    "养殖业": ("LHZL.DCE", "生猪"),
    "饲料": ("MZL.DCE", "豆粕"),
    "农产品加工": ("MZL.DCE", "豆粕"),
    "食品加工": ("SRZL.CZC", "白糖"),
    "食品饮料": ("SRZL.CZC", "白糖"),
}

# 翻倍基准窗口（交易日）
BULL_WINDOW = 250
# 翻倍阈值
BULL_RATIO = 2.0
# 近 250 交易日对应的大致日历天数上限（250 交易日 ≈ 1 年 ≈ 370 自然日）
BULL_CAL_DAYS = 370

_SECTOR_BETA_DB = _ROOT / "data" / "sector_beta.duckdb"


def _market_db_path() -> Path:
    p = os.environ.get("MARKETDB_DB_PATH")
    return Path(p).expanduser() if p else _ROOT / "data" / "market.duckdb"


def _fetch_bull_tickers() -> tuple[list[str], str]:
    """本地 duckdb 全市场扫翻倍牛股 → (6 位代码列表, 数据最新日期)。缺库/无 duckdb 返回 ([], '')。"""
    if _DUCKDB is None:
        return [], ""
    db = _market_db_path()
    if not db.exists():
        return [], ""
    sql = """
    WITH recent AS (
      SELECT thscode, date, high, low
      FROM v_daily_qfq
      WHERE date >= (SELECT MAX(date) - INTERVAL '{cal} DAY' FROM v_daily_qfq)
    ),
    ranked AS (
      SELECT thscode, high, low,
             ROW_NUMBER() OVER (PARTITION BY thscode ORDER BY date DESC) AS rn
      FROM recent
    ),
    bull AS (
      SELECT thscode, MAX(high) AS hi, MIN(low) AS lo
      FROM ranked WHERE rn <= {win}
      GROUP BY thscode
    )
    SELECT s.ticker
    FROM bull b JOIN v_symbol s ON s.thscode = b.thscode
    WHERE s.asset_type = 'a-share' AND b.lo > 0 AND b.hi / b.lo >= {ratio}
    """.format(cal=BULL_CAL_DAYS, win=BULL_WINDOW, ratio=BULL_RATIO)
    try:
        con = _DUCKDB.connect(str(db), read_only=True)
        try:
            tickers = [r[0] for r in con.execute(sql).fetchall()]
            max_date = con.execute("SELECT MAX(date) FROM v_daily_qfq").fetchone()[0]
            return tickers, str(max_date)
        finally:
            con.close()
    except Exception:
        return [], ""


def _pct_ranks(values: list) -> list[float]:
    """值 → 0~1 分位（降序：最高值分位 1.0，最低 0.0，None 当 0）。"""
    n = len(values)
    cleaned = [(0.0 if v is None else float(v)) for v in values]
    order = sorted(range(n), key=lambda i: cleaned[i])
    ranks = [0.0] * n
    for pos, i in enumerate(order):
        ranks[i] = pos / (n - 1) if n > 1 else 0.5
    return ranks


def _fetch_commodity_momentum() -> dict[str, dict]:
    """上游商品动量 → {thscode: {name, close, mom_20, mom_60}}。缺 key / 失败返回空 dict。"""
    if not os.environ.get("HITHINK_FINANCE_API_KEY"):
        return {}
    try:
        from app import hithink
    except Exception:
        return {}
    thscodes = sorted({ts for ts, _ in INDUSTRY_COMMODITY.values()})
    name_by_ts = {ts: name for ts, name in INDUSTRY_COMMODITY.values()}
    out: dict[str, dict] = {}
    for ts in thscodes:
        try:
            bars = hithink.fetch_futures_daily(ts, days=COMM_WIN_60 + 1)
        except Exception:
            bars = []
        if len(bars) < COMM_WIN_20 + 1:
            continue
        closes = [b["close"] for b in bars]
        def mom(win: int):
            if len(closes) <= win or not closes[-1]:
                return None
            base = closes[-win - 1]
            return round((closes[-1] / base - 1) * 100, 2) if base else None
        out[ts] = {
            "name": name_by_ts.get(ts, ts),
            "close": closes[-1],
            "mom_20": mom(COMM_WIN_20),
            "mom_60": mom(COMM_WIN_60),
        }
    return out


def _collect() -> tuple[list[dict], str, dict[str, dict]]:
    """汇总三信号 → 每行业一行 {name, code, stock_count, bull_count, ...}，返回 (rows, trade_date, comm_mom)。"""
    bull_tickers, max_date = _fetch_bull_tickers()

    if not bull_tickers:
        print("  ⚠️ 本地库无翻倍牛股数据（data/market.duckdb 缺失或未同步）。")
        print("     → 请先跑 /marketdb-sync 或 py tools/marketdb_local.py bootstrap 建库。")
        return [], max_date, {}

    # 牛股 → 行业（东财 f100，带日级缓存）
    industry_map = fetch_stock_industry_map(bull_tickers) or {}

    # 板块列表 + 成分数（分母）；剔除成分过少的微型板块（噪声）
    boards = [b for b in (fetch_sector_boards() or []) if (b.stock_count or 0) >= MIN_STOCKS]
    if not boards:
        print("  ⚠️ 东财行业板块接口不可达（push2delay/push2 被限流或断连）。")
        print("     → 稍后重试，或稍等几分钟再跑。牛股密度已算好，只差板块动量/资金流。")
        return [], max_date, _fetch_commodity_momentum()
    # 10 日动量 + 主力资金流
    flows = fetch_sector_fund_flow_rank("10日", "行业资金流") or []

    flow_by_name = {f.name: f for f in flows if f.name}
    flow_by_code = {f.code: f for f in flows if f.code}

    # 牛股按行业聚合
    bull_by_industry: dict[str, int] = {}
    for code in bull_tickers:
        ind = industry_map.get(code)
        if ind:
            bull_by_industry[ind] = bull_by_industry.get(ind, 0) + 1

    rows: list[dict] = []
    for b in boards:
        name = b.name
        flow = flow_by_name.get(name) or flow_by_code.get(b.code)
        bull_count = bull_by_industry.get(name, 0)
        stock_count = max(int(b.stock_count or 0), 1)
        rows.append({
            "sector_code": b.code or (flow.code if flow else ""),
            "sector_name": name,
            "stock_count": stock_count,
            "bull_count": bull_count,
            "bull_density": round(bull_count / stock_count, 4),
            "change_pct_10d": flow.change_pct if flow else None,
            "main_net_10d": flow.main_net if flow else None,
            "main_pct_10d": flow.main_pct if flow else None,
            "top_stock": flow.top_stock if flow else "",
        })

    # 上游商品动量（领先信号，仅映射行业，不影响综合分）
    comm_mom = _fetch_commodity_momentum()
    for r in rows:
        ts, cname = INDUSTRY_COMMODITY.get(r["sector_name"], (None, None))
        if ts and ts in comm_mom:
            r["commodity"] = cname
            r["commodity_mom"] = comm_mom[ts]["mom_60"]
        else:
            r["commodity"] = ""
            r["commodity_mom"] = None

    # 综合分 = 加权分位
    d_pct = _pct_ranks([r["bull_density"] for r in rows])
    m_pct = _pct_ranks([r["change_pct_10d"] for r in rows])
    f_pct = _pct_ranks([r["main_net_10d"] for r in rows])
    for i, r in enumerate(rows):
        r["score"] = round(
            W_DENSITY * d_pct[i] + W_MOMENTUM * m_pct[i] + W_FLOW * f_pct[i], 4
        )

    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows, max_date, comm_mom


def _write_db(rows: list[dict], trade_date: str, comm_mom: dict[str, dict] | None = None) -> bool:
    """落库 sector_rank + commodity_rank（同日 DELETE+INSERT 幂等 upsert），失败静默返回 False。"""
    if not rows or not trade_date or _DUCKDB is None:
        return False
    try:
        con = _DUCKDB.connect(str(_SECTOR_BETA_DB))
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sector_rank (
                  trade_date TEXT, sector_code TEXT, sector_name TEXT,
                  stock_count INTEGER, bull_count INTEGER, bull_density DOUBLE,
                  change_pct_10d DOUBLE, main_net_10d DOUBLE, main_pct_10d DOUBLE,
                  top_stock TEXT, commodity TEXT, commodity_mom DOUBLE,
                  score DOUBLE, rank INTEGER
                )
                """
            )
            # 兼容旧表（无 commodity 列）
            con.execute("ALTER TABLE sector_rank ADD COLUMN IF NOT EXISTS commodity TEXT")
            con.execute("ALTER TABLE sector_rank ADD COLUMN IF NOT EXISTS commodity_mom DOUBLE")
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sector_rank "
                "ON sector_rank(trade_date, sector_name)"
            )
            con.execute("DELETE FROM sector_rank WHERE trade_date = ?", [trade_date])
            con.executemany(
                "INSERT INTO sector_rank "
                "(trade_date, sector_code, sector_name, stock_count, bull_count, bull_density, "
                " change_pct_10d, main_net_10d, main_pct_10d, top_stock, commodity, commodity_mom, "
                " score, rank) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        trade_date, r["sector_code"], r["sector_name"], r["stock_count"],
                        r["bull_count"], r["bull_density"], r["change_pct_10d"],
                        r["main_net_10d"], r["main_pct_10d"], r["top_stock"],
                        r.get("commodity") or "", r.get("commodity_mom"),
                        r["score"], r["rank"],
                    )
                    for r in rows
                ],
            )

            # 商品动量单独落表（领先信号的时间序列）
            if comm_mom:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS commodity_rank (
                      trade_date TEXT, thscode TEXT, name TEXT,
                      close DOUBLE, mom_20 DOUBLE, mom_60 DOUBLE
                    )
                    """
                )
                con.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_commodity_rank "
                    "ON commodity_rank(trade_date, thscode)"
                )
                con.execute("DELETE FROM commodity_rank WHERE trade_date = ?", [trade_date])
                con.executemany(
                    "INSERT INTO commodity_rank (trade_date, thscode, name, close, mom_20, mom_60) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (trade_date, ts, v["name"], v["close"], v["mom_20"], v["mom_60"])
                        for ts, v in comm_mom.items()
                    ],
                )
        finally:
            con.close()
        return True
    except Exception:
        return False


def _fmt_yi(v) -> str:
    """元 → 亿元字符串（None 显示 --）。"""
    if v is None:
        return "--"
    return f"{v / 1e8:+.1f}"


def _print_scan(rows: list[dict], trade_date: str, limit: int, comm_mom: dict[str, dict] | None = None) -> None:
    print("=" * 78)
    print(f"行业景气雷达（sector-beta）· 数据日 {trade_date or '--'}")
    print("=" * 78)
    print(f"翻倍基准：近 {BULL_WINDOW} 交易日 前复权 MAX(high)/MIN(low) ≥ {BULL_RATIO}")
    print(f"综合分 = {W_DENSITY}·牛股密度分位 + {W_MOMENTUM}·动量分位 + {W_FLOW}·资金流分位")
    print()

    if not rows:
        print("  ⚠️ 无行业数据（东财板块接口不可达）")
        return

    def _row(r):
        return (
            f"{r['rank']:>3}  {r['sector_name']:<10} 成分{r['stock_count']:>3}  "
            f"牛股{r['bull_count']:>3}  密度{r['bull_density']*100:>5.1f}%  "
            f"10日涨跌{r['change_pct_10d'] if r['change_pct_10d'] is not None else 0.0:>+6.1f}%  "
            f"主力{_fmt_yi(r['main_net_10d']):>8}亿  分{r['score']:.3f}"
        )

    print(f"  ── 🏆 主线行业 TOP {min(limit, len(rows))} ──")
    for r in rows[:limit]:
        print("  " + _row(r))
        extra = []
        if r["top_stock"]:
            extra.append(f"主力最大股 {r['top_stock']}")
        if r.get("commodity_mom") is not None:
            extra.append(f"上游 {r['commodity']} 60日{r['commodity_mom']:+.1f}%")
        if extra:
            print("        └─ " + "；".join(extra))

    print()
    print(f"  ── 🧊 落后行业 BOTTOM {min(limit, len(rows))} ──")
    for r in rows[-limit:][::-1]:
        print("  " + _row(r))

    if comm_mom:
        print()
        print("  ── 🔥 上游商品景气（领先信号，60日动量降序）──")
        for ts, v in sorted(comm_mom.items(), key=lambda kv: -(kv[1].get("mom_60") or -1e9)):
            mom20 = v.get("mom_20")
            mom60 = v.get("mom_60")
            m20 = f"{mom20:+.1f}%" if mom20 is not None else "--"
            m60 = f"{mom60:+.1f}%" if mom60 is not None else "--"
            print(f"    {v['name']:<6} 20日{m20:>8}  60日{m60:>8}")
        print("        └─ 商品价格领先 A 股对应板块，60日上行=成本推动/需求回暖的前瞻确认")

    print()
    print("  说明:")
    print("    - 牛股密度 = 行业近 250 日翻倍股数 / 成分股数，是「行业 beta」强度的核心")
    print("    - 主线 = 密度高 + 近期有动量 + 资金持续流入；三者共振才有持续性")
    print(f"    - 已落库 {_SECTOR_BETA_DB.name}（同日重跑覆盖，--history 看历史演变）")


def _print_history(top_k: int) -> None:
    if _DUCKDB is None or not _SECTOR_BETA_DB.exists():
        print("  ⚠️ 无历史数据（data/sector_beta.duckdb 尚未生成，先跑一次扫描）")
        return
    try:
        con = _DUCKDB.connect(str(_SECTOR_BETA_DB), read_only=True)
        try:
            rows = con.execute(
                "SELECT trade_date, sector_name, score, rank, bull_density "
                "FROM sector_rank ORDER BY trade_date, rank"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        print("  ⚠️ 历史表读取失败")
        return

    if not rows:
        print("  ⚠️ 无历史数据")
        return

    dates = sorted({r[0] for r in rows})
    # 每期 top-K 行业集合
    top_by_date: dict[str, set] = {}
    for r in rows:
        d, name, score, rank, dens = r
        if rank is not None and rank <= top_k:
            top_by_date.setdefault(d, set()).add(name)

    latest = dates[-1]
    latest_top = top_by_date.get(latest, set())
    latest_rank = {r[1]: r[3] for r in rows if r[0] == latest}
    latest_score = {r[1]: r[2] for r in rows if r[0] == latest}

    print("=" * 78)
    print(f"sector-beta 时间序列（{len(dates)} 期：{dates[0]} ~ {latest}）")
    print("=" * 78)

    if len(dates) < 2:
        print("  ⚠️ 仅 1 期历史，需多运行几日累积后才能看持续性/轮动。")
        return

    # 连续上榜（最近 N 期仍在 top-K）
    streak: dict[str, int] = {}
    for name in latest_top:
        s = 0
        for d in reversed(dates):
            if name in top_by_date.get(d, set()):
                s += 1
            else:
                break
        streak[name] = s

    # 新晋 top（本期首次进入 top-K）
    prev_union: set = set()
    for d in dates[:-1]:
        prev_union |= top_by_date.get(d, set())
    new_entrants = latest_top - prev_union

    # 排名变化（本期 vs 上一期）
    prev_date = dates[-2]
    prev_rank = {r[1]: r[3] for r in rows if r[0] == prev_date}
    rank_delta = {name: (prev_rank.get(name, 0) - latest_rank.get(name, 0)) for name in latest_top}

    print(f"\n  ── 连续上榜（近 {len(dates)} 期持续留在 TOP {top_k}，越长越强）──")
    for name, s in sorted(streak.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<10} 连续 {s} 期上榜 ｜ 现排名 {latest_rank.get(name)} ｜ 分 {latest_score.get(name):.3f}")

    if new_entrants:
        print(f"\n  ── 🆕 新晋 TOP {top_k}（轮动/新主线启动信号）──")
        for name in sorted(new_entrants):
            print(f"    {name:<10} 现排名 {latest_rank.get(name)} ｜ 分 {latest_score.get(name):.3f}")

    if rank_delta:
        print(f"\n  ── 排名变化（本期 vs 上期，正=上升）──")
        for name, delta in sorted(rank_delta.items(), key=lambda kv: -kv[1]):
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
            print(f"    {name:<10} {arrow}{abs(delta):>3} 名 ｜ 现排名 {latest_rank.get(name)}")

    print()
    print("  说明: 持续性 = 真主线（非一日游）；新晋 = 轮动切换信号；排名上升 = 景气强化")


def main() -> int:
    argv = sys.argv[1:]

    history = "--history" in argv
    limit = 15
    for a in argv:
        if a.strip().isdigit():
            limit = max(1, int(a.strip()))

    if history:
        _print_history(limit)
        return 0

    rows, trade_date, comm_mom = _collect()
    if rows:
        _print_scan(rows, trade_date, limit, comm_mom)
        _write_db(rows, trade_date, comm_mom)
    return 0


if __name__ == "__main__":
    sys.exit(main())
