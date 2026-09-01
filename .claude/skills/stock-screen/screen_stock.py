#!/usr/bin/env python3
"""轻量智能选股（妙想 Miaoxiang stock-screen）

用法:
    py screen_stock.py <自然语言条件> [数量]

参数:
    条件  自然语言选股条件（妙想智能解析）
    数量  返回候选数（可选，默认 20，上限 50）

示例:
    py screen_stock.py 连续3日主力资金净流入且估值较低
    py screen_stock.py 今日放量上涨且主力资金净流入 30
    py screen_stock.py 市盈率低于20倍且净利润增长 20

输出: 结构化候选列表（代码/名称/行业/现价/涨跌幅/主力净额/估值状态等）。
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

# 定位项目根目录（skills/stock-screen 上三级：stock-screen -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient


def _parse_args(argv):
    """解析命令行参数，返回 (keyword, page_size)"""
    if not argv:
        return None, 20
    keyword = argv[0].strip()
    page_size = 20
    for a in argv[1:]:
        a = a.strip()
        if a.isdigit():
            page_size = max(1, min(int(a), 50))
    return keyword, page_size


def _fmt_amount(v):
    """元 -> 亿元（带符号，None 返回 —）"""
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v) / 1e8:.2f}亿"


def _fmt_pct(v, signed=True):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v):.2f}%" if signed else f"{v:.2f}%"


def _pad(s, width):
    """按显示宽度左对齐填充（中文按 2 宽度计）"""
    w = sum(2 if ord(c) > 0x1100 else 1 for c in str(s))
    return str(s) + " " * max(0, width - w)


def _render_candidates(cands):
    """把 stock_screen_structured 候选列表渲染为表格"""
    if not cands:
        return "⚠️ 无符合条件的股票\n"

    lines = []
    header = (
        f"{'代码':<8} {'名称':<10} {'行业':<10} {'现价':>8} {'涨跌幅':>8} "
        f"{'主力净额':>10} {'估值':<8} {'换手':>7} {'量比':>6}"
    )
    lines.append(header)
    lines.append("-" * 88)

    for c in cands:
        val = c.get("valuation_status") or "—"
        if c.get("valuation_percentile") is not None:
            val = f"{val}/{c['valuation_percentile']:.0f}%"
        lines.append(
            f"{c.get('code', '—'):<8} "
            f"{_pad(c.get('name', '—'), 10)} "
            f"{_pad(c.get('industry', '—'), 10)} "
            f"{str(c.get('price') or '—'):>8} "
            f"{_fmt_pct(c.get('change_pct')):>8} "
            f"{_fmt_amount(c.get('main_net')):>10} "
            f"{_pad(val, 8)} "
            f"{_fmt_pct(c.get('turnover_rate'), signed=False):>7} "
            f"{str(c.get('vol_ratio') or '—'):>6}"
        )

        # 多日主力净额序列（连续净流入查询时返回）
        flow_days = c.get("flow_days") or []
        if flow_days:
            seq = " ".join(f"{d.get('date', '')[-5:]}:{_fmt_amount(d.get('main_net'))}" for d in flow_days)
            lines.append(f"          主力净额序列: {seq}")

    lines.append(f"\n共 {len(cands)} 只")
    return "\n".join(lines)


def main():
    keyword, page_size = _parse_args(sys.argv[1:])
    if not keyword:
        print(__doc__)
        return 2

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)

    print(f"=== 智能选股: {keyword} ===\n")

    cands = mx.stock_screen_structured(keyword, page_size=page_size)
    if cands:
        print(_render_candidates(cands))
        return 0

    # 结构化解析失败时回退自然语言原始结果
    text = mx.stock_screen_as_text(keyword)
    print(text or "❌ 无返回（选股条件无法识别 / 无符合标的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
