#!/usr/bin/env python3
"""查询单个标的最近 N 个交易日的主力资金流向（妙想 Miaoxiang）

用法:
    py query_fund_flow.py <代码> [名称] [天数]

参数:
    代码   6 位 A 股 / 场内 ETF 代码（必填）
    名称   股票名称（可选，提升查询精度）
    天数   跟踪天数（可选，默认 1）：1=今天(交易日)或最近1个交易日；N=最近N个交易日

示例:
    py query_fund_flow.py 300432 富临精工        # 今日
    py query_fund_flow.py 300432 富临精工 3      # 最近3个交易日
    py query_fund_flow.py 159801 3               # ETF，最近3个交易日

输出: 按交易日倒序的表格，单位亿元（+ 净流入 / - 净流出）。
      主力 = 超大单 + 大单；散户 ≈ 小单。
"""
import re
import sys
from datetime import date
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/fund-flow 上三级：fund-flow -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient
from app.helpers import _detect_market
from app.data_fetcher import fetch_fund_flow_detail

# 5 档净流入的关键字段名（妙想返回的精确键名）
_TIERS = [
    ("主力", "主力净流入资金"),
    ("超大单", "超大单净流入资金"),
    ("大单", "大单净流入资金"),
    ("中单", "中单净流入资金"),
    ("小单", "小单净流入资金"),
]

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _parse_amount(value):
    """解析金额字符串 -> 元（float）。健壮处理「万元」「亿元」「万」「亿」及纯数字。

    库里的 MXClient._parse_amount 只认以「万/亿」结尾的串，遇到「万元/亿元」会失败，
    这里在脚本内做健壮版（不改共享库，避免影响其它调用方）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s in ("-", "--", "None", "null", "nan"):
        return None
    mult = 1.0
    if "亿" in s:
        mult = 1e8
        s = s.replace("亿", "")
    elif "万" in s:
        mult = 1e4
        s = s.replace("万", "")
    s = s.replace("元", "")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _norm_date(s) -> str:
    """日期归一化："2026-08-21(日)" / "2026-08-24 13:07" -> "2026-08-21" """
    m = _DATE_RE.match(str(s or "").strip())
    return m.group(1) if m else ""


def _fmt_amount(v) -> str:
    """元 -> 亿元字符串（保留符号，2 位小数）"""
    if v is None:
        return "   N/A"
    return f"{v / 1e8:+.2f}"


def _today_row_from_eastmoney(code: str):
    """东方财富实时资金流兜底（妙想实时通道失效时使用）

    通过 push2delay 子域获取当日实时 5 档净流入，返回 (日期, row dict) 或 None。
    row dict 键名与妙想结构化结果对齐，便于复用同一套打印逻辑。
    仅在能取到「当日」实时数据时返回（内部已做日期校验），非交易日返回 None。
    """
    try:
        flow = fetch_fund_flow_detail(code, _detect_market(code))
        if flow is None:
            return None
        if flow.main_net is None and flow.super_large_net is None:
            return None
        row = {
            "主力净流入资金": flow.main_net,
            "超大单净流入资金": flow.super_large_net,
            "大单净流入资金": flow.large_net,
            "中单净流入资金": flow.medium_net,
            "小单净流入资金": flow.small_net,
        }
        return date.today().strftime("%Y-%m-%d"), row
    except Exception:
        return None


def _parse_args(argv):
    """解析命令行参数，返回 (code, name, days)"""
    code = argv[1].strip()
    name, days = "", 1
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(1, min(int(a), 30))  # 天数限 1..30，防止请求过宽
        elif a:
            name = a
    return code, name, days


def main():
    if len(sys.argv) < 2:
        print("用法: py query_fund_flow.py <代码> [名称] [天数]")
        print("  天数: 1=今天/最近1个交易日(默认)，N=最近N个交易日")
        return 2

    code, name, days = _parse_args(sys.argv)

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)

    # 妙想「近N日」是自然日；要拿 N 个交易日，需按 1.5x + 3 多请求覆盖周末
    window = max(3, int(days * 1.5) + 3)
    query = (
        f"{code} {name} 近{window}日 资金流向 "
        f"主力净流入 超大单净流入 大单净流入 中单净流入 小单净流入"
    ).strip()

    rows, seen = [], set()
    for t in mx.query_structured(query):
        for r in t.get("rows") or []:
            d = _norm_date(r.get("日期"))
            main = _parse_amount(r.get("主力净流入资金"))
            if not d or main is None or d in seen:
                continue
            seen.add(d)
            rows.append((d, r))

    if not rows:
        # 回退：结构化无数据时，单日兜底 + 自然语言原始结果
        detail = mx.stock_fund_flow(code, name)
        if detail is not None:
            print(f"{code} {name or ''} 今日资金流向（单位：亿元，+净流入 / -净流出）".replace("  ", " ").strip())
            print(f"  主力净流入:     {_fmt_amount(detail.main_net)}")
            print(f"    超大单净流入: {_fmt_amount(detail.super_large_net)}")
            print(f"    大单净流入:   {_fmt_amount(detail.large_net)}")
            print(f"  中单净流入:     {_fmt_amount(detail.medium_net)}")
            print(f"  小单净流入:     {_fmt_amount(detail.small_net)}")
            return 0
        print("⚠️ 未查到资金流向数据（非交易时间 / 无数据 / 代码无效）\n")
        print(mx.query_as_text(query) or "❌ 无返回")
        return 0

    # 交易日倒序（最新在前），截取前 N 个
    rows.sort(key=lambda x: x[0], reverse=True)
    rows = rows[:days]

    today_str = date.today().strftime("%Y-%m-%d")

    # 妙想实时通道失效检测：今日（交易日）数据缺失时，用东方财富 push2delay 兜底补今日实时
    if days >= 1 and (not rows or rows[0][0] < today_str):
        em_row = _today_row_from_eastmoney(code)
        if em_row is not None:
            rows = [em_row] + [r for r in rows if r[0] < today_str]
            rows = rows[:days]

    label = f"最近{days}个交易日" if days > 1 else "今日/最近1个交易日"
    print(f"{code} {name} 主力资金流向 · {label}（单位：亿元，+净流入 / -净流出）".replace("  ", " ").strip())
    print("  日期          主力     超大单   大单     中单     小单")
    for d, r in rows:
        tag = " (今日·实时)" if d == today_str else ""
        cells = [_fmt_amount(_parse_amount(r.get(key))) for _, key in _TIERS]
        print(f"  {d}{tag:<9}  " + "  ".join(cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
