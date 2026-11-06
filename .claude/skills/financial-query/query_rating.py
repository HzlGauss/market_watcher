#!/usr/bin/env python3
"""个股机构评级 + 一致目标价查询（妙想 query「机构评级 目标价」）

用法:
    py query_rating.py <代码> [名称]

参数:
    代码  6 位 A 股代码
    名称  可选，提升查询精度

输出: 一致目标价 + 上涨空间 + 综合评级 + 各时间窗口评级家数汇总。
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


def _render(rating: dict) -> str:
    """把 get_stock_rating 结果渲染为文本"""
    lines = []
    head = f"{rating.get('code', '')} {rating.get('name', '')}".strip()
    lines.append(f"**{head} 机构评级（妙想）**")

    tp = rating.get("target_price")
    if tp is not None:
        parts = [f"{tp:.2f} 元"]
        if rating.get("target_rating"):
            parts.append(str(rating["target_rating"]))
        up = rating.get("upside_pct")
        if up is not None:
            parts.append(f"上涨空间 {up:+.2f}%")
        lines.append("  一致目标价: " + "，".join(parts))
    lines.append("")

    windows = rating.get("windows") or []
    ratings = rating.get("ratings") or {}
    if not windows:
        return "\n".join(lines)

    cols = ["窗口", "综合", "买入", "增持", "中性", "减持", "卖出", "合计"]
    widths = [8, 8, 6, 6, 6, 6, 6, 6]
    header = "  " + " | ".join(_pad(c, widths[i]) for i, c in enumerate(cols))
    lines.append(header)
    lines.append("  " + "-+-".join("-" * w for w in widths))
    for w in windows:
        d = ratings.get(w) or {}
        cells = [w, d.get("综合", ""), d.get("买入", ""), d.get("增持", ""),
                 d.get("中性", ""), d.get("减持", ""), d.get("卖出", ""), d.get("合计", "")]
        lines.append("  " + " | ".join(_pad(str(c), widths[i]) for i, c in enumerate(cells)))
    return "\n".join(lines)


def main():
    argv = sys.argv[1:]
    if not argv or any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__)
        return 2

    code = argv[0].strip()
    name = argv[1].strip() if len(argv) > 1 else ""
    if not re.fullmatch(r"\d{6}", code):
        print("❌ 代码应为 6 位数字，如 300661")
        return 1

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)
    rating = mx.get_stock_rating(code, name)
    if rating is None:
        print("❌ 未查到机构评级（无数据 / 非 A 股 / 问句无法识别）")
        return 0
    print(_render(rating))
    return 0


if __name__ == "__main__":
    sys.exit(main())
