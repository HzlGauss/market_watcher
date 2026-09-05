#!/usr/bin/env python3
"""市场热度雷达：涨幅榜 + 跌幅榜 + 龙虎榜 → 热点方向 / 避坑方向 数据包。

核心思路：
    1. 拉全市场行情（东财 clist，按涨跌幅降序）→ 涨幅榜 / 跌幅榜 + 涨跌停家数 + 个股行业
    2. 拉龙虎榜（东方财富）→ 净买入 / 净卖出排行 + 上榜原因
    3. 行业聚合：涨幅榜+龙虎榜净买入 的行业聚集 = 热点方向；跌幅榜+龙虎榜净卖出 = 避坑方向
    4. 板块资金流佐证：概念/行业板块今日主力净流入 top / 净流出 top
    5. 情绪面：涨停/跌停家数、龙虎榜净买/净卖额对比

本脚本只做「取数 + 聚合」，输出结构化数据包；「热点方向 / 避坑方向」结论由 AI 依据 SKILL.md 框架生成。

用法:
    py .claude/skills/market-heat/scan_market_heat.py [涨幅榜数量] [跌幅榜数量]

参数:
    涨幅榜数量   涨幅榜显示数量（可选，默认 20）
    跌幅榜数量   跌幅榜显示数量（可选，默认 20）

数据源:
    - 全市场行情 + 行业: 东财 clist（_fetch_em_clist，push2delay/push2 双 host 回退）
    - 龙虎榜: 东方财富（app.dragon_tiger.fetch_dragon_tiger_list）
    - 板块资金流: 东财数据中心（fetch_sector_fund_flow_rank，行业/概念）
    全部不依赖 MX_APIKEY。
"""
import os
import sys
from pathlib import Path

# 强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
os.environ.setdefault("TQDM_DISABLE", "1")

# 定位项目根目录（skills/market-heat 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 抑制 app 模块 WARNING 噪音
import logging
logging.disable(logging.WARNING)

from app.data_fetcher import _fetch_em_clist, fetch_sector_fund_flow_rank
from app.dragon_tiger import fetch_dragon_tiger_list

# 东财 clist 全 A 股（沪深主板 + 创业板 + 科创板）
_FS_ALL_A = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"
# f12=代码 f14=名称 f2=最新价 f3=涨跌幅 f6=成交额 f8=换手率 f10=量比 f20=总市值 f100=行业
_FIELDS = "f12,f14,f2,f3,f6,f8,f10,f20,f100"


def _num(v):
    """解析为 float，None/空/'-' 返回 None。"""
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_yi(v) -> str:
    """元 -> 亿元字符串（None 显示 --）。"""
    return f"{v / 1e8:.2f}亿" if v is not None else "  --"


def _fmt_pct(v) -> str:
    return f"{v:+.2f}%" if v is not None else "  --"


def _fmt_mktcap(v) -> str:
    """市值（元）-> 短字符串。"""
    if v is None:
        return "  --"
    if v >= 1e12:
        return f"{v / 1e12:.1f}万亿"
    return f"{v / 1e8:.0f}亿"


def _short(s, n=18) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- 数据拉取

def _fetch_spot() -> list[dict]:
    """拉全市场行情，返回按涨跌幅降序的 [{code,name,price,chg,amount,turnover,mktcap,industry}]。"""
    raw = _fetch_em_clist(_FS_ALL_A, _FIELDS, fid="f3")
    items = []
    for it in raw:
        code = str(it.get("f12", "")).zfill(6)
        chg = _num(it.get("f3"))
        if not code or chg is None:  # 过滤停牌/无涨跌幅
            continue
        items.append({
            "code": code,
            "name": str(it.get("f14", "")),
            "price": _num(it.get("f2")),
            "chg": chg,
            "amount": _num(it.get("f6")),
            "turnover": _num(it.get("f8")),
            "mktcap": _num(it.get("f20")),
            "industry": str(it.get("f100", "") or ""),
        })
    items.sort(key=lambda x: x["chg"], reverse=True)
    return items


def _fetch_lhb(industry_map: dict[str, str]):
    """拉龙虎榜，返回 (净买入 top, 净卖出 top) 各 10 只，行业从 industry_map 回查。

    同一股票可能因多个上榜原因重复出现（net_buy 相同），按 code 去重只保留一条。
    """
    try:
        records = fetch_dragon_tiger_list(max_count=60)
    except Exception:
        return [], []
    seen: dict[str, dict] = {}
    for r in records:
        code = str(r.code).zfill(6)
        if code in seen:
            continue
        seen[code] = {
            "code": code,
            "name": r.name,
            "net_buy": r.net_buy or 0.0,
            "change_pct": r.change_pct,
            "turnover": r.turnover_rate,
            "reason": r.reason,
            "industry": industry_map.get(code, ""),
        }
    items = list(seen.values())
    buys = sorted([x for x in items if x["net_buy"] > 0], key=lambda x: -x["net_buy"])[:10]
    sells = sorted([x for x in items if x["net_buy"] < 0], key=lambda x: x["net_buy"])[:10]
    return buys, sells


def _fetch_sector_flow(sector_type: str, top_n: int = 8):
    """拉板块资金流，返回 (净流入 top, 净流出 top)。"""
    try:
        flows = fetch_sector_fund_flow_rank("今日", sector_type)
    except Exception:
        return [], []
    inflows = [f for f in flows if (f.main_net or 0) > 0][:top_n]
    outflows = [f for f in flows if (f.main_net or 0) < 0][-top_n:]
    return inflows, outflows


# ---------------------------------------------------------------- 聚合

def _count_industry(codes, industry_map: dict[str, str]) -> list[tuple[str, int]]:
    """行业聚集计数，按出现次数降序。"""
    counter: dict[str, int] = {}
    for code in codes:
        ind = (industry_map.get(code, "") or "").strip() or "未知"
        counter[ind] = counter.get(ind, 0) + 1
    return sorted(counter.items(), key=lambda x: -x[1])


def _print_rank(title: str, rows, n: int, show_industry=True):
    """打印涨/跌榜表格。"""
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    if not rows:
        print("  ⚠️ 无数据（行情源不可达）")
        return
    header = (f"  {'代码':<8}{'名称':<10}{'涨跌幅':>8}{'成交额':>10}{'换手率':>8}{'市值':>8}"
              + (f"{'行业':<12}" if show_industry else ""))
    print(header)
    print("  " + "-" * 100)
    for r in rows[:n]:
        line = (f"  {r['code']:<8}{_short(r['name'], 9):<10}{_fmt_pct(r['chg']):>8}"
                f"{_fmt_yi(r['amount']):>10}{_fmt_pct(r['turnover']):>8}{_fmt_mktcap(r['mktcap']):>8}")
        if show_industry:
            line += f"{_short(r.get('industry'), 11):<12}"
        print(line)


def _print_lhb(buys, sells):
    """打印龙虎榜净买入/净卖出。"""
    print()
    print("=" * 72)
    print("【3. 龙虎榜资金】")
    print("=" * 72)
    if not buys and not sells:
        print("  ⚠️ 无龙虎榜数据（最近交易日无上榜，或接口不可达）")
        return
    header = f"  {'代码':<8}{'名称':<10}{'净买入':>10}{'涨跌幅':>8}{'行业':<12}  上榜原因"
    print("  ── 净买入 top ──")
    print(header)
    print("  " + "-" * 100)
    for r in buys:
        print(f"  {r['code']:<8}{_short(r['name'], 9):<10}{_fmt_yi(r['net_buy']):>10}"
              f"{_fmt_pct(r['change_pct']):>8}{_short(r['industry'], 11):<12}  {_short(r['reason'], 40)}")
    print()
    print("  ── 净卖出 top ──")
    print(header)
    print("  " + "-" * 100)
    for r in sells:
        print(f"  {r['code']:<8}{_short(r['name'], 9):<10}{_fmt_yi(r['net_buy']):>10}"
              f"{_fmt_pct(r['change_pct']):>8}{_short(r['industry'], 11):<12}  {_short(r['reason'], 40)}")


def _print_industry_agg(title: str, counts: list[tuple[str, int]]):
    """打印行业聚集统计。"""
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    if not counts:
        print("  ⚠️ 无数据")
        return
    for ind, cnt in counts[:15]:
        bar = "█" * cnt
        print(f"  {ind:<14}{cnt:>3}  {bar}")


def _print_sector_flow(title: str, inflows, outflows):
    """打印板块资金流佐证。"""
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    if not inflows and not outflows:
        print("  ⚠️ 无板块资金流数据（接口不可达）")
        return
    print("  ── 主力净流入 top ──")
    for f in inflows:
        print(f"    {_short(f.name, 14):<14} 净流入 {_fmt_yi(f.main_net):>8}  涨跌 {_fmt_pct(f.change_pct)}  主力股 {_short(f.top_stock, 10)}")
    print("  ── 主力净流出 top ──")
    for f in outflows:
        print(f"    {_short(f.name, 14):<14} 净流出 {_fmt_yi(f.main_net):>8}  涨跌 {_fmt_pct(f.change_pct)}  主力股 {_short(f.top_stock, 10)}")


# ---------------------------------------------------------------- 主流程

def _parse_args(argv):
    n_up = n_down = 20
    for i, a in enumerate(argv[1:3]):
        a = a.strip()
        if a.isdigit():
            v = max(5, min(int(a), 50))
            if i == 0:
                n_up = v
            else:
                n_down = v
    return n_up, n_down


def main():
    n_up, n_down = _parse_args(sys.argv)

    print("=" * 72)
    print("市场热度雷达（涨幅榜 + 跌幅榜 + 龙虎榜 → 热点/避坑方向）")
    print("=" * 72)
    print("  （非交易时段显示最近收盘数据）")

    # ---- 全市场行情 ----
    items = _fetch_spot()
    if not items:
        print("❌ 全市场行情不可达（东财 clist 断连）")
        return 1
    print(f"  全市场有效样本: {len(items)} 只")

    industry_map = {x["code"]: x["industry"] for x in items}

    # ---- 涨跌榜 ----
    up_list = [x for x in items if x["chg"] > 0]
    down_list = [x for x in items if x["chg"] < 0][::-1]  # 跌幅最大的在前
    _print_rank(f"【1. 涨幅榜】（前 {n_up}）", up_list, n_up)
    _print_rank(f"【2. 跌幅榜】（前 {n_down}）", down_list, n_down)

    # ---- 龙虎榜 ----
    lhb_buys, lhb_sells = _fetch_lhb(industry_map)
    _print_lhb(lhb_buys, lhb_sells)

    # ---- 热点/避坑方向聚合 ----
    hot_codes = [x["code"] for x in up_list[:n_up]] + [x["code"] for x in lhb_buys]
    cold_codes = [x["code"] for x in down_list[:n_down]] + [x["code"] for x in lhb_sells]
    hot_counts = _count_industry(hot_codes, industry_map)
    cold_counts = _count_industry(cold_codes, industry_map)
    _print_industry_agg("【4. 热点方向聚合】（涨幅榜 + 龙虎榜净买入 的行业聚集）", hot_counts)
    _print_industry_agg("【5. 避坑方向聚合】（跌幅榜 + 龙虎榜净卖出 的行业聚集）", cold_counts)

    # ---- 板块资金流佐证 ----
    concept_in, concept_out = _fetch_sector_flow("概念资金流")
    industry_in, industry_out = _fetch_sector_flow("行业资金流")
    _print_sector_flow("【6. 板块资金流佐证 · 概念板块（今日）】", concept_in, concept_out)
    _print_sector_flow("【6. 板块资金流佐证 · 行业板块（今日）】", industry_in, industry_out)

    # ---- 情绪面 ----
    print()
    print("=" * 72)
    print("【7. 情绪面】")
    print("=" * 72)
    limit_up = sum(1 for x in items if x["chg"] >= 9.9)
    limit_down = sum(1 for x in items if x["chg"] <= -9.9)
    up_count = len(up_list)
    down_count = len(down_list)
    lhb_net_buy = sum(x["net_buy"] for x in lhb_buys)
    lhb_net_sell = sum(x["net_buy"] for x in lhb_sells)
    print(f"  涨跌家数: 上涨 {up_count} / 下跌 {down_count}  |  涨停(≥9.9%) {limit_up} / 跌停(≤-9.9%) {limit_down}")
    print(f"  龙虎榜: 净买入 top10 合计 {_fmt_yi(lhb_net_buy)}  vs  净卖出 top10 合计 {_fmt_yi(lhb_net_sell)}")

    print()
    print("  说明:")
    print("    - 涨跌榜/行业来自东财全市场快照；龙虎榜为最近交易日数据；板块资金流为今日主力净流入")
    print("    - 热点方向 = 行业聚集 + 概念板块净流入 + 龙虎榜净买入 三者共振；单一维度信号 = 轮动/脉冲")
    print("    - 避坑方向 = 跌幅榜行业聚集 + 概念板块净流出 + 龙虎榜净卖出")
    print("    - 涨停阈值按 9.9% 近似（含创业板/科创板 20cm，ST 5% 涨停不计入）")
    print("    - 概念板块含「融资融券/MSCI中国/富时罗素」等指数/风格类条目，属噪音，分析热点时忽略")
    return 0


if __name__ == "__main__":
    sys.exit(main())
