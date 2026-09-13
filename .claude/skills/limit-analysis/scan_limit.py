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
    - 涨停池/炸板池/跌停池: 东财（akshare stock_zt_pool_em / stock_zt_pool_zbgc_em / stock_zt_pool_dtgc_em，需 pip install akshare）
    不依赖 MX_APIKEY。涨跌停判定由东财精确口径（10%/20%/ST 5%）给出，非 9.9% 近似。
"""
import os
import sys
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
    """返回最近交易日 YYYYMMDD。"""
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
    """题材归类：按所属行业聚合计数。"""
    if df is None:
        return []
    cnt = Counter(str(r.get("所属行业", "") or "").strip() for _, r in df.iterrows())
    cnt.pop("", None)
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
        header = f"  {'代码':<8}{'名称':<10}{'连板':>4}{'涨停统计':>8}{'封单额':>10}{'行业':<12}"
        print(header)
        print("  " + "-" * 72)
        for h in high:
            print(f"  {h['code']:<8}{_short(h['name'], 9):<10}{h['consec']:>4}"
                  f"{h['stat']:>8}{_fmt_yi(h['seal']):>10}{_short(h['industry'], 11):<12}")


def _print_break_rate(zt_n, zb_n):
    print()
    print("=" * 72)
    print("【2. 炸板率】")
    print("=" * 72)
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
    header = f"  {'代码':<8}{'名称':<10}{'连板':>4}{'封单额':>10}{'行业':<12}"
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
            "consec": _int(r.get("连续跌停")),
            "seal": _num(r.get("封单资金")),
            "industry": str(r.get("所属行业", "")),
        })
    rows.sort(key=lambda x: -x["consec"])
    header = f"  {'代码':<8}{'名称':<10}{'连跌':>4}{'封单额':>10}{'行业':<12}"
    print(header)
    print("  " + "-" * 72)
    for r in rows:
        print(f"  {r['code']:<8}{_short(r['name'], 9):<10}{r['consec']:>4}"
              f"{_fmt_yi(r['seal']):>10}{_short(r['industry'], 11):<12}")


# ---------------------------------------------------------------- 主流程

def main():
    argv = sys.argv[1:]
    date = argv[0].strip() if argv and argv[0].strip().isdigit() else _latest_trade_date()

    print("=" * 72)
    print(f"涨跌停分析（涨停梯队 + 炸板率 + 封单 + 题材 + 跌停 → 短线情绪周期）")
    print("=" * 72)
    print(f"  交易日: {date}")

    try:
        import akshare as ak
    except ImportError:
        print("❌ 未安装 akshare，无法拉取涨跌停池（pip install akshare）")
        return 1

    zt_df = _fetch_pool(ak.stock_zt_pool_em, date, "涨停池")
    zb_df = _fetch_pool(ak.stock_zt_pool_zbgc_em, date, "炸板池")
    dt_df = _fetch_pool(ak.stock_zt_pool_dtgc_em, date, "跌停池")

    zt_n = len(zt_df) if zt_df is not None else 0
    zb_n = len(zb_df) if zb_df is not None else 0
    dt_n = len(dt_df) if dt_df is not None else 0

    # 1. 涨停梯队
    tiers, high = _tier_stats(zt_df)
    _print_tiers(tiers, high)

    # 2. 炸板率
    _print_break_rate(zt_n, zb_n)

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
    print("    - 涨跌停判定由东财精确口径给出（10%/20%/ST 5%），非 9.9% 近似")
    print("    - 涨停统计 = N天M板（如 3/3 = 3天3板）；封单额 = 收盘封板资金")
    print("    - 炸板率 = 炸板数 /（涨停数+炸板数），反映封板坚决度与短线分歧")
    print("    - 本脚本只做取数聚合，『情绪周期 / 主线题材』结论由 AI 依据 SKILL.md 框架生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
