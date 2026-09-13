#!/usr/bin/env python3
"""大盘盯盘：8 大指数行情 + 全市场广度 + 板块领涨领跌 + 两融 → 大盘研判数据包。

核心思路：
    1. 拉 8 大核心指数实时行情（上证/深证/创业板/创业板50/科创50/沪深300/中证500/中证1000）
    2. 拉指数 60 日 K 线 → 算近 60 日位置百分位 + 均线多空排列
    3. 拉全市场快照（东财 clist）→ 涨跌家数 / 成交额 / 涨停跌停（9.9% 近似）
    4. 拉行业/概念板块资金流 → 领涨 / 领跌板块 + 全市场主力净流入近似
    5. 拉两融余额（融资/融券，替代已停披露的北向资金）

本脚本只做「取数 + 聚合」，输出结构化数据包；「大盘强弱 / 进攻防守」结论由 AI 依据 SKILL.md 框架生成。

用法:
    py .claude/skills/market-watch/scan_market_watch.py

数据源:
    - 指数行情 + K 线: 新浪（app.data_fetcher.fetch_major_indices / fetch_index_klines）
    - 全市场广度: 东财 clist（_fetch_em_clist）
    - 板块资金流: 东财数据中心（fetch_sector_fund_flow_rank）
    - 两融余额: akshare（fetch_margin_data，替代已停披露的北向资金）
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

# 定位项目根目录（skills/market-watch 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 抑制 app 模块 WARNING 噪音
import logging
logging.disable(logging.WARNING)

from app.data_fetcher import (
    fetch_major_indices,
    fetch_index_klines,
    _fetch_em_clist,
    fetch_sector_fund_flow_rank,
    fetch_margin_data,
)
from app.technical import calc_ma_alignment

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
    return f"{v / 1e8:.0f}亿" if v is not None else "  --"


def _fmt_pct(v) -> str:
    return f"{v:+.2f}%" if v is not None else "  --"


# ---------------------------------------------------------------- 数据拉取

def _fetch_index_rows() -> list[dict]:
    """拉 8 大指数行情 + 60 日 K 线，算位置百分位 + 均线排列。"""
    quotes = fetch_major_indices()
    if not quotes:
        return []
    codes = [q.code for q in quotes]
    klines_map = fetch_index_klines(codes)

    rows: list[dict] = []
    for q in quotes:
        kl = klines_map.get(q.code, [])
        pos = None
        if kl:
            recent = kl[-60:]
            highs = [k.high for k in recent if k.high is not None]
            lows = [k.low for k in recent if k.low is not None]
            price = q.price
            if price is None and recent:
                price = recent[-1].close
            if highs and lows and price is not None and max(highs) > min(lows):
                pos = (price - min(lows)) / (max(highs) - min(lows)) * 100
        ma = calc_ma_alignment(kl) if kl else None
        rows.append({
            "code": q.code,
            "name": q.name,
            "price": q.price,
            "chg": q.change_pct,
            "amount": q.amount,
            "pos": pos,
            "ma": ma.alignment if ma else "数据不足",
        })
    return rows


def _fetch_breadth() -> dict:
    """拉全市场快照，聚合涨跌家数 / 成交额 / 涨跌停。"""
    raw = _fetch_em_clist(_FS_ALL_A, _FIELDS, fid="f3")
    up = down = flat = limit_up = limit_down = 0
    total_amount = 0.0
    for it in raw:
        chg = _num(it.get("f3"))
        if chg is None:
            continue
        if chg > 0:
            up += 1
        elif chg < 0:
            down += 1
        else:
            flat += 1
        if chg >= 9.9:
            limit_up += 1
        if chg <= -9.9:
            limit_down += 1
        total_amount += _num(it.get("f6")) or 0.0
    return {
        "total": up + down + flat,
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "amount_yi": total_amount / 1e8,
    }


def _fetch_sector_sides(sector_type: str, top_n: int = 8):
    """拉板块资金流，返回 (净流入 top, 净流出 top, 全市场主力净流入合计)。"""
    flows = fetch_sector_fund_flow_rank("今日", sector_type)
    if not flows:
        return [], [], 0.0
    inflows = [f for f in flows if (f.main_net or 0) > 0][:top_n]
    outflows = [f for f in flows if (f.main_net or 0) < 0]
    outflows = sorted(outflows, key=lambda f: f.main_net)[:top_n]
    total_main = sum(f.main_net or 0.0 for f in flows)
    return inflows, outflows, total_main


# ---------------------------------------------------------------- 输出

def _print_index_table(rows: list[dict]):
    print()
    print("=" * 72)
    print("【1. 核心指数】")
    print("=" * 72)
    if not rows:
        print("  ⚠️ 无指数行情（行情源不可达）")
        return
    header = f"  {'指数':<10}{'现价':>10}{'涨跌幅':>8}{'成交额':>9}{'60日位置':>9}{'均线':<10}"
    print(header)
    print("  " + "-" * 72)
    for r in rows:
        pos_s = f"{r['pos']:.0f}%" if r["pos"] is not None else "  --"
        price_s = f"{r['price']:.2f}" if r["price"] is not None else "  --"
        print(f"  {r['name']:<10}{price_s:>10}{_fmt_pct(r['chg']):>8}"
              f"{_fmt_yi(r['amount']):>9}{pos_s:>9}{r['ma']:<10}")


def _print_breadth(b: dict):
    print()
    print("=" * 72)
    print("【2. 市场广度】")
    print("=" * 72)
    if not b.get("total"):
        print("  ⚠️ 无全市场数据（东财 clist 断连）")
        return
    up_ratio = b["up"] / b["total"] * 100 if b["total"] else 0
    print(f"  涨跌家数: 上涨 {b['up']} / 下跌 {b['down']} / 平盘 {b['flat']}  （上涨占比 {up_ratio:.1f}%）")
    print(f"  涨停(≥9.9%) {b['limit_up']} / 跌停(≤-9.9%) {b['limit_down']}   |   全市场成交额 {b['amount_yi']:.0f} 亿")
    print(f"  （注：涨跌停为 9.9% 近似口径，精确梯队/炸板率请走 /limit-analysis）")


def _print_sector(title: str, inflows, outflows, total_main=None):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    if not inflows and not outflows:
        print("  ⚠️ 无板块资金流数据（接口不可达）")
        return
    if total_main is not None:
        print(f"  全市场主力净流入合计（行业板块求和近似）: {_fmt_yi(total_main)}")
    print("  ── 领涨 · 主力净流入 top ──")
    for f in inflows:
        print(f"    {f.name:<14} 净流入 {_fmt_yi(f.main_net):>8}  涨跌 {_fmt_pct(f.change_pct)}  主力股 {f.top_stock}")
    print("  ── 领跌 · 主力净流出 top ──")
    for f in outflows:
        print(f"    {f.name:<14} 净流出 {_fmt_yi(f.main_net):>8}  涨跌 {_fmt_pct(f.change_pct)}  主力股 {f.top_stock}")


def _print_margin(m):
    print()
    print("=" * 72)
    print("【4. 两融余额（替代已停披露的北向资金）】")
    print("=" * 72)
    if m is None or not m.date:
        print("  ⚠️ 无两融数据（akshare 未装或接口不可达）")
        return
    print(f"  数据日期: {m.date}")
    print(f"  融资余额 {m.financing_balance:.0f} 亿  |  融券余额 {m.securities_lending_balance:.0f} 亿  |  两融总余额 {m.total_balance:.0f} 亿")
    print(f"  融资净买入(当日) {m.financing_net_buy:+.1f} 亿  →  {m.financing_change_direction}")


# ---------------------------------------------------------------- 主流程

def main():
    print("=" * 72)
    print("大盘盯盘（8 指数 + 市场广度 + 板块资金流 + 两融 → 大盘研判数据包）")
    print("=" * 72)
    print("  （非交易时段显示最近收盘数据）")

    # 1. 核心指数
    _print_index_table(_fetch_index_rows())

    # 2. 市场广度
    _print_breadth(_fetch_breadth())

    # 3. 板块领涨领跌
    ind_in, ind_out, ind_total = _fetch_sector_sides("行业资金流")
    _print_sector("【3. 板块资金流 · 行业板块（今日）】", ind_in, ind_out, ind_total)
    con_in, con_out, _ = _fetch_sector_sides("概念资金流")
    _print_sector("【3. 板块资金流 · 概念板块（今日）】", con_in, con_out)

    # 4. 两融
    _print_margin(fetch_margin_data())

    print()
    print("  说明:")
    print("    - 60日位置 = 现价在近60日高低区间的百分位（0%=近60日最低 / 100%=近60日最高）")
    print("    - 均线 = MA5/10/20/60 排列状态（多头/空头/缠绕/多头回调/空头反弹）")
    print("    - 两融余额为 T+1 日频数据，替代已停止披露的北向资金实时净流入")
    print("    - 本脚本只做取数聚合，『大盘强弱 / 进攻防守』结论由 AI 依据 SKILL.md 框架生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
