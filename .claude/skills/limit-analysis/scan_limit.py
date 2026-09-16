#!/usr/bin/env python3
"""涨跌停分析：涨停梯队 + 炸板率 + 封单额 + 题材归类 + 跌停池 → 短线情绪周期数据包。

核心思路：
    1. 拉最近交易日涨停池（东财 stock_zt_pool_em）→ 连板数/封板资金/炸板次数/涨停统计/所属行业
    2. 拉炸板池（stock_zt_pool_zbgc_em）→ 曾涨停后炸板 → 算炸板率
    3. 拉跌停池（stock_zt_pool_dtgc_em）→ 连续跌停/开板次数/所属行业
    4. 聚合：涨停梯队（连板高度分层）/ 炸板率 / 封单额 TOP / 题材归类 / 情绪周期

本脚本只做「取数 + 聚合」，输出结构化数据包；「情绪周期 / 主线题材」结论由 AI 依据 SKILL.md 框架生成。

用法:
    py .claude/skills/limit-analysis/scan_limit.py [日期YYYYMMDD]

参数:
    日期   可选，指定交易日（默认最近交易日）

数据源:
    - 涨停池/炸板池/跌停池: 同花顺官方金融数据（HITHINK_FINANCE_API_KEY，无 key/失败时回退东财 akshare）
    不依赖 MX_APIKEY。涨跌停判定由同花顺/东财精确口径（10%/20%/ST 5%）给出，非 9.9% 近似。
"""
import os
import re
import sys
import time
import datetime
from collections import Counter
from pathlib import Path

# 强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
os.environ.setdefault("TQDM_DISABLE", "1")

# 定位项目根目录（skills/limit-analysis 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 抑制 app 模块 WARNING 噪音
import logging
logging.disable(logging.WARNING)

# 加载 .env（同花顺 API Key 等）
try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass


def _num(v):
    """解析为 float，None/空/'-' 返回 None。"""
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v, default=0):
    """解析为 int。"""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default


def _fmt_yi(v) -> str:
    """元 -> 亿元字符串（None 显示 --）。"""
    return f"{v / 1e8:.2f}亿" if v is not None else "  --"


def _short(s, n=12) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _latest_trade_date() -> str:
    """返回最近交易日 YYYYMMDD（同花顺日历优先，AKShare 兜底）。"""
    try:
        from app import hithink
        days = hithink.fetch_trade_days()
        if days:
            today = datetime.date.today().strftime("%Y%m%d")
            past = [d for d in days if d <= today]
            return (past or days)[-1]
    except Exception:
        pass
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        dates = [d for d in cal["trade_date"] if d < datetime.date.today()]
        return sorted(dates)[-1].strftime("%Y%m%d")
    except Exception:
        return datetime.date.today().strftime("%Y%m%d")


# ---------------------------------------------------------------- 数据拉取

def _fetch_pool(fn, date: str, name: str):
    """拉东财涨/跌/炸板池，返回 DataFrame 或 None。"""
    try:
        import akshare as ak
    except ImportError:
        print(f"❌ 未安装 akshare，无法拉取{name}（pip install akshare）")
        return None
    try:
        df = fn(date=date)
    except Exception:
        df = None
    if df is None or getattr(df, "empty", True):
        return None
    return df


def _df_from_records(records, columns):
    """list[dict] → pandas DataFrame（无 pandas 时返回 None，交由 AKShare 兜底）。"""
    if not records:
        return None
    try:
        import pandas as pd
    except ImportError:
        return None
    return pd.DataFrame(records, columns=columns)


def _fetch_hithink(kind: str, date: str):
    """同花顺涨/跌/炸板池 → DataFrame（东财兼容中文列名）。失败/无 key 返回 None。"""
    try:
        from app import hithink
        items = hithink.fetch_limit_pool(kind, date)
    except Exception:
        return None
    if not items:
        return None

    if kind == "limit_up":
        records = [{
            "代码": str(it.get("ticker", "")).zfill(6),
            "名称": str(it.get("name", "")),
            "连板数": _int(it.get("continue_day_cnt")),
            "涨停统计": str(it.get("continue_day_text", "")),
            "封板资金": _num(it.get("seal_money")),
            "所属行业": str(it.get("limit_up_reason") or ""),
        } for it in items]
        return _df_from_records(records, ["代码", "名称", "连板数", "涨停统计", "封板资金", "所属行业"])

    if kind == "limit_break":
        # 炸板池仅用于炸板率条数；THS 无行业列
        records = [{
            "代码": str(it.get("ticker", "")).zfill(6),
            "名称": str(it.get("name", "")),
            "所属行业": "",
        } for it in items]
        return _df_from_records(records, ["代码", "名称", "所属行业"])

    if kind == "limit_down":
        # THS 无 连续跌停/封单资金/所属行业 三列，用可得的 涨跌幅/换手率 替代
        records = [{
            "代码": str(it.get("ticker", "")).zfill(6),
            "名称": str(it.get("name", "")),
            "涨跌幅": _num(it.get("price_change_ratio_pct")),
            "换手率": _num(it.get("turnover_ratio_pct")),
            "所属行业": "",
        } for it in items]
        return _df_from_records(records, ["代码", "名称", "涨跌幅", "换手率", "所属行业"])

    return None


# ---------------------------------------------------------------- 本地库兜底

def _board_limit_pct(code: str, name: str) -> float:
    """涨跌停幅度（%）：ST 5 / 主板 10 / 创业科创 20 / 北交所 30。"""
    if "ST" in (name or "").upper():
        return 5.0
    c = (code or "").zfill(6)
    if c.startswith(("68", "30")):
        return 20.0
    if c.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def _detect_limit_ups_local(date: str):
    """本地库兜底：用未复权涨幅 + 涨跌停制度重建涨停池（含连板数）。

    供同花顺/东财涨停池均失败时使用。封板资金/所属行业本地无数据（置 None/空），
    炸板池/跌停池无法重建。返回 pandas DataFrame（东财兼容列名），失败返回 None。
    """
    try:
        from tools.marketdb_local import db_max_date, market_db_path
    except Exception:
        return None
    maxd = db_max_date()
    if not maxd:
        return None
    # 目标日 = min(请求日, 本地最大日)；本地库落后于请求日时按库内最新
    target = maxd  # YYYY-MM-DD
    if date:
        req = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        if req < maxd:
            target = req
    db_file = market_db_path()
    if not db_file.exists():
        return None
    start = (datetime.date.fromisoformat(target) - datetime.timedelta(days=60)).isoformat()
    try:
        import duckdb
        con = duckdb.connect(str(db_file), read_only=True)
    except Exception:
        return None
    try:
        rows = con.execute("""
            SELECT k.thscode, s.name, k.date, k.close
            FROM raw_kline_daily k
            LEFT JOIN dim_symbol s ON s.thscode = k.thscode
            WHERE k.date >= ? AND k.date <= ?
            ORDER BY k.thscode, k.date
        """, [start, target]).fetchall()
    except Exception:
        con.close()
        return None
    con.close()

    from collections import defaultdict
    by_code = defaultdict(list)
    names = {}
    for thscode, name, d, close in rows:
        by_code[thscode].append((d, close))
        names[thscode] = name or ""

    target_d = datetime.date.fromisoformat(target)
    records = []
    for thscode, seq in by_code.items():
        code6 = str(thscode).split(".")[0].zfill(6)
        name = names.get(thscode, "")
        limit = _board_limit_pct(code6, name)
        sub = [(d, c) for (d, c) in seq if d <= target_d]
        if not sub:
            continue
        last_date, last_close = sub[-1]
        if last_date != target_d or last_close is None or last_close <= 0:
            continue
        # 从目标日向前数连续涨停（一字板 open=high=low=close 同样命中，仅用 close 判定）
        consec = 0
        for i in range(len(sub) - 1, 0, -1):
            c = sub[i][1]
            prev_close = sub[i - 1][1]
            if prev_close is None or prev_close <= 0 or c is None:
                break
            limit_price = round(prev_close * (1.0 + limit / 100.0), 2)
            if c >= limit_price - 0.001:
                consec += 1
            else:
                break
        if consec >= 1:
            records.append({
                "代码": code6,
                "名称": name,
                "连板数": consec,
                "涨停统计": f"{consec}/{consec}",
                "封板资金": None,
                "所属行业": "",
            })
    return _df_from_records(records, ["代码", "名称", "连板数", "涨停统计", "封板资金", "所属行业"])


# ---------------------------------------------------------------- 聚合

def _tier_stats(zt_df):
    """涨停梯队：按连板数分层，返回 [(层级名, 家数)] 与 高度板列表。"""
    if zt_df is None:
        return [], []
    consec = [_int(r.get("连板数")) for _, r in zt_df.iterrows()]
    counter = Counter(consec)
    # 首板 / 2板 / 3板 / 4板+ 分层
    tiers = [
        ("首板", counter.get(1, 0)),
        ("2板", counter.get(2, 0)),
        ("3板", counter.get(3, 0)),
        ("4板+", sum(v for k, v in counter.items() if k >= 4)),
    ]
    # 高度板（连板数 >= 2 的个股，按连板数降序）
    high = []
    for _, r in zt_df.iterrows():
        c = _int(r.get("连板数"))
        if c >= 2:
            high.append({
                "code": str(r.get("代码", "")).zfill(6),
                "name": str(r.get("名称", "")),
                "consec": c,
                "stat": str(r.get("涨停统计", "")),
                "seal": _num(r.get("封板资金")),
                "industry": str(r.get("所属行业", "")),
            })
    high.sort(key=lambda x: -x["consec"])
    return tiers, high


def _seal_top(zt_df, n=10):
    """封单额 TOP：涨停池按封板资金降序。"""
    if zt_df is None:
        return []
    rows = []
    for _, r in zt_df.iterrows():
        seal = _num(r.get("封板资金"))
        rows.append({
            "code": str(r.get("代码", "")).zfill(6),
            "name": str(r.get("名称", "")),
            "consec": _int(r.get("连板数")),
            "seal": seal,
            "industry": str(r.get("所属行业", "")),
        })
    rows.sort(key=lambda x: -(x["seal"] or 0))
    return rows[:n]


def _industry_agg(df, top_n=12):
    """题材归类：按所属行业/涨停原因聚合计数。

    同花顺源的「所属行业」实为 '+' 分隔的涨停原因，按 token 拆开计数更贴近题材；
    东财源是单一行业字符串，无 '+' 时按整串计数。
    """
    if df is None:
        return []
    cnt = Counter()
    for _, r in df.iterrows():
        raw = str(r.get("所属行业", "") or "").strip()
        if not raw or raw.lower() == "none":
            continue
        for token in re.split(r"[+＋/、]", raw):
            token = token.strip()
            if token and token.lower() != "none":
                cnt[token] += 1
    return cnt.most_common(top_n)


# ---------------------------------------------------------------- 输出

def _print_tiers(tiers, high):
    print()
    print("=" * 72)
    print("【1. 涨停梯队】")
    print("=" * 72)
    if not tiers or sum(t for _, t in tiers) == 0:
        print("  ⚠️ 无涨停数据")
        return
    total = sum(t for _, t in tiers)
    print(f"  涨停 {total} 只   |   梯队: " + " / ".join(f"{k}{v}" for k, v in tiers))
    max_consec = max((h["consec"] for h in high), default=1)
    print(f"  最高连板（空间板）: {max_consec} 板")
    if high:
        print("  ── 连板股（≥2板，按高度降序）──")
        header = f"  {'代码':<8}{'名称':<10}{'连板':>4}{'涨停统计':>8}{'封单额':>10}{'题材':<12}"
        print(header)
        print("  " + "-" * 72)
        for h in high:
            print(f"  {h['code']:<8}{_short(h['name'], 9):<10}{h['consec']:>4}"
                  f"{h['stat']:>8}{_fmt_yi(h['seal']):>10}{_short(h['industry'], 11):<12}")


def _print_break_rate(zt_n, zb_n, zb_known=True):
    print()
    print("=" * 72)
    print("【2. 炸板率】")
    print("=" * 72)
    if not zb_known:
        print("  ⚠️ 炸板池数据缺失（本地兜底/接口不可达），炸板率不可算")
        return
    denom = zt_n + zb_n
    if denom == 0:
        print("  ⚠️ 无涨跌停数据")
        return
    rate = zb_n / denom * 100
    print(f"  涨停 {zt_n} / 炸板 {zb_n}  →  炸板率 {rate:.1f}%")
    if rate <= 20:
        verdict = "封板坚决，情绪偏强"
    elif rate <= 35:
        verdict = "炸板一般，情绪中性"
    else:
        verdict = "炸板率高，分歧加剧/情绪偏弱"
    print(f"  情绪判定: {verdict}")


def _print_seal_top(rows):
    print()
    print("=" * 72)
    print("【3. 封单额 TOP（真金封板强度）】")
    print("=" * 72)
    if not rows:
        print("  ⚠️ 无涨停数据")
        return
    header = f"  {'代码':<8}{'名称':<10}{'连板':>4}{'封单额':>10}{'题材':<12}"
    print(header)
    print("  " + "-" * 72)
    for r in rows:
        print(f"  {r['code']:<8}{_short(r['name'], 9):<10}{r['consec']:>4}"
              f"{_fmt_yi(r['seal']):>10}{_short(r['industry'], 11):<12}")


def _print_industry(title, counts):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    if not counts:
        print("  ⚠️ 无数据")
        return
    for ind, cnt in counts:
        bar = "█" * cnt
        print(f"  {ind:<14}{cnt:>3}  {bar}")


def _print_dt_pool(dt_df):
    print()
    print("=" * 72)
    print("【5. 跌停池】")
    print("=" * 72)
    if dt_df is None:
        print("  ⚠️ 无跌停数据（或当日无跌停）")
        return
    rows = []
    for _, r in dt_df.iterrows():
        rows.append({
            "code": str(r.get("代码", "")).zfill(6),
            "name": str(r.get("名称", "")),
            "pct": _num(r.get("涨跌幅")),
            "turnover": _num(r.get("换手率")),
            "consec": _num(r.get("连续跌停")),   # 仅东财源有
            "seal": _num(r.get("封单资金")),      # 仅东财源有
            "industry": str(r.get("所属行业", "")),
        })
    rows.sort(key=lambda x: -(x["pct"] or 0))
    header = f"  {'代码':<8}{'名称':<10}{'涨跌幅':>8}{'换手':>7}{'连跌':>4}{'封单额':>10}{'题材':<12}"
    print(header)
    print("  " + "-" * 72)
    for r in rows:
        pct = f"{r['pct']:.2f}%" if r["pct"] is not None else "   --"
        turn = f"{r['turnover']:.1f}%" if r["turnover"] is not None else "  --"
        consec = f"{int(r['consec'])}" if r["consec"] is not None else " --"
        print(f"  {r['code']:<8}{_short(r['name'], 9):<10}{pct:>8}{turn:>7}{consec:>4}"
              f"{_fmt_yi(r['seal']):>10}{_short(r['industry'], 11):<12}")


def _print_ladder():
    """【连板梯队矩阵】近30日最高板演变 + 连板次日晋级率（同花顺 limit-up-ladder）。

    seal_nextday 为 bool（次日是否封板）；最近交易日为 null 跳过，不计入晋级率样本。
    """
    print()
    print("=" * 72)
    print("【1b. 连板梯队矩阵（近30日 · 同花顺 · 晋级率）】")
    print("=" * 72)
    try:
        from app import hithink
        data = hithink.fetch_limit_up_ladder()
    except Exception:
        data = {}
    items = (data or {}).get("item") or []
    if not items:
        print("  ⚠️ 无连板梯队数据（无 key 或接口不可达）")
        return
    board_keys = ("two_board", "three_board", "four_board", "five_board", "six_board", "seven_over")

    print("  近8日最高连板演变:")
    for day in items[-8:]:
        boards = day.get("boards") or {}
        max_n = 0
        for k in board_keys:
            for s in (boards.get(k) or []):
                max_n = max(max_n, _int(s.get("board_num")))
        bar = "█" * min(max_n, 10)
        print(f"    {day.get('date', '')}  {max_n}板  {bar}")

    total = sealed = 0
    for day in items:
        boards = day.get("boards") or {}
        for k in board_keys:
            for s in (boards.get(k) or []):
                if s.get("seal_nextday") is None:
                    continue
                total += 1
                if s.get("seal_nextday") is True:
                    sealed += 1
    if total:
        rate = sealed / total * 100
        verdict = ("情绪强（连板晋级率高）" if rate >= 60
                   else ("情绪中性" if rate >= 40 else "情绪弱（晋级率低，接力差）"))
        print(f"  近30日连板次日晋级率: {sealed}/{total} = {rate:.0f}%  →  {verdict}")
    else:
        print("  次日晋级率: 无样本")


# ---------------------------------------------------------------- 主流程

def main():
    argv = sys.argv[1:]
    date = argv[0].strip() if argv and argv[0].strip().isdigit() else _latest_trade_date()

    print("=" * 72)
    print(f"涨跌停分析（涨停梯队 + 炸板率 + 封单 + 题材 + 跌停 → 短线情绪周期）")
    print("=" * 72)
    print(f"  交易日: {date}")

    # 优先同花顺（稳定主源），失败/无 key 回退东财 akshare
    # 同花顺限流较紧，三池逐次拉取间加小间隔降低 429
    zt_df = _fetch_hithink("limit_up", date)
    time.sleep(1.0)
    zb_df = _fetch_hithink("limit_break", date)
    time.sleep(1.0)
    dt_df = _fetch_hithink("limit_down", date)

    if zt_df is None or zb_df is None or dt_df is None:
        try:
            import akshare as ak
        except ImportError:
            ak = None
        if ak is not None:
            if zt_df is None:
                zt_df = _fetch_pool(ak.stock_zt_pool_em, date, "涨停池")
            if zb_df is None:
                zb_df = _fetch_pool(ak.stock_zt_pool_zbgc_em, date, "炸板池")
            if dt_df is None:
                dt_df = _fetch_pool(ak.stock_zt_pool_dtgc_em, date, "跌停池")

    # 本地库兜底：涨停池仍缺失时，用未复权涨幅 + 涨跌停制度重建（炸板/跌停不可重建）
    if zt_df is None:
        zt_df = _detect_limit_ups_local(date)
        if zt_df is not None and not getattr(zt_df, "empty", True):
            try:
                from tools.marketdb_local import db_max_date
                _md = db_max_date() or "?"
            except Exception:
                _md = "?"
            print(f"\n  ⚠️ 涨停池为本地库兜底重建（未复权涨幅 + 涨跌停制度；封单/题材/炸板/跌停不可得，数据截至 {_md}）")
            print("     可先 py tools/marketdb_local.py sync 或恢复 API 后重跑。")

    if zt_df is None and zb_df is None and dt_df is None:
        print("❌ 同花顺、东财 akshare 与本地库均不可用（配置 HITHINK_FINANCE_API_KEY 或 pip install akshare，或先落库 marketdb）")
        return 1

    zt_n = len(zt_df) if zt_df is not None else 0
    zb_n = len(zb_df) if zb_df is not None else 0
    dt_n = len(dt_df) if dt_df is not None else 0

    # 1. 涨停梯队
    tiers, high = _tier_stats(zt_df)
    _print_tiers(tiers, high)

    # 1b. 连板梯队矩阵（近30日 + 晋级率）
    _print_ladder()

    # 2. 炸板率
    _print_break_rate(zt_n, zb_n, zb_known=(zb_df is not None))

    # 3. 封单额 TOP
    _print_seal_top(_seal_top(zt_df))

    # 4. 题材归类
    _print_industry("【4. 题材归类 · 涨停题材聚集】", _industry_agg(zt_df))
    if zb_df is not None:
        _print_industry("【4. 题材归类 · 炸板题材聚集】", _industry_agg(zb_df))

    # 5. 跌停池
    _print_dt_pool(dt_df)

    # 情绪面汇总
    print()
    print("=" * 72)
    print("【6. 情绪面汇总】")
    print("=" * 72)
    print(f"  涨停 {zt_n} / 跌停 {dt_n} / 炸板 {zb_n}")
    print(f"  炸板率 {(zb_n / (zt_n + zb_n) * 100) if (zt_n + zb_n) else 0:.1f}%   |   最高连板 {max((h['consec'] for h in high), default=0)} 板")
    dt_agg = _industry_agg(dt_df, 6)
    if dt_agg:
        print(f"  跌停题材聚集: " + " / ".join(f"{ind}×{c}" for ind, c in dt_agg))

    print()
    print("  说明:")
    print("    - 涨跌停判定由同花顺/东财精确口径给出（10%/20%/ST 5%），非 9.9% 近似")
    print("    - 涨停统计 = N天M板（如 3/3 = 3天3板）；封单额 = 收盘封板资金")
    print("    - 题材列：同花顺源=涨停原因，东财源=所属行业；跌停题材同花顺无数据")
    print("    - 炸板率 = 炸板数 /（涨停数+炸板数），反映封板坚决度与短线分歧")
    print("    - 本脚本只做取数聚合，『情绪周期 / 主线题材』结论由 AI 依据 SKILL.md 框架生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
