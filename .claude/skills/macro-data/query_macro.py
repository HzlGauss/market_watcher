#!/usr/bin/env python3
"""宏观数据查询（CPI / PPI，数据源 akshare）

用法:
    py query_macro.py [cpi|ppi|all] [月份数]

参数:
    指标     cpi=居民消费价格指数 / ppi=工业生产者出厂价格指数 / all=两者（默认）
    月份数   显示最近 N 个月（默认 12）

示例:
    py query_macro.py                 # CPI + PPI 最近 12 个月
    py query_macro.py cpi             # 仅 CPI 最近 12 个月
    py query_macro.py ppi 24          # 仅 PPI 最近 24 个月
    py query_macro.py all 6           # CPI + PPI 最近 6 个月

输出: 最新一期关键数值（同比/环比/累计）+ 最近 N 个月趋势表。

数据源: akshare macro_china_cpi / macro_china_ppi（统计局口径，月度）。
需 pip install akshare。
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

# 定位项目根目录（skills/macro-data 上三级：macro-data -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))


def _pad(s, width):
    """按显示宽度左对齐填充（中文按 2 宽度计）"""
    w = sum(2 if ord(c) > 0x1100 else 1 for c in str(s))
    return str(s) + " " * max(0, width - w)


def _fmt(v):
    """数值 → 带符号百分比的字符串（同比/环比列）"""
    try:
        f = float(v)
        return f"{f:+.1f}%"
    except (TypeError, ValueError):
        return str(v) if v is not None else ""


def _get_cpi(n):
    """取最近 n 个月 CPI，返回 (columns, rows) 供渲染"""
    import akshare as ak

    df = ak.macro_china_cpi()
    df = df.head(n)
    # 展示列：月份 + 全国/城市/农村的同比与环比
    cols = ["月份", "全国-同比增长", "全国-环比增长", "城市-同比增长", "农村-同比增长"]
    rows = []
    for _, r in df.iterrows():
        rows.append(
            {
                "月份": str(r.get("月份", "")),
                "全国-同比增长": _fmt(r.get("全国-同比增长")),
                "全国-环比增长": _fmt(r.get("全国-环比增长")),
                "城市-同比增长": _fmt(r.get("城市-同比增长")),
                "农村-同比增长": _fmt(r.get("农村-同比增长")),
            }
        )
    return cols, rows, df


def _get_ppi(n):
    """取最近 n 个月 PPI，返回 (columns, rows) 供渲染"""
    import akshare as ak

    df = ak.macro_china_ppi()
    df = df.head(n)
    cols = ["月份", "当月同比", "累计"]
    rows = []
    for _, r in df.iterrows():
        rows.append(
            {
                "月份": str(r.get("月份", "")),
                "当月同比": _fmt(r.get("当月同比增长")),
                "累计": f"{r.get('累计')}",
            }
        )
    return cols, rows, df


def _render(title, columns, rows):
    """渲染一张表"""
    lines = [f"**{title}**"]
    widths = [max(sum(2 if ord(ch) > 0x1100 else 1 for ch in str(c)), 6) for c in columns]
    for r in rows:
        for i, c in enumerate(columns):
            widths[i] = max(widths[i], sum(2 if ord(ch) > 0x1100 else 1 for ch in str(r.get(c, ""))))
    widths = [min(w, 20) for w in widths]

    header = "  " + " | ".join(_pad(c, widths[i]) for i, c in enumerate(columns))
    lines.append(header)
    lines.append("  " + "-+-".join("-" * w for w in widths))
    for r in rows:
        cells = []
        for i, c in enumerate(columns):
            v = str(r.get(c, "")).strip()
            if len(v) > 20:
                v = v[:19] + "…"
            cells.append(_pad(v, widths[i]))
        lines.append("  " + " | ".join(cells))
    lines.append("")
    return "\n".join(lines)


def _latest_summary(df, tag, yoy_col, mom_col=None, cum_col=None):
    """提取最新一期关键数值，返回一句话摘要"""
    try:
        r = df.iloc[0]
        month = str(r.get("月份", "")).replace("月份", "").strip()
        yoy = _fmt(r.get(yoy_col))
        parts = [f"{month} 同比 {yoy}"]
        if mom_col:
            parts.append(f"环比 {_fmt(r.get(mom_col))}")
        if cum_col:
            parts.append(f"累计 {r.get(cum_col)}")
        return f"{tag}最新: " + " | ".join(parts)
    except Exception:
        return f"{tag}最新: 无数据"


def main():
    argv = sys.argv[1:]
    if any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__)
        return 2

    indicator = "all"
    n = 12
    if argv:
        indicator = argv[0].strip().lower()
        if indicator not in ("cpi", "ppi", "all"):
            print(f"❌ 未知指标 '{indicator}'（可选: cpi / ppi / all）")
            return 2
    if len(argv) >= 2:
        try:
            n = int(argv[1])
            n = max(1, min(n, 60))
        except ValueError:
            print(f"❌ 月份数需为整数: '{argv[1]}'")
            return 2

    try:
        import akshare  # noqa: F401
    except ImportError:
        print("❌ 未安装 akshare，请先执行: pip install akshare")
        return 1

    label = {"cpi": "CPI", "ppi": "PPI", "all": "CPI + PPI"}[indicator]
    print(f"=== 宏观数据查询: {label}（最近 {n} 个月，数据源 akshare / 统计局）===\n")

    try:
        if indicator in ("cpi", "all"):
            cols, rows, df = _get_cpi(n)
            print(_latest_summary(df, "CPI", "全国-同比增长", "全国-环比增长", "全国-累计"))
            print()
            print(_render("CPI 居民消费价格指数（同比/环比）", cols, rows))
        if indicator in ("ppi", "all"):
            cols, rows, df = _get_ppi(n)
            print(_latest_summary(df, "PPI", "当月同比增长", None, "累计"))
            print()
            print(_render("PPI 工业生产者出厂价格指数", cols, rows))
    except Exception as e:
        print(f"❌ 查询失败: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
