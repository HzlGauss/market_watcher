#!/usr/bin/env python3
"""打新申购分析（新股 IPO / 新债 可转债）

用法:
    py analyze_ipo.py <代码> [名称]
    py analyze_ipo.py --list [数量]

参数:
    代码   新股代码（如 688837）或可转债代码（如 113710）
    名称   可选，提升匹配精度
    --list  列出待申购的新股/新债清单（打新日历）

输出 4 段数据包：
    ① 发行信息（板块/发行价/发行市盈率/行业市盈率/申购上市日期）
    ② 破发判断（规则引擎：破发概率区间 + 结论标签 + 综合评分）
    ③ 市场环境（上证/深证/创业板）
    ④ 质地数据（妙想，可选，需 MX_APIKEY）
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

# 定位项目根目录（skills/ipo-analysis 上三级：ipo-analysis -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.ipo_analyzer import (
    detect_and_analyze,
    list_upcoming,
    _fetch_market_context,
    _mx_query,
    _num,
)


def _fmt(v, suffix="", nd=2):
    """数值格式化，None → '--'"""
    if v is None:
        return "--"
    return f"{v:.{nd}f}{suffix}"


def _print_verdict(a):
    """打印②破发判断：概率/结论/评分 + 关键因子"""
    print("【② 破发判断（规则引擎）】")
    print(f"  破发概率区间:  {a['break_prob']}")
    print(f"  申购结论:      {a['verdict']}")
    print(f"  综合评分:      {a['score']}/100")
    for f in a["factors"]:
        print(f"  · {f}")
    print()


def _print_stock(a):
    """打印新股数据包"""
    print(f"=== 打新分析: {a['code']} {a['name']}（新股 · {a['board']}）===\n")

    print("【① 发行信息】")
    print(f"  代码/简称:     {a['code']} {a['name']}")
    print(f"  板块:          {a['board']}")
    print(f"  发行价:        {_fmt(a['issue_price'], ' 元')}")
    print(f"  发行市盈率:    {_fmt(a['issue_pe'])}")
    print(f"  行业市盈率:    {_fmt(a['industry_pe'])}")
    print(f"  发行总数:      {_fmt(a['total_shares_wan'], ' 万股', 0)}")
    print(f"  申购日期:      {a['sub_date'] or '--'}")
    print(f"  上市日期:      {a['list_date'] or '--'}")
    print(f"  状态:          {a['status']}")
    print(f"  网上中签率:    {_fmt(a['win_rate'], '%')}")
    print()

    _print_verdict(a)
    _print_market()


def _print_bond(a):
    """打印新债数据包"""
    print(f"=== 打新分析: {a['code']} {a['name']}（新债 / 可转债）===\n")

    print("【① 发行信息】")
    print(f"  代码/简称:     {a['code']} {a['name']}")
    print(f"  正股:          {a['stock_name']} {a['stock_code']}（现价 {_fmt(a['stock_price'], ' 元')}）")
    print(f"  转股价:        {_fmt(a['conv_price'], ' 元')}")
    print(f"  转股价值:      {_fmt(a['conv_value'])}")
    print(f"  转股溢价率:    {_fmt(a['premium'], '%')}")
    print(f"  债券评级:      {a['rating'] or '--'}")
    print(f"  发行规模:      {_fmt(a['scale'], ' 亿')}")
    print(f"  申购日期:      {a['sub_date'] or '--'}")
    print(f"  上市时间:      {a['list_time'] or '--'}")
    print(f"  状态:          {a['status']}")
    print(f"  网上中签率:    {_fmt(a['win_rate'], '%')}")
    print()

    _print_verdict(a)
    _print_market()


def _print_market():
    print("【③ 市场环境】")
    market = _fetch_market_context()
    if market:
        for k, v in market.items():
            print(f"  {k}: {v}")
    else:
        print("  (获取失败)")
    print()


def _print_quality(config, result):
    """④质地数据：有 key 走妙想，无 key 打印提示。"""
    print("【④ 质地数据（妙想，可选）】")
    if not getattr(config, "mx_apikeys", None):
        print("  ⚠️ 需在 .env 配置 MX_APIKEY；无 key 时由 AI 用自身知识补充")
        print()
        return
    if result["kind"] == "stock":
        q = f"{result['name']} {result['code']} 新股 主营业务 所属行业 营收 净利润 同比增速 毛利率"
    else:
        q = f"{result['stock_name']} {result['stock_code']} 主营业务 所属行业 营收 净利润 同比增速"
    text = _mx_query(q, config)
    print(text if text else "  (无返回)")
    print()


def _print_list(limit: int):
    """打印待申购清单（打新日历）"""
    print(f"=== 待申购清单（打新日历）===\n")
    data = list_upcoming(limit)

    if data["stocks"]:
        print("【新股 · 待申购】")
        for d in data["stocks"]:
            print(
                f"  {d.get('股票代码', '')} {d.get('股票简称', '')} | "
                f"发行价 {_fmt(_num(d.get('发行价格')), '')} | "
                f"发行PE {_fmt(_num(d.get('发行市盈率')), '')} / 行业PE {_fmt(_num(d.get('行业市盈率')), '')} | "
                f"申购 {d.get('申购日期', '')}"
            )
        print()

    if data["bonds"]:
        print("【新债 · 待申购】")
        for d in data["bonds"]:
            print(
                f"  {d.get('债券代码', '')} {d.get('债券简称', '')} | "
                f"正股 {d.get('正股简称', '')} | "
                f"转股价值 {_fmt(_num(d.get('转股价值')), '')} | "
                f"评级 {d.get('信用评级', '')} | "
                f"申购 {d.get('申购日期', '')}"
            )
        print()

    if not data["stocks"] and not data["bonds"]:
        print("  (暂无待申购标的，或 akshare 未安装)")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    # --list 模式
    if "--list" in args:
        limit = 10
        for a in args:
            if a.isdigit():
                limit = int(a)
                break
        _print_list(limit)
        return 0

    code = args[0].strip()
    name = ""
    for a in args[1:]:
        a = a.strip()
        if a and not a.isdigit() and not a.startswith("--"):
            name = a

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")

    result = detect_and_analyze(code, name)

    if result["kind"] == "not_found":
        print(f"❌ 未找到标的: {code} {name}")
        print("  · 新股代码示例：688837 / 601091 / 301686（6 位数字）")
        print("  · 新债代码示例：113710 / 123284（11/12 开头）")
        print("  · 用 --list 查看当前待申购清单：")
        _print_list(10)
        return 1

    if result["kind"] == "stock":
        _print_stock(result)
    else:
        _print_bond(result)

    _print_quality(config, result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
