#!/usr/bin/env python3
"""个股分析数据包：用妙想（Miaoxiang）拉取个股的基本信息、近 N 日 K 线、
近 5 日资金流、近 7 日资讯，并计算技术位（MA5/10/20、高/低点、区间涨跌幅）。

本脚本只负责「取数 + 算指标」，输出一份结构化数据包；走势预测与买卖参考
由 AI 依据 SKILL.md 的分析框架在脚本输出之上生成。

用法:
    py .claude/skills/stock-analysis/analyze_stock.py <代码> [名称] [天数]

参数:
    代码   6 位 A 股代码（必填）
    名称   股票名称（可选，提升查询精度）
    天数   拉取的日 K 线根数（可选，默认 30，上限 60）

输出: 分 4 段——基本信息 / 近N日K线+技术位 / 近5日资金流 / 近7日资讯。
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

# 定位项目根目录（skills/stock-analysis 上三级：stock-analysis -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------- 解析工具

def _num(value) -> float | None:
    """从带后缀的字符串（"89.66元"/"-3.716%"/"608万股"）中提取首个数字。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+\.?\d*", str(value))
    return float(m.group()) if m else None


def _parse_amount(value) -> float | None:
    """金额字符串 -> 元（健壮处理「万元」「亿元」「万」「亿」及纯数字）。"""
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
        mult, s = 1e8, s.replace("亿", "")
    elif "万" in s:
        mult, s = 1e4, s.replace("万", "")
    s = s.replace("元", "")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _norm_date(s) -> str:
    """日期归一化："2026-08-21(日)" / "2026-08-24 13:07" -> "2026-08-21" """
    m = _DATE_RE.search(str(s or ""))
    return m.group(1) if m else ""


def _fmt_amount(v) -> str:
    """元 -> 亿元字符串（保留符号，2 位小数）。"""
    if v is None:
        return "   N/A"
    return f"{v / 1e8:+.2f}"


def _ma(closes: list[float], n: int) -> float | None:
    """简单移动平均（收盘价序列按日期升序）。"""
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)


# ---------------------------------------------------------------- 取数

def _parse_args(argv):
    code = argv[1].strip()
    name, days = "", 30
    for a in argv[2:]:
        a = a.strip()
        if a.isdigit():
            days = max(5, min(int(a), 60))
        elif a:
            name = a
    return code, name, days


def main():
    if len(sys.argv) < 2:
        print("用法: py .claude/skills/stock-analysis/analyze_stock.py <代码> [名称] [天数]")
        return 2

    code, name, days = _parse_args(sys.argv)

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)

    # ---- 1. 基本信息 ----
    print("=" * 72)
    print(f"【1. 基本信息】{code} {name}")
    print("=" * 72)
    info = mx.query_as_text(
        f"{code} {name} 股票简称 最新价 涨跌幅 市盈率 总市值 所属行业".strip()
    )
    print(info[:1200] if info else "⚠️ 未查到基本信息")

    # ---- 2. 近 N 日 K 线 + 技术位 ----
    print()
    print("=" * 72)
    print(f"【2. 近 {days} 日 K 线 + 技术位】")
    print("=" * 72)
    closes, rows = [], []
    for t in mx.query_structured(
        f"{code} {name} 近{days}个交易日 日K线 开盘价 收盘价 最高价 最低价 成交量 换手率".strip()
    ):
        for r in t.get("rows") or []:
            d = _norm_date(r.get("日期"))
            close = _num(r.get("收盘价"))
            if not d or close is None:
                continue
            rows.append((d, r, close))

    if rows:
        rows.sort(key=lambda x: x[0])  # 日期升序
        closes = [c for _, _, c in rows]
        ma5, ma10, ma20 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20)
        closes30 = closes[-30:] if len(closes) >= 30 else closes
        closes10 = closes[-10:] if len(closes) >= 10 else closes
        rng30 = (closes[-1] - closes[0]) / closes[0] * 100 if closes else None

        print(f"  收盘 {closes[-1]:.2f}  |  MA5 {ma5}  MA10 {ma10}  MA20 {ma20}")
        print(f"  近30日: 高 {max(closes30):.2f} / 低 {min(closes30):.2f}  |  近10日: 高 {max(closes10):.2f} / 低 {min(closes10):.2f}")
        print(f"  区间涨跌幅: {rng30:+.2f}%  |  日期 开 高 低 收 涨跌幅 换手")
        for d, r, close in rows[-min(days, len(rows)):]:
            o = _num(r.get("开盘价")); h = _num(r.get("最高价")); l = _num(r.get("最低价"))
            pct = _num(r.get("涨跌幅")); tr = _num(r.get("换手率"))
            def f(x): return f"{x:.2f}" if x is not None else "  --"
            print(f"    {d}  {f(o)} {f(h)} {f(l)} {f(close)} "
                  f"{pct if pct is None else f'{pct:+.2f}%'}  {tr if tr is None else f'{tr:.2f}%'}")
    else:
        print("  ⚠️ 未查到 K 线数据")

    # ---- 3. 近 5 日资金流 ----
    print()
    print("=" * 72)
    print("【3. 近 5 日资金流（亿元，+净流入 / -净流出）】")
    print("=" * 72)
    window = max(5, int(5 * 1.5) + 3)
    q = (f"{code} {name} 近{window}日 资金流向 "
         f"主力净流入 超大单净流入 大单净流入 中单净流入 小单净流入").strip()
    flow_rows, seen = [], set()
    for t in mx.query_structured(q):
        for r in t.get("rows") or []:
            d = _norm_date(r.get("日期"))
            main = _parse_amount(r.get("主力净流入资金"))
            if not d or main is None or d in seen:
                continue
            seen.add(d)
            flow_rows.append((d, r))
    if flow_rows:
        flow_rows.sort(key=lambda x: x[0], reverse=True)
        print("  日期          主力     超大单   大单     中单     小单")
        for d, r in flow_rows[:5]:
            cells = [_fmt_amount(_parse_amount(r.get(k))) for k in
                     ("主力净流入资金", "超大单净流入资金", "大单净流入资金", "中单净流入资金", "小单净流入资金")]
            print(f"  {d}  " + "  ".join(cells))
        tot = sum((_parse_amount(r.get("主力净流入资金")) or 0) for _, r in flow_rows[:5])
        print(f"  → 5 日主力累计净流入: {tot / 1e8:+.2f} 亿")
    else:
        print("  ⚠️ 未查到资金流数据")

    # ---- 4. 近 7 日资讯 ----
    print()
    print("=" * 72)
    print("【4. 近 7 日资讯（新闻/公告/研报）】")
    print("=" * 72)
    news = mx.fin_search_as_text(f"{name or code} {code}", hours=24 * 7)
    print(news[:3200] if news else "⚠️ 未查到资讯")


if __name__ == "__main__":
    sys.exit(main())
