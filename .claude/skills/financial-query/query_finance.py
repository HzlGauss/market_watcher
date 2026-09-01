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
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)

    print(f"=== 金融数据查询: {question} ===\n")

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
