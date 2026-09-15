#!/usr/bin/env python3
"""自然语言金融数据查询（妙想 Miaoxiang query）

用法:
    py query_finance.py <自然语言问题>

参数:
    问题  任意金融数据问句，妙想自动识别证券与指标

示例:
    py query_finance.py 招商银行 市盈率 历史分位
    py query_finance.py 贵州茅台 近5日主力资金净流入
    py query_finance.py 宁德时代 最新财报 净利润 营收 同比增长
    py query_finance.py 沪深300 市盈率 市净率

输出: 一个或多个结构化表格（标题 + 列名 + 行数据），失败时回退自然语言原始结果。
"""
import re
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/financial-query 上三级：financial-query -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient


def _pad(s, width):
    """按显示宽度左对齐填充（中文按 2 宽度计）"""
    w = sum(2 if ord(c) > 0x1100 else 1 for c in str(s))
    return str(s) + " " * max(0, width - w)


def _render_tables(tables):
    """把 query_structured 的表格列表渲染为文本"""
    lines = []
    for t in tables:
        title = t.get("title") or t.get("entity_name") or "数据"
        if t.get("entity_name") and t.get("entity_name") != title:
            title = f"{title}（{t['entity_name']}）"
        if t.get("code"):
            title = f"{title} {t['code']}"
        lines.append(f"**{title}**")

        columns = t.get("columns") or []
        rows = t.get("rows") or []
        if not columns:
            continue

        # 列宽按内容自适应（上限截断）
        widths = []
        for c in columns:
            w = sum(2 if ord(ch) > 0x1100 else 1 for ch in str(c))
            for r in rows[:20]:
                w = max(w, sum(2 if ord(ch) > 0x1100 else 1 for ch in str(r.get(c, ""))))
            widths.append(min(w, 24))

        # 表头
        header = "  " + " | ".join(_pad(c, widths[i]) for i, c in enumerate(columns))
        lines.append(header)
        lines.append("  " + "-+-".join("-" * w for w in widths))

        # 数据行（上限 20 行）
        for r in rows[:20]:
            cells = []
            for i, c in enumerate(columns):
                v = str(r.get(c, "")).strip()
                if len(v) > 24:
                    v = v[:23] + "…"
                cells.append(_pad(v, widths[i]))
            lines.append("  " + " | ".join(cells))
        if len(rows) > 20:
            lines.append(f"  ... 省略 {len(rows) - 20} 行 ...")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 同花顺直查（估值/财务指标主源）

# 命中即走同花顺的指标关键词
_HITHINK_METRIC_KW = (
    "市盈率", "PE", "市净率", "PB", "市销率", "PS", "市现率", "PCF",
    "ROE", "净资产收益率", "毛利率", "净利率", "负债率", "资产负债率",
)
# 同花顺不覆盖（历史分位/资金流/股息/指数估值/绝对财报额），命中即交妙想
_HITHINK_EXCLUDE_RE = re.compile(
    r"分位|历史|资金|股息|分红|主力|指数|沪深|中证|上证|科创|创业板|"
    r"财报|净利润|营收|同比|净利|北向|换手|量比"
)


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fpct(v) -> str:
    return f"{v:+.2f}%" if v is not None else "--"


def _fnum(v) -> str:
    return f"{v:.2f}" if v is not None else "--"


def _try_hithink(question: str) -> str | None:
    """问句可解析出单一 A 股、且只问估值/比率指标时，优先同花顺直查。

    返回渲染文本；不适用（分位/资金流/股息/指数/绝对财报额等）返回 None 交妙想。
    """
    if _HITHINK_EXCLUDE_RE.search(question):
        return None
    if not any(k in question for k in _HITHINK_METRIC_KW):
        return None
    # 提取标的：6 位代码优先，否则首 token 当作名称
    m = re.search(r"\b\d{6}\b", question)
    entity = m.group(0) if m else (question.split() or [""])[0].strip()
    if not entity:
        return None
    try:
        from app import hithink
        items = hithink.fetch_meta_search(entity, asset_type="a-share")
    except Exception:
        return None
    if not items:
        return None
    it = items[0]
    if it.get("asset_type") != "a-share":
        return None
    code = str(it.get("ticker") or "")
    name = str(it.get("name") or "")
    if not code:
        return None

    # 估值快照 + 最新财报指标
    vals: dict = {}
    ind: dict = {}
    try:
        from app import hithink
        v = hithink.fetch_valuations_snapshot([code])
        if v:
            vals = v[0]
    except Exception:
        pass
    try:
        from app import hithink
        ind = hithink.fetch_financial_indicators(code)
    except Exception:
        pass
    prof = ind.get("profitability") or {}
    solvency = ind.get("solvency") or {}

    lines = [f"**{name} {code}（同花顺直查 · 当前估值 + 最新财报）**"]
    lines.append("  " + " | ".join(["指标", "数值"]))
    lines.append("  " + "-+-".join(["-" * 14, "-" * 12]))

    def row(label, v):
        lines.append(f"  {_pad(label, 14)} | {_pad(v, 12)}")

    row("PE-TTM", _fnum(_num(vals.get("pe_ttm"))))
    row("PE-MRQ", _fnum(_num(vals.get("pe_mrq"))))
    row("PB(MRQ)", _fnum(_num(vals.get("pb_mrq"))))
    row("PS-TTM", _fnum(_num(vals.get("ps_ttm"))))
    row("PCF-TTM", _fnum(_num(vals.get("pcf_ttm"))))
    row("ROE(加权)", _fpct(_num(prof.get("index_weighted_avg_roe"))))
    row("毛利率", _fpct(_num(prof.get("sale_gross_margin"))))
    row("净利率", _fpct(_num(prof.get("sale_net_interest_ratio"))))
    row("资产负债率", _fpct(_num(solvency.get("assets_debt_ratio"))))
    return "\n".join(lines)


def main():
    argv = sys.argv[1:]
    if not argv or any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__)
        return 2

    question = " ".join(a.strip() for a in argv).strip()
    if not question:
        print(__doc__)
        return 2

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")

    print(f"=== 金融数据查询: {question} ===\n")

    # 同花顺直查优先（估值/财务比率，无需 MX_APIKEY，需 HITHINK_FINANCE_API_KEY）
    hithink_text = _try_hithink(question)
    if hithink_text:
        print(hithink_text)
        return 0

    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)

    tables = mx.query_structured(question)
    if tables:
        print(_render_tables(tables))
        return 0

    # 结构化解析失败时回退自然语言原始结果
    text = mx.query_as_text(question)
    print(text or "❌ 无返回（非交易时间 / 无数据 / 问句无法识别）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
