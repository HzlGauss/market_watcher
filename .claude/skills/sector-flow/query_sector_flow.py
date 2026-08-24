#!/usr/bin/env python3
"""查询 A 股行业/概念/地域板块资金流排名（东方财富数据中心-板块资金流）

用法:
    py query_sector_flow.py [周期] [板块类型] [数量]

参数:
    周期      回看周期（可选，默认 5日）：今日 / 5日 / 10日（也接受 1/5/10/今天）
    板块类型  板块类型（可选，默认 行业）：行业 / 概念 / 地域
    数量      显示前 N 名净流入 + 后 N 名净流出（可选，默认全部）；也可用 --top N

示例:
    py query_sector_flow.py                 # 行业板块 5日资金流排名（全部）
    py query_sector_flow.py 今日            # 行业板块 今日资金流排名
    py query_sector_flow.py 10日 概念       # 概念板块 10日资金流排名
    py query_sector_flow.py 10日 行业 20    # 行业板块 10日，前20净流入+后20净流出
    py query_sector_flow.py --top 15        # 行业板块 5日，前15+后15

输出: 按主力净流入降序的表格，单位亿元（+ 净流入 / - 净流出）。
      主力净流入 = 超大单 + 大单；数据源直连东方财富 push2 clist 接口。
"""
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/sector-flow 上三级：sector-flow -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.data_fetcher import fetch_sector_fund_flow_rank

_INDICATORS = {
    "今日": "今日", "今天": "今日", "1": "今日",
    "5日": "5日", "5": "5日",
    "10日": "10日", "10": "10日",
}
_SECTOR_TYPES = {"行业": "行业资金流", "概念": "概念资金流", "地域": "地域资金流"}


def _fmt_amount(v):
    """金额转亿元，带正负号（None 返回 —）"""
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v) / 1e8:.2f}"


def _fmt_pct(v, signed=True):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v):.2f}%" if signed else f"{v:.2f}%"


def _pad(s, width):
    """按显示宽度左对齐填充（中文按 2 宽度计）"""
    w = sum(2 if ord(c) > 0x1100 else 1 for c in s)
    return s + " " * max(0, width - w)


def _parse_args(argv):
    indicator, sector_type, top = "5日", "行业", None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--top":
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                top = max(1, int(argv[i + 1]))
                i += 1
            else:
                print("⚠️ --top 需要跟一个正整数")
        elif a in _INDICATORS:
            indicator = _INDICATORS[a]
        elif a in _SECTOR_TYPES:
            sector_type = a
        elif a.isdigit():
            top = max(1, int(a))
        else:
            print(f"⚠️ 忽略未知参数: {a}")
        i += 1
    return indicator, sector_type, top


def _print_row(i, f):
    gain = _fmt_pct(f.change_pct)
    net = _fmt_amount(f.main_net)
    pct = _fmt_pct(f.main_pct, signed=False)
    top_stock = f.top_stock or "—"
    print(f"{i:<4} {_pad(f.name, 12)}{gain:>8}  {net:>10}  {pct:>7}  {top_stock}")


def main():
    argv = sys.argv[1:]
    if any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__)
        return 0

    indicator, sector_type, top = _parse_args(argv)
    flows = fetch_sector_fund_flow_rank(
        indicator=indicator, sector_type=_SECTOR_TYPES[sector_type]
    )

    if not flows:
        print(
            f"❌ 未获取到 {sector_type}板块 {indicator} 资金流数据"
            "（东财接口不可达或非交易时段）"
        )
        return 1

    print(
        f"A股 {sector_type}板块资金流排名 · {indicator}（单位：亿元，+净流入 / -净流出）"
    )
    print("排名 板块        涨跌幅    主力净流入  净占比   主力净流入最大股")
    print("-" * 68)

    total = len(flows)
    if top and total > 2 * top:
        for i, f in enumerate(flows[:top], 1):
            _print_row(i, f)
        print(f"  ... 中间省略 {total - 2 * top} 个板块 ...")
        for i, f in enumerate(flows[-top:], total - top + 1):
            _print_row(i, f)
    else:
        for i, f in enumerate(flows, 1):
            _print_row(i, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
